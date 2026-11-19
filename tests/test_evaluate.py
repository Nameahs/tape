import json
import warnings
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from tape import db
from tape.evaluate import (
    ET,
    MIN_OUTCOMES,
    UNPRICED_SQL,
    NotEnoughOutcomes,
    PricesUnavailable,
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


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        ("2026-10-12T14:00:00-04:00", 0),  # market open: Monday's bar is still moving
        ("2026-10-12T16:10:00-04:00", 0),  # just after the close, not yet settled
        ("2026-10-12T17:00:00-04:00", 1),  # settled
        ("2026-10-12T21:00:00+00:00", 1),  # UTC clock, 5:00 pm ET
        ("2026-10-13T09:00:00-04:00", 1),  # next morning
    ],
)
def test_compute_outcomes_waits_for_the_event_day_to_settle(conn, now, expected):
    f = make_filing(1, accepted_at="2026-10-09T17:30:00-04:00")  # Friday evening -> Monday Oct 12
    db.upsert_filing(conn, f)
    db.upsert_score(conn, make_score(f.accession_no))
    assert compute_outcomes(conn, FakePrices(), now=datetime.fromisoformat(now)) == expected
    if not expected:
        # Nothing written, so a later run still prices it.
        assert compute_outcomes(conn, FakePrices(), now=datetime(2026, 10, 13, 9, tzinfo=ET)) == 1


def test_compute_outcomes_before_any_settled_day_writes_nothing(conn):
    f = make_filing(1, accepted_at="2026-10-04T12:00:00-04:00")
    db.upsert_filing(conn, f)
    db.upsert_score(conn, make_score(f.accession_no))
    assert compute_outcomes(conn, FakePrices(), now=datetime(2026, 10, 5, 12, tzinfo=ET)) == 0


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


@pytest.mark.parametrize(
    ("accepted", "priced"),
    [
        # The window starts at midnight Eastern on the cutoff day (Sep 14 for a 30-day lookback from Oct 14),
        # whatever offset the timestamp was stored with.
        ("2026-09-13T23:59:59-04:00", False),
        ("2026-09-14T03:59:59+00:00", False),  # still Sep 13 in New York
        ("2026-09-14T00:00:00-04:00", True),
        ("2026-09-14T04:00:00+00:00", True),
    ],
)
def test_compute_outcomes_window_starts_at_midnight_eastern(conn, accepted, priced):
    f = make_filing(1, accepted_at=accepted)
    db.upsert_filing(conn, f)
    db.upsert_score(conn, make_score(f.accession_no))
    prices = RecordingPrices()
    compute_outcomes(conn, prices, lookback_days=30, today=TODAY)
    assert bool(prices.calls) is priced


def test_compute_outcomes_window_query_uses_the_accepted_index(conn):
    plan = " ".join(r["detail"] for r in conn.execute("EXPLAIN QUERY PLAN " + UNPRICED_SQL, ("2026-09-14",)))
    assert "idx_filings_accepted_jd" in plan


class BrokenPrices:
    def closes(self, tickers, start, end):
        raise KeyError("Close")


class NoMarketPrices(FakePrices):
    def closes(self, tickers, start, end):
        return super().closes(tickers, start, end).drop(columns="SPY")


class EmptyPrices:
    def closes(self, tickers, start, end):
        return pd.DataFrame()


@pytest.mark.parametrize("source", [BrokenPrices(), NoMarketPrices(), EmptyPrices()])
def test_compute_outcomes_reports_an_unusable_price_source(conn, source):
    f = make_filing(1, ticker="EXMP", accepted_at="2026-10-07T08:00:00-04:00")
    db.upsert_filing(conn, f)
    db.upsert_score(conn, make_score(f.accession_no, 3.0))
    with pytest.raises(PricesUnavailable):
        compute_outcomes(conn, source, today=TODAY)
    # Left unpriced, so the next run picks it up.
    assert compute_outcomes(conn, FakePrices(), today=TODAY) == 1


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


