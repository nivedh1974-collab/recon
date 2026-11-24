"""Recon agent screens (PRD: Agent screens). Run:  streamlit run recon/ui.py

Every button calls the Recon API, so the rules, holds and audit log are the same as the API's.
By default the API runs in-process (one command). Set RECON_API=http://127.0.0.1:8000 to use a
running uvicorn server instead. Recon never sends a reply: the agent sends it from the helpdesk.
"""
import os
import uuid

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Recon", page_icon="🧾", layout="wide")


def _demo_model(complaint: str, temperature: float) -> dict:
    """Stand-in for screenshots and demos without a Groq key. Not the real classifier."""
    t = complaint.lower()
    if any(w in t for w in ("wrong upi", "wrong number", "by mistake")):
        dtype = "Wrong UPI ID"
    elif any(w in t for w in ("shop", "recharge", "bill", "merchant", "order", "store")):
        dtype = "P2M failed"
    else:
        dtype = "P2P failed"
    return {"dispute_type": dtype, "confidence": 0.9, "fraud_signal": False,
            "reason": "Demo model: keyword match, not the real classifier."}


_demo_model.model_name = "demo stand-in (keyword match, not the real model)"


@st.cache_resource
def _client():
    if os.environ.get("RECON_DEMO_MODEL"):
        from recon import classifier
        classifier._call_model = _demo_model
    base = os.environ.get("RECON_API")
    if base:
        import httpx
        return httpx.Client(base_url=base, timeout=60)
    from fastapi.testclient import TestClient
    from recon.api import app
    return TestClient(app)


def call(method: str, path: str, **kw):
    """Returns (ok, body). Errors come back in the API's {"error": {...}} shape."""
    r = _client().request(method, path, **kw)
    body = r.json() if r.content else {}
    if r.status_code >= 400:
        err = body.get("error", {}) if isinstance(body, dict) else {}
        return False, f"{err.get('code', r.status_code)}: {err.get('message', body)}"
    return True, body


def rs(x):
    return "-" if x in (None, "") else f"Rs {x:,.2f}".replace(".00", "") if isinstance(x, (int, float)) else str(x)


# ---------- sidebar ----------
st.sidebar.title("Recon")
st.sidebar.caption("Failed-payment copilot for support agents")
if "goto" in st.session_state:
    st.session_state["page"] = st.session_state.pop("goto")
agent = st.sidebar.text_input("Your agent id", value=st.session_state.get("agent", "A01"))
st.session_state["agent"] = agent
page = st.sidebar.radio("Screen", ["Queue", "Case", "New case"], key="page")
if not os.environ.get("GROQ_API_KEY") and not os.environ.get("RECON_DEMO_MODEL"):
    st.sidebar.warning("GROQ_API_KEY is not set. Model calls will fail and new cases go to Needs review.")
if os.environ.get("RECON_DEMO_MODEL"):
    st.sidebar.info("Demo model: a fixed stand-in, not the real classifier.")
st.sidebar.caption("Recon never sends replies or moves money. You do, after checking.")


def open_case(case_id: str):
    st.session_state["case_id"] = case_id
    st.session_state["goto"] = "Case"


# ---------- Queue ----------
def queue_screen():
    st.header("Queue")
    st.caption("Possible fraud first, then past deadline, then Needs review, then newest.")
    c1, c2, c3 = st.columns([2, 1, 1])
    statuses = c1.multiselect("Status", ["OPEN", "SENT", "ESCALATED", "CLOSED"], default=["OPEN", "ESCALATED"])
    only_fraud = c2.checkbox("Possible fraud only")
    only_review = c3.checkbox("Needs review only")
    ok, cases = call("GET", "/v1/cases")
    if not ok:
        st.error(cases)
        return
    cases = [c for c in cases if c["case_status"] in statuses]
    if only_fraud:
        cases = [c for c in cases if c["possible_fraud"]]
    if only_review:
        cases = [c for c in cases if c["needs_review"]]
    if not cases:
        st.info("No cases match. Use New case to add one.")
        return
    df = pd.DataFrame([{
        "Case": c["case_id"],
        "Flag": "🚩 Fraud" if c["possible_fraud"] else "⏰ Late" if (c["days_past_deadline"] or 0) > 0
                else "❓ Review" if c["needs_review"] else "",
        "Headline": c["headline"], "Status": c["case_status"], "Amount": rs(c["amount_inr"]),
        "Deadline": c["deadline_date"] or "-", "Compensation": rs(c["compensation_inr"]) if c["compensation_inr"] else "-",
        "Agent": c["agent_id"] or "-", "Complaint": c["complaint"],
    } for c in cases])
    sel = st.dataframe(df, hide_index=True, use_container_width=True, on_select="rerun",
                       selection_mode="single-row", key="queue_table")
    rows = sel.selection.rows if sel and sel.selection else []
    if rows:
        st.button(f"Open {df.iloc[rows[0]]['Case']}", type="primary",
                  on_click=open_case, args=(df.iloc[rows[0]]["Case"],))


