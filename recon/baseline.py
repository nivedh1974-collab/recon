"""Manual baseline (PRD: Validation). Times the same kind of work done by hand and with Recon.

Two matched sets of 6 cases. Set A is done by hand, set B with Recon, so no case is seen twice.
The complaints are written for this test and matched to mock transactions (seeded, so stable).
Answers are scored against the rules engine.

    python -m recon.baseline report      # print results
"""
import json
import sys
from dataclasses import asdict
from datetime import date, datetime
from statistics import median
from typing import Optional

from recon.rules_engine import decide

NEXT_STEPS = ["Reply only", "Escalate to PSP bank", "Route to fraud team", "Ask customer for details"]
TYPES = ["P2P failed", "P2M failed", "Wrong UPI ID", "Out of scope", "Needs review"]

# (key, scenario, txn_id or None, expected dispute type, complaint template)
CASES = {
    "A": [
        ("A1", "failed, not reversed, late", "TXN000061", "P2P failed",
         "I sent Rs {amt} to {payee} on {day} using {app}. Money got debited but {payee} says nothing came."),
        ("A2", "failed, refunded", "TXN000029", "P2M failed",
         "Paid Rs {amt} to {payee} on {day} through {app}. Payment failed but money was cut. Where is my money?"),
        ("A3", "pending", "TXN000053", "P2M failed",
         "My payment of Rs {amt} to {payee} on {day} is still showing pending in {app}. Amount is debited."),
        ("A4", "not debited", "TXN000021", "P2P failed",
         "Tried to send Rs {amt} to {payee} on {day} on {app}, it showed failed. Has my money gone?"),
        ("A5", "wrong UPI ID", "TXN000005", "Wrong UPI ID",
         "On {day} I sent Rs {amt} on {app} but typed one digit wrong. It went to {payee}, who I don't know."),
        ("A6", "possible fraud", None, "Out of scope",
         "Got a call from someone saying he is from bank KYC team. He asked me to install an app and share the "
         "OTP. After that Rs 9,500 was debited from my account."),
    ],
    "B": [
        ("B1", "failed, not reversed, late", "TXN000080", "P2M failed",
         "Paid Rs {amt} to {payee} on {day} with {app}. Shop says not received, my account is debited."),
        ("B2", "failed, refunded", "TXN000018", "P2M failed",
         "Rs {amt} payment to {payee} on {day} failed on {app} but money was deducted. Please return it."),
        ("B3", "pending", "TXN000116", "P2M failed",
         "{app} payment of Rs {amt} to {payee} on {day} still pending, money gone from bank."),
        ("B4", "not debited", "TXN000033", "P2P failed",
         "Sent Rs {amt} to {payee} on {day} via {app}, it said failed. Is the money cut from my account?"),
        ("B5", "wrong UPI ID", "TXN000009", "Wrong UPI ID",
         "By mistake I paid Rs {amt} to the wrong number on {day} on {app}. It went to {payee}. Please help."),
        ("B6", "possible fraud", None, "Out of scope",
         "A buyer on OLX sent me a QR code and said scan it to receive the payment. I scanned and Rs 4,000 "
         "was debited instead."),
    ],
}
MODES = {"A": "manual", "B": "recon"}