def test_spearman_is_nan_when_a_side_is_constant():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert np.isnan(spearman(pd.Series([1.0, 1.0, 1.0]), pd.Series([0.1, 0.2, 0.3])))
        assert np.isnan(spearman(pd.Series([1.0, 2.0, 3.0]), pd.Series([0.2, 0.2, 0.2])))


def test_evaluate_with_a_constant_score_event_group_does_not_warn():
    df = _frame(signal=1.0, n=200)
    df.loc[df["event"] == "other", "materiality"] = 1.0
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        r = evaluate(df)
    assert np.isnan(r.by_event["other"]["spearman"])
    assert not np.isnan(r.by_event["mna"]["spearman"])
    assert not np.isnan(r.spearman)


def test_evaluate_with_every_score_tied_does_not_warn():
    df = _frame(signal=1.0, n=200)
    df["materiality"] = 3.0
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        r = evaluate(df)
    assert np.isnan(r.spearman) and np.isnan(r.random_spearman)
    assert all(np.isnan(v["spearman"]) for v in r.by_event.values())
    assert "n/a" in format_report(r)


def test_lift_is_nan_when_unflagged_filings_never_move():
    df = _frame(signal=1.0, n=200)
    rule = rule_flag(df["items"])
    df.loc[~rule, ["abs_ar", "abnormal_return"]] = 0.0
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        r = evaluate(df)
    assert np.isnan(r.rule_lift)


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


def test_evaluate_does_not_depend_on_row_order_when_scores_tie():
    # Whole-number scores, so every cut (top 10%, bottom 50%, Jev's top k, each day's top 10)
    # runs through a block of tied filings.
    df = _frame(signal=1.0, n=200)
    df["materiality"] = df["materiality"].round()
    shuffled = df.sample(frac=1, random_state=3)
    assert evaluate(shuffled).to_json() == evaluate(df).to_json()
    assert evaluate(df.iloc[::-1]).to_json() == evaluate(df).to_json()


def test_cost_per_filing_averages_only_known_costs():
    df = _frame(signal=1.0, n=200)
    df["cost_usd"] = 0.0002
    df.loc[df.index[:50], "cost_usd"] = np.nan
    r = evaluate(df)
    assert r.cost_per_filing_usd == pytest.approx(0.0002)
    assert r.cost_unknown == 50
    assert "$0.000200 (cost unknown for 50 of 200)" in format_report(r)


def test_cost_per_filing_with_every_cost_known_says_nothing_extra():
    r = evaluate(_frame(signal=1.0, n=200))
    assert r.cost_unknown == 0
    assert "Jev cost per filing: $0.000100, median" in format_report(r)


def test_cost_per_filing_is_unknown_when_no_score_has_a_cost():
    df = _frame(signal=1.0, n=200)
    df["cost_usd"] = np.nan
    r = evaluate(df)
    assert np.isnan(r.cost_per_filing_usd) and r.cost_unknown == 200
    assert "Jev cost per filing: unknown, median" in format_report(r)


def _strict_loads(text: str) -> dict:
    def reject(token):
        raise ValueError(f"non-JSON constant {token}")
    return json.loads(text, parse_constant=reject)


def test_report_json_writes_undefined_metrics_as_null():
    df = _frame(signal=1.0, n=200)
    df["items"] = "8.01"  # no rule items anywhere, so both lifts are undefined
    df.loc[df["event"] == "mna", "materiality"] = 3.0  # constant scores: that event's spearman is undefined
    r = evaluate(df)
    assert r.rule_flagged == 0 and np.isnan(r.rule_lift) and np.isnan(r.jev_lift_same_budget)

    data = _strict_loads(r.to_json())

    assert data["rule_lift"] is None and data["jev_lift_same_budget"] is None
    assert data["by_event"]["mna"]["spearman"] is None
    assert data["gap_ratio"] == pytest.approx(r.gap_ratio)  # defined metrics are unchanged


def test_report_json_with_unknown_cost_is_strict_json():
    df = _frame(signal=1.0, n=200)
    df["cost_usd"] = np.nan
    assert _strict_loads(evaluate(df).to_json())["cost_per_filing_usd"] is None


