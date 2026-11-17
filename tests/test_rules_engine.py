from datetime import date

from recon.rules_engine import decide

RULES = {
    "R-P2P": {"tat_days": 1, "comp_per_day_inr": 100},
    "R-P2M": {"tat_days": 5, "comp_per_day_inr": 100},
    "R-WRONG": {"tat_days": None, "comp_per_day_inr": 0},
}


def txn(**overrides):
    base = {
        "amount_inr": 2000.0, "created_at": "2026-10-05T20:14", "status": "FAILED",
        "debited": True, "credited": False, "reversal_status": "NONE", "reversed_at": None,
    }
    base.update(overrides)
    return base


def test_p2p_within_deadline_owes_nothing_yet():
    d = decide("P2P failed", txn(), RULES, as_of=date(2026, 10, 6))
    assert d.deadline_date == date(2026, 10, 6)
    assert d.compensation_inr == 0 and not d.escalate


def test_p2p_three_days_late_owes_300_and_escalates():
    d = decide("P2P failed", txn(), RULES, as_of=date(2026, 10, 9))
    assert d.days_past_deadline == 3 and d.compensation_inr == 300
    assert d.escalate and "3 day(s) past RBI deadline" in d.escalation_reasons


def test_p2m_uses_t_plus_5():
    d = decide("P2M failed", txn(), RULES, as_of=date(2026, 10, 10))
    assert d.deadline_date == date(2026, 10, 10) and d.compensation_inr == 0


def test_reversed_late_still_owes_compensation():
    d = decide("P2P failed", txn(reversal_status="SUCCESS", reversed_at="2026-10-11T09:00"),
               RULES, as_of=date(2026, 10, 20))
    assert d.refund_state == "refunded_late" and d.compensation_inr == 500


def test_complaint_c007_rs30_returned_after_four_months():
    # Real case: Rs 30 came back ~4 months late; customer got ~Rs 12,000.
    d = decide("P2P failed", txn(amount_inr=30.0, created_at="2026-01-01T10:00",
                                 reversal_status="SUCCESS", reversed_at="2026-05-01T10:00"), RULES)
    assert d.compensation_inr == 11_900  # 119 days late x Rs 100


def test_wrong_upi_id_has_no_deadline_or_compensation():
    d = decide("Wrong UPI ID", txn(status="SUCCESS", credited=True), RULES, as_of=date(2026, 12, 1))
    assert d.deadline_date is None and d.compensation_inr == 0 and not d.escalate


def test_wrong_upi_id_above_10k_escalates():
    d = decide("Wrong UPI ID", txn(amount_inr=45_000, status="SUCCESS", credited=True), RULES)
    assert d.escalate and d.compensation_inr == 0


def test_never_debited_means_no_refund_due():
    d = decide("P2P failed", txn(debited=False), RULES, as_of=date(2026, 11, 1))
    assert d.refund_state == "not_due" and d.compensation_inr == 0


def test_pending_status_escalates():
    d = decide("P2P failed", txn(status="PENDING", credited=None), RULES, as_of=date(2026, 10, 5))
    assert d.escalate and "status pending" in d.escalation_reasons


def test_low_confidence_escalates():
    d = decide("P2P failed", txn(), RULES, confidence=0.6, as_of=date(2026, 10, 5))
    assert d.escalate


def test_fraud_routes_out_and_never_drafts():
    d = decide("P2P failed", txn(), RULES, fraud_signal=True)
    assert d.route_to == "Fraud team" and d.escalate and d.compensation_inr == 0


def test_no_transaction_asks_for_utr():
    d = decide("P2P failed", None, RULES)
    assert d.refund_state == "unknown" and "UTR" in d.agent_note


def test_out_of_scope_autopay_routes_to_team():
    d = decide("Out of scope", None, RULES, out_of_scope_reason="AutoPay")
    assert d.route_to == "AutoPay team" and not d.escalate
