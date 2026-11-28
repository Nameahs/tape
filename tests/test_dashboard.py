import json
from datetime import UTC, datetime

import pytest

from tape import db
from tape.dashboard import DAILY_SQL, SPEND_SQL, _daily, _daily_chart, _recent, build, write
from tape.evaluate import evaluate
from tests.conftest import make_filing, make_score
from tests.test_evaluate import _frame

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


def test_build_lists_score_buckets_in_score_order(conn):
    # Inserted out of order, as a hand-edited report might be.
    report = {**REPORT, "by_score": {"5": _BUCKETS["5"], "1": _BUCKETS["1"], "3": _BUCKETS["3"]}}
    html = build(conn, report, NOW)
    rows = [html.index(f"<tr><td>{k}</td>") for k in ("1", "3", "5")]
    assert rows == sorted(rows)


def test_build_escapes_score_bucket_labels(conn):
    # build() is public and takes the dict as given, without _read_report's checks.
    report = {**REPORT, "by_score": {**REPORT["by_score"], "<i>x</i>": {"n": 1, "mean_abs_ar": 0.01}}}
    html = build(conn, report, NOW)
    assert "<i>x</i>" not in html


def test_build_shows_gap_interval(conn):
    html = build(conn, {**REPORT, "gap_ci_low": 1.9, "gap_ci_high": 2.95}, NOW)
    assert "90% CI 1.90-2.95x, random 1.00x" in html
    html = build(conn, {**REPORT, "gap_ci_low": float("nan"), "gap_ci_high": float("nan")}, NOW)
    assert "90% CI" not in html


UNDEFINED = {"rule_flagged": 0, "rule_lift": None, "jev_lift_same_budget": None, "spearman": None,
             "random_spearman": None, "gap_ci_low": None, "gap_ci_high": None}


def test_build_shows_na_for_undefined_metrics(conn):
    html = build(conn, {**REPORT, **UNDEFINED}, NOW)
    assert "Item-code rule (0 flags): lift n/a." in html
    assert "Jev at the same alert volume: lift n/a." in html
    assert "<b>n/a</b><span class=\"muted\">random n/a</span>" in html  # rank correlation tile
    assert "90% CI" not in html
    assert "nan" not in html


def test_build_shows_na_for_nan_in_older_reports(conn, tmp_path):
    # Reports written before undefined metrics became null hold bare NaN tokens, which json.loads accepts.
    report = tmp_path / "eval.json"
    report.write_text(json.dumps({**REPORT, "rule_flagged": 0, "rule_lift": float("nan"),
                                  "jev_lift_same_budget": float("nan"), "spearman": float("nan")}))
    html = write(conn, tmp_path / "dash.html", report).read_text()
    assert "Item-code rule (0 flags): lift n/a." in html
    assert "Rank correlation" in html and "nanx" not in html


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


_BUCKETS = REPORT["by_score"]