# ---------- Case ----------
def case_screen():
    case_id = st.session_state.get("case_id")
    if not case_id:
        st.info("Pick a case in the Queue first.")
        return
    ok, c = call("GET", f"/v1/cases/{case_id}")
    if not ok:
        st.error(c)
        return
    rec = c["record_at_open"] or {}
    cur = c["current"]
    model = rec.get("model", {})
    txn = rec.get("transaction")
    fraud = c["possible_fraud"]

    st.header(f"Case {case_id}")
    if fraud:
        st.error("**Possible fraud: route to fraud team.** Don't promise a refund or a deadline.  \n"
                 "Signals: " + ", ".join((rec.get("possible_fraud") or {}).get("signals", [])))
    elif c["dispute_type"] == "Needs review":
        st.warning("**Needs review.** " + " ".join(rec.get("needs_review_reasons", [])))
    else:
        st.success(f"**{c['dispute_type']}**")
    st.caption(f"Status {c['case_status']} · agent {c['agent_id'] or 'unassigned'} · opened {rec.get('recorded_at', '')}")

    tab_case, tab_hist = st.tabs(["Case", "History"])
    with tab_case:
        left, right = st.columns(2)
        with left:
            st.subheader("Complaint")
            st.write(rec.get("complaint", {}).get("text", ""))
            st.subheader("Payment status")
            sl = rec.get("status_lookup")
            if txn:
                st.write(f"**{txn['status']}** · {txn['txn_type']} · {rs(txn['amount_inr'])} · {txn['app']}")
                st.write(f"Debited: {'yes' if txn['debited'] else 'no'} · Credited: "
                         f"{'-' if txn['credited'] is None else 'yes' if txn['credited'] else 'no'} · "
                         f"Reversal: {txn['reversal_status']}")
                st.caption(f"UTR {txn['utr']} · {txn['txn_id']} · paid {txn['created_at']} · error {txn['error_code'] or '-'}"
                           + (f" · checked in {sl['attempts']} attempts" if sl and sl["attempts"] > 1 else ""))
            elif sl:
                st.write(f"Status check: **{sl['outcome']}** after {sl['attempts']} attempt(s)")
            else:
                st.write("No UTR or transaction given.")
            st.subheader("Model")
            st.write(f"Guess: **{model.get('guess')}** · confidence {model.get('confidence')}")
            st.caption(f"{model.get('reason') or ''}  \n{model.get('name')} · prompt {model.get('prompt_version')}")
            for o in model.get("overrides") or []:
                st.caption(f"Code override: {o}")
        with right:
            st.subheader("Rules")
            m1, m2, m3 = st.columns(3)
            dl = cur["deadline_date"]
            m1.metric("Deadline", pd.to_datetime(dl).strftime("%d %b") if dl else "-",
                      help=f"RBI deadline {dl}" if dl else "No RBI deadline for this case")
            m2.metric("Days late", cur["days_past_deadline"] or 0)
            m3.metric("Compensation", rs(cur["compensation_inr"]) if cur["compensation_inr"] else "Rs 0")
            st.write(f"Rule: {cur['rule_id'] or '-'} · Escalate: {'yes' if cur['escalate'] else 'no'}")
            if cur["escalation_reasons"]:
                st.caption("Why: " + cur["escalation_reasons"])
            note = (rec.get("decision") or {}).get("agent_note")
            if note:
                st.info(note)

            st.subheader("Reply")
            if fraud:
                st.write("No draft for a possible-fraud case.")
            elif c["sent_reply"]:
                st.write("Sent:")
                st.code(c["sent_reply"], language=None)
            else:
                text = st.text_area("Draft (edit before sending)", value=cur["draft_reply"] or "", height=170,
                                    key=f"draft_{case_id}")
                st.caption("Copy this into your helpdesk and send it there. Then mark it sent.")
                if st.button("Mark reply sent", type="primary", disabled=c["case_status"] == "CLOSED"):
                    act(case_id, "reply_sent", value=text)

        st.divider()
        st.subheader("Actions")
        closed = c["case_status"] == "CLOSED"
        a1, a2, a3 = st.columns(3)
        with a1:
            if st.button("Assign to me", disabled=closed):
                act(case_id, "opened")
            types = ["P2P failed", "P2M failed", "Wrong UPI ID", "Out of scope", "Needs review"]
            new_type = st.selectbox("Change type", types, index=types.index(c["dispute_type"]))
            type_note = st.text_input("Why (required)", key="type_note")
            if st.button("Save type", disabled=closed or new_type == c["dispute_type"]):
                act(case_id, "dispute_type_changed", value=new_type, note=type_note)
            if st.button("Close case", disabled=closed):
                act(case_id, "closed")
        with a2:
            st.write("Payment service provider bank")
            if st.button("Escalate (I approve)", disabled=closed or fraud):
                key = st.session_state.setdefault(f"esc_key_{case_id}", str(uuid.uuid4()))
                reasons = [r for r in (cur["escalation_reasons"] or "agent decision").split("; ") if r]
                ok, body = call("POST", "/v1/escalations", headers={"Idempotency-Key": key},
                                json={"txn_id": (txn or {}).get("txn_id") or "", "reasons": reasons,
                                      "approved_by": agent, "case_id": case_id})
                (st.success(f"Escalated: {body['escalation_id']}") if ok else st.error(body))
            if fraud:
                st.caption("Held: possible fraud goes to the fraud team.")
        with a3:
            st.write("Fraud team")
            if st.button("Route to fraud team", disabled=closed or not fraud):
                act(case_id, "fraud_routed")
            clear_note = st.text_input("Not fraud because (required)", key="clear_note", disabled=not fraud)
            if st.button("Clear fraud flag", disabled=closed or not fraud):
                act(case_id, "fraud_cleared", note=clear_note)

    with tab_hist:
        st.caption("Append-only. Nothing here can be edited or deleted.")
        st.dataframe(pd.DataFrame(c["history"]).rename(columns={
            "logged_at": "When", "actor": "Who", "action": "What", "old_value": "Before", "new_value": "After"})
            [["When", "Who", "What", "Before", "After"]], hide_index=True, use_container_width=True)


