"""Failure handling (PRD: Failure handling). Model calls are faked; retries don't wait."""
import httpx
import pytest
from fastapi.testclient import TestClient

from recon import api_v1, classifier
from recon.api import app
from recon.db import connect
from recon.payment_system import lookup_with_retry

client = TestClient(app)


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(api_v1, "SLEEP", lambda s: None)


@pytest.fixture
def model(monkeypatch):
    def set_answer(answer):
        if isinstance(answer, Exception):
            def boom(complaint, temperature):
                raise answer
            monkeypatch.setattr(classifier, "_call_model", boom)
        else:
            monkeypatch.setattr(classifier, "_call_model", lambda complaint, temperature: answer)
    return set_answer


def _txn(where="status='FAILED' AND debited=1 AND txn_type='P2P' AND reversal_status='NONE'"):
    with connect() as conn:
        return dict(conn.execute(f"SELECT * FROM transactions WHERE {where} LIMIT 1").fetchone())


P2P_OK = {"dispute_type": "P2P failed", "confidence": 0.95, "reason": "Paid a person, debited, not received."}
TEXT = "paid 500 to my brother yesterday, debited, he did not get it"


# --- status lookup retry ---
def test_retry_once_then_succeed():
    sleeps = []
    with connect() as conn:
        r = lookup_with_retry(conn, txn_id="TXN000001", mock_fail="timeout-once", sleep=sleeps.append)
    assert r.outcome == "ok" and r.attempts == 2 and len(sleeps) == 1


def test_not_found_is_not_retried():
    with connect() as conn:
        r = lookup_with_retry(conn, txn_id="TXN999999", mock_fail=None, sleep=lambda s: None)
    assert r.outcome == "not_found" and r.attempts == 1


def test_status_endpoint_retries_once():
    body = client.post("/v1/status-lookup", json={"txn_id": "TXN000001"}, headers={"X-Mock-Fail": "error-once"}).json()
    assert body["attempts"] == 2


def test_status_endpoint_gives_up_after_two_attempts():
    r = client.post("/v1/status-lookup", json={"txn_id": "TXN000001"}, headers={"X-Mock-Fail": "timeout"})
    assert r.status_code == 504 and "after 2 attempts" in r.json()["error"]["message"]


# --- cases ---
def test_case_status_fails_twice_goes_to_needs_review(model):
    model(P2P_OK)
    t = _txn()
    body = client.post("/v1/cases", json={"complaint_text": TEXT, "txn_id": t["txn_id"]},
                       headers={"X-Mock-Fail": "timeout"}).json()
    assert body["dispute_type"] == "Needs review" and body["model_guess"] == "P2P failed"
    assert body["needs_review_reasons"] == ["Status check timed out twice. Check the payment manually before replying."]
    assert body["status_lookup"]["attempts"] == 2
    assert body["decision"]["deadline_date"] is None and body["decision"]["compensation_inr"] == 0
    assert body["draft"]["template_id"] == "checking"


def test_case_status_recovers_on_retry(model):
    model(P2P_OK)
    t = _txn()
    body = client.post("/v1/cases", json={"complaint_text": TEXT, "txn_id": t["txn_id"]},
                       headers={"X-Mock-Fail": "timeout-once"}).json()
    assert body["needs_review"] is False and body["dispute_type"] == "P2P failed"
    assert body["status_lookup"]["attempts"] == 2 and body["decision"]["rule_id"] == "R-P2P"


def test_case_schema_failure(model):
    model({"dispute_type": "Refund", "confidence": 0.9})
    body = client.post("/v1/cases", json={"complaint_text": TEXT}).json()
    assert body["dispute_type"] == "Needs review"
    assert "format check" in body["needs_review_reasons"][0] and "'Refund'" in body["needs_review_reasons"][0]


def test_case_model_timeout(model):
    model(httpx.ReadTimeout("read timed out"))
    body = client.post("/v1/cases", json={"complaint_text": TEXT}).json()
    assert body["needs_review_reasons"] == ["Model did not answer in time."]


def test_case_low_confidence(model):
    model({**P2P_OK, "confidence": 0.6})
    body = client.post("/v1/cases", json={"complaint_text": TEXT}).json()
    assert body["dispute_type"] == "Needs review"
    assert body["needs_review_reasons"] == ["Low confidence (0.60). Model guessed P2P failed."]


def test_case_unknown_utr(model):
    model(P2P_OK)
    body = client.post("/v1/cases", json={"complaint_text": TEXT, "utr": "000000000000"}).json()
    assert body["needs_review_reasons"][0].startswith("No transaction found for 000000000000")
    assert body["status_lookup"]["attempts"] == 1


def test_case_happy_path_has_no_reasons(model):
    model(P2P_OK)
    t = _txn()
    body = client.post("/v1/cases", json={"complaint_text": TEXT, "txn_id": t["txn_id"]}).json()
    assert body["needs_review"] is False and body["needs_review_reasons"] == []
    assert body["draft"]["template_id"] == "refund_due" and body["draft"]["sent"] is False


def test_case_fraud_gets_no_draft(model):
    model({**P2P_OK, "fraud_signal": True})
    body = client.post("/v1/cases", json={"complaint_text": "paid a stranger for a task job, scam"}).json()
    assert body["decision"]["route_to"] == "Fraud team" and body["draft"] is None and body["draft_blocked"]
