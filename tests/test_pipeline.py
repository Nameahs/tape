from datetime import UTC, date, datetime

import pytest
import respx

from tape import db, pipeline
from tape.config import Settings, Thresholds
from tape.edgar import FEED_URL, Edgar
from tape.pipeline import backfill, rescore_routes, run_pass, today_et
from tests.fakes import FakeJev
from tests.test_edgar import FIX, _mock_filing


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


def test_failed_pass_records_error_and_reraises(conn):
    class DownEdgar:
        def latest_8k(self):
            raise ConnectionError("EDGAR unreachable")

    with pytest.raises(ConnectionError):
        run_pass(conn, DownEdgar(), FakeJev(), Settings())
    (run,) = _runs(conn)
    assert run["finished_at"] and run["error"] == "ConnectionError: EDGAR unreachable"
