from datetime import date

import numpy as np
import pandas as pd
import pytest

from tape import db
from tape.evaluate import (
    MIN_OUTCOMES,
    NotEnoughOutcomes,
    bootstrap_gap_ci,
    compute_outcomes,
    evaluate,
    event_day,
    format_report,
    rule_flag,
    spearman,
)
from tests.conftest import make_filing, make_score

# Mon Oct 5 to Mon Oct 12, 2026; Fri Oct 9 traded, then the weekend.
DAYS = [date(2026, 10, d) for d in (5, 6, 7, 8, 9, 12)]
TODAY = date(2026, 10, 14)


@pytest.mark.parametrize(
    ("accepted", "expected"),
    [
        ("2026-10-06T08:15:00-04:00", date(2026, 10, 6)),   # pre-market: same day
        ("2026-10-06T15:59:59-04:00", date(2026, 10, 6)),   # just before the close
        ("2026-10-06T16:00:00-04:00", date(2026, 10, 7)),   # at the close: next day
        ("2026-10-09T17:30:00-04:00", date(2026, 10, 12)),  # Friday evening: Monday
        ("2026-10-10T11:00:00-04:00", date(2026, 10, 12)),  # Saturday: Monday
        ("2026-10-06T19:30:00+00:00", date(2026, 10, 6)),   # UTC input, 3:30 pm ET
        ("2026-10-12T18:00:00-04:00", None),                # no later trading day yet
    ],
)
def test_event_day(accepted, expected):
    assert event_day(accepted, DAYS) == expected


class FakePrices:
    def closes(self, tickers, start, end):
        idx = DAYS
        data = {
            "SPY": [100, 101, 101, 101, 101, 101],    # +1% on Oct 6
            "EXMP": [50, 50, 45, 45, 45, 45],         # -10% on Oct 7
        }
        return pd.DataFrame({t: data[t] for t in tickers if t in data}, index=idx)


def test_compute_outcomes(conn):
    f = make_filing(1, accepted_at="2026-10-06T16:30:00-04:00")  # after close -> Oct 7
    db.upsert_filing(conn, f)
    db.upsert_score(conn, make_score(f.accession_no))
    no_ticker = make_filing(2, ticker=None)
    db.upsert_filing(conn, no_ticker)
    db.upsert_score(conn, make_score(no_ticker.accession_no))
    assert compute_outcomes(conn, FakePrices(), today=TODAY) == 1
    row = conn.execute("SELECT * FROM outcomes").fetchone()
    assert row["event_day"] == "2026-10-07"
    assert row["abnormal_return"] == pytest.approx(-0.10)
    assert compute_outcomes(conn, FakePrices(), today=TODAY) == 0  # idempotent


class RecordingPrices(FakePrices):
    def __init__(self):
        self.calls = []

    def closes(self, tickers, start, end):
        self.calls.append((tickers, start, end))
        return super().closes(tickers, start, end)


def test_compute_outcomes_skips_filings_older_than_lookback(conn):
    # An unpriceable filing from a backfill months ago must not pin the download window.
    stale = make_filing(1, accepted_at="2026-06-01T10:00:00-04:00", ticker="GONE")
    recent = make_filing(2, accepted_at="2026-10-06T16:30:00-04:00")
    for f in (stale, recent):
        db.upsert_filing(conn, f)
        db.upsert_score(conn, make_score(f.accession_no))
    prices = RecordingPrices()
    assert compute_outcomes(conn, prices, lookback_days=30, today=TODAY) == 1
    (tickers, start, _), = prices.calls
    assert "GONE" not in tickers
    assert start == date(2026, 9, 29)  # 7 days before the oldest filing inside the window


def test_compute_outcomes_with_nothing_in_window_downloads_nothing(conn):
    f = make_filing(1, accepted_at="2026-06-01T10:00:00-04:00")
    db.upsert_filing(conn, f)
    db.upsert_score(conn, make_score(f.accession_no))
    prices = RecordingPrices()
    assert compute_outcomes(conn, prices, lookback_days=30, today=TODAY) == 0
    assert prices.calls == []


