from datetime import date

import pytest

from tape import db
from tape.stats import collect, format_stats
from tests.conftest import make_filing, make_score

TODAY = date(2026, 10, 16)


def _add(conn, n, accepted_at, *, score=None, route=None, cost=None, scored_at="2026-10-16T12:00:00Z"):
    f = make_filing(n, accepted_at=accepted_at)
    db.upsert_filing(conn, f)
    if score is not None:
        db.upsert_score(conn, make_score(f.accession_no, score, cost_usd=cost))
        conn.execute("UPDATE scores SET scored_at = ? WHERE accession_no = ?", (scored_at, f.accession_no))
    if route:
        db.set_route(conn, f.accession_no, route)
    conn.commit()


def test_buckets_by_eastern_day_and_counts_routes(conn):
    _add(conn, 1, "2026-10-16T09:30:00-04:00", score=4.5, route="now", cost=0.01)
    _add(conn, 2, "2026-10-16T10:00:00-04:00", score=2.0, route="log", cost=0.02)
    _add(conn, 3, "2026-10-16T11:00:00-04:00")  # ingested, not scored yet
    # 01:30 UTC on the 16th is still the evening of the 15th in New York.
    _add(conn, 4, "2026-10-16T01:30:00+00:00", score=3.5, route="digest", cost=0.03)

    s = collect(conn, days=2, today=TODAY)

    assert [d.day for d in s.days] == [date(2026, 10, 15), TODAY]
    prev, cur = s.days
    assert (prev.filings, prev.scored, prev.routes["digest"]) == (1, 1, 1)
    assert (cur.filings, cur.scored) == (3, 2)
    assert cur.routes == {"now": 1, "digest": 0, "log": 1}
    assert cur.cost_usd == pytest.approx(0.03)


def test_empty_days_are_listed_and_old_filings_ignored(conn):
    _add(conn, 1, "2026-10-01T10:00:00-04:00", score=3.0, cost=0.5)

    s = collect(conn, days=7, today=TODAY)

    assert len(s.days) == 7
    assert all(d.filings == 0 for d in s.days)


def test_spend_follows_scored_at_not_filing_day(conn):
    # A backfill scores a month-old filing today: it costs money this week even though the filing is old.
    _add(conn, 1, "2026-09-01T10:00:00-04:00", score=3.0, cost=0.5, scored_at="2026-10-16T14:00:00Z")
    _add(conn, 2, "2026-09-02T10:00:00-04:00", score=3.0, cost=0.7, scored_at="2026-09-02T15:00:00Z")

    s = collect(conn, days=7, today=TODAY)

    assert s.calls == 1
    assert s.spend_usd == pytest.approx(0.5)


def test_rejects_non_positive_window(conn):
    with pytest.raises(ValueError):
        collect(conn, days=0, today=TODAY)


def test_format_stats(conn):
    _add(conn, 1, "2026-10-16T09:30:00-04:00", score=4.5, route="now", cost=0.01)

    out = format_stats(collect(conn, days=2, today=TODAY))

    lines = out.splitlines()
    assert lines[0].split() == ["day", "filings", "scored", "now", "digest", "log", "cost"]
    assert lines[2].split() == ["2026-10-16", "1", "1", "1", "0", "0", "$0.01"]
    assert lines[3].split() == ["total", "1", "1"]
    assert lines[-1] == "Jev spend in window: $0.01 over 1 calls"


def test_counts_filings_scoring_gave_up_on(conn):
    _add(conn, 1, "2026-09-01T09:30:00-04:00")  # outside the window: still counted, it needs a look
    _add(conn, 2, "2026-10-16T09:30:00-04:00")
    stuck = make_filing(1).accession_no
    for _ in range(db.MAX_SCORE_ATTEMPTS):
        db.record_score_failure(conn, stuck, "RuntimeError: boom")
    db.record_score_failure(conn, make_filing(2).accession_no, "ReadTimeout: slow")  # still being retried

    s = collect(conn, days=2, today=TODAY)
    assert s.given_up == 1
    assert format_stats(s).splitlines()[-1].startswith("Gave up scoring 1 filing(s) after 3 failed attempts")


def test_format_stats_omits_given_up_line_when_there_are_none(conn):
    assert "Gave up" not in format_stats(collect(conn, days=1, today=TODAY))