def test_format_report_says_na_for_undefined_metrics():
    df = _frame(signal=1.0, n=200)
    df["items"] = "8.01"
    text = format_report(evaluate(df))
    assert "Item-code rule flags 0: lift n/a; Jev top 0: lift n/a" in text
    assert "nan" not in text


def test_format_report_says_na_when_scores_all_tie():
    df = _frame(signal=1.0, n=200)
    df["materiality"] = 3.0
    text = format_report(evaluate(df))
    assert "Spearman score vs move: n/a" in text
    assert "nan" not in text


def test_format_report_lists_events_by_filing_count():
    df = _frame(signal=1.0, n=200)
    df["event"] = ["mna"] * 120 + ["earnings_guidance"] * 76 + ["other"] * 4
    r = evaluate(df)
    lines = format_report(r).splitlines()
    start = lines.index("By event (5+ filings)")
    block = lines[start + 1:start + 3]
    assert block[0].startswith("  mna: ") and block[0].endswith("(n=120)")
    assert block[1].startswith("  earnings_guidance: ") and block[1].endswith("(n=76)")
    assert f"{r.by_event['mna']['mean_abs_ar']:.2%} mean move" in block[0]
    assert not any(line.startswith("  other:") for line in lines)  # under 5 filings, not in by_event


def test_format_report_by_event_says_na_for_undefined_spearman():
    df = _frame(signal=1.0, n=200)
    df.loc[df["event"] == "other", "materiality"] = 1.0
    text = format_report(evaluate(df))
    other = next(line for line in text.splitlines() if line.startswith("  other:"))
    assert "Spearman n/a" in other


def test_format_report_omits_by_event_block_when_empty():
    df = _frame(signal=1.0, n=200)
    df["event"] = [f"e{i % 50}" for i in range(200)]  # 4 filings each, none qualify
    assert "By event" not in format_report(evaluate(df))


def test_top10_hit_rate_pools_picks_over_days_with_a_choice():
    df = _frame(signal=1.0, n=400)  # 20 event days of 20 filings
    r = evaluate(df)
    assert r.top10_days == 20
    expected = sum(
        int((g.sort_values("accession_no").nlargest(10, "materiality")["abs_ar"] > 0.03).sum())
        for _, g in df.groupby("event_day")
    ) / 200
    assert r.hit_rate_top10_daily == pytest.approx(expected)
    assert "(20 days with more than 10 filings)" in format_report(r)


def test_top10_hit_rate_ignores_days_too_small_to_choose_from():
    df = _frame(signal=1.0, n=400)
    base = evaluate(df).hit_rate_top10_daily
    thin = df.iloc[:30].copy()
    thin["accession_no"] = [f"t{i}" for i in range(30)]
    thin["event_day"] = [f"2026-10-{1 + i // 10:02d}" for i in range(30)]  # three days of exactly 10
    thin["abs_ar"] = 0.5  # every one a big mover; would drag the rate up if counted
    r = evaluate(pd.concat([df, thin], ignore_index=True))
    assert r.top10_days == 20
    assert r.hit_rate_top10_daily == pytest.approx(base)


def test_top10_hit_rate_is_na_when_no_day_has_more_than_10_filings():
    df = _frame(signal=1.0, n=200)
    df["event_day"] = [f"2026-09-{1 + i % 25:02d}" for i in range(200)]  # 8 filings a day
    r = evaluate(df)
    assert r.top10_days == 0 and np.isnan(r.hit_rate_top10_daily)
    text = format_report(r)
    assert "Top-10 daily picks moving > 3%: n/a  (0 days with more than 10 filings)" in text
    assert "nan" not in text
    assert _strict_loads(r.to_json())["hit_rate_top10_daily"] is None
def test_format_report_says_na_when_no_score_has_a_latency():
    df = _frame(signal=1.0, n=200)
    df["latency_ms"] = None
    r = evaluate(df)
    assert np.isnan(r.median_latency_ms)
    text = format_report(r)
    assert text.endswith("median latency n/a")
    assert "nan" not in text
    assert _strict_loads(r.to_json())["median_latency_ms"] is None


def test_format_report_prints_median_latency_in_ms():
    assert format_report(evaluate(_frame(signal=1.0, n=200))).endswith("median latency 120 ms")
