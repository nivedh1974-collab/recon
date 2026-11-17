"""Rules engine: deadline, compensation and escalation. Plain code, no model.

Source: RBI circular RBI/2019-20/67 (20 Sept 2019), TAT for failed transactions.
- UPI P2P, debited but not credited to beneficiary: auto-reversal by T+1,
  Rs 100 per day of delay after that.
- UPI P2M, debited but merchant didn't get confirmation: auto-reversal by T+5,
  Rs 100 per day of delay after that.
- T = the calendar date of the transaction.
The rules themselves (TAT days, Rs per day) are read from the `rules` table,
so a change in RBI rules is a row edit, not a code change.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

ESCALATION_AMOUNT_INR = 10_000
MIN_CONFIDENCE = 0.8  # assumption; to be calibrated on the 61 real complaints

DISPUTE_TO_RULE = {"P2P failed": "R-P2P", "P2M failed": "R-P2M", "Wrong UPI ID": "R-WRONG"}
OUT_OF_SCOPE_ROUTES = {
    "AutoPay": "AutoPay team",
    "fraud": "Fraud team",
    "double payment": "Agent reply: ask payee to return extra amount",
    "merchant display issue": "Merchant support",
}


@dataclass
class Decision:
    rule_id: Optional[str] = None
    refund_state: str = "not_applicable"  # not_applicable | not_due | due | refunded | refunded_late | unknown
    deadline_date: Optional[date] = None
    days_past_deadline: int = 0
    compensation_inr: int = 0
    escalate: bool = False
    escalation_reasons: list = field(default_factory=list)
    route_to: Optional[str] = None
    agent_note: str = ""


def _to_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value)).date()


def decide(
    dispute_type: str,
    txn: Optional[dict],
    rules: dict,
    confidence: float = 1.0,
    fraud_signal: bool = False,
    out_of_scope_reason: Optional[str] = None,
    amount_inr: Optional[float] = None,
    as_of: Optional[date] = None,
) -> Decision:
    """Apply RBI rules to one case.

    dispute_type: P2P failed | P2M failed | Wrong UPI ID | Out of scope | Needs review
    txn: row from the status API, or None if no transaction matched
    rules: {rule_id: {"tat_days": int|None, "comp_per_day_inr": int}}
    """
    d = Decision()
    as_of = as_of or date.today()
    amount = txn["amount_inr"] if txn else amount_inr

    # 1. Fraud always leaves the normal flow, whatever the label says.
    if fraud_signal or out_of_scope_reason == "fraud":
        d.route_to = OUT_OF_SCOPE_ROUTES["fraud"]
        d.escalate = True
        d.escalation_reasons.append("fraud signal")
        d.agent_note = "Do not draft a reply. Route to fraud team."
        return d

    if dispute_type == "Out of scope":
        d.route_to = OUT_OF_SCOPE_ROUTES.get(out_of_scope_reason or "", "Manual triage")
        d.agent_note = f"Out of scope for Recon v1 ({out_of_scope_reason or 'unspecified'})."
        return d

    # 2. Model unsure: a person decides.
    if dispute_type == "Needs review" or confidence < MIN_CONFIDENCE:
        d.escalate = True
        d.escalation_reasons.append(f"low confidence ({confidence:.2f})")

    if amount is not None and amount > ESCALATION_AMOUNT_INR:
        d.escalate = True
        d.escalation_reasons.append(f"amount above Rs {ESCALATION_AMOUNT_INR:,}")

    rule_id = DISPUTE_TO_RULE.get(dispute_type)
    d.rule_id = rule_id
    if rule_id is None:
        d.agent_note = d.agent_note or "No rule applied until the dispute type is confirmed."
        return d

    rule = rules[rule_id]

    # 3. Wrong UPI ID: user-authorised payment, no RBI deadline, no compensation.
    if rule["tat_days"] is None:
        d.agent_note = "Payment was authorised by the customer. Bank can only request a return."
        return d

    # 4. Failed P2P / P2M: need the transaction to know anything for sure.
    if txn is None:
        d.refund_state = "unknown"
        d.agent_note = "Transaction not found. Ask the customer for the UTR."
        return d

    if txn["status"] in ("PENDING", "UNKNOWN"):
        d.escalate = True
        d.escalation_reasons.append(f"status {txn['status'].lower()}")

    if not txn["debited"]:
        d.refund_state = "not_due"
        d.agent_note = "Money never left the customer's account."
        return d

    if txn["status"] == "SUCCESS" and txn["credited"]:
        d.refund_state = "not_due"
        d.agent_note = "Records show the payee was credited. Share the UTR so the payee can check with their bank."
        return d

    txn_day = _to_date(txn["created_at"])
    d.deadline_date = txn_day + timedelta(days=rule["tat_days"])
    reversed_day = _to_date(txn.get("reversed_at"))

    if txn.get("reversal_status") == "SUCCESS" and reversed_day:
        late_by = (reversed_day - d.deadline_date).days
        d.refund_state = "refunded_late" if late_by > 0 else "refunded"
    else:
        late_by = (as_of - d.deadline_date).days
        d.refund_state = "due"

    d.days_past_deadline = max(late_by, 0)
    d.compensation_inr = d.days_past_deadline * rule["comp_per_day_inr"]

    if d.days_past_deadline > 0:
        d.escalate = True
        d.escalation_reasons.append(f"{d.days_past_deadline} day(s) past RBI deadline")
        d.agent_note = f"Bank owes Rs {d.compensation_inr:,} compensation. Escalate to payment service provider bank."
    elif d.refund_state == "refunded":
        d.agent_note = f"Refund already returned on {reversed_day}."
    else:
        d.agent_note = f"Refund due by {d.deadline_date}. No compensation owed yet."
    return d
