"""Possible-fraud flag (PRD: Possible fraud)."""
import uuid

import pytest
from fastapi.testclient import TestClient

from recon import api_v1, classifier
from recon.api import app
from recon.classifier import fraud_guard
from recon.db import connect

client = TestClient(app)
P2P = {"dispute_type": "P2P failed", "confidence": 0.95, "fraud_signal": False, "reason": "debited, not received"}


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setattr(api_v1, "SLEEP", lambda s: None)
    def set_answer(a):
        monkeypatch.setattr(classifier, "_call_model", lambda complaint, temperature: a)
    return set_answer


def _txn_id():
    with connect() as conn:
        return conn.execute("SELECT txn_id FROM transactions WHERE status='FAILED' AND debited=1 "
                            "AND txn_type='P2P' AND reversal_status='NONE' LIMIT 1").fetchone()[0]


@pytest.mark.parametrize("text,signal", [
    ("Someone called from bank, I shared the OTP and 5000 was gone", "OTP or PIN shared"),
    ("I did not make this transaction, 2000 debited at night", "unauthorised debit"),
    ("caller asked me to install AnyDesk for refund", "remote access app"),
    ("got a part time job offer on Telegram, paid 3000 for tasks", "job, task or investment scam"),
    ("buyer said scan this QR to receive money and my money went", "collect or QR trick"),
    ("mere saath dhokha hua, paise kat gaye", "scam words"),
])
def test_code_guard_names_the_signal(text, signal):
    assert signal in fraud_guard(text)


@pytest.mark.parametrize("text", [
    "This app is a fraud, my payment of 500 failed and money not refunded",
    "Worst fraud app, recharge not done",
    "sent 2000 to a stranger by mistake, wrong UPI ID",
])
def test_angry_words_alone_are_not_fraud(text):
    assert fraud_guard(text) == []


def test_flagged_case_is_routed_not_treated_as_failed_payment(model):
    model(P2P)  # model says failed payment; the code guard still catches the OTP
    body = client.post("/v1/cases", json={"complaint_text": "shared OTP with a caller, 5000 debited",
                                           "txn_id": _txn_id()}).json()
    assert body["headline"] == "Possible fraud: route to fraud team"
    assert body["possible_fraud"] == {"flagged": True, "signals": ["OTP or PIN shared"]}
    d = body["decision"]
    assert d["route_to"] == "Fraud team" and d["deadline_date"] is None and d["compensation_inr"] == 0
    assert body["draft"] is None and "1930" in d["agent_note"]


def test_model_flag_alone_also_routes(model):
    model({**P2P, "fraud_signal": True, "fraud_words": ["investment"]})
    body = client.post("/v1/cases", json={"complaint_text": "paid 3000 to a person for returns, nothing came"}).json()
    assert body["possible_fraud"]["signals"] == ["model flagged fraud"]


def test_psp_escalation_is_held_for_fraud_cases(model):
    model(P2P)
    cid = client.post("/v1/cases", json={"complaint_text": "I did not make this transaction",
                                         "txn_id": _txn_id()}).json()["case_id"]
    r = client.post("/v1/escalations", json={"txn_id": _txn_id(), "reasons": ["x"], "approved_by": "A01",
                                             "case_id": cid}, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 409 and r.json()["error"]["code"] == "FRAUD_HOLD"


def test_agent_routes_or_clears_with_note(model):
    model(P2P)
    tid = _txn_id()
    cid = client.post("/v1/cases", json={"complaint_text": "paid my brother 500, OTP came late, not received",
                                         "txn_id": tid}).json()["case_id"]
    r = client.post(f"/v1/cases/{cid}/events", json={"agent_id": "A03", "event": "fraud_cleared"})
    assert r.json()["error"]["code"] == "NOTE_REQUIRED"
    got = client.post(f"/v1/cases/{cid}/events", json={"agent_id": "A03", "event": "fraud_cleared",
                                                       "note": "OTP mention was about a delay, not shared"}).json()
    assert got["possible_fraud"] is False
    with connect() as conn:
        row = conn.execute("SELECT rule_id, deadline_date, draft_reply FROM cases WHERE case_id=?", (cid,)).fetchone()
    assert row["rule_id"] == "R-P2P" and row["deadline_date"] and row["draft_reply"]
    actions = [h["action"] for h in got["history"]]
    assert "possible_fraud" in actions and actions[-3:] == ["fraud_cleared", "rules_applied", "draft_created"]

    cid2 = client.post("/v1/cases", json={"complaint_text": "hacked account, 9000 gone"}).json()["case_id"]
    got2 = client.post(f"/v1/cases/{cid2}/events", json={"agent_id": "A03", "event": "fraud_routed"}).json()
    assert got2["case_status"] == "ESCALATED" and got2["history"][-1]["action"] == "routed_to_fraud_team"
