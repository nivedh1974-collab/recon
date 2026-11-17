"""Transaction status API: how Recon asks the payment system what happened.

Run:  uvicorn recon.api:app --reload
Docs: http://127.0.0.1:8000/docs
"""
from fastapi import FastAPI, HTTPException

from recon.db import connect
from recon.error_codes import ALL_CODES

app = FastAPI(title="Recon mock payment system", version="0.1.0")


def _row_to_dict(row) -> dict:
    data = dict(row)
    data["debited"] = bool(data["debited"])
    data["credited"] = None if data["credited"] is None else bool(data["credited"])
    data["error_meaning"] = ALL_CODES.get(data["error_code"]) if data["error_code"] else None
    return data


@app.get("/health")
def health() -> dict:
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    return {"status": "ok", "transactions": n}


@app.get("/transactions/{txn_id}")
def get_by_txn_id(txn_id: str) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM transactions WHERE txn_id = ?", (txn_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return _row_to_dict(row)


@app.get("/transactions/utr/{utr}")
def get_by_utr(utr: str) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM transactions WHERE utr = ?", (utr,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return _row_to_dict(row)
