from datetime import UTC, datetime

from tape import db
from tape.dashboard import _daily, build, write
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
    assert "90% CI" not in html  # reports written before the interval existed


def test_build_shows_gap_interval(conn):
    html = build(conn, {**REPORT, "gap_ci_low": 1.9, "gap_ci_high": 2.95}, NOW)
    assert "90% CI 1.90-2.95x, random 1.00x" in html
    html = build(conn, {**REPORT, "gap_ci_low": float("nan"), "gap_ci_high": float("nan")}, NOW)
    assert "90% CI" not in html


def test_build_without_report_or_data(conn):
    html = build(conn, None, NOW)
    assert "No evaluation yet" in html and "Nothing scored" in html
    assert "No Jev calls in the last 14 days" in html


def test_write(conn, tmp_path):
    _seed(conn)
    out = write(conn, tmp_path / "d" / "dash.html", tmp_path / "missing.json")
    assert out.read_text().startswith("<!doctype html>")


def _score_at(conn, n, scored_at, cost):
    f = make_filing(n)
    db.upsert_filing(conn, f)
    db.upsert_score(conn, make_score(f.accession_no, cost_usd=cost))
    conn.execute("UPDATE scores SET scored_at = ? WHERE accession_no = ?", (scored_at, f.accession_no))


def test_daily_buckets_by_eastern_day_of_scoring(conn):
    _score_at(conn, 1, "2026-10-13T21:00:00Z", 0.002)  # 17:00 ET on the 13th
    _score_at(conn, 2, "2026-10-13T03:30:00Z", 0.001)  # 23:30 ET on the 12th
    _score_at(conn, 3, "2026-10-13T04:30:00Z", None)   # 00:30 ET on the 13th, no cost recorded
    _score_at(conn, 4, "2026-09-01T12:00:00Z", 0.5)    # outside the window
    daily = _daily(conn, NOW)
    assert len(daily) == 14
    assert daily[0][0].isoformat() == "2026-09-30" and daily[-1][0].isoformat() == "2026-10-13"
    by_day = {d.isoformat(): (n, round(c, 6)) for d, n, c in daily}
    assert by_day["2026-10-13"] == (2, 0.002)
    assert by_day["2026-10-12"] == (1, 0.001)
    assert by_day["2026-10-11"] == (0, 0.0)


def test_build_shows_daily_section(conn):
    _score_at(conn, 1, "2026-10-13T21:00:00Z", 0.0025)
    _score_at(conn, 2, "2026-10-10T15:00:00Z", 0.001)
    html = build(conn, None, NOW)
    assert "Jev volume and cost, last 14 days" in html
    assert "<td>Tue Oct 13</td>" in html and "<td>Wed Oct 07</td>" in html  # empty days are listed
    assert "$0.0035" in html  # window total
