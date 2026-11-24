"""Tests run on a fresh throwaway database, never on data/recon.db.

The audit log is append-only, so test cases written to the real database could never be removed.
RECON_DB is set before any recon module is imported.
"""
import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="recon-test-")) / "recon.db"
os.environ["RECON_DB"] = str(_tmp)

from recon import seed  # noqa: E402


def pytest_sessionstart(session):
    seed.main()
