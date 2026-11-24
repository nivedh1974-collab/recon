"""Case record and audit log (PRD: Controls)."""
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from recon import api_v1, classifier
from recon.api import app
from recon.db import connect

client = TestClient(app)
P2P_OK = {"dispute_type": "P2P failed", "confidence": 0.95, "reason": "Paid a person, debited, not received."}


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    monkeypatch.setattr(api_v1, "SLEEP", lambda s: None)
    monkeypatch.setattr(classifier, "_call_model", lambda complaint, temperature: P2P_OK)


def _txn_id():
    with connect() as conn:
        return conn.execute("SELECT txn_id FROM transactions WHERE status='FAILED' AND debited=1 "
                            "AND txn_type='P2P' AND reversal_status='NONE' LIMIT 1").fetchone()[0]


def _open(**kw):
    body = {"complaint_text": "paid 500 to my brother yesterday, not received", "txn_id": _txn_id(), **kw}
    return client.post("/v1/cases", json=body).json()


def test_case_is_stored_with_full_record():
    case = _open()
    got = client.get(f"/v1/cases/{case['case_id']}").json()
    rec = got["record_at_open"]
    assert rec["complaint"]["text"].startswith("paid 500")
    assert rec["model"]["prompt_version"] == classifier.prompt_version() and rec["model"]["name"]
    assert rec["decision"]["rule_id"] == "R-P2P" and rec["status_lookup"]["attempts"] == 1
    actions = [h["action"] for h in got["history"]]
    assert actions == ["case_created", "status_checked", "classified", "rules_applied", "draft_created"]
    assert all(h["actor"] == "recon" for h in got["history"])


def test_needs_review_reasons_are_logged():
    case = client.post("/v1/cases", json={"complaint_text": "paid 500 to shop", "txn_id": _txn_id()},
                       headers={"X-Mock-Fail": "timeout"}).json()
    hist = client.get(f"/v1/cases/{case['case_id']}").json()["history"]
    nr = [h for h in hist if h["action"] == "needs_review"]
    assert nr and "Status check timed out twice" in nr[0]["new_value"]


def test_agent_steps_are_logged_with_agent_id():
    cid = _open()["case_id"]
    client.post(f"/v1/cases/{cid}/events", json={"agent_id": "A07", "event": "opened"})
    client.post(f"/v1/cases/{cid}/events", json={"agent_id": "A07", "event": "reply_sent", "value": "Edited reply"})
    esc = client.post("/v1/escalations", json={"txn_id": _txn_id(), "reasons": ["late"], "approved_by": "A07",
                                               "case_id": cid}, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert esc.status_code in (201, 409)
    client.post(f"/v1/cases/{cid}/events", json={"agent_id": "A07", "event": "closed", "note": "done"})
    got = client.get(f"/v1/cases/{cid}").json()
    agent_rows = [h for h in got["history"] if h["actor"] == "A07"]
    assert [h["action"] for h in agent_rows][:2] == ["case_opened", "reply_sent"]
    assert agent_rows[-1]["action"] == "case_closed" and got["case_status"] == "CLOSED"
    sent = next(h for h in agent_rows if h["action"] == "reply_sent")
    assert sent["old_value"].startswith("Hi,") and sent["new_value"] == "Edited reply"


def test_dispute_type_change_needs_a_note_and_keeps_old_value():
    cid = _open()["case_id"]
    r = client.post(f"/v1/cases/{cid}/events", json={"agent_id": "A02", "event": "dispute_type_changed",
                                                     "value": "Wrong UPI ID"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "NOTE_REQUIRED"
    client.post(f"/v1/cases/{cid}/events", json={"agent_id": "A02", "event": "dispute_type_changed",
                                                 "value": "Wrong UPI ID", "note": "customer typed wrong ID"})
    got = client.get(f"/v1/cases/{cid}").json()
    row = got["history"][-1]
    assert row["old_value"] == "P2P failed" and row["new_value"].startswith("Wrong UPI ID")
    assert got["record_at_open"]["dispute_type"] == "P2P failed"  # snapshot never changes


def test_closed_case_takes_no_more_events():
    cid = _open()["case_id"]
    client.post(f"/v1/cases/{cid}/events", json={"agent_id": "A01", "event": "closed"})
    r = client.post(f"/v1/cases/{cid}/events", json={"agent_id": "A01", "event": "opened"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CASE_CLOSED"


def test_audit_log_cannot_be_edited_or_deleted():
    cid = _open()["case_id"]
    with connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE audit_log SET new_value = 'x' WHERE case_id = ?", (cid,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM audit_log WHERE case_id = ?", (cid,))


def test_unknown_case_404_and_history_page_renders():
    assert client.get("/v1/cases/CASE-NOPE").status_code == 404
    cid = _open()["case_id"]
    page = client.get(f"/cases/{cid}")
    assert page.status_code == 200 and "append-only" in page.text and "rules_applied" in page.text
