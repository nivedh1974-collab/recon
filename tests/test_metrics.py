import sqlite3

from recon.db import init_db
from recon.metrics import compute, edit_class


def test_edit_class_thresholds():
    assert edit_class("Your refund is due by 6 Oct.", "Your refund is due by 6 Oct.") == "none"
    assert edit_class("Your refund is due by 6 Oct 2026.", "Your refund is due by 6 Oct 2026!") == "minor"
    assert edit_class("Your refund is due by 6 Oct.", "We are checking with your bank, please wait.") == "major"
    assert edit_class(None, "anything") is None


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute("INSERT INTO complaints VALUES ('C1','2026-10-01T09:00','in-app','x',NULL,NULL,NULL)")
    return conn


def _case(conn, cid, deadline, escalate, dtype, draft, sent, opened, done, action="reply_sent"):
    conn.execute(
        "INSERT INTO cases (case_id, complaint_id, dispute_type, deadline_date, escalate, draft_reply, sent_reply, case_source) "
        "VALUES (?, 'C1', ?, ?, ?, ?, ?, 'live')", (cid, dtype, deadline, escalate, draft, sent))
    conn.executemany("INSERT INTO audit_log (case_id, actor, action, logged_at) VALUES (?, 'AG01', ?, ?)",
                     [(cid, "case_opened", opened), (cid, action, done)])


def test_compute_from_logged_events():
    conn = _db()
    _case(conn, "A", "2026-10-02", 0, "P2P failed", "d", "d", "2026-10-01T10:00", "2026-10-01T10:04")
    _case(conn, "B", "2026-10-02", 1, "P2P failed", "draft text", "totally different", "2026-10-03T10:00", "2026-10-03T10:10", "escalated")
    _case(conn, "C", None, 0, "Needs review", None, None, "2026-10-01T10:00", "2026-10-01T10:06")
    m = compute(conn)
    assert m["cases"] == 3 and m["simulated"] is False
    assert m["handle_time_min"]["median"] == 6.0
    assert m["deadlines_met"] == {"share": 0.5, "met": 1, "n": 2}   # B acted after its deadline
    assert m["escalation_rate"]["share"] == 0.333
    assert m["needs_review_rate"]["share"] == 0.333
    assert m["draft_edits"]["none"] == 0.5 and m["draft_edits"]["major"] == 0.5 and m["draft_edits"]["n"] == 2


def test_empty_database_says_no_cases():
    assert compute(_db()) == {"cases": 0}


def test_dashboard_and_metrics_endpoints_agree():
    from fastapi.testclient import TestClient
    from recon import simulate
    from recon.api import app
    simulate.main()
    client = TestClient(app)
    m = client.get("/metrics").json()
    page = client.get("/dashboard").text
    assert m["simulated"] is True and "Simulated data" in page
    assert f"{m['cases']} cases" in page