def act(case_id, event, value=None, note=None):
    ok, body = call("POST", f"/v1/cases/{case_id}/events",
                    json={"agent_id": agent, "event": event, "value": value, "note": note})
    if ok:
        st.rerun()
    st.error(body)


# ---------- New case ----------
def new_case_screen():
    st.header("New case")
    st.caption("Demo only. In production, cases arrive from the helpdesk.")
    text = st.text_area("Complaint text", height=140, placeholder="Paid 2000 to my brother yesterday, debited, he didn't get it")
    c1, c2 = st.columns(2)
    ref = c1.text_input("UTR or transaction id (optional)", placeholder="TXN000061")
    fail = c2.selectbox("Simulate status-check failure", ["none", "timeout-once", "timeout", "error"])
    if st.button("Open case", type="primary", disabled=not text.strip()):
        body = {"complaint_text": text}
        if ref.strip().upper().startswith("TXN"):
            body["txn_id"] = ref.strip().upper()
        elif ref.strip():
            body["utr"] = ref.strip()
        headers = {} if fail == "none" else {"X-Mock-Fail": fail}
        with st.spinner("Checking payment status and reading the complaint..."):
            ok, res = call("POST", "/v1/cases", json=body, headers=headers)
        if ok:
            open_case(res["case_id"])
            st.rerun()
        st.error(res)


{"Queue": queue_screen, "Case": case_screen, "New case": new_case_screen}[page]()
