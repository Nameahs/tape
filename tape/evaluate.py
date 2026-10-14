"""Does Jev's materiality score predict next-day stock moves? Measured against two baselines.

Event day: a filing accepted before the 4:00 pm ET close on a trading day is priced that day;
anything later (or on a non-trading day) is priced on the next trading day.
Abnormal return: stock close-to-close return minus SPY's over the same day.
"""

from __future__ import annotations

import json
import sqlite3
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")
CLOSE = time(16, 0)
MARKET = "SPY"
# Item codes a rule-writer would flag without any AI.
RULE_ITEMS = {"1.01", "2.01", "2.02", "5.02"}
BIG_MOVE = 0.03


class PriceSource(Protocol):
    def closes(self, tickers: list[str], start: date, end: date) -> pd.DataFrame:
        """Adjusted closes, indexed by date, one column per ticker. Missing data is NaN."""
        ...


class YFinancePrices:
    def closes(self, tickers: list[str], start: date, end: date) -> pd.DataFrame:
        import yfinance as yf  # optional dependency: pip install tape[prices]

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


def compute_outcomes(conn: sqlite3.Connection, prices: PriceSource) -> int:
    rows = conn.execute(
        """
        SELECT f.accession_no, f.ticker, f.accepted_at FROM filings f
        JOIN scores s USING (accession_no) LEFT JOIN outcomes o USING (accession_no)
        WHERE o.accession_no IS NULL AND f.ticker IS NOT NULL
        """
    ).fetchall()
    if not rows:
        return 0
    first = min(datetime.fromisoformat(r["accepted_at"]).date() for r in rows) - timedelta(days=7)
    last = max(datetime.fromisoformat(r["accepted_at"]).date() for r in rows) + timedelta(days=7)
    tickers = sorted({r["ticker"] for r in rows} | {MARKET})
    closes = prices.closes(tickers, first, last).sort_index()
    trading_days = [d for d in closes.index if pd.notna(closes.at[d, MARKET])]
    returns = closes.loc[trading_days].pct_change()

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
    return float(x.rank().corr(y.rank()))


def rule_flag(items: pd.Series) -> pd.Series:
    return items.fillna("").map(lambda s: bool(RULE_ITEMS & set(s.split(","))))


@dataclass
class Report:
    n: int
    by_score: dict[str, dict[str, float]]
    top10_mean: float
    bottom50_mean: float
    gap_ratio: float
    spearman: float
    hit_rate_top10_daily: float
    random_gap_ratio: float
    random_spearman: float
    rule_flagged: int
    rule_lift: float
    jev_lift_same_budget: float
    by_event: dict[str, dict[str, float]]
    cost_per_filing_usd: float
    median_latency_ms: float

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def _gap(score: pd.Series, move: pd.Series) -> float:
    order = score.sort_values(ascending=False).index
    n = len(order)
    top = move.loc[order[: max(1, n // 10)]].mean()
    bottom = move.loc[order[n // 2:]].mean()
    return float(top / bottom) if bottom else float("nan")


def _lift(flag: pd.Series, move: pd.Series) -> float:
    if flag.all() or not flag.any():
        return float("nan")
    return float(move[flag].mean() / move[~flag].mean())


def evaluate(df: pd.DataFrame, shuffles: int = 200, seed: int = 7) -> Report:
    if len(df) < 20:
        raise ValueError(f"need at least 20 filings with outcomes, have {len(df)}")
    df = df.reset_index(drop=True)
    score, move = df["materiality"], df["abs_ar"]

    bucket = score.round().clip(1, 5).astype(int)
    by_score = {
        str(k): {"n": int(len(g)), "mean_abs_ar": float(g.mean())} for k, g in move.groupby(bucket)
    }
    order = score.sort_values(ascending=False).index
    n = len(df)

    daily_hits = []
    for _, g in df.groupby("event_day"):
        top = g.nlargest(10, "materiality")
        daily_hits.append((top["abs_ar"] > BIG_MOVE).mean())

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
        spearman=spearman(score, move),
        hit_rate_top10_daily=float(np.mean(daily_hits)),
        random_gap_ratio=float(np.nanmean(rand_gaps)),
        random_spearman=float(np.nanmean(rand_rhos)),
        rule_flagged=k,
        rule_lift=_lift(flag, move),
        jev_lift_same_budget=_lift(jev_flag, move),
        by_event=by_event,
        cost_per_filing_usd=float(df["cost_usd"].fillna(0).mean()),
        median_latency_ms=float(df["latency_ms"].median()),
    )


def format_report(r: Report) -> str:
    lines = [
        f"Filings evaluated: {r.n}",
        "",
        "Mean |abnormal return| by materiality score",
        *(f"  {k}: {v['mean_abs_ar']:.2%}  (n={v['n']})" for k, v in sorted(r.by_score.items())),
        "",
        f"Top 10% vs bottom 50%:  {r.top10_mean:.2%} vs {r.bottom50_mean:.2%}  = {r.gap_ratio:.2f}x"
        f"  (random {r.random_gap_ratio:.2f}x)",
        f"Spearman score vs move: {r.spearman:.3f}  (random {r.random_spearman:.3f})",
        f"Top-10 daily picks moving > {BIG_MOVE:.0%}: {r.hit_rate_top10_daily:.0%}",
        f"Item-code rule flags {r.rule_flagged}: lift {r.rule_lift:.2f}x; Jev top {r.rule_flagged}: "
        f"lift {r.jev_lift_same_budget:.2f}x",
        f"Jev cost per filing: ${r.cost_per_filing_usd:.6f}, median latency {r.median_latency_ms:.0f} ms",
    ]
    return "\n".join(lines)
