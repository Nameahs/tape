import sqlite3
import threading
from datetime import UTC, date, datetime

import httpx
import pytest
import respx
from typesafe_sdk import TypeSafeAPIError, TypeSafeAPITimeoutError

from tape import db, pipeline
from tape.config import Settings, Thresholds
from tape.edgar import FEED_URL, TICKERS_URL, Edgar, EdgarBlockedError, FeedPage, FilingRef
from tape.pipeline import PassStats, backfill, feed_gap, rescore_routes, run_pass, score_and_route, today_et
from tests.conftest import make_filing, make_score
from tests.fakes import FakeJev
from tests.test_edgar import FIX, INDEX, MAINTENANCE_PAGE, _index_with_accepted, _index_without_documents, _mock_filing


@respx.mock
def test_run_pass_end_to_end_and_rerun(conn):
    respx.get(FEED_URL).respond(200, text=(FIX / "feed.atom").read_text())
    _mock_filing(respx)
    edgar, jev, settings = Edgar("ua", per_second=1000), FakeJev(), Settings()

    stats = run_pass(conn, edgar, jev, settings)
    assert (stats.discovered, stats.fetched, stats.scored) == (1, 1, 1)
    assert stats.routes["now"] == 1  # CEO resignation fixture
    assert db.pending_briefs(conn)[0]["ticker"] == "EXMP"

    again = run_pass(conn, edgar, jev, settings)
    assert (again.fetched, again.scored) == (0, 0)
    assert len(jev.calls) == 1


@respx.mock
def test_backfill_skips_weekends(conn):
    idx = "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.{}.idx"
    weekday = respx.get(idx.format("20261009")).respond(200, text=(FIX / "form.idx").read_text())
    _mock_filing(respx)
    # end=Mon Oct 12 with days=3 covers Fri 9, Sat 10, Sun 11
    stats = backfill(conn, Edgar("ua", per_second=1000), FakeJev(), Settings(), days=3, end=date(2026, 10, 12))
    assert weekday.called and stats.scored == 1


IDX = "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.{}.idx"


@respx.mock
@pytest.mark.parametrize("status", [404, 403])
def test_backfill_counts_a_weekday_with_no_index(conn, caplog, status):
    respx.get(IDX.format("20261014")).respond(status, text="<html><body>Forbidden</body></html>")
    with caplog.at_level("WARNING", logger="tape.pipeline"):
        stats = backfill(conn, Edgar("ua", per_second=1000), FakeJev(), Settings(), days=1, end=date(2026, 10, 15))
    assert stats.missing_days == 1 and "missing_days=1" in stats.summary()
    assert "no daily index for 2026-10-14" in caplog.text
    assert _runs(conn)[0]["missing_days"] == 1


@respx.mock
def test_backfill_keeps_going_after_a_weekday_with_no_index(conn):
    respx.get(IDX.format("20261013")).respond(404)
    respx.get(IDX.format("20261014")).respond(200, text=(FIX / "form.idx").read_text())
    _mock_filing(respx)
    stats = backfill(conn, Edgar("ua", per_second=1000), FakeJev(), Settings(), days=2, end=date(2026, 10, 15))
    assert stats.missing_days == 1 and stats.scored == 1


@respx.mock
def test_backfill_does_not_count_a_weekend_or_a_quiet_day(conn, caplog):
    respx.get(IDX.format("20261009")).respond(200, text="")  # an index with no 8-Ks in it
    # end=Mon Oct 12 with days=3 covers Fri 9, Sat 10, Sun 11; the weekend is never requested.
    with caplog.at_level("WARNING", logger="tape.pipeline"):
        stats = backfill(conn, Edgar("ua", per_second=1000), FakeJev(), Settings(), days=3, end=date(2026, 10, 12))
    assert stats.missing_days == 0 and "missing_days" not in stats.summary()
    assert "no daily index" not in caplog.text


def test_today_et_lags_utc_in_the_evening():
    # 9:30pm EDT on Wed Oct 21 is already Thu Oct 22 in UTC.
    assert today_et(datetime(2026, 10, 22, 1, 30, tzinfo=UTC)) == date(2026, 10, 21)
    assert today_et(datetime(2026, 10, 22, 4, 30, tzinfo=UTC)) == date(2026, 10, 22)


@respx.mock
def test_backfill_defaults_to_eastern_today(conn, monkeypatch):
    idx = "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.{}.idx"
    tuesday = respx.get(idx.format("20261020")).respond(200, text=(FIX / "form.idx").read_text())
    _mock_filing(respx)
    monkeypatch.setattr(pipeline, "today_et", lambda: date(2026, 10, 21))
    stats = backfill(conn, Edgar("ua", per_second=1000), FakeJev(), Settings(), days=1)
    assert tuesday.called and stats.scored == 1


