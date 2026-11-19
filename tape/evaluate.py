"""Does Jev's materiality score predict next-day stock moves? Measured against two baselines.

Event day: a filing accepted before the 4:00 pm ET close on a trading day is priced that day;
anything later (or on a non-trading day) is priced on the next trading day.
Abnormal return: stock close-to-close return minus SPY's over the same day.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from tape.config import ConfigError
from tape.fmt import fmt_metric

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
CLOSE = time(16, 0)
# Until this time Yahoo's bar for today can still hold the last trade rather than the official
# close, so today's row isn't used for outcomes before it.
SETTLED = time(17, 0)
MARKET = "SPY"
# Item codes a rule-writer would flag without any AI.
RULE_ITEMS = {"1.01", "2.01", "2.02", "5.02"}
BIG_MOVE = 0.03
DAILY_PICKS = 10  # how many of each day's filings the top-10 hit rate assumes someone reads
# Filings older than this are not priced on a normal run. One with a ticker Yahoo can't price
# never gets an outcome, and without a bound it would drag the download window back forever.
PRICE_LOOKBACK_DAYS = 30
MIN_OUTCOMES = 20  # below this the bucket means and the shuffled baseline are mostly noise


# Scored filings with a ticker and no outcome, accepted at or after an instant. Unpriceable filings
# never leave this set, so the bound is applied in SQL on idx_filings_accepted_jd rather than after
# reading the whole history; the WHERE clause must keep the exact julianday(accepted_at) expression.
UNPRICED_SQL = """
SELECT f.accession_no, f.ticker, f.accepted_at FROM filings f
JOIN scores s USING (accession_no) LEFT JOIN outcomes o USING (accession_no)
WHERE julianday(f.accepted_at) >= julianday(?) AND o.accession_no IS NULL AND f.ticker IS NOT NULL
"""


class NotEnoughOutcomes(ValueError):
    """Too few filings have next-day prices to evaluate yet; normal for the first weeks after deploy."""

    def __init__(self, have: int, need: int = MIN_OUTCOMES) -> None:
        super().__init__(f"need at least {need} filings with outcomes, have {have}")
        self.have, self.need = have, need


class PricesUnavailable(RuntimeError):
    """The price source failed or returned nothing usable. Filings stay eligible for later runs."""


class PriceSource(Protocol):
    def closes(self, tickers: list[str], start: date, end: date) -> pd.DataFrame:
        """Adjusted closes, indexed by date, one column per ticker. Missing data is NaN."""
        ...


class YFinancePrices:
    def closes(self, tickers: list[str], start: date, end: date) -> pd.DataFrame:
        try:
            import yfinance as yf
        except ImportError as e:
            raise ConfigError('yfinance is not installed; run pip install -e ".[prices]" or pass --skip-prices') from e

        # EDGAR uses '-' for share classes (BRK-B); Yahoo does too.
        data = yf.download(tickers, start=start, end=end + timedelta(days=1), auto_adjust=True,
                           progress=False, group_by="column")
        closes = data["Close"]
        if isinstance(closes, pd.Series):
            closes = closes.to_frame(tickers[0])
        closes.index = pd.to_datetime(closes.index).date
        return closes


def event_day(accepted_at: str, trading_days: list[date]) -> date | None:
    ts = datetime.fromisoformat(accepted_at).astimezone(ET)
    d = ts.date()
    if ts.time() < CLOSE:
        i = bisect_left(trading_days, d)  # same day if it trades, else the next one
    else:
        i = bisect_right(trading_days, d)  # strictly after
    return trading_days[i] if i < len(trading_days) else None


def settled(day: date, now: datetime) -> bool:
    """Whether `day`'s close is final as of `now`: an earlier day, or today once SETTLED has passed."""
    now = now.astimezone(ET)
    return day < now.date() or (day == now.date() and now.time() >= SETTLED)


