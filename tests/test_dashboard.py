from datetime import UTC, datetime

import pytest

from tape import db
from tape.dashboard import DAILY_SQL, SCORING_SQL, _daily, _daily_chart, build, write
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


@pytest.mark.parametrize("text", ['{"n": 1200, "gap_rat', "", "[]", "null"])
def test_write_with_unusable_report_leaves_out_evaluation(text, conn, tmp_path, caplog):
    _seed(conn)
    report = tmp_path / "eval.json"
    report.write_text(text)

    html = write(conn, tmp_path / "dash.html", report).read_text()

    assert "No evaluation yet" in html
    assert str(report) in caplog.text
    assert report.read_text() == text  # left for someone to look at


def test_failed_write_keeps_previous_dashboard(conn, tmp_path, monkeypatch):
    out = tmp_path / "dash.html"
    out.write_text("old page")

    def fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", fail)
    with pytest.raises(OSError):
        write(conn, out, None)
    assert out.read_text() == "old page"


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


def test_daily_chart(conn):
    _score_at(conn, 1, "2026-10-13T21:00:00Z", 0.0025)
    _score_at(conn, 2, "2026-10-10T15:00:00Z", 0.001)
    _score_at(conn, 3, "2026-10-09T15:00:00Z", None)  # a call with no cost draws no bar
    svg = _daily_chart(_daily(conn, NOW))
    assert svg.count('class="bar"') == 2
    assert "Tue Oct 13: $0.0025, 1 calls" in svg
    assert svg.count('class="val"') == 1 and ">$0.0025</text>" in svg  # only the peak is labeled
    assert ">10-13<" in svg and ">10-12<" not in svg  # alternate day labels, today always shown


def test_daily_chart_empty_when_no_spend(conn):
    _score_at(conn, 1, "2026-10-13T21:00:00Z", None)
    assert _daily_chart(_daily(conn, NOW)) == ""
    html = build(conn, None, NOW)
    assert "Table view" in html and "No Jev calls" not in html  # calls but no recorded cost


def test_daily_query_seeks_on_the_scored_at_index(conn):
    window = ("2026-10-01T00:00:00+00:00",)
    plan = " ".join(r["detail"] for r in conn.execute("EXPLAIN QUERY PLAN " + DAILY_SQL, window))
    assert "USING INDEX idx_scores_scored_jd" in plan


def test_scoring_tiles_include_backfills_not_old_scoring_of_recent_filings(conn):
    old = make_filing(1, company="Backfill", accepted_at="2020-01-01T12:00:00Z")
    recent = make_filing(2, company="Recent", accepted_at="2026-10-13T20:00:00Z")
    for filing, scored_at, cost in (
        (old, "2026-10-13T21:00:00Z", 1.25),
        (recent, "2026-10-12T21:59:59Z", 9.0),
    ):
        db.upsert_filing(conn, filing)
        db.upsert_score(conn, make_score(filing.accession_no, cost_usd=cost))
        conn.execute("UPDATE scores SET scored_at = ? WHERE accession_no = ?", (scored_at, filing.accession_no))
        db.set_route(conn, filing.accession_no, "now")

    html = build(conn, None, NOW)

    assert 'Filings scored, 24h</span><b>1</b>' in html
    assert 'Jev cost, 24h</span><b>$1.2500</b>' in html
    assert '<td>Tue Oct 13</td><td class="num">1</td><td class="num">$1.2500</td>' in html
    assert 'Briefed now</span><b>1</b>' in html
    assert "Backfill" not in html and "Recent" in html


def test_scoring_tiles_query_seeks_on_the_scored_at_index(conn):
    window = ("2026-10-12T22:00:00+00:00",)
    plan = " ".join(r["detail"] for r in conn.execute("EXPLAIN QUERY PLAN " + SCORING_SQL, window))
    assert "USING INDEX idx_scores_scored_jd" in plan
