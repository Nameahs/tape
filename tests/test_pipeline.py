import threading
from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from tape import db, pipeline
from tape.config import Settings, Thresholds
from tape.edgar import FEED_URL, TICKERS_URL, Edgar, EdgarBlockedError, FilingRef
from tape.pipeline import PassStats, backfill, rescore_routes, run_pass, score_and_route, today_et
from tests.conftest import make_filing
from tests.fakes import FakeJev
from tests.test_edgar import FIX, INDEX, _index_without_documents, _mock_filing


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
def test_reroute_uses_new_thresholds(conn):
    respx.get(FEED_URL).respond(200, text=(FIX / "feed.atom").read_text())
    _mock_filing(respx)
    run_pass(conn, Edgar("ua", per_second=1000), FakeJev(), Settings())
    strict = Settings(thresholds=Thresholds(brief_now=4.9, brief_now_with_surprise=4.9, digest=4.0))
    assert rescore_routes(conn, strict) == {"digest": 1}


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
    assert all(r["finished_at"] and r["error"] is None for r in runs)


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
    assert run["error"].startswith("EdgarBlockedError:") and run["scored"] == 1


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
    assert run["error"].startswith("EdgarBlockedError:") and run["scored"] == 1
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
