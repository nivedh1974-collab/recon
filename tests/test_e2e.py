"""End-to-end (PRD: Validation). Drives whole cases through the API the screens use, then checks the
audit trail and the ops metrics built from it. The model is faked: this tests the plumbing, not the model."""
import uuid

import pytest
from fastapi.testclient import TestClient

from recon import api_v1, baseline, classifier
from recon.api import app
from recon.db import connect

client = TestClient(app)


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    def model(complaint, temperature):
        t = complaint.lower()
        dtype = "P2M failed" if any(w in t for w in ("shop", "paid rs")) else "P2P failed"
        return {"dispute_type": dtype, "confidence": 0.93, "reason": "fake model"}
    model.model_name = "test stand-in"
    monkeypatch.setattr(classifier, "_call_model", model)
    monkeypatch.setattr(api_v1, "SLEEP", lambda s: None)


def ev(cid, event, **kw):
    r = client.post(f"/v1/cases/{cid}/events", json={"agent_id": "A09", "event": event, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def actions(cid):
    return [(h["actor"], h["action"]) for h in client.get(f"/v1/cases/{cid}").json()["history"]]


def test_failed_payment_end_to_end():
    v = baseline.case_view(connect(), "A1")  # TXN000061: P2P failed, not reversed, past T+1
    case = client.post("/v1/cases", json={"complaint_text": v["complaint"], "txn_id": "TXN000061"}).json()
    cid = case["case_id"]
    assert case["dispute_type"] == "P2P failed" and case["decision"]["escalate"] and case["draft"]
    ev(cid, "opened")
    ev(cid, "reply_sent", value=case["draft"]["draft"] + " Sorry again.")
    esc = client.post("/v1/escalations", headers={"Idempotency-Key": str(uuid.uuid4())},
                      json={"txn_id": "TXN000061", "reasons": case["decision"]["escalation_reasons"],
                            "approved_by": "A09", "case_id": cid})
    assert esc.status_code in (201, 409)  # 409 if another test already escalated this transaction
    final = ev(cid, "closed")
    assert final["case_status"] == "CLOSED"
    acts = actions(cid)
    assert acts[:5] == [("recon", "case_created"), ("recon", "status_checked"), ("recon", "classified"),
                        ("recon", "rules_applied"), ("recon", "draft_created")]
    assert ("A09", "case_opened") in acts and ("A09", "reply_sent") in acts and acts[-1] == ("A09", "case_closed")
    rec = final["record_at_open"]
    assert rec["model"]["name"] == "test stand-in" and rec["transaction"]["txn_id"] == "TXN000061"

    m = client.get("/metrics").json()
    assert m["handle_time_min"]["n"] >= 1 and m["draft_edits"]["n"] >= 1 and m["deadlines_met"]["n"] >= 1


def test_fraud_end_to_end():
    v = baseline.case_view(connect(), "A6")
    case = client.post("/v1/cases", json={"complaint_text": v["complaint"]}).json()
    cid = case["case_id"]
    assert case["headline"].startswith("Possible fraud") and case["draft"] is None
    held = client.post("/v1/escalations", headers={"Idempotency-Key": str(uuid.uuid4())},
                       json={"txn_id": "TXN000001", "reasons": ["x"], "approved_by": "A09", "case_id": cid})
    assert held.json()["error"]["code"] == "FRAUD_HOLD"
    ev(cid, "fraud_routed", note="OTP shared with caller")
    ev(cid, "closed")
    acts = actions(cid)
    assert ("recon", "possible_fraud") in acts and ("A09", "routed_to_fraud_team") in acts
    assert not any(a == "reply_sent" for _, a in acts)


def test_needs_review_end_to_end():
    case = client.post("/v1/cases", json={"complaint_text": "paid Rs 99 at a shop, pending", "txn_id": "TXN000116"},
                       headers={"X-Mock-Fail": "timeout"}).json()
    cid = case["case_id"]
    assert case["dispute_type"] == "Needs review" and case["draft"]["template_id"] == "checking"
    ev(cid, "dispute_type_changed", value="P2M failed", note="checked the payment by hand: pending")
    ev(cid, "reply_sent", value="We are checking with the bank and will update you.")
    acts = actions(cid)
    assert ("recon", "needs_review") in acts and ("A09", "dispute_type_changed") in acts


def test_baseline_scoring_both_modes():
    conn = connect()
    run = baseline.start(conn, "A2", "T1")
    exp = baseline.expected(conn, "A2")
    res = baseline.finish(conn, run, {**exp, "deadline_date": "2026-10-15"})  # one field wrong
    assert res["correct"] == {"dispute_type": True, "deadline": False, "compensation": True, "next_step": True}

    v = baseline.case_view(conn, "B4")
    cid = client.post("/v1/cases", json={"complaint_text": v["complaint"], "txn_id": "TXN000033"}).json()["case_id"]
    run = baseline.start(conn, "B4", "T1", cid)
    ev(cid, "reply_sent", value="The money did not leave your account.")
    answers, end = baseline.recon_answers(conn, cid)
    res = baseline.finish(conn, run, answers, finished_at=end)
    assert all(res["correct"].values()), res
    r = baseline.report(conn)
    assert r["manual"]["n"] >= 1 and r["recon"]["n"] >= 1