def ensure_table(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS baseline_runs (
        run_id TEXT PRIMARY KEY, case_key TEXT NOT NULL, mode TEXT NOT NULL, agent_id TEXT NOT NULL,
        started_at TEXT NOT NULL, finished_at TEXT, seconds REAL, case_id TEXT,
        answers TEXT, expected TEXT, correct TEXT)""")
    conn.commit()


def _txn(conn, txn_id):
    if not txn_id:
        return None
    t = dict(conn.execute("SELECT * FROM transactions WHERE txn_id = ?", (txn_id,)).fetchone())
    t["debited"], t["credited"] = bool(t["debited"]), None if t["credited"] is None else bool(t["credited"])
    return t


def case_view(conn, key: str) -> dict:
    """What the tester sees: complaint text and (manual mode) the raw transaction record."""
    _, scenario, txn_id, _, tpl = next(c for s in CASES.values() for c in s if c[0] == key)
    t = _txn(conn, txn_id)
    text = tpl.format(amt=f"{t['amount_inr']:,.2f}", payee=t["payee_name"], app=t["app"],
                      day=datetime.fromisoformat(t["created_at"]).strftime("%d %b")) if t else tpl
    return {"key": key, "complaint": text, "txn": t}


def expected(conn, key: str, as_of: Optional[date] = None) -> dict:
    _, _, txn_id, dtype, _ = next(c for s in CASES.values() for c in s if c[0] == key)
    t = _txn(conn, txn_id)
    rules = {r["rule_id"]: dict(r) for r in conn.execute("SELECT * FROM rules")}
    fraud = dtype == "Out of scope"
    d = decide(dtype, t, rules, fraud_signal=fraud, as_of=as_of or date.today())
    if fraud:
        step = "Route to fraud team"
    elif d.escalate:
        step = "Escalate to PSP bank"
    else:
        step = "Reply only"
    return {"dispute_type": dtype, "deadline_date": d.deadline_date.isoformat() if d.deadline_date else None,
            "compensation_inr": d.compensation_inr, "next_step": step}


def score(answers: dict, exp: dict) -> dict:
    return {
        "dispute_type": answers.get("dispute_type") == exp["dispute_type"],
        "deadline": (answers.get("deadline_date") or None) == exp["deadline_date"],
        "compensation": int(answers.get("compensation_inr") or 0) == int(exp["compensation_inr"]),
        "next_step": answers.get("next_step") == exp["next_step"],
    }


def start(conn, key: str, agent_id: str, case_id: Optional[str] = None) -> str:
    ensure_table(conn)
    run_id = f"RUN-{key}-{datetime.now():%H%M%S%f}"
    conn.execute("INSERT INTO baseline_runs (run_id, case_key, mode, agent_id, started_at, case_id) "
                 "VALUES (?,?,?,?,?,?)", (run_id, key, MODES[key[0]], agent_id,
                                         datetime.now().isoformat(timespec="milliseconds"), case_id))
    conn.commit()
    return run_id


def finish(conn, run_id: str, answers: dict, finished_at: Optional[str] = None) -> dict:
    row = dict(conn.execute("SELECT * FROM baseline_runs WHERE run_id = ?", (run_id,)).fetchone())
    end = finished_at or datetime.now().isoformat(timespec="milliseconds")
    secs = (datetime.fromisoformat(end) - datetime.fromisoformat(row["started_at"])).total_seconds()
    exp = expected(conn, row["case_key"])
    ok = score(answers, exp)
    conn.execute("UPDATE baseline_runs SET finished_at=?, seconds=?, answers=?, expected=?, correct=? WHERE run_id=?",
                 (end, round(secs, 1), json.dumps(answers), json.dumps(exp), json.dumps(ok), run_id))
    conn.commit()
    return {"seconds": round(secs, 1), "correct": ok, "expected": exp}


def recon_answers(conn, case_id: str) -> tuple:
    """What the agent ended with in Recon, and when they were done (reply sent, routed, escalated or closed)."""
    c = dict(conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone())
    log = [dict(r) for r in conn.execute("SELECT action, logged_at FROM audit_log WHERE case_id = ? ORDER BY log_id",
                                         (case_id,))]
    actions = {r["action"] for r in log}
    if "routed_to_fraud_team" in actions:
        step = "Route to fraud team"
    elif "escalated" in actions:
        step = "Escalate to PSP bank"
    elif c["dispute_type"] == "Needs review":
        step = "Ask customer for details"
    else:
        step = "Reply only"
    done = [r["logged_at"] for r in log if r["action"] in ("reply_sent", "routed_to_fraud_team", "case_closed")]
    rec = json.loads(c["case_record"] or "{}")
    dtype = c["dispute_type"]
    if c["fraud_signal"] and dtype != "Out of scope":
        dtype = "Out of scope"  # routed as fraud: counts as the fraud answer
    answers = {"dispute_type": dtype, "deadline_date": c["deadline_date"],
               "compensation_inr": c["compensation_inr"], "next_step": step,
               "model_guess": rec.get("model", {}).get("guess")}
    return answers, (max(done) if done else None)


def report(conn) -> dict:
    ensure_table(conn)
    rows = [dict(r) for r in conn.execute("SELECT * FROM baseline_runs WHERE seconds IS NOT NULL")]
    out = {"testers": len({r["agent_id"] for r in rows})}
    for mode in ("manual", "recon"):
        rs = [r for r in rows if r["mode"] == mode]
        if not rs:
            out[mode] = {"n": 0}
            continue
        oks = [json.loads(r["correct"]) for r in rs]
        out[mode] = {"n": len(rs), "median_seconds": round(median(r["seconds"] for r in rs), 1),
                     **{f"{k}_correct": round(sum(o[k] for o in oks) / len(oks), 2) for k in oks[0]}}
    return out


if __name__ == "__main__":
    from recon.db import connect
    if sys.argv[1:] == ["report"]:
        print(json.dumps(report(connect()), indent=2))
    else:
        print(__doc__)
