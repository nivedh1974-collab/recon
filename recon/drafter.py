"""Reply drafter, v1: fixed templates filled from the case. No model.

The agent edits and sends. Recon never sends a reply itself.
A model-written draft can replace these later; the templates stay as the fallback.
"""
from typing import Optional

TEMPLATES = {
    "refund_due": (
        "Hi, sorry for the trouble with your payment of Rs {amount} (UTR {utr}). "
        "The money was debited but did not reach {payee}. Under RBI rules it {due_verb} back in your account by {deadline}. "
        "{late_line}We are following up with the bank and will update you."
    ),
    "refunded": (
        "Hi, your payment of Rs {amount} (UTR {utr}) was reversed to your account on {reversed_at}. "
        "{late_line}Please check your bank statement. Reply here if you can't see it."
    ),
    "not_debited": (
        "Hi, we checked your payment (UTR {utr}). The money did not leave your account, so no refund is needed. "
        "You can try the payment again."
    ),
    "payee_credited": (
        "Hi, we checked your payment of Rs {amount} (UTR {utr}). Our records show it reached the receiver. "
        "Please share this UTR with them so they can check with their bank."
    ),
    "wrong_upi_id": (
        "Hi, sorry to hear the money went to the wrong person. Because the payment was approved with your UPI PIN, "
        "it cannot be reversed automatically. We will ask the receiver's bank to request a return. "
        "This depends on the receiver agreeing."
    ),
    "need_details": (
        "Hi, sorry for the trouble. To check this, please share the UTR (12-digit number in your payment history), "
        "the amount and the date of the payment."
    ),
    "checking": (
        "Hi, sorry for the trouble. We are checking the status of your payment (UTR {utr}) with the bank "
        "and will update you as soon as we have it."
    ),
    "out_of_scope": (
        "Hi, thanks for reaching out. We have passed this to our {route} team, who will contact you."
    ),
}


class DraftNotAllowed(Exception):
    pass


def _money(x) -> str:
    if x is None:
        return "?"
    return f"{x:,.0f}" if float(x).is_integer() else f"{x:,.2f}"


def draft(case: dict) -> dict:
    """case keys: dispute_type, refund_state, route_to, amount_inr, utr, payee, deadline_date,
    days_past_deadline, compensation_inr, reversed_at, fraud_signal."""
    if case.get("fraud_signal") or case.get("route_to") == "Fraud team":
        raise DraftNotAllowed("Possible fraud: route to the fraud team. No reply is drafted.")

    dtype, state = case.get("dispute_type"), case.get("refund_state")
    comp = case.get("compensation_inr") or 0
    late_line = (f"It is {case.get('days_past_deadline')} day(s) late, so you are owed Rs {_money(comp)} "
                 f"compensation by your bank. ") if comp else ""

    if case.get("status_unavailable"):
        tid = "checking"
    elif dtype == "Out of scope":
        tid = "out_of_scope"
    elif dtype == "Wrong UPI ID":
        tid = "wrong_upi_id"
    elif dtype == "Needs review" or not case.get("utr") or state in (None, "unknown"):
        tid = "need_details"
    elif state == "due":
        tid = "refund_due"
    elif state in ("refunded", "refunded_late"):
        tid = "refunded"
    elif state == "not_due" and case.get("debited") is False:
        tid = "not_debited"
    elif state == "not_due":
        tid = "payee_credited"
    else:
        tid = "need_details"

    text = TEMPLATES[tid].format(
        amount=_money(case.get("amount_inr")), utr=case.get("utr") or "?",
        payee=case.get("payee") or "the receiver", deadline=case.get("deadline_date") or "?",
        reversed_at=case.get("reversed_at") or "?", late_line=late_line,
        due_verb="was due" if comp else "is due",
        route=(case.get("route_to") or "support").replace(" team", ""),
    )
    return {"template_id": tid, "draft": text, "language": "en", "sent": False}


def build_case(classification: dict, decision: dict, txn: Optional[dict]) -> dict:
    """Join classifier output, rules decision and transaction into drafter input."""
    return {
        "dispute_type": classification["dispute_type"],
        "fraud_signal": classification.get("fraud_signal", False),
        "refund_state": decision.get("refund_state"),
        "route_to": decision.get("route_to"),
        "deadline_date": decision.get("deadline_date"),
        "days_past_deadline": decision.get("days_past_deadline"),
        "compensation_inr": decision.get("compensation_inr"),
        "amount_inr": txn["amount_inr"] if txn else classification.get("amount_inr"),
        "utr": txn["utr"] if txn else classification.get("utr"),
        "payee": txn["payee_name"] if txn else None,
        "debited": txn["debited"] if txn else None,
        "reversed_at": (txn.get("reversed_at") or "")[:10] or None if txn else None,
    }