@pytest.mark.parametrize("report, reason", [
    ({k: v for k, v in REPORT.items() if k != "n"}, "'n' missing"),
    ({k: v for k, v in REPORT.items() if k != "by_score"}, "'by_score' missing"),
    ({k: v for k, v in REPORT.items() if k != "rule_flagged"}, "'rule_flagged' missing"),
    ({**REPORT, "n": "1200"}, "'n' missing or not a number"),
    ({**REPORT, "by_score": [1, 2]}, "'by_score' missing or not an object"),
    ({**REPORT, "by_score": {**_BUCKETS, "3": {"n": 2}}}, "score bucket '3'"),
    ({**REPORT, "by_score": {**_BUCKETS, "4": {"n": 2, "mean_abs_ar": None}}}, "score bucket '4'"),
    ({**REPORT, "by_score": {**_BUCKETS, "5": 0.06}}, "score bucket '5'"),
    ({**REPORT, "by_score": {**_BUCKETS, "6": {"n": 2, "mean_abs_ar": 0.01}}}, "score bucket '6' is not one of"),
    ({**REPORT, "by_score": {**_BUCKETS, "0": {"n": 2, "mean_abs_ar": 0.01}}}, "score bucket '0' is not one of"),
    ({**REPORT, "by_score": {"3.0": {"n": 2, "mean_abs_ar": 0.01}}}, "score bucket '3.0' is not one of"),
    ({**REPORT, "by_score": {"<b>4</b>": {"n": 2, "mean_abs_ar": 0.01}}}, "score bucket '<b>4</b>' is not one of"),
    ({**REPORT, "by_event": [1]}, "'by_event' not an object"),
    ({**REPORT, "by_event": {"mna": {"n": 9}}}, "event 'mna'"),
    ({**REPORT, "by_event": {"mna": {"n": 9, "mean_abs_ar": 0.02, "spearman": "0.3"}}}, "event 'mna'"),
])
def test_write_with_malformed_report_leaves_out_evaluation(report, reason, conn, tmp_path, caplog):
    _seed(conn)
    path = tmp_path / "eval.json"
    path.write_text(json.dumps(report))

    html = write(conn, tmp_path / "dash.html", path).read_text()

    assert "No evaluation yet" in html
    assert "Filings scored, 24h" in html  # the rest of the page still renders
    assert str(path) in caplog.text and reason in caplog.text


def test_write_accepts_report_with_only_required_keys(conn, tmp_path):
    path = tmp_path / "eval.json"
    path.write_text(json.dumps({"n": 3, "rule_flagged": 0, "by_score": {"2": {"n": 3, "mean_abs_ar": 0.01}}}))

    html = write(conn, tmp_path / "dash.html", path).read_text()

    assert "3 filings with next-day prices" in html
    assert "Rank correlation" in html and "nan" not in html


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
    by_day = {d.isoformat(): (n, round(c, 6), u) for d, n, c, u in daily}
    assert by_day["2026-10-13"] == (2, 0.002, 1)
    assert by_day["2026-10-12"] == (1, 0.001, 0)
    assert by_day["2026-10-11"] == (0, 0.0, 0)


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


def _tile_value(html, label):
    start = html.index(f'<span class="muted">{label}</span><b>') + len(f'<span class="muted">{label}</span><b>')
    return html[start:html.index("</b>", start)]


def test_24h_tiles_count_backfill_spend_by_when_jev_was_called(conn):
    old = make_filing(1, accepted_at="2020-03-02T09:00:00-05:00")
    db.upsert_filing(conn, old)
    db.upsert_score(conn, make_score(old.accession_no, cost_usd=1.25))
    db.set_route(conn, old.accession_no, "now")
    conn.execute("UPDATE scores SET scored_at = ?", ("2026-10-13T20:00:00Z",))

    html = build(conn, None, NOW)

    assert _tile_value(html, "Filings scored, 24h") == "1"
    assert _tile_value(html, "Jev cost, 24h") == "$1.2500"
    assert "<td>Tue Oct 13</td><td class=\"num\">1</td><td class=\"num\">$1.2500</td>" in html  # agrees with daily
    assert _tile_value(html, "Briefed now") == "0"  # a 2020 filing isn't news


def test_24h_tiles_leave_out_calls_outside_the_window(conn):
    _score_at(conn, 1, "2026-10-12T21:59:00Z", 0.5)   # just over 24 hours before NOW
    _score_at(conn, 2, "2026-10-13T22:30:00Z", 0.25)  # after NOW, e.g. a run still writing
    _score_at(conn, 3, "2026-10-13T12:00:00Z", 0.003)
    html = build(conn, None, NOW)
    assert _tile_value(html, "Filings scored, 24h") == "1"
    assert _tile_value(html, "Jev cost, 24h") == "$0.0030"


def test_briefed_now_tile_counts_recent_filings_routed_now(conn):
    _seed(conn)
    db.set_route(conn, make_filing(2).accession_no, "now")  # accepted Oct 1, outside the window
    assert _tile_value(build(conn, None, NOW), "Briefed now") == "1"


