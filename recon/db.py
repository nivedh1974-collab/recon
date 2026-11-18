"""SQLite schema for Recon. Matches the Data dictionary tab in the PRD."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "recon.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    txn_id          TEXT PRIMARY KEY,
    utr             TEXT UNIQUE NOT NULL,
    created_at      TEXT NOT NULL,          -- ISO datetime, starts the T+1 / T+5 clock
    amount_inr      REAL NOT NULL,
    txn_type        TEXT NOT NULL CHECK (txn_type IN ('P2P', 'P2M')),
    app             TEXT NOT NULL,          -- PhonePe, Google Pay, Paytm
    payer_vpa       TEXT NOT NULL,
    payer_bank      TEXT NOT NULL,          -- remitter bank
    payee_vpa       TEXT NOT NULL,
    payee_name      TEXT NOT NULL,
    payee_bank      TEXT NOT NULL,          -- beneficiary bank
    status          TEXT NOT NULL CHECK (status IN ('SUCCESS', 'FAILED', 'PENDING', 'UNKNOWN')),
    debited         INTEGER NOT NULL,       -- 1 = money left the payer's account
    credited        INTEGER,                -- 1 = reached payee, 0 = not, NULL = not known yet
    error_code      TEXT,                   -- NPCI response code, see recon/error_codes.py
    reversal_status TEXT NOT NULL DEFAULT 'NONE' CHECK (reversal_status IN ('NONE', 'SUCCESS', 'FAILED')),
    reversed_at     TEXT
);

CREATE TABLE IF NOT EXISTS complaints (
    complaint_id  TEXT PRIMARY KEY,
    received_at   TEXT NOT NULL,
    channel       TEXT NOT NULL CHECK (channel IN ('in-app', 'email', 'chat')),
    text          TEXT NOT NULL,
    language      TEXT,
    txn_id        TEXT REFERENCES transactions(txn_id),
    test_case_ref TEXT                      -- C001..C065 for the real-complaint test set
);

CREATE TABLE IF NOT EXISTS rules (
    rule_id          TEXT PRIMARY KEY,
    dispute_type     TEXT NOT NULL,
    tat_days         INTEGER,               -- NULL = no RBI deadline
    comp_per_day_inr INTEGER NOT NULL,
    source           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    case_id            TEXT PRIMARY KEY,
    complaint_id       TEXT NOT NULL REFERENCES complaints(complaint_id),
    dispute_type       TEXT,
    confidence         REAL,
    fraud_signal       INTEGER DEFAULT 0,
    rule_id            TEXT REFERENCES rules(rule_id),
    deadline_date      TEXT,
    days_past_deadline INTEGER DEFAULT 0,
    compensation_inr   INTEGER DEFAULT 0,
    escalate           INTEGER DEFAULT 0,
    escalation_reasons TEXT,
    draft_reply        TEXT,
    sent_reply         TEXT,
    case_status        TEXT NOT NULL DEFAULT 'OPEN' CHECK (case_status IN ('OPEN', 'SENT', 'ESCALATED', 'CLOSED')),
    agent_id           TEXT,
    created_at         TEXT,
    closed_at          TEXT,
    case_source        TEXT NOT NULL DEFAULT 'live' CHECK (case_source IN ('live', 'simulated'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    log_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id   TEXT NOT NULL REFERENCES cases(case_id),
    actor     TEXT NOT NULL,                -- 'recon' or an agent id
    action    TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    logged_at TEXT NOT NULL
);
"""

RULES = [
    ("R-P2P", "P2P failed", 1, 100, "RBI/2019-20/67, TAT for failed transactions, Sept 2019"),
    ("R-P2M", "P2M failed", 5, 100, "RBI/2019-20/67, TAT for failed transactions, Sept 2019"),
    ("R-WRONG", "Wrong UPI ID", None, 0, "User-authorised payment; bank can only request a return"),
]


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after v1; ALTER older databases so a git pull doesn't break them.
MIGRATIONS = {
    "cases": {
        "created_at": "TEXT",
        "case_source": "TEXT NOT NULL DEFAULT 'live'",
    },
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, ddl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.executemany("INSERT OR REPLACE INTO rules VALUES (?, ?, ?, ?, ?)", RULES)
    conn.commit()
