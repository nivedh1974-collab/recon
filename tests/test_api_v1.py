"""API v1 tests. The classifier's model call is faked, so no network or key is needed."""
import uuid

import pytest
from fastapi.testclient import TestClient

from recon import classifier
from recon.api import app
from recon.db import connect

client = TestClient(app)


def _txn(where):
    with connect() as conn:
        return dict(conn.execute(f"SELECT * FROM transactions WHERE {where} LIMIT 1").fetchone())


@pytest.fixture
def fake_model(monkeypatch):
    def set_answer(answer):
        monkeypatch.setattr(classifier, "_call_model", lambda complaint, temperature: answer)
    return set_answer


def test_status_lookup_by_utr():
    t = _txn("status='FAILED' AND debited=1")
    body = client.post("/v1/status-lookup", json={"utr": t["utr"]}).json()
    assert body["txn_id"] == t["txn_id"] and body["debited"] is True and body["source"] == "mock payment system"


def test_status_lookup_errors():
    assert client.post("/v1/status-lookup", json={}).json()["error"]["code"] == "MISSING_REFERENCE"
    r = client.post("/v1/status-lookup", json={"txn_id": "TXN999999"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "TXN_NOT_FOUND"
    r = client.post("/v1/status-lookup", json={"txn_id": "TXN000001"}, headers={"X-Mock-Fail": "timeout"})
    assert r.status_code == 504 and r.json()["error"]["code"] == "STATUS_TIMEOUT"


def test_classify_returns_classification_and_rules_decision(fake_model):
    t = _txn("status='FAILED' AND debited=1 AND txn_type='P2P' AND reversal_status='NONE'")
    fake_model({"dispute_type": "P2P failed", "confidence": 0.95, "amount_inr": t["amount_inr"]})
    body = client.post("/v1/classify", json={"complaint_text": "paid my friend, debited, not received",
                                             "txn_id": t["txn_id"]}).json()
    assert body["classification"]["dispute_type"] == "P2P failed"
    assert body["decision"]["rule_id"] == "R-P2P" and body["decision"]["deadline_date"]
    assert body["fallback"] is False


def test_classify_input_errors():
    r = client.post("/v1/classify", json={"complaint_text": ""})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_INPUT"
    r = client.post("/v1/classify", json={"complaint_text": "x" * 4001})
    assert r.status_code == 413
    r = client.post("/v1/classify", json={"complaint_text": "help", "txn_id": "TXN999999"})
    assert r.status_code == 404


def test_classify_model_failure_falls_back_to_needs_review(monkeypatch):
    def boom(complaint, temperature):
        raise RuntimeError("Groq 500")
    monkeypatch.setattr(classifier, "_call_model", boom)
    body = client.post("/v1/classify", json={"complaint_text": "paid 500 to shop, failed"}).json()
    assert body["classification"]["dispute_type"] == "Needs review" and body["fallback"] is True
    assert body["decision"]["escalate"] is True


def test_draft_reply_uses_transaction_and_is_never_sent():
    t = _txn("status='FAILED' AND debited=1 AND reversal_status='NONE'")
    body = client.post("/v1/draft-reply", json={"dispute_type": f"{t['txn_type']} failed", "refund_state": "due",
                                                "txn_id": t["txn_id"], "deadline_date": "2026-10-03"}).json()
    assert body["template_id"] == "refund_due" and t["utr"] in body["draft"] and body["sent"] is False


def test_draft_reply_refuses_fraud():
    r = client.post("/v1/draft-reply", json={"dispute_type": "Out of scope", "fraud_signal": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "DRAFT_NOT_ALLOWED"


def test_draft_reply_asks_for_details_when_vague():
    body = client.post("/v1/draft-reply", json={"dispute_type": "Needs review"}).json()
    assert body["template_id"] == "need_details"


def test_escalation_needs_agent_and_key():
    t = _txn("status='PENDING'")
    r = client.post("/v1/escalations", json={"txn_id": t["txn_id"], "reasons": ["status pending"]},
                    headers={"Idempotency-Key": "k1"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_INPUT"
    r = client.post("/v1/escalations", json={"txn_id": t["txn_id"], "reasons": ["x"], "approved_by": "A01"})
    assert r.json()["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"


def test_escalation_retry_is_safe_and_duplicates_are_blocked():
    with connect() as conn:
        conn.execute("DELETE FROM escalations")
        conn.commit()
    t = _txn("status='UNKNOWN'")
    payload = {"txn_id": t["txn_id"], "reasons": ["status unknown"], "approved_by": "A01"}
    key = str(uuid.uuid4())
    first = client.post("/v1/escalations", json=payload, headers={"Idempotency-Key": key})
    again = client.post("/v1/escalations", json=payload, headers={"Idempotency-Key": key})
    assert first.status_code == 201 and again.status_code == 200
    assert first.json()["escalation_id"] == again.json()["escalation_id"] and first.json()["approved_by"] == "A01"
    other = client.post("/v1/escalations", json=payload, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert other.status_code == 409 and other.json()["error"]["code"] == "ALREADY_ESCALATED"


def test_escalation_psp_timeout():
    t = _txn("status='FAILED' AND debited=1")
    r = client.post("/v1/escalations", json={"txn_id": t["txn_id"], "reasons": ["late"], "approved_by": "A01"},
                    headers={"Idempotency-Key": str(uuid.uuid4()), "X-Mock-Fail": "timeout"})
    assert r.status_code == 504 and r.json()["error"]["code"] == "PSP_TIMEOUT"