def test_compute_outcomes_longer_lookback_reaches_old_filings(conn):
    f = make_filing(1, accepted_at="2026-10-06T16:30:00-04:00")
    db.upsert_filing(conn, f)
    db.upsert_score(conn, make_score(f.accession_no))
    later = date(2026, 12, 31)
    assert compute_outcomes(conn, FakePrices(), today=later) == 0
    assert compute_outcomes(conn, FakePrices(), lookback_days=120, today=later) == 1


class GappyPrices:
    def closes(self, tickers, start, end):
        data = {
            "SPY": [100, 101, 101, 101, 101, 101],
            "EXMP": [50, 50, np.nan, 55, 55, 55],     # no close on Oct 7
        }
        return pd.DataFrame({t: data[t] for t in tickers if t in data}, index=DAYS)


def test_compute_outcomes_skips_a_day_after_a_missing_close(conn):
    # Event day Oct 8 follows a day with no EXMP close. Filling the gap would book the two-day
    # move from Oct 6 (+10%) as Oct 8's return; with no prior close there is no one-day return.
    f = make_filing(1, accepted_at="2026-10-07T17:00:00-04:00")
    db.upsert_filing(conn, f)
    db.upsert_score(conn, make_score(f.accession_no))
    assert compute_outcomes(conn, GappyPrices()) == 0
    assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0


def _frame(signal: float, n: int = 400, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    score = rng.uniform(1, 5, n)
    move = np.abs(rng.normal(0, 0.01 + signal * (score - 1) / 100, n))
    items = np.where(rng.random(n) < 0.3, "2.02,9.01", "8.01")
    return pd.DataFrame({
        "accession_no": [f"a{i}" for i in range(n)], "ticker": "X", "items": items, "materiality": score,
        "event": rng.choice(["mna", "earnings_guidance", "other"], n), "surprise": 0.5, "cost_usd": 0.0001,
        "latency_ms": 120.0, "event_day": [f"2026-09-{1 + i % 20:02d}" for i in range(n)],
        "abnormal_return": move, "abs_ar": move,
    })


def test_evaluate_detects_signal():
    r = evaluate(_frame(signal=2.0))
    assert r.gap_ratio > 2 and r.spearman > 0.3
    assert abs(r.random_spearman) < 0.05 and r.random_gap_ratio == pytest.approx(1, abs=0.2)
    assert r.jev_lift_same_budget > r.rule_lift  # rule flags are random here
    assert set(r.by_score) <= {"1", "2", "3", "4", "5"}
    assert r.gap_ci_low < r.gap_ratio < r.gap_ci_high
    assert r.gap_ci_low > 1.5  # a real signal on 400 rows stays clear of 1x
    text = format_report(r)
    assert "Spearman" in text and "90% CI" in text


def test_evaluate_without_signal_looks_random():
    r = evaluate(_frame(signal=0.0))
    assert abs(r.spearman) < 0.15 and r.gap_ratio < 1.5


def test_evaluate_needs_enough_rows():
    with pytest.raises(NotEnoughOutcomes) as exc:
        evaluate(_frame(0.0, n=10))
    assert (exc.value.have, exc.value.need) == (10, MIN_OUTCOMES)
    # Still a ValueError, so existing callers that catch that keep working.
    assert isinstance(exc.value, ValueError)


def test_rule_flag_and_spearman():
    assert rule_flag(pd.Series(["2.02,9.01", "8.01", None])).tolist() == [True, False, False]
    assert spearman(pd.Series([1, 2, 3]), pd.Series([10, 20, 30])) == pytest.approx(1.0)


def test_bootstrap_ci_narrows_with_more_data():
    small, large = _frame(signal=2.0, n=100), _frame(signal=2.0, n=2000)
    lo_s, hi_s = bootstrap_gap_ci(small["materiality"], small["abs_ar"])
    lo_l, hi_l = bootstrap_gap_ci(large["materiality"], large["abs_ar"])
    assert hi_l - lo_l < hi_s - lo_s


def test_bootstrap_ci_is_deterministic_and_handles_zero_moves():
    df = _frame(signal=1.0, n=200)
    assert bootstrap_gap_ci(df["materiality"], df["abs_ar"]) == bootstrap_gap_ci(df["materiality"], df["abs_ar"])
    lo, hi = bootstrap_gap_ci(df["materiality"], pd.Series(0.0, index=df.index))
    assert np.isnan(lo) and np.isnan(hi)