def compute_outcomes(conn: sqlite3.Connection, prices: PriceSource,
                     lookback_days: int = PRICE_LOOKBACK_DAYS, today: date | None = None,
                     now: datetime | None = None) -> int:
    """Price scored filings accepted in the last `lookback_days` that have no outcome yet.

    A filing whose event day hasn't settled yet is left for a later run: an outcome is never
    revisited once written, so an intraday return stored now would stay wrong for good.

    Raises PricesUnavailable if the source fails or has no market prices for the window. The
    window always spans trading days, so that means an outage rather than a quiet stretch.
    """
    now = (now or datetime.now(ET)).astimezone(ET)
    cutoff = (today or now.date()) - timedelta(days=lookback_days)
    since = datetime.combine(cutoff, time(), ET).isoformat()
    rows = conn.execute(UNPRICED_SQL, (since,)).fetchall()
    if not rows:
        return 0
    first = min(datetime.fromisoformat(r["accepted_at"]).date() for r in rows) - timedelta(days=7)
    last = max(datetime.fromisoformat(r["accepted_at"]).date() for r in rows) + timedelta(days=7)
    tickers = sorted({r["ticker"] for r in rows} | {MARKET})
    try:
        closes = prices.closes(tickers, first, last).sort_index()
    except ConfigError:
        raise  # a setup mistake won't clear by itself, so it shouldn't pass for an outage
    except Exception as e:  # yfinance raises whatever its HTTP and parsing layers do
        raise PricesUnavailable(f"{type(e).__name__}: {e}") from e
    if closes.empty or MARKET not in closes.columns:
        raise PricesUnavailable(f"no {MARKET} prices between {first} and {last}")
    trading_days = [d for d in closes.index if pd.notna(closes.at[d, MARKET]) and settled(d, now)]
    if not trading_days:
        return 0
    # No filling: a missing prior close must leave the return NaN rather than fold several days'
    # move into one (pandas 2.x pads by default, pandas 3 doesn't).
    returns = closes.loc[trading_days].pct_change(fill_method=None)

    written = 0
    for r in rows:
        day = event_day(r["accepted_at"], trading_days)
        if day is None or day == trading_days[0] or r["ticker"] not in returns.columns:
            continue  # no next-day price yet, or no prior close to compare to
        stock, market = returns.at[day, r["ticker"]], returns.at[day, MARKET]
        if pd.isna(stock) or pd.isna(market):
            continue
        ar = float(stock - market)
        conn.execute(
            "INSERT OR REPLACE INTO outcomes VALUES (?, ?, ?, ?, ?, ?)",
            (r["accession_no"], day.isoformat(), float(stock), float(market), ar, abs(ar)),
        )
        written += 1
    conn.commit()
    return written


