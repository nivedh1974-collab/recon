from fastapi.testclient import TestClient

from recon.api import app
from recon.db import connect

client = TestClient(app)


def test_health_counts_500_transactions():
    assert client.get("/health").json() == {"status": "ok", "transactions": 500}


def test_lookup_by_txn_id_and_utr_match():
    by_id = client.get("/transactions/TXN000001").json()
    by_utr = client.get(f"/transactions/utr/{by_id['utr']}").json()
    assert by_id == by_utr


def test_unknown_transaction_returns_404():
    assert client.get("/transactions/TXN999999").status_code == 404


def test_failed_debited_rows_explain_their_error_code():
    with connect() as conn:
        txn_id = conn.execute(
            "SELECT txn_id FROM transactions WHERE status='FAILED' AND debited=1 LIMIT 1"
        ).fetchone()[0]
    body = client.get(f"/transactions/{txn_id}").json()
    assert body["debited"] is True and body["credited"] is False
    assert body["error_meaning"]


def test_business_declines_are_never_debited():
    with connect() as conn:
        bad = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE error_code IN ('ZM','Z9','Z8','U16','ZA') AND debited=1"
        ).fetchone()[0]
    assert bad == 0