@respx.mock
def test_backfill_fills_in_tickers_when_the_map_comes_back(conn, monkeypatch):
    monkeypatch.setattr("tape.edgar.time.sleep", lambda s: None)
    monkeypatch.setattr("tape.edgar.TICKERS_RETRY_AFTER", 0.0)
    idx = "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.20261013.idx"
    respx.get(idx).respond(200, text=(FIX / "form.idx").read_text())
    _mock_filing(respx)
    ok = httpx.Response(200, text=(FIX / "company_tickers.json").read_text())
    respx.get(TICKERS_URL).side_effect = [httpx.Response(503)] * 4 + [ok]

    # Fixed dates, so the filing is well outside the week a pass looks back whenever this runs.
    backfill(conn, Edgar("ua", per_second=1000), FakeJev(), Settings(), days=1, end=date(2026, 10, 14))

    assert conn.execute("SELECT ticker FROM filings").fetchone()[0] == "EXMP"


@respx.mock
def test_rerunning_a_backfill_repairs_tickers_in_its_span(conn):
    # Tuesday's index lists Friday evening's filing when Monday is a holiday, so the window reaches it.
    db.upsert_filing(conn, make_filing(1, cik="0000000001", ticker=None, accepted_at="2026-10-09T18:00:00-04:00"))
    db.upsert_filing(conn, make_filing(2, cik="0000000001", ticker=None, accepted_at="2026-10-08T18:00:00-04:00"))
    idx = "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.20261013.idx"
    respx.get(idx).respond(200, text=(FIX / "form.idx").read_text().replace("26-000101", "26-000001"))
    respx.get(TICKERS_URL).respond(200, text=(FIX / "company_tickers.json").read_text())

    backfill(conn, Edgar("ua", per_second=1000), FakeJev(), Settings(), days=1, end=date(2026, 10, 14))

    tickers = dict(conn.execute("SELECT accession_no, ticker FROM filings").fetchall())
    assert tickers["0000000001-26-000001"] == "EXMP"
    assert tickers["0000000001-26-000002"] is None  # before the backfill's span


@respx.mock
def test_blocked_backfill_skips_the_ticker_fill(conn):
    db.upsert_filing(conn, make_filing(1, cik="0000000001", ticker=None, accepted_at="2026-10-13T09:00:00-04:00"))
    idx = "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.20261013.idx"
    respx.get(idx).respond(200, text=(FIX / "form.idx").read_text())
    respx.get(INDEX).respond(403, text=BLOCK_PAGE)
    tickers = respx.get(TICKERS_URL).respond(200, json={})

    with pytest.raises(EdgarBlockedError):
        backfill(conn, Edgar("ua", per_second=1000), FakeJev(), Settings(), days=1, end=date(2026, 10, 14))

    assert not tickers.called


@respx.mock
def test_reroute_uses_new_thresholds(conn):
    respx.get(FEED_URL).respond(200, text=(FIX / "feed.atom").read_text())
    _mock_filing(respx)
    run_pass(conn, Edgar("ua", per_second=1000), FakeJev(), Settings())
    strict = Settings(thresholds=Thresholds(brief_now=4.9, brief_now_with_surprise=4.9, digest=4.0))
    report = rescore_routes(conn, strict)
    assert report.totals == {"digest": 1}
    assert report.moves == {("now", "digest"): 1}


def _seed_routes(conn, scores_and_routes):
    for n, (materiality, old) in enumerate(scores_and_routes, start=1):
        f = make_filing(n)
        db.upsert_filing(conn, f)
        db.upsert_score(conn, make_score(f.accession_no, materiality, surprise=0.0))
        if old is not None:
            db.set_route(conn, f.accession_no, old)
    conn.commit()


def _routes(conn):
    return dict(conn.execute("SELECT accession_no, route FROM briefs").fetchall())


def test_reroute_reports_moves_between_routes(conn):
    # Default cutoffs: >= 4.0 now, >= 2.75 digest. Raising brief_now to 4.5 moves only the 4.2.
    _seed_routes(conn, [(4.2, "now"), (4.6, "now"), (3.0, "digest"), (1.0, "log"), (4.7, None)])
    tuned = Settings(thresholds=Thresholds(brief_now=4.5))

    report = rescore_routes(conn, tuned)

    assert report.totals == {"now": 2, "digest": 2, "log": 1}
    assert report.moves == {("now", "digest"): 1, (None, "now"): 1}
    assert report.changed == 2
    assert _routes(conn)["0000000001-26-000001"] == "digest"
    assert _routes(conn)["0000000001-26-000005"] == "now"


def test_reroute_with_unchanged_thresholds_moves_nothing(conn):
    _seed_routes(conn, [(4.2, "now"), (3.0, "digest"), (1.0, "log")])

    report = rescore_routes(conn, Settings())

    assert report.changed == 0
    assert report.totals == {"now": 1, "digest": 1, "log": 1}


def test_reroute_dry_run_leaves_briefs_untouched(conn):
    _seed_routes(conn, [(4.2, "now"), (3.0, "digest"), (4.7, None)])
    before = _routes(conn)
    tuned = Settings(thresholds=Thresholds(brief_now=4.5))

    report = rescore_routes(conn, tuned, dry_run=True)

    assert report.moves == {("now", "digest"): 1, (None, "now"): 1}
    assert _routes(conn) == before
    assert not conn.in_transaction


