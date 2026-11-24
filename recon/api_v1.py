"""Recon API v1. Spec: PRD section "API".

Four endpoints, all called by the agent screen:
    POST /v1/status-lookup   ask the payment system what happened (mock)
    POST /v1/classify        model reads the complaint; code applies RBI rules
    POST /v1/draft-reply     template reply for the agent to edit; never sent by Recon
    POST /v1/escalations     raise a case with the payment service provider bank (mock);
                             needs an agent id and an Idempotency-Key

Errors always look like {"error": {"code": "...", "message": "..."}}.
Testing hook: header X-Mock-Fail: timeout | error makes the mock payment system or
mock PSP bank fail, so failure handling can be shown without a real outage.
"""
import json
import uuid
from dataclasses import asdict
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import time

from recon import case_flow, case_store, classifier, drafter, payment_system
from recon.db import connect, ensure_schema
from recon.error_codes import ALL_CODES
from recon.rules_engine import decide

router = APIRouter(prefix="/v1")

# Timeouts (seconds). Assumptions until measured; see PRD API spec.
TIMEOUTS = {"status_lookup": 3, "classify": classifier.CLASSIFIER_TIMEOUT_S, "draft_reply": 2, "escalation": 5}
MAX_COMPLAINT_CHARS = 4000
SLEEP = time.sleep  # tests swap this out so retries don't wait


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(status_code=exc.status, content=error_body(exc.code, exc.message))


async def validation_handler(request: Request, exc: RequestValidationError):
    first = exc.errors()[0]
    field = ".".join(str(p) for p in first["loc"] if p != "body")
    return JSONResponse(status_code=400, content=error_body("INVALID_INPUT", f"{field}: {first['msg']}"))


_schema_ready = False


def _conn():
    global _schema_ready
    conn = connect()
    if not _schema_ready:
        ensure_schema(conn)
        _schema_ready = True
    return conn


