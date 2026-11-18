"""Simulated agent sessions so the dashboard has something to show before real agents use it.

Every case written here is marked case_source = 'simulated', and the dashboard says so.
Agent behaviour (handle time, how much they edit, when they open the case) is random
within stated ranges. These are NOT measurements of anything real.

Run:  python -m recon.simulate
"""
import random
from datetime import datetime, timedelta

from recon.db import connect, init_db
from recon.rules_engine import decide

SEED = 7

# Assumed ranges for the simulation only.
HANDLE_MINUTES = (2, 15)
COMPLAINT_DELAY_HOURS = (1, 120)   # time from payment to customer complaint
QUEUE_WAIT_HOURS = (0, 30)         # time the case sits before an agent opens it
EDIT_MIX = {"none": 0.5, "minor": 0.3, "major": 0.2}


def template_draft(txn: dict, d) -> str:
    """Stand-in for the model's draft until the reply drafter is built."""
    amt = f"Rs {txn['amount_inr']:,.0f}"
    if d.refund_state in ("refunded", "refunded_late"):
        return f"Your {amt} payment was reversed to your account. {d.agent_note}"
    if d.deadline_date:
        return (f"Your {amt} payment did not reach the receiver. Your bank must return it by "
                f"{d.deadline_date:%d %b %Y}. {d.agent_note}")
    return f"We checked your {amt} payment. {d.agent_note}"


def agent_edit(draft: str, kind: str, rng: random.Random) -> str:
    if kind == "none":
        return draft
    if kind == "minor":
        return draft + " Sorry for the trouble."
    return "Hi, we're looking into this with your bank and will update you within 24 hours. " + draft[: len(draft) // 3]


def main() -> None:
    rng = random.Random(SEED)
    conn = connect()
    init_db(conn)
    conn.execute("DELETE FROM audit_log WHERE case_id IN (SELECT case_id FROM cases WHERE case_source='simulated')")
    conn.execute("DELETE FROM cases WHERE case_source='simulated'")
    conn.execute("DELETE FROM complaints WHERE complaint_id LIKE 'SIM%'")
    rules = {r["rule_id"]: dict(r) for r in conn.execute("SELECT * FROM rules")}

    txns = [dict(r) for r in conn.execute(
        "SELECT * FROM transactions WHERE debited = 1 AND status != 'SUCCESS' ORDER BY txn_id")]
    for i, t in enumerate(txns, 1):
        paid = datetime.fromisoformat(t["created_at"])
        received = paid + timedelta(hours=rng.uniform(*COMPLAINT_DELAY_HOURS))
        opened = received + timedelta(hours=rng.uniform(*QUEUE_WAIT_HOURS))
        done = opened + timedelta(minutes=rng.uniform(*HANDLE_MINUTES))

        dispute = "P2P failed" if t["txn_type"] == "P2P" else "P2M failed"
        confidence = round(rng.uniform(0.6, 0.99), 2)
        if confidence < 0.7:
            dispute = "Needs review"
        d = decide(dispute, t, rules, confidence=confidence, as_of=done.date())
        draft = template_draft(t, d)
        kind = rng.choices(list(EDIT_MIX), weights=list(EDIT_MIX.values()))[0]
        sent = agent_edit(draft, kind, rng)

        cid, case_id = f"SIM{i:04d}", f"CASE-SIM{i:04d}"
        agent = f"AG{rng.randint(1, 6):02d}"
        conn.execute(
            "INSERT INTO complaints (complaint_id, received_at, channel, text, txn_id) VALUES (?,?,?,?,?)",
            (cid, received.isoformat(timespec="minutes"), "in-app",
             f"[simulated] Rs {t['amount_inr']:,.0f} debited, not received", t["txn_id"]))
        conn.execute(
            """INSERT INTO cases (case_id, complaint_id, dispute_type, confidence, rule_id, deadline_date,
               days_past_deadline, compensation_inr, escalate, escalation_reasons, draft_reply, sent_reply,
               case_status, agent_id, created_at, closed_at, case_source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'simulated')""",
            (case_id, cid, dispute, confidence, d.rule_id,
             d.deadline_date.isoformat() if d.deadline_date else None,
             d.days_past_deadline, d.compensation_inr, int(d.escalate), "; ".join(d.escalation_reasons),
             draft, sent, "ESCALATED" if d.escalate else "SENT", agent,
             received.isoformat(timespec="minutes"), done.isoformat(timespec="minutes")))
        log = [("recon", "case_created", None, dispute, received),
               (agent, "case_opened", None, None, opened),
               (agent, "reply_sent", draft, sent, done)]
        if d.escalate:
            log.append((agent, "escalated", None, "; ".join(d.escalation_reasons), done))
        conn.executemany(
            "INSERT INTO audit_log (case_id, actor, action, old_value, new_value, logged_at) VALUES (?,?,?,?,?,?)",
            [(case_id, a, act, old, new, ts.isoformat(timespec="minutes")) for a, act, old, new, ts in log])
    conn.commit()
    print(f"Simulated {len(txns)} cases")


if __name__ == "__main__":
    main()
