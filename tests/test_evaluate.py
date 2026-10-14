from datetime import date

import numpy as np
import pandas as pd
import pytest

from tape import db
from tape.evaluate import compute_outcomes, evaluate, event_day, format_report, rule_flag, spearman
from tests.conftest import make_filing, make_score

# Mon Oct 5 to Mon Oct 12, 2026; Fri Oct 9 traded, then the weekend.
DAYS = [date(2026, 10, d) for d in (5, 6, 7, 8, 9, 12)]


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
    assert compute_outcomes(conn, FakePrices()) == 1
    row = conn.execute("SELECT * FROM outcomes").fetchone()
    assert row["event_day"] == "2026-10-07"
    assert row["abnormal_return"] == pytest.approx(-0.10)
    assert compute_outcomes(conn, FakePrices()) == 0  # idempotent


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
    assert "Spearman" in format_report(r)


def test_evaluate_without_signal_looks_random():
    r = evaluate(_frame(signal=0.0))
    assert abs(r.spearman) < 0.15 and r.gap_ratio < 1.5


def test_evaluate_needs_enough_rows():
    with pytest.raises(ValueError):
        evaluate(_frame(0.0, n=10))


def test_rule_flag_and_spearman():
    assert rule_flag(pd.Series(["2.02,9.01", "8.01", None])).tolist() == [True, False, False]
    assert spearman(pd.Series([1, 2, 3]), pd.Series([10, 20, 30])) == pytest.approx(1.0)
