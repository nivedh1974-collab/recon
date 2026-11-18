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


# ---------- Ops metrics (PRD: Ops metrics dashboard, US-16) ----------
from fastapi.responses import HTMLResponse  # noqa: E402

from recon import metrics as ops_metrics  # noqa: E402


@app.get("/metrics")
def get_metrics() -> dict:
    with connect() as conn:
        return ops_metrics.compute(conn)


def _pct(x):
    return "n/a" if x is None else f"{x:.0%}"


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> str:
    with connect() as conn:
        m = ops_metrics.compute(conn)
    if not m.get("cases"):
        body = "<p>No cases yet.</p>"
    else:
        h, e, dl = m["handle_time_min"], m["draft_edits"], m["deadlines_met"]
        rows = [
            ("Handle time per ticket", f"median {h['median']} min, p90 {h['p90']} min", h["n"], "Median under 5 min"),
            ("AI draft edit rate", f"none {_pct(e['none'])}, minor {_pct(e['minor'])}, major {_pct(e['major'])}", e["n"], "60%+ none or minor"),
            ("Deadlines met", f"{_pct(dl['share'])} ({dl['met']} of {dl['n']})", dl["n"], "95%+"),
            ("Escalation rate", _pct(m["escalation_rate"]["share"]), m["escalation_rate"]["n"], "Tracked, no target"),
            ("Needs review rate", _pct(m["needs_review_rate"]["share"]), m["needs_review_rate"]["n"], "Under 10%"),
        ]
        trs = "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td><td>{d}</td></tr>" for a, b, c, d in rows)
        banner = ('<p class="sim">Simulated data. These numbers come from simulated agent sessions, '
                  'not real agents.</p>') if m["simulated"] else ""
        body = (f"{banner}<table><tr><th>Metric</th><th>Value</th><th>Cases</th><th>Target (assumption)</th></tr>"
                f"{trs}</table><p class='meta'>{m['cases']} cases. Computed {m['computed_at']}.</p>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Recon ops metrics</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font-family:system-ui,sans-serif;max-width:900px;margin:32px auto;padding:0 16px;color:#1a1a1a}}
table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #ddd;padding:8px;text-align:left}}
.sim{{background:#fff4d6;padding:8px 12px;border-radius:6px}}.meta{{color:#666;font-size:14px}}</style></head>
<body><h1>Recon ops metrics</h1>{body}</body></html>"""
