"""Case record and audit log (PRD: Controls).

Every case stores a full snapshot of what Recon saw and decided when it opened
(complaint, status lookup, model output, model + prompt version, rules applied,
Needs review reasons, draft). Every later step, by Recon or an agent, is one
append-only row in audit_log. The database refuses edits and deletes on that table.
"""
import json
import os
import uuid
from datetime import datetime
from typing import Optional

from recon import classifier

AGENT_EVENTS = ("opened", "dispute_type_changed", "reply_sent", "closed")
STATUS_AFTER = {"reply_sent": "SENT", "closed": "CLOSED", "escalated": "ESCALATED"}


class CaseError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log(conn, case_id: str, actor: str, action: str, old=None, new=None, at: Optional[str] = None) -> None:
    conn.execute(
        "INSERT INTO audit_log (case_id, actor, action, old_value, new_value, logged_at) VALUES (?,?,?,?,?,?)",
        (case_id, actor, action, old if old is None or isinstance(old, str) else json.dumps(old),
         new if new is None or isinstance(new, str) else json.dumps(new), at or _now()))


def save_new_case(conn, result: dict, complaint_text: str, txn_id: Optional[str], utr: Optional[str],
                  channel: str = "in-app") -> str:
    """Store a case opened by case_flow.open_case and log what Recon did. Returns the case id."""
    now = _now()
    suffix = uuid.uuid4().hex[:8].upper()
    complaint_id, case_id = f"CMP-{suffix}", f"CASE-{suffix}"
    d = result["decision"]
    model = os.environ.get("GROQ_MODEL", classifier.DEFAULT_MODEL)
    record = {
        "complaint": {"text": complaint_text, "channel": channel, "txn_id": txn_id, "utr": utr},
        "status_lookup": result["status_lookup"],
        "model": {"name": model, "prompt_version": classifier.prompt_version(),
                  "guess": result["model_guess"], "confidence": result["confidence"], **result["classification"]},
        "needs_review_reasons": result["needs_review_reasons"],
        "dispute_type": result["dispute_type"],
        "decision": d,
        "draft": result["draft"],
        "draft_blocked": result["draft_blocked"],
        "recorded_at": now,
    }
    conn.execute("INSERT INTO complaints (complaint_id, received_at, channel, text, txn_id) VALUES (?,?,?,?,?)",
                 (complaint_id, now, channel, complaint_text, result["txn_id"]))
    conn.execute(
        """INSERT INTO cases (case_id, complaint_id, dispute_type, confidence, fraud_signal, rule_id, deadline_date,
           days_past_deadline, compensation_inr, escalate, escalation_reasons, draft_reply, case_status,
           created_at, case_source, case_record, model_guess, needs_review_reasons, model, prompt_version)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'OPEN', ?, 'live', ?,?,?,?,?)""",
        (case_id, complaint_id, result["dispute_type"], result["confidence"],
         int(bool(result["classification"].get("fraud_signal"))), d["rule_id"], d["deadline_date"],
         d["days_past_deadline"], d["compensation_inr"], int(d["escalate"]), "; ".join(d["escalation_reasons"]),
         (result["draft"] or {}).get("draft"), now, json.dumps(record), result["model_guess"],
         json.dumps(result["needs_review_reasons"]), model, classifier.prompt_version()))

    log(conn, case_id, "recon", "case_created", None, complaint_id, now)
    if result["status_lookup"]:
        s = result["status_lookup"]
        log(conn, case_id, "recon", "status_checked", None, f"{s['outcome']} after {s['attempts']} attempt(s)", now)
    log(conn, case_id, "recon", "classified", result["model_guess"],
        f"{result['dispute_type']} ({result['confidence']:.2f})", now)
    if result["needs_review_reasons"]:
        log(conn, case_id, "recon", "needs_review", None, "; ".join(result["needs_review_reasons"]), now)
    log(conn, case_id, "recon", "rules_applied", None,
        f"rule {d['rule_id'] or '-'}, deadline {d['deadline_date'] or '-'}, compensation Rs {d['compensation_inr']}, "
        f"escalate {'yes' if d['escalate'] else 'no'}", now)
    if result["draft"]:
        log(conn, case_id, "recon", "draft_created", None, result["draft"]["template_id"], now)
    elif result["draft_blocked"]:
        log(conn, case_id, "recon", "draft_blocked", None, result["draft_blocked"], now)
    conn.commit()
    return case_id


def _case_row(conn, case_id: str) -> dict:
    row = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if row is None:
        raise CaseError(404, "CASE_NOT_FOUND", f"No case {case_id}")
    return dict(row)


def history(conn, case_id: str) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT actor, action, old_value, new_value, logged_at FROM audit_log WHERE case_id = ? ORDER BY log_id",
        (case_id,))]


def get_case(conn, case_id: str) -> dict:
    c = _case_row(conn, case_id)
    record = json.loads(c["case_record"]) if c.get("case_record") else None
    return {
        "case_id": case_id,
        "case_status": c["case_status"],
        "dispute_type": c["dispute_type"],
        "agent_id": c["agent_id"],
        "sent_reply": c["sent_reply"],
        "closed_at": c["closed_at"],
        "case_source": c["case_source"],
        "record_at_open": record,
        "history": history(conn, case_id),
    }


def agent_event(conn, case_id: str, agent_id: str, event: str, value: Optional[str] = None,
                note: Optional[str] = None) -> dict:
    """One agent step. Every step is logged with the agent's id."""
    if event not in AGENT_EVENTS:
        raise CaseError(400, "INVALID_INPUT", f"event must be one of {AGENT_EVENTS}")
    c = _case_row(conn, case_id)
    if c["case_status"] == "CLOSED":
        raise CaseError(409, "CASE_CLOSED", f"{case_id} is closed; open a new case instead")

    if event == "opened":
        conn.execute("UPDATE cases SET agent_id = ? WHERE case_id = ?", (agent_id, case_id))
        log(conn, case_id, agent_id, "case_opened")
    elif event == "dispute_type_changed":
        if value not in classifier.DISPUTE_TYPES:
            raise CaseError(400, "INVALID_INPUT", f"value must be one of {classifier.DISPUTE_TYPES}")
        if not note:
            raise CaseError(400, "NOTE_REQUIRED", "Changing the dispute type needs a note saying why")
        conn.execute("UPDATE cases SET dispute_type = ? WHERE case_id = ?", (value, case_id))
        log(conn, case_id, agent_id, "dispute_type_changed", c["dispute_type"], f"{value} | note: {note}")
    elif event == "reply_sent":
        if not value:
            raise CaseError(400, "INVALID_INPUT", "value must hold the reply text the agent sent")
        conn.execute("UPDATE cases SET sent_reply = ?, case_status = 'SENT', agent_id = COALESCE(agent_id, ?) "
                     "WHERE case_id = ?", (value, agent_id, case_id))
        log(conn, case_id, agent_id, "reply_sent", c["draft_reply"], value)
    elif event == "closed":
        conn.execute("UPDATE cases SET case_status = 'CLOSED', closed_at = ? WHERE case_id = ?", (_now(), case_id))
        log(conn, case_id, agent_id, "case_closed", c["case_status"], note or None)
    conn.commit()
    return get_case(conn, case_id)


def log_escalation(conn, case_id: str, agent_id: str, escalation_id: str, reasons: list) -> None:
    _case_row(conn, case_id)
    conn.execute("UPDATE cases SET case_status = 'ESCALATED' WHERE case_id = ?", (case_id,))
    log(conn, case_id, agent_id, "escalated", None, f"{escalation_id}: " + "; ".join(reasons))
    conn.commit()