def test_24h_cost_tile_notes_scores_of_unknown_cost(conn):
    _score_at(conn, 1, "2026-10-13T12:00:00Z", 0.003)
    _score_at(conn, 2, "2026-10-13T13:00:00Z", None)
    _score_at(conn, 3, "2026-10-13T14:00:00Z", None)
    _score_at(conn, 4, "2026-10-12T12:00:00Z", None)  # outside the 24h window, still in the daily table
    html = build(conn, None, NOW)
    assert _tile_value(html, "Jev cost, 24h") == "$0.0030"
    assert '<b>$0.0030</b><span class="muted">2 of unknown cost</span>' in html
    assert '<td>Tue Oct 13</td><td class="num">3</td><td class="num">$0.0030 (2 unknown)</td>' in html
    assert '<td>Mon Oct 12</td><td class="num">1</td><td class="num">$0.0000 (1 unknown)</td>' in html
    assert '<th class="num">$0.0030 (3 unknown)</th>' in html  # window total


def test_cost_shows_no_unknown_note_when_every_cost_is_known(conn):
    _score_at(conn, 1, "2026-10-13T12:00:00Z", 0.003)
    _score_at(conn, 2, "2026-10-13T13:00:00Z", 0.0)  # a free call is known, not unknown
    html = build(conn, None, NOW)
    assert "unknown" not in html
    assert '<b>$0.0030</b></div>' in html


def test_spend_query_seeks_on_the_scored_at_index(conn):
    window = ("2026-10-12T22:00:00+00:00", "2026-10-13T22:00:00+00:00")
    plan = " ".join(r["detail"] for r in conn.execute("EXPLAIN QUERY PLAN " + SPEND_SQL, window))
    assert "USING INDEX idx_scores_scored_jd" in plan


def test_recent_table_cuts_score_ties_newest_first(conn):
    # 22 filings above the tie, then six tied at 3.0 inserted out of acceptance order: only three fit
    # under the 25-row limit, and they should be the three newest. Filings 23 and 24 share an
    # acceptance time and fall back to accession number.
    for n in range(1, 23):
        f = make_filing(n, company=f"Co {n}", accepted_at=f"2026-10-13T{n - 1:02d}:00:00+00:00")
        db.upsert_filing(conn, f)
        db.upsert_score(conn, make_score(f.accession_no, 4.5))
    tied = {23: "2026-10-13T12:00:00-04:00", 24: "2026-10-13T12:00:00-04:00", 25: "2026-10-13T08:00:00-04:00",
            26: "2026-10-13T00:30:00+00:00", 27: "2026-10-12T21:00:00-04:00", 28: "2026-10-13T13:00:00-04:00"}
    for n in (25, 24, 27, 28, 26, 23):
        f = make_filing(n, company=f"Co {n}", accepted_at=tied[n])
        db.upsert_filing(conn, f)
        db.upsert_score(conn, make_score(f.accession_no, 3.0))

    rows = _recent(conn, "2026-10-12T22:00:00+00:00")
    assert len(rows) == 25
    assert [r["company"] for r in rows[22:]] == ["Co 28", "Co 23", "Co 24"]
    assert [r["company"] for r in rows[:22]] == [f"Co {n}" for n in range(22, 0, -1)]


BY_EVENT = {
    "other": {"n": 12, "mean_abs_ar": 0.008, "spearman": None},
    "mna": {"n": 42, "mean_abs_ar": 0.0184, "spearman": 0.31},
    "<x>": {"n": 12, "mean_abs_ar": 0.01, "spearman": 0.05},
}


def test_build_shows_results_within_each_event(conn):
    html = build(conn, {**REPORT, "by_event": BY_EVENT}, NOW)
    assert "Within each event type" in html
    assert html.index("<td>mna</td>") < html.index("<td>&lt;x&gt;</td>") < html.index("<td>other</td>")
    mna_row = html[html.index("<td>mna</td>"):].split("</tr>")[0]
    assert "42" in mna_row and "1.84%" in mna_row and "0.31" in mna_row
    other_row = html[html.index("<td>other</td>"):].split("</tr>")[0]
    assert "n/a" in other_row