@respx.mock
def test_empty_filing_is_skipped_and_retried_later(conn):
    respx.get(FEED_URL).respond(200, text=(FIX / "feed.atom").read_text())
    _mock_filing(respx)
    index = respx.get(INDEX)
    index.respond(200, text=_index_without_documents())
    edgar, jev, settings = Edgar("ua", per_second=1000), FakeJev(), Settings()

    stats = run_pass(conn, edgar, jev, settings)
    assert (stats.fetched, stats.fetch_errors, stats.scored) == (0, 1, 0)
    assert jev.calls == []
    assert conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 0

    # Once EDGAR serves the full index page, the next pass picks the filing up.
    index.respond(200, text=(FIX / "index.htm").read_text())
    again = run_pass(conn, edgar, jev, settings)
    assert (again.fetched, again.scored) == (1, 1)
    assert conn.execute("SELECT COUNT(*) FROM fetch_failures").fetchone()[0] == 0


@respx.mock
def test_empty_filing_is_fetched_at_most_max_attempts_times(conn, caplog):
    respx.get(FEED_URL).respond(200, text=(FIX / "feed.atom").read_text())
    _mock_filing(respx)
    index = respx.get(INDEX)
    index.respond(200, text=_index_without_documents())
    edgar, jev, settings = Edgar("ua", per_second=1000), FakeJev(), Settings()

    passes = [run_pass(conn, edgar, jev, settings) for _ in range(db.MAX_FETCH_ATTEMPTS + 2)]

    assert index.call_count == db.MAX_FETCH_ATTEMPTS
    assert [p.fetch_errors for p in passes] == [1] * db.MAX_FETCH_ATTEMPTS + [0, 0]
    assert all(p.fetched == 0 and p.discovered == 1 for p in passes)
    [row] = db.fetch_given_up(conn)
    assert row["last_error"].startswith("EmptyFilingError: no text found")
    assert "giving up on fetching" in caplog.text


@respx.mock
def test_http_error_on_fetch_counts_as_a_failed_attempt(conn):
    respx.get(FEED_URL).respond(200, text=(FIX / "feed.atom").read_text())
    _mock_filing(respx)
    respx.get(INDEX).respond(404)
    stats = run_pass(conn, Edgar("ua", per_second=1000), FakeJev(), Settings())
    assert stats.fetch_errors == 1
    (attempts, error), = conn.execute("SELECT attempts, last_error FROM fetch_failures").fetchall()
    assert attempts == 1 and error.startswith("HTTPStatusError")


BLOCK_PAGE = "<html><body>Request Rate Threshold Exceeded</body></html>"


def _refs(n):
    return [FilingRef(f"0000000009-26-00000{i}", "0000000009", "Blocked Co", "8-K",
                      f"https://www.sec.gov/Archives/edgar/data/9/{i}/index.htm") for i in range(1, n + 1)]


@respx.mock
def test_ingest_stops_at_an_edgar_block_without_recording_failures(conn, caplog):
    first, second, third = _refs(3)
    respx.get(first.index_url).respond(404)
    blocked = respx.get(second.index_url).respond(403, text=BLOCK_PAGE)
    untouched = respx.get(third.index_url).respond(200, text=(FIX / "index.htm").read_text())
    stats = PassStats()

    pipeline.ingest(conn, Edgar("ua", per_second=1000), [first, second, third], Settings(), stats)

    assert blocked.call_count == 1 and not untouched.called
    assert stats.blocked and stats.fetch_errors == 1  # the 404 still counts; the block does not
    assert [r["accession_no"] for r in conn.execute("SELECT accession_no FROM fetch_failures")] == [
        first.accession_no]
    assert caplog.text.count("EDGAR is blocking requests") == 1
    assert "blocked" in stats.summary()


@respx.mock
def test_filings_seen_during_a_block_are_fetched_on_the_next_pass(conn):
    respx.get(FEED_URL).respond(200, text=(FIX / "feed.atom").read_text())
    _mock_filing(respx)
    index = respx.get(INDEX)
    edgar, jev, settings = Edgar("ua", per_second=1000), FakeJev(), Settings()

    index.respond(403, text=BLOCK_PAGE)
    for _ in range(db.MAX_FETCH_ATTEMPTS + 1):
        stats = run_pass(conn, edgar, jev, settings)
        assert (stats.fetched, stats.fetch_errors, stats.blocked) == (0, 0, True)
    assert db.fetch_given_up(conn) == []

    index.respond(200, text=(FIX / "index.htm").read_text())
    again = run_pass(conn, edgar, jev, settings)
    assert (again.fetched, again.scored, again.blocked) == (1, 1, False)


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr("tape.edgar.time.sleep", lambda _: None)


