from datetime import date

import respx

from tape import db
from tape.config import Settings, Thresholds
from tape.edgar import FEED_URL, Edgar
from tape.pipeline import backfill, rescore_routes, run_pass
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


@respx.mock
def test_reroute_uses_new_thresholds(conn):
    respx.get(FEED_URL).respond(200, text=(FIX / "feed.atom").read_text())
    _mock_filing(respx)
    run_pass(conn, Edgar("ua", per_second=1000), FakeJev(), Settings())
    strict = Settings(thresholds=Thresholds(brief_now=4.9, brief_now_with_surprise=4.9, digest=4.0))
    assert rescore_routes(conn, strict) == {"digest": 1}
