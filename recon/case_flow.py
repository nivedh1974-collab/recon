"""Open a case: status lookup -> classify -> rules -> draft, with the failure rules applied.

PRD: Failure handling. Anything Recon can't trust sends the case to Needs review,
and the reason is always shown to the agent. Recon never guesses past a failure.
"""
import time
from dataclasses import asdict
from datetime import date, datetime
from typing import Optional

from recon import classifier, drafter
from recon.payment_system import lookup_with_retry
from recon.rules_engine import MIN_CONFIDENCE, decide


def _jsonable(d: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in d.items()}


def needs_review_reasons(lookup, ref: Optional[str], c: dict) -> list:
    reasons = []
    if lookup is not None:
        if lookup.failed:
            what = "timed out" if lookup.outcome == "timeout" else "returned an error"
            reasons.append(f"Status check {what} twice. Check the payment manually before replying.")
        elif lookup.outcome == "not_found":
            reasons.append(f"No transaction found for {ref}. Ask the customer to confirm the UTR.")

    failure = c.get("failure")
    if failure == "schema":
        reasons.append("Model answer failed the format check: " + "; ".join(c.get("schema_problems", [])) + ".")
    elif failure == "model_timeout":
        reasons.append("Model did not answer in time.")
    elif failure == "model_error":
        reasons.append("Model call failed.")
    elif c["dispute_type"] == "Needs review":
        why = "; ".join(c.get("overrides") or []) or c.get("reason") or "no reason given"
        reasons.append(f"Model could not decide: {why}.")
    elif c["confidence"] < MIN_CONFIDENCE:
        reasons.append(f"Low confidence ({c['confidence']:.2f}). Model guessed {c['dispute_type']}.")
    return reasons


def open_case(conn, complaint_text: str, txn_id: Optional[str] = None, utr: Optional[str] = None,
              mock_fail: Optional[str] = None, call=None, sleep=time.sleep) -> dict:
    lookup, txn = None, None
    if txn_id or utr:
        lookup = lookup_with_retry(conn, txn_id, utr, mock_fail, sleep=sleep)
        txn = lookup.txn

    c = classifier.classify(complaint_text, txn=txn, call=call)
    reasons = needs_review_reasons(lookup, txn_id or utr, c)
    final_type = "Needs review" if reasons else c["dispute_type"]

    rules = {r["rule_id"]: dict(r) for r in conn.execute("SELECT * FROM rules")}
    d = decide(final_type, txn, rules, confidence=c["confidence"], fraud_signal=c["fraud_signal"],
               out_of_scope_reason=c["out_of_scope_reason"], amount_inr=c["amount_inr"])
    decision = _jsonable(asdict(d))

    draft, draft_blocked = None, None
    case_in = drafter.build_case({**c, "dispute_type": final_type}, decision, txn)
    case_in["status_unavailable"] = bool(lookup and lookup.failed)
    case_in["utr"] = case_in["utr"] or utr
    try:
        draft = drafter.draft(case_in)
    except drafter.DraftNotAllowed as e:
        draft_blocked = str(e)

    return {
        "dispute_type": final_type,
        "needs_review": bool(reasons),
        "needs_review_reasons": reasons,
        "model_guess": c["dispute_type"],
        "confidence": c["confidence"],
        "classification": {k: c.get(k) for k in ("fraud_signal", "fraud_words", "out_of_scope_reason", "reason",
                                                 "overrides", "failure", "amount_inr", "app")},
        "status_lookup": None if lookup is None else
            {"attempts": lookup.attempts, "outcome": lookup.outcome, "notes": lookup.notes},
        "txn_id": txn["txn_id"] if txn else None,
        "decision": decision,
        "draft": draft,
        "draft_blocked": draft_blocked,
    }