def _load_txn(conn, txn_id: Optional[str] = None, utr: Optional[str] = None) -> dict:
    if txn_id:
        row = conn.execute("SELECT * FROM transactions WHERE txn_id = ?", (txn_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM transactions WHERE utr = ?", (utr,)).fetchone()
    if row is None:
        raise ApiError(404, "TXN_NOT_FOUND", f"No transaction for {'txn_id ' + txn_id if txn_id else 'UTR ' + str(utr)}")
    t = dict(row)
    t["debited"] = bool(t["debited"])
    t["credited"] = None if t["credited"] is None else bool(t["credited"])
    return t


def _mock_fail(x_mock_fail: Optional[str], system: str, timeout_key: str):
    if x_mock_fail == "timeout":
        raise ApiError(504, f"{system}_TIMEOUT", f"No answer within {TIMEOUTS[timeout_key]}s")
    if x_mock_fail == "error":
        raise ApiError(502, f"{system}_ERROR", "Upstream system returned an error")


def _jsonable(d: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in d.items()}


# ---------- 1. Status lookup ----------
class StatusLookupIn(BaseModel):
    txn_id: Optional[str] = None
    utr: Optional[str] = None


@router.post("/status-lookup")
def status_lookup(body: StatusLookupIn, x_mock_fail: Optional[str] = Header(None)) -> dict:
    if not body.txn_id and not body.utr:
        raise ApiError(400, "MISSING_REFERENCE", "Send txn_id or utr")
    with _conn() as conn:
        res = payment_system.lookup_with_retry(conn, body.txn_id, body.utr, x_mock_fail, sleep=SLEEP)
    if res.failed:
        code = "STATUS_TIMEOUT" if res.outcome == "timeout" else "STATUS_ERROR"
        raise ApiError(504 if res.outcome == "timeout" else 502, code,
                       f"Status check failed after {res.attempts} attempts: " + "; ".join(res.notes))
    if res.outcome == "not_found":
        ref = f"txn_id {body.txn_id}" if body.txn_id else f"UTR {body.utr}"
        raise ApiError(404, "TXN_NOT_FOUND", f"No transaction for {ref}")
    t = res.txn
    return {
        "txn_id": t["txn_id"], "utr": t["utr"], "txn_type": t["txn_type"], "amount_inr": t["amount_inr"],
        "status": t["status"], "debited": t["debited"], "credited": t["credited"],
        "error_code": t["error_code"], "error_meaning": ALL_CODES.get(t["error_code"]) if t["error_code"] else None,
        "reversal_status": t["reversal_status"], "reversed_at": t["reversed_at"],
        "funding_source": t.get("funding_source", "UPI"), "created_at": t["created_at"],
        "checked_at": datetime.now().isoformat(timespec="seconds"), "source": "mock payment system",
        "attempts": res.attempts,
    }


# ---------- 2. Classify ----------
class ClassifyIn(BaseModel):
    complaint_text: str = Field(min_length=1)
    txn_id: Optional[str] = None


@router.post("/classify")
def classify(body: ClassifyIn) -> dict:
    text = body.complaint_text.strip()
    if not text:
        raise ApiError(400, "INVALID_INPUT", "complaint_text is empty")
    if len(text) > MAX_COMPLAINT_CHARS:
        raise ApiError(413, "COMPLAINT_TOO_LONG", f"complaint_text is over {MAX_COMPLAINT_CHARS} characters")
    with _conn() as conn:
        txn = _load_txn(conn, body.txn_id) if body.txn_id else None
        rules = {r["rule_id"]: dict(r) for r in conn.execute("SELECT * FROM rules")}
    c = classifier.classify(text, txn=txn)
    d = decide(c["dispute_type"], txn, rules, confidence=c["confidence"], fraud_signal=c["fraud_signal"],
               out_of_scope_reason=c["out_of_scope_reason"], amount_inr=c["amount_inr"])
    return {
        "classification": {k: c[k] for k in ("dispute_type", "confidence", "amount_inr", "txn_date", "utr", "app",
                                             "fraud_signal", "fraud_words", "out_of_scope_reason", "reason",
                                             "overrides") if k in c} | {"overrides": c.get("overrides", [])},
        "decision": _jsonable(asdict(d)),
        "txn_id": body.txn_id,
        "fallback": c["reason"].startswith("classifier error"),
    }


# ---------- 3. Draft reply ----------
class DraftIn(BaseModel):
    dispute_type: str
    refund_state: Optional[str] = None
    route_to: Optional[str] = None
    fraud_signal: bool = False
    txn_id: Optional[str] = None
    amount_inr: Optional[float] = None
    utr: Optional[str] = None
    deadline_date: Optional[str] = None
    days_past_deadline: int = 0
    compensation_inr: int = 0


@router.post("/draft-reply")
def draft_reply(body: DraftIn) -> dict:
    if body.dispute_type not in classifier.DISPUTE_TYPES:
        raise ApiError(400, "INVALID_INPUT", f"dispute_type must be one of {classifier.DISPUTE_TYPES}")
    case = body.model_dump()
    if body.txn_id:
        with _conn() as conn:
            t = _load_txn(conn, body.txn_id)
        case.update(amount_inr=t["amount_inr"], utr=t["utr"], payee=t["payee_name"], debited=t["debited"],
                    reversed_at=(t["reversed_at"] or "")[:10] or None)
    try:
        return drafter.draft(case)
    except drafter.DraftNotAllowed as e:
        raise ApiError(409, "DRAFT_NOT_ALLOWED", str(e))


# ---------- 4. Escalation to the payment service provider bank ----------
class EscalationIn(BaseModel):
    txn_id: str
    reasons: List[str] = Field(min_length=1)
    approved_by: str = Field(min_length=1, description="Agent id. Recon never escalates on its own.")
    case_id: Optional[str] = None


def _psp_bank(app_name: str) -> str:
    return f"Mock PSP bank ({app_name})"


def _esc_out(row) -> dict:
    out = dict(row)
    out["reasons"] = json.loads(out["reasons"])
    out.pop("idempotency_key")
    out["mock"] = True
    return out


@router.post("/escalations", status_code=201)
def create_escalation(body: EscalationIn, idempotency_key: Optional[str] = Header(None),
                      x_mock_fail: Optional[str] = Header(None)):
    if not idempotency_key:
        raise ApiError(400, "MISSING_IDEMPOTENCY_KEY", "Send an Idempotency-Key header so a retry can't escalate twice")
    with _conn() as conn:
        same = conn.execute("SELECT * FROM escalations WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        if same:  # retry of a request that already went through: same answer, no new escalation
            return JSONResponse(status_code=200, content=_esc_out(same))
        t = _load_txn(conn, body.txn_id)
        open_one = conn.execute("SELECT escalation_id FROM escalations WHERE txn_id = ? AND status = 'SUBMITTED'",
                                (body.txn_id,)).fetchone()
        if open_one:
            raise ApiError(409, "ALREADY_ESCALATED", f"{body.txn_id} already has escalation {open_one[0]}")
        if body.case_id:
            try:
                case_store.get_case(conn, body.case_id)
            except case_store.CaseError as e:
                raise ApiError(e.status, e.code, e.message)
        _mock_fail(x_mock_fail, "PSP", "escalation")
        esc_id = "ESC-" + uuid.uuid4().hex[:8].upper()
        conn.execute(
            "INSERT INTO escalations VALUES (?, ?, ?, ?, ?, ?, ?, 'SUBMITTED', ?)",
            (esc_id, idempotency_key, body.txn_id, body.case_id, json.dumps(body.reasons), body.approved_by,
             _psp_bank(t["app"]), datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        if body.case_id:
            case_store.log_escalation(conn, body.case_id, body.approved_by, esc_id, body.reasons)
        row = conn.execute("SELECT * FROM escalations WHERE escalation_id = ?", (esc_id,)).fetchone()
    return _esc_out(row)


# ---------- 5. Open a case (status lookup + classify + rules + draft, with failure rules) ----------
class CaseIn(BaseModel):
    complaint_text: str = Field(min_length=1)
    txn_id: Optional[str] = None
    utr: Optional[str] = None


@router.post("/cases")
def open_case(body: CaseIn, x_mock_fail: Optional[str] = Header(None)) -> dict:
    text = body.complaint_text.strip()
    if not text:
        raise ApiError(400, "INVALID_INPUT", "complaint_text is empty")
    if len(text) > MAX_COMPLAINT_CHARS:
        raise ApiError(413, "COMPLAINT_TOO_LONG", f"complaint_text is over {MAX_COMPLAINT_CHARS} characters")
    with _conn() as conn:
        result = case_flow.open_case(conn, text, body.txn_id, body.utr, x_mock_fail, sleep=SLEEP)
        result["case_id"] = case_store.save_new_case(conn, result, text, body.txn_id, body.utr)
    return result


# ---------- 6. Case record and history (PRD: Controls) ----------
class CaseEventIn(BaseModel):
    agent_id: str = Field(min_length=1)
    event: str
    value: Optional[str] = None
    note: Optional[str] = None


@router.get("/cases/{case_id}")
def get_case(case_id: str) -> dict:
    with _conn() as conn:
        try:
            return case_store.get_case(conn, case_id)
        except case_store.CaseError as e:
            raise ApiError(e.status, e.code, e.message)


@router.post("/cases/{case_id}/events")
def case_event(case_id: str, body: CaseEventIn) -> dict:
    with _conn() as conn:
        try:
            return case_store.agent_event(conn, case_id, body.agent_id, body.event, body.value, body.note)
        except case_store.CaseError as e:
            raise ApiError(e.status, e.code, e.message)
