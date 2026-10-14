from datetime import UTC, datetime

from tape import db
from tape.dashboard import build, write
from tests.conftest import make_filing, make_score

NOW = datetime(2026, 10, 13, 22, 0, tzinfo=UTC)
REPORT = {
    "n": 1200, "gap_ratio": 2.4, "random_gap_ratio": 1.0, "spearman": 0.21, "random_spearman": 0.0,
    "rule_flagged": 300, "rule_lift": 1.5, "jev_lift_same_budget": 2.1,
    "by_score": {"1": {"n": 500, "mean_abs_ar": 0.012}, "3": {"n": 400, "mean_abs_ar": 0.02},
                 "5": {"n": 30, "mean_abs_ar": 0.061}},
}


def _seed(conn):
    f = make_filing(1, company="Example <Corp>", accepted_at="2026-10-13T16:31:02-04:00")
    db.upsert_filing(conn, f)
    db.upsert_score(conn, make_score(f.accession_no, 4.6, cost_usd=0.0001))
    db.set_route(conn, f.accession_no, "now")
    old = make_filing(2, accepted_at="2026-10-01T10:00:00-04:00")
    db.upsert_filing(conn, old)
    db.upsert_score(conn, make_score(old.accession_no, 4.9))


def test_build_with_report(conn):
    _seed(conn)
    html = build(conn, REPORT, NOW)
    assert "Example &lt;Corp&gt;" in html  # escaped
    assert html.count("<tr><td class=\"num\">") == 1  # the old filing is outside the 24h window
    assert "2.40x" in html and "random 1.00x" in html
    assert html.count('class="bar"') == 3  # buckets 2 and 4 are absent
    assert "6.1%" in html  # bucket 5 label
    assert "Table view" in html


def test_build_without_report_or_data(conn):
    html = build(conn, None, NOW)
    assert "No evaluation yet" in html and "Nothing scored" in html


def test_write(conn, tmp_path):
    _seed(conn)
    out = write(conn, tmp_path / "d" / "dash.html", tmp_path / "missing.json")
    assert out.read_text().startswith("<!doctype html>")
