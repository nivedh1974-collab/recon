"""Build the mock database before tests if it isn't there yet (it is gitignored)."""
from recon import seed
from recon.db import DB_PATH


def pytest_sessionstart(session):
    if not DB_PATH.exists():
        seed.main()