@respx.mock
@pytest.mark.parametrize("failure", [
    {"return_value": httpx.Response(503)},
    {"return_value": httpx.Response(429)},
    {"side_effect": httpx.ReadTimeout("slow")},
], ids=["503", "429", "timeout"])
def test_filings_seen_during_an_outage_are_fetched_after_it(conn, no_backoff, failure):
    respx.get(FEED_URL).respond(200, text=(FIX / "feed.atom").read_text())
    _mock_filing(respx)
    index = respx.get(INDEX)
    edgar, jev, settings = Edgar("ua", per_second=1000), FakeJev(), Settings()

    index.mock(**failure)
    for _ in range(db.MAX_FETCH_ATTEMPTS + 1):
        stats = run_pass(conn, edgar, jev, settings)
        assert (stats.fetched, stats.fetch_errors, stats.outage) == (0, 1, True)
        assert stats.summary().endswith(" outage")
    assert conn.execute("SELECT COUNT(*) FROM fetch_failures").fetchone()[0] == 0

    index.mock(return_value=httpx.Response(200, text=(FIX / "index.htm").read_text()))
    again = run_pass(conn, edgar, jev, settings)
    assert (again.fetched, again.scored, again.outage) == (1, 1, False)


@respx.mock
def test_ingest_stops_after_three_outage_failures_in_a_row(conn, no_backoff):
    refs = _refs(4)
    routes = [respx.get(ref.index_url).respond(503) for ref in refs]
    stats = PassStats()

    stopped = pipeline.ingest(conn, Edgar("ua", per_second=1000), refs, Settings(), stats)

    assert isinstance(stopped, httpx.HTTPStatusError)
    assert stats.fetch_errors == pipeline.FETCH_OUTAGE_STOP_AFTER == 3
    assert routes[2].called and not routes[3].called


@respx.mock
def test_a_maintenance_page_served_with_200_does_not_count_against_filings(conn, caplog):
    respx.get(FEED_URL).respond(200, text=(FIX / "feed.atom").read_text())
    _mock_filing(respx)
    index = respx.get(INDEX)
    edgar, jev, settings = Edgar("ua", per_second=1000), FakeJev(), Settings()

    index.respond(200, text=MAINTENANCE_PAGE)
    for _ in range(db.MAX_FETCH_ATTEMPTS + 1):
        stats = run_pass(conn, edgar, jev, settings)
        assert (stats.fetched, stats.fetch_errors, stats.outage) == (0, 1, True)
    assert conn.execute("SELECT COUNT(*) FROM fetch_failures").fetchone()[0] == 0
    assert "Traceback" not in caplog.text and "KeyError" not in caplog.text

    index.respond(200, text=(FIX / "index.htm").read_text())
    again = run_pass(conn, edgar, jev, settings)
    assert (again.fetched, again.scored, again.outage) == (1, 1, False)


@respx.mock
def test_an_index_page_missing_its_accepted_time_is_the_filings_failure(conn, caplog):
    respx.get(FEED_URL).respond(200, text=(FIX / "feed.atom").read_text())
    _mock_filing(respx)
    respx.get(INDEX).respond(200, text=_index_with_accepted(""))

    stats = run_pass(conn, Edgar("ua", per_second=1000), FakeJev(), Settings())

    assert (stats.fetched, stats.fetch_errors, stats.outage) == (0, 1, False)
    [row] = conn.execute("SELECT last_error FROM fetch_failures").fetchall()
    assert row["last_error"] == f"IndexPageError: {INDEX} has no Accepted time"
    assert "Traceback" not in caplog.text


@respx.mock
def test_one_filing_edgar_cannot_serve_does_not_hold_back_the_rest(conn, no_backoff):
    # A lone 5xx may be about that filing's URL. The feed lists newest first, so stopping at once would
    # starve the older filings behind it until it scrolled off.
    bad, missing = _refs(2)
    respx.get(bad.index_url).respond(500)
    respx.get(missing.index_url).respond(404)
    stats = PassStats()

    stopped = pipeline.ingest(conn, Edgar("ua", per_second=1000), [bad, missing], Settings(), stats)

    assert stopped is None and stats.outage and stats.fetch_errors == 2
    # The 404 is about the filing, so it still counts towards giving up.
    assert [r["accession_no"] for r in conn.execute("SELECT accession_no FROM fetch_failures")] == [
        missing.accession_no]


@respx.mock
def test_outage_on_filing_pages_still_scores_stored_filings_and_skips_the_ticker_fill(conn, no_backoff, monkeypatch):
    with db.transaction(conn):
        db.upsert_filing(conn, make_filing(1))
    refs = _refs(3)
    for ref in refs:
        respx.get(ref.index_url).respond(503)
    tickers = respx.get(TICKERS_URL).respond(200, json={})
    edgar = Edgar("ua", per_second=1000)
    monkeypatch.setattr(edgar, "latest_8k", lambda: FeedPage(refs, False))

    stats = run_pass(conn, edgar, FakeJev(), Settings())

    assert stats.outage and stats.scored == 1 and not tickers.called
    error, outage = conn.execute("SELECT error, outage FROM runs").fetchone()
    assert error is None and outage == 1