@pytest.mark.parametrize("by_event", [None, {}])
def test_build_leaves_out_event_table_when_report_has_none(by_event, conn):
    report = REPORT if by_event is None else {**REPORT, "by_event": by_event}
    assert "Within each event type" not in build(conn, report, NOW)


def test_write_shows_event_table_from_a_report_on_disk(conn, tmp_path):
    path = tmp_path / "eval.json"
    path.write_text(json.dumps({**REPORT, "by_event": BY_EVENT}))
    html = write(conn, tmp_path / "dash.html", path).read_text()
    assert "Within each event type" in html and "No evaluation yet" not in html


def test_build_says_means_are_capped(conn):
    html = build(conn, {**REPORT, "abs_ar_cap": 0.142, "capped": 12}, NOW)
    assert "Means cap |abnormal return| at 14.20% (12 filings capped)." in html
    html = build(conn, {**REPORT, "abs_ar_cap": 0.05, "capped": 1}, NOW)
    assert "(1 filing capped)" in html


@pytest.mark.parametrize("extra", [{}, {"abs_ar_cap": None, "capped": 0}, {"abs_ar_cap": "x", "capped": 3}])
def test_build_leaves_out_cap_note_without_a_usable_cap(extra, conn):
    html = build(conn, {**REPORT, **extra}, NOW)
    assert "Means cap" not in html
    assert "n/a" not in html.split("Does a higher score")[1].split("Item-code rule")[0]


def test_write_shows_cap_note_from_a_report_on_disk(conn, tmp_path):
    report_path = tmp_path / "eval.json"
    report_path.write_text(evaluate(_frame(signal=1.0, n=300)).to_json(), encoding="utf-8")
    out = write(conn, tmp_path / "index.html", report_path)
    assert "Means cap |abnormal return| at " in out.read_text(encoding="utf-8")


def test_header_shows_when_the_last_pass_started_in_et(conn):
    db.finish_run(conn, db.start_run(conn, "pass", started_at="2026-10-13T19:55:00+00:00"))
    db.finish_run(conn, db.start_run(conn, "pass", started_at="2026-10-13T20:00:00+00:00"))
    html = build(conn, None, NOW)
    assert "Last pipeline pass: 2026-10-13 16:00 ET." in html


def test_header_ignores_backfills_for_the_last_pass(conn):
    # Backfills are started by hand, so they say nothing about whether the schedule is firing.
    db.finish_run(conn, db.start_run(conn, "pass", started_at="2026-10-13T21:00:00+00:00"))
    db.finish_run(conn, db.start_run(conn, "backfill", started_at="2026-10-13T21:30:00+00:00"))
    assert "Last pipeline pass: 2026-10-13 17:00 ET." in build(conn, None, NOW)


def test_header_warns_when_no_pass_has_run(conn):
    html = build(conn, None, NOW)
    assert '<p class="warn" role="status">No pipeline pass has run yet' in html


def test_recent_pass_is_not_a_warning(conn):
    db.finish_run(conn, db.start_run(conn, "pass", started_at="2026-10-13T20:00:00+00:00"))  # exactly 2h before
    html = build(conn, None, NOW)
    assert '<p class="muted">Last pipeline pass: 2026-10-13 16:00 ET.</p>' in html
    assert 'class="warn"' not in html


def test_header_warns_when_the_last_pass_is_stale(conn):
    db.finish_run(conn, db.start_run(conn, "pass", started_at="2026-10-13T19:59:00+00:00"))
    html = build(conn, None, NOW)
    assert ('<p class="warn" role="status">Last pipeline pass: 2026-10-13 15:59 ET, 2 hours before this page '
            "was built.") in html


def test_stale_warning_counts_days_once_the_gap_is_long(conn):
    db.finish_run(conn, db.start_run(conn, "pass", started_at="2026-10-09T20:00:00+00:00"))
    assert "Last pipeline pass: 2026-10-09 16:00 ET, 4 days before this page was built." in build(conn, None, NOW)
