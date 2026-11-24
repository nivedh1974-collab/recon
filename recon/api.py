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


# ---------- API v1 (PRD: API spec) ----------
from fastapi.exceptions import RequestValidationError  # noqa: E402

from recon import api_v1  # noqa: E402

app.include_router(api_v1.router)
app.add_exception_handler(api_v1.ApiError, api_v1.api_error_handler)
app.add_exception_handler(RequestValidationError, api_v1.validation_handler)


# ---------- Case history view (PRD: Controls) ----------
import html as _html  # noqa: E402
import json as _json  # noqa: E402

from recon import case_store  # noqa: E402


@app.get("/cases/{case_id}", response_class=HTMLResponse)
def case_history_page(case_id: str) -> HTMLResponse:
    esc = _html.escape
    with connect() as conn:
        try:
            c = case_store.get_case(conn, case_id)
        except case_store.CaseError:
            return HTMLResponse("<p>No such case.</p>", status_code=404)
    rec = c["record_at_open"] or {}
    model = rec.get("model", {})
    d = rec.get("decision", {})
    reasons = "".join(f"<li>{esc(r)}</li>" for r in rec.get("needs_review_reasons", [])) or "<li>None</li>"
    summary = [
        ("Status", c["case_status"]), ("Dispute type now", c["dispute_type"]),
        ("Model guess", f"{model.get('guess')} ({model.get('confidence')})"),
        ("Model / prompt", f"{model.get('name')} / {model.get('prompt_version')}"),
        ("Rule, deadline", f"{d.get('rule_id') or '-'}, {d.get('deadline_date') or '-'}"),
        ("Compensation", f"Rs {d.get('compensation_inr', 0)}"), ("Agent", c["agent_id"] or "-"),
    ]
    srows = "".join(f"<tr><th>{esc(a)}</th><td>{esc(str(b))}</td></tr>" for a, b in summary)
    hrows = "".join(
        f"<tr><td>{esc(h['logged_at'])}</td><td>{esc(h['actor'])}</td><td>{esc(h['action'])}</td>"
        f"<td>{esc(h['old_value'] or '')}</td><td>{esc(h['new_value'] or '')}</td></tr>" for h in c["history"])
    complaint = esc(rec.get("complaint", {}).get("text", ""))
    record_json = esc(_json.dumps(rec, indent=2))
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Case {esc(case_id)}</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#1f2328}}
table{{border-collapse:collapse;width:100%;margin:8px 0 24px}}td,th{{border-bottom:1px solid #d0d7de;padding:6px 8px;
text-align:left;vertical-align:top}}th{{width:180px;color:#57606a;font-weight:500}}
blockquote{{margin:0;padding:8px 12px;background:#f6f8fa;border-left:3px solid #d0d7de}}
pre{{background:#f6f8fa;padding:12px;overflow-x:auto;font-size:12px}}.note{{color:#57606a}}</style></head><body>
<h1>Case {esc(case_id)}</h1>
<p class="note">History is append-only. Nothing here can be edited or deleted.</p>
<h2>Complaint</h2><blockquote>{complaint}</blockquote>
<h2>Summary</h2><table>{srows}</table>
<h2>Needs review reasons at open</h2><ul>{reasons}</ul>
<h2>History</h2><table><tr><th>When</th><th>Who</th><th>What</th><th>Before</th><th>After</th></tr>{hrows}</table>
<details><summary>Full record at open (JSON)</summary><pre>{record_json}</pre></details>
</body></html>""")