@respx.mock
def test_backfill_stopped_by_an_outage_scores_what_it_has_then_fails(conn, no_backoff):
    idx = "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.{}.idx"
    refs = _refs(3)
    lines = [f"8-K  Blocked Co  9  20261009  edgar/data/9/{r.accession_no}.txt" for r in refs]
    respx.get(idx.format("20261009")).respond(200, text="rule\n---\n" + "\n".join(lines))
    for ref in refs:
        folder = ref.accession_no.replace("-", "")
        respx.get(f"https://www.sec.gov/Archives/edgar/data/9/{folder}/{ref.accession_no}-index.htm").respond(503)
    next_day = respx.get(idx.format("20261012"))
    with db.transaction(conn):
        db.upsert_filing(conn, make_filing(1))

    with pytest.raises(httpx.HTTPStatusError):
        backfill(conn, Edgar("ua", per_second=1000), FakeJev(), Settings(), days=4, end=date(2026, 10, 13))

    assert not next_day.called
    assert conn.execute("SELECT COUNT(*) FROM fetch_failures").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 1


def _store_recent_without_ticker(conn):
    accepted = datetime.now(UTC).replace(microsecond=0).isoformat()
    with db.transaction(conn):
        db.upsert_filing(conn, make_filing(1, cik="0000000001", ticker=None, accepted_at=accepted))


@respx.mock
def test_blocked_feed_still_scores_stored_filings(conn, caplog):
    # Stored on an earlier pass, but Jev timed out before it was scored; no ticker yet either.
    _store_recent_without_ticker(conn)
    feed = respx.get(FEED_URL).respond(403, text=BLOCK_PAGE)
    tickers = respx.get(TICKERS_URL).respond(200, json={})
    jev = FakeJev()

    stats = run_pass(conn, Edgar("ua", per_second=1000), jev, Settings())

    assert feed.call_count == 1
    assert not tickers.called  # the ticker backfill would only add to the block
    assert stats.blocked and (stats.discovered, stats.scored) == (0, 1)
    assert len(jev.calls) == 1
    assert caplog.text.count("EDGAR is blocking requests") == 1
    [run] = _runs(conn)
    assert run["error"] is None and run["scored"] == 1 and run["blocked"] == 1


@pytest.mark.parametrize(
    "failure",
    [httpx.Response(503), httpx.ConnectTimeout("timed out")],
    ids=["503", "timeout"],
)
@respx.mock
def test_failed_feed_still_scores_stored_filings(conn, monkeypatch, failure):
    monkeypatch.setattr("tape.edgar.time.sleep", lambda _s: None)
    # Stored on an earlier pass, but Jev timed out before it was scored; no ticker yet either.
    _store_recent_without_ticker(conn)
    respx.get(FEED_URL).mock(side_effect=[failure] * 4)
    tickers = respx.get(TICKERS_URL).respond(200, json={})
    jev = FakeJev()

    with pytest.raises(httpx.HTTPError):
        run_pass(conn, Edgar("ua", per_second=1000), jev, Settings())

    assert not tickers.called  # another EDGAR download, with its own retries
    assert len(jev.calls) == 1
    assert db.unscored(conn) == []
    [run] = _runs(conn)
    assert run["scored"] == 1 and run["cost_usd"] > 0 and not run["blocked"]
    assert run["error"].startswith(("HTTPStatusError:", "ConnectTimeout:"))


@respx.mock
def test_block_mid_ingest_skips_the_ticker_backfill(conn):
    respx.get(FEED_URL).respond(200, text=(FIX / "feed.atom").read_text())
    respx.get(INDEX).respond(403, text=BLOCK_PAGE)
    tickers = respx.get(TICKERS_URL).respond(200, json={})
    _store_recent_without_ticker(conn)
    stats = run_pass(conn, Edgar("ua", per_second=1000), FakeJev(), Settings())
    assert stats.blocked and not tickers.called
    assert stats.scored == 1


