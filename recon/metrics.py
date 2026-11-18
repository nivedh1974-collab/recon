"""Ops metrics, computed from the cases and audit_log tables. Nothing hard-coded.

Definitions match the "Ops metrics dashboard" section of the PRD.
"""
import sqlite3
from datetime import datetime
from difflib import SequenceMatcher
from statistics import median, quantiles
from typing import Optional

MINOR_EDIT_MAX = 0.20  # assumption: up to 20% of text changed counts as a minor edit


def edit_class(draft: Optional[str], sent: Optional[str]) -> Optional[str]:
    if not draft or sent is None:
        return None
    if draft == sent:
        return "none"
    changed = 1 - SequenceMatcher(None, draft, sent).ratio()
    return "minor" if changed <= MINOR_EDIT_MAX else "major"


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _share(part: int, whole: int) -> Optional[float]:
    return round(part / whole, 3) if whole else None


def compute(conn: sqlite3.Connection) -> dict:
    cases = [dict(r) for r in conn.execute("SELECT * FROM cases")]
    if not cases:
        return {"cases": 0}

    events = {}
    for r in conn.execute(
        "SELECT case_id, action, logged_at FROM audit_log "
        "WHERE action IN ('case_opened', 'reply_sent', 'escalated') ORDER BY logged_at"
    ):
        events.setdefault(r["case_id"], {}).setdefault(r["action"], r["logged_at"])

    handle_minutes, met, with_deadline = [], 0, 0
    edits = {"none": 0, "minor": 0, "major": 0}
    for c in cases:
        ev = events.get(c["case_id"], {})
        done_at = min((ev[a] for a in ("reply_sent", "escalated") if a in ev), default=None)
        if "case_opened" in ev and done_at:
            handle_minutes.append((_ts(done_at) - _ts(ev["case_opened"])).total_seconds() / 60)
        if c["deadline_date"]:
            with_deadline += 1
            if done_at and _ts(done_at).date().isoformat() <= c["deadline_date"]:
                met += 1
        kind = edit_class(c["draft_reply"], c["sent_reply"])
        if kind:
            edits[kind] += 1

    sent = sum(edits.values())
    n = len(cases)
    return {
        "cases": n,
        "simulated": any(c["case_source"] == "simulated" for c in cases),
        "handle_time_min": {
            "median": round(median(handle_minutes), 1) if handle_minutes else None,
            "p90": round(quantiles(handle_minutes, n=10)[-1], 1) if len(handle_minutes) >= 2 else None,
            "n": len(handle_minutes),
        },
        "draft_edits": {
            **{k: _share(v, sent) for k, v in edits.items()},
            "n": sent,
        },
        "deadlines_met": {"share": _share(met, with_deadline), "met": met, "n": with_deadline},
        "escalation_rate": {"share": _share(sum(1 for c in cases if c["escalate"]), n), "n": n},
        "needs_review_rate": {"share": _share(sum(1 for c in cases if c["dispute_type"] == "Needs review"), n), "n": n},
        "computed_at": datetime.now().isoformat(timespec="seconds"),
    }
