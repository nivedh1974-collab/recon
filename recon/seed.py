"""Generate the mock payment system: 500 UPI transactions for October 2026.

Real aggregates drive the mix (NPCI Sep/Aug 2026 files in Drive):
- App share among PhonePe / Google Pay / Paytm: NPCI UPI Applications, Sep 2026.
- Payer bank share and debit-reversal success: NPCI Top 50 Remitter Banks, Aug 2026.

The failure rate is deliberately ENRICHED. Real UPI technical declines are under 1%,
which would leave a 500-row sample with almost nothing to dispute. Here about a
quarter of rows are problem cases so every Recon path gets exercised.

Run:  python -m recon.seed
"""
import random
import string
from datetime import datetime, timedelta

from recon import error_codes as ec
from recon.db import connect, init_db

SEED = 42
N_TXNS = 500
START = datetime(2026, 10, 1)

# Sep 2026 volume (Mn): PhonePe 10,907 / Google Pay 7,715 / Paytm 1,943
APPS = {"PhonePe": 10907, "Google Pay": 7715, "Paytm": 1943}
APP_HANDLES = {"PhonePe": "ybl", "Google Pay": "okaxis", "Paytm": "ptyes"}

# Aug 2026 remitter volume (Mn) and debit-reversal success %.
# Kotak (2.88%) and ICICI (1.73%) look like reporting glitches in the Aug file,
# so their July values (95.15%, 89.57%) are used instead.
BANKS = {
    "State Bank of India": (6846, 0.9660),
    "Bank of Baroda": (1831, 0.9515),
    "HDFC Bank": (1786, 1.0000),
    "Union Bank of India": (1602, 0.8039),
    "Punjab National Bank": (1540, 0.9339),
    "Canara Bank": (1283, 0.7963),
    "Kotak Mahindra Bank": (1214, 0.9515),
    "Axis Bank": (1071, 0.9393),
    "ICICI Bank": (829, 0.8957),
    "Federal Bank": (313, 0.9360),
}

FIRST_NAMES = ["Ravi", "Priya", "Arjun", "Sneha", "Kiran", "Anil", "Meera", "Rahul",
               "Divya", "Suresh", "Fatima", "Joseph", "Lakshmi", "Imran", "Neha"]
MERCHANTS = ["Sharma Stores", "Zomato", "Swiggy", "Jio Recharge", "Airtel", "BSNL",
             "IRCTC", "Ajio", "PharmEasy", "BESCOM", "Rapido", "Uber India",
             "Big Bazaar", "Apollo Pharmacy", "Local Kirana"]

# Scenario mix (share of rows). Enriched; see module docstring.
SCENARIOS = [
    ("success", 0.70),
    ("business_decline", 0.08),
    ("tech_fail_before_debit", 0.04),
    ("tech_fail_after_debit", 0.11),
    ("beneficiary_frozen", 0.01),
    ("pending", 0.04),
    ("unknown", 0.02),
]


def pick(rng: random.Random, weighted: dict):
    keys = list(weighted)
    weights = [v[0] if isinstance(v, tuple) else v for v in weighted.values()]
    return rng.choices(keys, weights=weights, k=1)[0]


def amount(rng: random.Random, txn_type: str) -> float:
    median = 500 if txn_type == "P2P" else 250
    value = rng.lognormvariate(0, 1.3) * median
    if rng.random() < 0.05:  # tail of large payments to test the Rs 10,000 escalation rule
        value = rng.uniform(10_001, 90_000)
    return round(min(max(value, 1), 100_000), 2)


def make_txn(i: int, rng: random.Random) -> dict:
    app = pick(rng, APPS)
    payer_bank = pick(rng, BANKS)
    txn_type = "P2M" if rng.random() < 0.6 else "P2P"
    created = START + timedelta(minutes=rng.randint(0, 31 * 24 * 60 - 1))
    payer = rng.choice(FIRST_NAMES)
    if txn_type == "P2M":
        payee_name = rng.choice(MERCHANTS)
        payee_vpa = payee_name.lower().replace(" ", "") + "@okaxis"
    else:
        payee_name = rng.choice(FIRST_NAMES) + " " + rng.choice(string.ascii_uppercase)
        payee_vpa = payee_name.split()[0].lower() + str(rng.randint(10, 99)) + "@" + rng.choice(list(APP_HANDLES.values()))

    txn = {
        "txn_id": f"TXN{i:06d}",
        "utr": str(rng.randint(10**11, 10**12 - 1)),
        "created_at": created.isoformat(timespec="minutes"),
        "amount_inr": amount(rng, txn_type),
        "txn_type": txn_type,
        "app": app,
        "payer_vpa": f"{payer.lower()}{rng.randint(100, 999)}@{APP_HANDLES[app]}",
        "payer_bank": payer_bank,
        "payee_vpa": payee_vpa,
        "payee_name": payee_name,
        "payee_bank": pick(rng, BANKS),
        "status": "SUCCESS", "debited": 1, "credited": 1,
        "error_code": None, "reversal_status": "NONE", "reversed_at": None,
    }

    scenario = rng.choices([s for s, _ in SCENARIOS], weights=[w for _, w in SCENARIOS], k=1)[0]
    if scenario == "business_decline":
        txn.update(status="FAILED", debited=0, credited=0, error_code=rng.choice(list(ec.BUSINESS_DECLINES)))
    elif scenario == "tech_fail_before_debit":
        txn.update(status="FAILED", debited=0, credited=0, error_code=rng.choice(list(ec.TECH_DECLINES_BEFORE_DEBIT)))
    elif scenario in ("tech_fail_after_debit", "beneficiary_frozen"):
        codes = ec.TECH_DECLINES_AFTER_DEBIT if scenario == "tech_fail_after_debit" else ec.BENEFICIARY_FROZEN
        txn.update(status="FAILED", debited=1, credited=0, error_code=rng.choice(list(codes)))
        _apply_reversal(txn, created, rng)
    elif scenario == "pending":
        txn.update(status="PENDING", debited=1, credited=None)
    elif scenario == "unknown":
        txn.update(status="UNKNOWN", debited=1, credited=None)
    return txn


def _apply_reversal(txn: dict, created: datetime, rng: random.Random) -> None:
    """Did the money come back on its own, and how fast?"""
    success_rate = BANKS[txn["payer_bank"]][1]
    if rng.random() < success_rate:
        # Most reversals land same day; some miss T+1 / T+5 to create TAT breaches.
        hours = rng.choice([2, 6, 12, 20, 30, 50, 100, 160])
        txn["reversal_status"] = "SUCCESS"
        txn["reversed_at"] = (created + timedelta(hours=hours)).isoformat(timespec="minutes")
    elif rng.random() < 0.5:
        txn["reversal_status"] = "FAILED"
        txn["error_code"] = "RR"
    # else: NONE, money still stuck with no reversal attempt recorded


def main() -> None:
    rng = random.Random(SEED)
    conn = connect()
    init_db(conn)
    conn.execute("DELETE FROM transactions")
    rows = [make_txn(i, rng) for i in range(1, N_TXNS + 1)]
    cols = list(rows[0])
    conn.executemany(
        f"INSERT INTO transactions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [tuple(r[c] for c in cols) for r in rows],
    )
    conn.commit()

    print(f"Inserted {len(rows)} transactions")
    for row in conn.execute(
        "SELECT status, debited, reversal_status, COUNT(*) n FROM transactions "
        "GROUP BY 1, 2, 3 ORDER BY n DESC"
    ):
        print(dict(row))


if __name__ == "__main__":
    main()