def _runs(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id")]


@respx.mock
def test_each_pass_is_recorded(conn):
    respx.get(FEED_URL).respond(200, text=(FIX / "feed.atom").read_text())
    _mock_filing(respx)
    edgar, settings = Edgar("ua", per_second=1000), Settings()
    first = run_pass(conn, edgar, FakeJev(), settings)
    run_pass(conn, edgar, FakeJev(), settings)

    runs = _runs(conn)
    assert [(r["kind"], r["discovered"], r["fetched"], r["scored"], r["routed_now"]) for r in runs] == [
        ("pass", 1, 1, 1, 1), ("pass", 1, 0, 0, 0),
    ]
    assert runs[0]["cost_usd"] == pytest.approx(first.cost_usd)
    assert all(r["finished_at"] and r["error"] is None and not r["blocked"] for r in runs)


@respx.mock
def test_backfill_is_one_run(conn):
    idx = "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.{}.idx"
    respx.get(idx.format("20261009")).respond(200, text=(FIX / "form.idx").read_text())
    _mock_filing(respx)
    backfill(conn, Edgar("ua", per_second=1000), FakeJev(), Settings(), days=3, end=date(2026, 10, 12))
    assert [(r["kind"], r["scored"]) for r in _runs(conn)] == [("backfill", 1)]


@respx.mock
def test_backfill_blocked_by_edgar_fails_and_records_why(conn):
    idx = "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.{}.idx"
    respx.get(idx.format("20261008")).respond(200, text=(FIX / "form.idx").read_text())
    _mock_filing(respx)
    later = respx.get(idx.format("20261009")).respond(
        403, text="<html><body>Request Rate Threshold Exceeded</body></html>")
    with pytest.raises(EdgarBlockedError):
        backfill(conn, Edgar("ua", per_second=1000), FakeJev(), Settings(), days=4, end=date(2026, 10, 12))
    assert later.called
    (run,) = _runs(conn)
    assert run["error"].startswith("EdgarBlockedError:") and run["scored"] == 1 and run["blocked"] == 1


@respx.mock
def test_backfill_blocked_mid_day_scores_what_it_has_then_fails(conn):
    idx = "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.{}.idx"
    form_idx = (FIX / "form.idx").read_text()
    respx.get(idx.format("20261008")).respond(200, text=form_idx)
    _mock_filing(respx)
    # The 9th lists a new filing whose index page is blocked.
    respx.get(idx.format("20261009")).respond(200, text=form_idx.replace("26-000101", "26-000102"))
    blocked_index = respx.get(
        "https://www.sec.gov/Archives/edgar/data/1/000000000126000102/0000000001-26-000102-index.htm"
    ).respond(403, text=BLOCK_PAGE)
    next_day = respx.get(idx.format("20261012"))

    with pytest.raises(EdgarBlockedError):
        backfill(conn, Edgar("ua", per_second=1000), FakeJev(), Settings(), days=5, end=date(2026, 10, 13))

    assert blocked_index.call_count == 1
    assert not next_day.called  # no more requests once blocked
    (run,) = _runs(conn)
    assert run["error"].startswith("EdgarBlockedError:") and run["scored"] == 1 and run["blocked"] == 1
    assert conn.execute("SELECT COUNT(*) FROM fetch_failures").fetchone()[0] == 0


def test_failed_pass_records_error_and_reraises(conn):
    class DownEdgar:
        def latest_8k(self):
            raise ConnectionError("EDGAR unreachable")

    with pytest.raises(ConnectionError):
        run_pass(conn, DownEdgar(), FakeJev(), Settings())
    (run,) = _runs(conn)
    assert run["finished_at"] and run["error"] == "ConnectionError: EDGAR unreachable"


def test_interrupted_scoring_keeps_scores_already_returned(conn, monkeypatch):
    """A kill partway through a batch must not throw away (and re-bill) filings Jev already scored."""
    stored = threading.Event()
    upsert_score = db.upsert_score

    def upsert_and_signal(c, s):
        upsert_score(c, s)
        stored.set()

    monkeypatch.setattr(db, "upsert_score", upsert_and_signal)

    class Interrupted(FakeJev):
        def system_one(self, state, questions, *, model=None):
            if "interrupt" in state["text"]:
                stored.wait(timeout=2)
                raise KeyboardInterrupt
            return super().system_one(state, questions, model=model)

    first, second = make_filing(1, clean_text="fine"), make_filing(2, clean_text="interrupt here")
    for f in (first, second):
        db.upsert_filing(conn, f)
    conn.commit()

    with pytest.raises(KeyboardInterrupt):
        score_and_route(conn, Interrupted(), Settings(), PassStats())
    assert [r["accession_no"] for r in conn.execute("SELECT accession_no FROM scores")] == [first.accession_no]
    assert [r["accession_no"] for r in db.unscored(conn)] == [second.accession_no]


def test_score_and_route_stops_retrying_a_filing_jev_keeps_rejecting(conn, caplog):
    stuck, fine = make_filing(1, clean_text="explode every time"), make_filing(2, clean_text="fine")
    for f in (stuck, fine):
        db.upsert_filing(conn, f)
    conn.commit()
    jev = FakeJev(fail_on={"explode"})

    for _ in range(db.MAX_SCORE_ATTEMPTS + 2):
        score_and_route(conn, jev, Settings(), PassStats())

    sent = [c[0]["text"] for c in jev.calls]
    assert sent.count("explode every time") == db.MAX_SCORE_ATTEMPTS
    assert sent.count("fine") == 1
    [row] = db.given_up(conn)
    assert (row["accession_no"], row["last_error"]) == (stuck.accession_no, "RuntimeError: boom")
    assert f"giving up on {stuck.accession_no}" in caplog.text


def _api_error(status):
    return TypeSafeAPIError(status, None, httpx.Headers())


@pytest.mark.parametrize("error", [_api_error(s) for s in (401, 403, 408, 429, 500, 503)]
                         + [TypeSafeAPITimeoutError(60.0)], ids=repr)
def test_a_jev_outage_never_gives_up_on_a_filing(conn, caplog, error):
    f = make_filing(1, clean_text="fine")
    db.upsert_filing(conn, f)
    conn.commit()
    jev = FakeJev(error=error)

    for _ in range(db.MAX_SCORE_ATTEMPTS + 2):
        score_and_route(conn, jev, Settings(), PassStats())

    assert len(jev.calls) == db.MAX_SCORE_ATTEMPTS + 2
    assert db.given_up(conn) == [] and len(db.unscored(conn)) == 1
    attempts, last_error = conn.execute("SELECT attempts, last_error FROM score_failures").fetchone()
    assert attempts == 0 and last_error.startswith(type(error).__name__)
    assert "giving up" not in caplog.text


def test_filing_scores_once_a_jev_outage_ends(conn):
    f = make_filing(1, clean_text="fine")
    db.upsert_filing(conn, f)
    conn.commit()
    jev = FakeJev(error=_api_error(503))
    for _ in range(db.MAX_SCORE_ATTEMPTS + 1):
        score_and_route(conn, jev, Settings(), PassStats())

    jev.error = None
    stats = PassStats()
    score_and_route(conn, jev, Settings(), stats)

    assert stats.scored == 1
    assert conn.execute("SELECT COUNT(*) FROM score_failures").fetchone()[0] == 0


@pytest.mark.parametrize("status", [400, 404, 422])
def test_a_rejected_filing_is_still_given_up_on(conn, status):
    f = make_filing(1, clean_text="fine")
    db.upsert_filing(conn, f)
    conn.commit()
    jev = FakeJev(error=_api_error(status))

    for _ in range(db.MAX_SCORE_ATTEMPTS + 2):
        score_and_route(conn, jev, Settings(), PassStats())

    assert len(jev.calls) == db.MAX_SCORE_ATTEMPTS
    assert [r["accession_no"] for r in db.given_up(conn)] == [f.accession_no]


def test_an_outage_does_not_undo_attempts_a_rejected_filing_has_used(conn):
    f = make_filing(1, clean_text="fine")
    db.upsert_filing(conn, f)
    db.record_score_failure(conn, f.accession_no, "TypeSafeAPIError: 400")
    conn.commit()

    score_and_route(conn, FakeJev(error=_api_error(503)), Settings(), PassStats())

    assert conn.execute("SELECT attempts FROM score_failures").fetchone()[0] == 1


def test_an_outage_logs_one_warning_per_pass_and_no_tracebacks(conn, caplog):
    for n in (1, 2, 3):
        db.upsert_filing(conn, make_filing(n, clean_text="fine"))
    conn.commit()

    score_and_route(conn, FakeJev(error=_api_error(503)), Settings(), PassStats())

    [record] = [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]
    assert "left 3 filing(s) for a later pass" in record.getMessage() and "503" in record.getMessage()
    assert record.exc_info is None


def test_an_outage_leaves_most_of_a_large_backlog_unsent_and_says_so(conn, caplog):
    for n in range(1, 101):
        db.upsert_filing(conn, make_filing(n, clean_text="fine"))
    conn.commit()
    jev = FakeJev(error=_api_error(503))

    score_and_route(conn, jev, Settings(), PassStats())

    assert len(jev.calls) < 15
    [record] = [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]
    assert f"left 100 filing(s) for a later pass, {100 - len(jev.calls)} of them not sent" in record.getMessage()
    assert len(db.unscored(conn)) == 100

    jev.error = None
    stats = PassStats()
    score_and_route(conn, jev, Settings(), stats)

    assert stats.scored == 100
    assert conn.execute("SELECT COUNT(*) FROM score_failures").fetchone()[0] == 0


def test_score_and_route_clears_a_failure_once_the_filing_scores(conn):
    f = make_filing(1, clean_text="fine")
    db.upsert_filing(conn, f)
    db.record_score_failure(conn, f.accession_no, "ReadTimeout: slow")
    conn.commit()

    stats = PassStats()
    score_and_route(conn, FakeJev(), Settings(), stats)
    assert stats.scored == 1
    assert conn.execute("SELECT COUNT(*) FROM score_failures").fetchone()[0] == 0


@respx.mock
def test_filing_stored_without_ticker_is_filled_in_on_a_later_pass(conn, tmp_path, monkeypatch):
    monkeypatch.setattr("tape.edgar.time.sleep", lambda s: None)
    monkeypatch.setattr(pipeline, "TICKER_BACKFILL_DAYS", 36500)  # the fixture filing is from October 2026
    respx.get(FEED_URL).respond(200, text=(FIX / "feed.atom").read_text())
    _mock_filing(respx)
    tickers = respx.get(TICKERS_URL)
    ok = httpx.Response(200, text=(FIX / "company_tickers.json").read_text())
    tickers.side_effect = [httpx.Response(503)] * 4 + [ok]
    jev, settings = FakeJev(), Settings()

    stats = run_pass(conn, Edgar("ua", per_second=1000, cache_dir=tmp_path), jev, settings)
    assert (stats.fetched, stats.fetch_errors) == (1, 0)
    assert conn.execute("SELECT ticker FROM filings").fetchone()[0] is None

    # The next scheduled pass is a new process with a fresh Edgar.
    run_pass(conn, Edgar("ua", per_second=1000, cache_dir=tmp_path), jev, settings)
    assert conn.execute("SELECT ticker FROM filings").fetchone()[0] == "EXMP"


def test_fill_missing_tickers_skips_old_filings(conn):
    class Tickers:
        def ticker_for(self, cik):
            return "EXMP"

    db.upsert_filing(conn, make_filing(1, cik="0000000001", ticker=None, accepted_at="2026-10-20T09:00:00-04:00"))
    db.upsert_filing(conn, make_filing(2, cik="0000000001", ticker=None, accepted_at="2026-09-01T09:00:00-04:00"))
    assert pipeline.fill_missing_tickers(conn, Tickers(), now=datetime(2026, 10, 21, tzinfo=UTC)) == 1
    tickers = dict(conn.execute("SELECT accession_no, ticker FROM filings").fetchall())
    assert tickers == {"0000000001-26-000001": "EXMP", "0000000001-26-000002": None}


def _ref(n: int) -> FilingRef:
    return FilingRef(make_filing(n).accession_no, "0000000001", "Example Corp", "8-K", "https://example.test/i")


@pytest.mark.parametrize(
    ("stored", "refs", "full", "gap"),
    [
        pytest.param([1], [2, 3], True, True, id="full page, nothing stored from it"),
        pytest.param([1], [1, 2], True, False, id="full page reaching a stored filing"),
        pytest.param([1], [2, 3], False, False, id="short page holds everything there is"),
        pytest.param([], [2, 3], True, False, id="first pass on an empty store"),
        pytest.param([1], [], True, False, id="full page of amendments only"),
    ],
)
def test_feed_gap(conn, stored, refs, full, gap):
    for n in stored:
        db.upsert_filing(conn, make_filing(n))
    assert feed_gap(conn, FeedPage([_ref(n) for n in refs], full)) is gap


def test_summary_keeps_blocked_last_after_a_gap():
    assert PassStats(feed_gap=True).summary().endswith("cost=$0.0000 gap")
    assert PassStats(feed_gap=True, blocked=True).summary().endswith(" gap blocked")
    assert " gap" not in PassStats().summary()


def test_run_pass_warns_about_a_feed_gap_and_still_ingests(conn, caplog):
    db.upsert_filing(conn, make_filing(1))
    conn.commit()

    class FullFeed(Edgar):
        def latest_8k(self):
            return FeedPage([_ref(2)], True)

        def fetch(self, ref, max_chars):
            return make_filing(2)

        def ticker_for(self, cik):
            return None

    stats = run_pass(conn, FullFeed("ua"), FakeJev(), Settings())
    assert stats.feed_gap and stats.fetched == 1
    assert "may have been missed" in caplog.text
    assert conn.execute("SELECT feed_gap FROM runs").fetchone()[0] == 1


@respx.mock
def test_backfill_fetches_a_filing_given_up_on_during_an_old_outage(tmp_path):
    path = tmp_path / "v10.db"
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA)
    for script in db.MIGRATIONS[:10]:
        old.executescript(script)
    old.execute("PRAGMA user_version = 10")
    old.execute("INSERT INTO fetch_failures VALUES ('0000000001-26-000101', 3, 'ReadTimeout: timed out', "
                "'2026-10-13T15:00:00Z')")
    old.commit()
    old.close()
    respx.get(IDX.format("20261013")).respond(200, text=(FIX / "form.idx").read_text())
    _mock_filing(respx)

    conn = db.connect(path)
    stats = backfill(conn, Edgar("ua", per_second=1000), FakeJev(), Settings(), days=1, end=date(2026, 10, 14))

    assert (stats.fetched, stats.scored) == (1, 1)
    assert db.fetch_given_up(conn) == []