def load_frame(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT f.accession_no, f.ticker, f.items, s.materiality, s.event, s.surprise, s.cost_usd,
               s.latency_ms, o.event_day, o.abnormal_return, o.abs_abnormal_return AS abs_ar
        FROM outcomes o JOIN scores s USING (accession_no) JOIN filings f USING (accession_no)
        """,
        conn,
    )


def spearman(x: pd.Series, y: pd.Series) -> float:
    """Rank correlation, or NaN when it is undefined.

    A constant side has no rank order to correlate (an event group Jev scored all 1s, say), and
    letting `corr` divide by its zero spread makes numpy print RuntimeWarnings for an expected case.
    """
    if x.nunique() < 2 or y.nunique() < 2:
        return float("nan")
    return float(x.rank().corr(y.rank()))


def _mean_defined(values: list[float]) -> float:
    """Mean of the non-NaN values, or NaN (without numpy's empty-slice warning) when there are none."""
    defined = [v for v in values if not math.isnan(v)]
    return sum(defined) / len(defined) if defined else float("nan")


def rule_flag(items: pd.Series) -> pd.Series:
    return items.fillna("").map(lambda s: bool(RULE_ITEMS & set(s.split(","))))


@dataclass
class Report:
    n: int
    by_score: dict[str, dict[str, float]]
    top10_mean: float
    bottom50_mean: float
    gap_ratio: float
    gap_ci_low: float
    gap_ci_high: float
    spearman: float
    hit_rate_top10_daily: float
    random_gap_ratio: float
    random_spearman: float
    rule_flagged: int
    rule_lift: float
    jev_lift_same_budget: float
    by_event: dict[str, dict[str, float]]
    cost_per_filing_usd: float  # over filings whose score has a known cost; NaN if none does
    median_latency_ms: float
    # Scores stored without a cost because Jev returned no token usage; left out of cost_per_filing_usd.
    cost_unknown: int = 0
    # Event days with more than DAILY_PICKS filings, the only ones hit_rate_top10_daily is computed over.
    top10_days: int = 0

    def to_json(self) -> str:
        # Undefined metrics (no rule flags, all scores tied, ...) are NaN in memory. Bare NaN tokens
        # aren't JSON and make strict readers reject the whole file, so they go out as null, and
        # allow_nan=False makes any that slip past fail here instead of in someone else's parser.
        return json.dumps(_finite_or_none(asdict(self)), indent=2, allow_nan=False)


def _finite_or_none(value):
    if isinstance(value, dict):
        return {k: _finite_or_none(v) for k, v in value.items()}
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _ranked(score: pd.Series) -> pd.Index:
    """Index of `score` highest first. Stable, so tied filings keep their row order and every cut
    through a block of equal scores (top 10%, bottom 50%, Jev's top k) picks the same filings.
    """
    return score.sort_values(ascending=False, kind="stable").index


def _gap(score: pd.Series, move: pd.Series) -> float:
    order = _ranked(score)
    n = len(order)
    top = move.loc[order[: max(1, n // 10)]].mean()
    bottom = move.loc[order[n // 2:]].mean()
    return float(top / bottom) if bottom else float("nan")


def bootstrap_gap_ci(score: pd.Series, move: pd.Series, resamples: int = 1000, level: float = 0.90,
                     seed: int = 7) -> tuple[float, float]:
    """Percentile bootstrap interval for the top-10% / bottom-50% gap ratio.

    Rows are resampled with replacement, so a backfill of a few hundred filings shows how much
    the headline ratio could move on a different sample of the same size.
    """
    s, m = score.to_numpy(dtype=float), move.to_numpy(dtype=float)
    n = len(s)
    top_n, bottom_start = max(1, n // 10), n // 2
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(resamples, n))
    order = np.argsort(-s[idx], axis=1, kind="stable")
    ranked = np.take_along_axis(m[idx], order, axis=1)
    top = ranked[:, :top_n].mean(axis=1)
    bottom = ranked[:, bottom_start:].mean(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        gaps = np.where(bottom > 0, top / bottom, np.nan)
    gaps = gaps[np.isfinite(gaps)]
    if not len(gaps):
        return float("nan"), float("nan")
    tail = (1 - level) / 2 * 100
    low, high = np.percentile(gaps, [tail, 100 - tail])
    return float(low), float(high)


def _lift(flag: pd.Series, move: pd.Series) -> float:
    if flag.all() or not flag.any():
        return float("nan")
    return float(move[flag].mean() / move[~flag].mean())


def evaluate(df: pd.DataFrame, shuffles: int = 200, seed: int = 7) -> Report:
    if len(df) < MIN_OUTCOMES:
        raise NotEnoughOutcomes(len(df))
    # A fixed row order, so the tie-breaking in the cuts below (and the shuffled baseline) doesn't
    # depend on how SQLite happened to return the rows.
    df = df.sort_values("accession_no", kind="stable").reset_index(drop=True)
    score, move = df["materiality"], df["abs_ar"]

    # Round half up so each bucket covers [k - 0.5, k + 0.5). Series.round() rounds halves to even,
    # which sends 2.5 down to 2 and 3.5 up to 4 and makes the middle buckets uneven.
    bucket = np.floor(score + 0.5).clip(1, 5).astype(int)
    by_score = {
        str(k): {"n": int(len(g)), "mean_abs_ar": float(g.mean())} for k, g in move.groupby(bucket)
    }
    order = _ranked(score)
    n = len(df)

    # Only days with more filings than picks: on a thinner day the "top 10" is every filing, so Jev chose
    # nothing and the day would just pull the rate toward the base rate. Picks are pooled across days,
    # so a day with one filing can't weigh as much as a busy one.
    hits = picks = top10_days = 0
    for _, g in df.groupby("event_day"):
        if len(g) <= DAILY_PICKS:
            continue
        top = g.nlargest(DAILY_PICKS, "materiality")
        hits += int((top["abs_ar"] > BIG_MOVE).sum())
        picks += len(top)
        top10_days += 1

    rng = np.random.default_rng(seed)
    rand_gaps, rand_rhos = [], []
    for _ in range(shuffles):
        fake = pd.Series(rng.permutation(score.to_numpy()), index=score.index)
        rand_gaps.append(_gap(fake, move))
        rand_rhos.append(spearman(fake, move))

    # Compare Jev to the rule at the same alert volume: flag Jev's top-k where k = rule's count.
    flag = rule_flag(df["items"])
    k = int(flag.sum())
    jev_flag = pd.Series(False, index=df.index)
    jev_flag.loc[order[:k]] = True

    ci_low, ci_high = bootstrap_gap_ci(score, move, seed=seed)

    by_event = {
        ev: {
            "n": int(len(g)),
            "mean_abs_ar": float(g["abs_ar"].mean()),
            "spearman": spearman(g["materiality"], g["abs_ar"]),
        }
        for ev, g in df.groupby("event") if len(g) >= 5
    }

    return Report(
        n=n,
        by_score=by_score,
        top10_mean=float(move.loc[order[: max(1, n // 10)]].mean()),
        bottom50_mean=float(move.loc[order[n // 2:]].mean()),
        gap_ratio=_gap(score, move),
        gap_ci_low=ci_low,
        gap_ci_high=ci_high,
        spearman=spearman(score, move),
        hit_rate_top10_daily=hits / picks if picks else float("nan"),
        random_gap_ratio=_mean_defined(rand_gaps),
        random_spearman=_mean_defined(rand_rhos),
        rule_flagged=k,
        rule_lift=_lift(flag, move),
        jev_lift_same_budget=_lift(jev_flag, move),
        by_event=by_event,
        # An unknown cost is not a zero one: averaging it in as free would pull the per-filing cost down.
        cost_per_filing_usd=float(df["cost_usd"].mean()) if df["cost_usd"].notna().any() else float("nan"),
        median_latency_ms=float(df["latency_ms"].median()),
        cost_unknown=int(df["cost_usd"].isna().sum()),
        top10_days=top10_days,
    )


def format_report(r: Report) -> str:
    lines = [
        f"Filings evaluated: {r.n}",
        "",
        "Mean |abnormal return| by materiality score",
        *(f"  {k}: {v['mean_abs_ar']:.2%}  (n={v['n']})" for k, v in sorted(r.by_score.items())),
        *_by_event_lines(r.by_event),
        "",
        f"Top 10% vs bottom 50%:  {r.top10_mean:.2%} vs {r.bottom50_mean:.2%}  = {fmt_metric(r.gap_ratio, '.2f', 'x')}"
        f"  ({_ci_text(r)}; random {fmt_metric(r.random_gap_ratio, '.2f', 'x')})",
        f"Spearman score vs move: {fmt_metric(r.spearman, '.3f')}  (random {fmt_metric(r.random_spearman, '.3f')})",
        f"Top-{DAILY_PICKS} daily picks moving > {BIG_MOVE:.0%}: {fmt_metric(r.hit_rate_top10_daily, '.0%')}"
        f"  ({r.top10_days} days with more than {DAILY_PICKS} filings)",
        f"Item-code rule flags {r.rule_flagged}: lift {fmt_metric(r.rule_lift, '.2f', 'x')}; "
        f"Jev top {r.rule_flagged}: lift {fmt_metric(r.jev_lift_same_budget, '.2f', 'x')}",
        f"Jev cost per filing: {_cost_text(r)}, median latency {fmt_metric(r.median_latency_ms, '.0f', ' ms')}",
    ]
    return "\n".join(lines)


def _by_event_lines(by_event: dict[str, dict[str, float]]) -> list[str]:
    """Rank correlation within each event type, biggest groups first. The overall Spearman mixes
    kinds of news with very different typical moves; this says whether Jev ranks within one.
    """
    if not by_event:
        return []
    events = sorted(by_event.items(), key=lambda kv: (-kv[1]["n"], kv[0]))
    return ["", "By event (5+ filings)", *(
        f"  {ev}: {v['mean_abs_ar']:.2%} mean move, Spearman {fmt_metric(v['spearman'], '.2f')}  (n={v['n']})"
        for ev, v in events
    )]


def _ci_text(r: Report) -> str:
    if not (math.isfinite(r.gap_ci_low) and math.isfinite(r.gap_ci_high)):
        return "90% CI n/a"
    return f"90% CI {r.gap_ci_low:.2f}-{r.gap_ci_high:.2f}x"


def _cost_text(r: Report) -> str:
    if r.cost_unknown >= r.n:
        return "unknown"
    text = f"${r.cost_per_filing_usd:.6f}"
    if r.cost_unknown:
        text += f" (cost unknown for {r.cost_unknown:,} of {r.n:,})"
    return text
