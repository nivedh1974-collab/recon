"""Build the mock database before tests if it is missing or empty (it is gitignored)."""
import sqlite3

from recon import seed
from recon.db import DB_PATH


def _has_transactions() -> bool:
    if not DB_PATH.exists():
        return False
    try:
        with sqlite3.connect(DB_PATH) as conn:
            return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] > 0
    except sqlite3.OperationalError:
        return False


def pytest_sessionstart(session):
    if not _has_transactions():
        seed.main()