def test_a_retried_filing_is_scored_by_the_next_pass(conn):
    f = make_filing(1, clean_text="explode every time")
    db.upsert_filing(conn, f)
    conn.commit()
    jev = FakeJev(fail_on={"explode"})
    for _ in range(db.MAX_SCORE_ATTEMPTS):
        score_and_route(conn, jev, Settings(), PassStats())
    assert [r["accession_no"] for r in db.given_up(conn)] == [f.accession_no]

    jev.fail_on = set()  # whatever made Jev reject it has been fixed
    assert db.retry_given_up(conn) == 1
    stats = PassStats()
    score_and_route(conn, jev, Settings(), stats)

    assert stats.scored == 1
    assert conn.execute("SELECT COUNT(*) FROM score_failures").fetchone()[0] == 0


@respx.mock
def test_a_retried_fetch_is_fetched_by_the_next_pass(conn):
    respx.get(FEED_URL).respond(200, text=(FIX / "feed.atom").read_text())
    _mock_filing(respx)
    index = respx.get(INDEX)
    index.respond(200, text=_index_without_documents())
    edgar, jev, settings = Edgar("ua", per_second=1000), FakeJev(), Settings()
    for _ in range(db.MAX_FETCH_ATTEMPTS + 1):
        run_pass(conn, edgar, jev, settings)
    assert len(db.fetch_given_up(conn)) == 1

    index.respond(200, text=(FIX / "index.htm").read_text())  # EDGAR has repaired the filing
    assert db.retry_fetch_given_up(conn) == 1
    stats = run_pass(conn, edgar, jev, settings)

    assert (stats.fetched, stats.scored) == (1, 1)
    assert conn.execute("SELECT COUNT(*) FROM fetch_failures").fetchone()[0] == 0
