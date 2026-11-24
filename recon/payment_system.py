"""Client for the mock payment system, with one retry.

Rule (PRD: Failure handling): a status lookup that times out or errors is retried once.
If the retry fails too, the case goes to Needs review with the reason shown.
A "not found" answer is not retried: asking again won't change it.

Failure simulation, for tests and demos (X-Mock-Fail header):
    timeout | error            every attempt fails
    timeout-once | error-once  first attempt fails, the retry works
"""
import time
from dataclasses import dataclass, field
from typing import Optional

MAX_ATTEMPTS = 2          # first try + one retry
RETRY_WAIT_S = 0.5        # assumption
TIMEOUT_S = 3             # per attempt; assumption, see PRD API spec


class StatusTimeout(Exception):
    pass


class StatusError(Exception):
    pass


@dataclass
class LookupResult:
    txn: Optional[dict] = None
    attempts: int = 0
    outcome: str = "ok"             # ok | not_found | timeout | error
    notes: list = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.outcome in ("timeout", "error")


def _row(conn, txn_id: Optional[str], utr: Optional[str]) -> Optional[dict]:
    if txn_id:
        row = conn.execute("SELECT * FROM transactions WHERE txn_id = ?", (txn_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM transactions WHERE utr = ?", (utr,)).fetchone()
    if row is None:
        return None
    t = dict(row)
    t["debited"] = bool(t["debited"])
    t["credited"] = None if t["credited"] is None else bool(t["credited"])
    return t


def fetch_status(conn, txn_id=None, utr=None, mock_fail: Optional[str] = None, attempt: int = 1):
    """One call to the mock payment system."""
    mode = (mock_fail or "").lower()
    if mode == "timeout" or (mode == "timeout-once" and attempt == 1):
        raise StatusTimeout(f"no answer within {TIMEOUT_S}s")
    if mode == "error" or (mode == "error-once" and attempt == 1):
        raise StatusError("payment system returned an error")
    return _row(conn, txn_id, utr)


def lookup_with_retry(conn, txn_id=None, utr=None, mock_fail: Optional[str] = None,
                      sleep=time.sleep) -> LookupResult:
    res = LookupResult()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        res.attempts = attempt
        try:
            txn = fetch_status(conn, txn_id, utr, mock_fail, attempt)
        except StatusTimeout as e:
            res.outcome = "timeout"
            res.notes.append(f"attempt {attempt}: {e}")
        except StatusError as e:
            res.outcome = "error"
            res.notes.append(f"attempt {attempt}: {e}")
        else:
            res.txn, res.outcome = txn, ("ok" if txn else "not_found")
            if attempt > 1:
                res.notes.append(f"attempt {attempt}: ok")
            return res
        if attempt < MAX_ATTEMPTS:
            sleep(RETRY_WAIT_S)
    return res
