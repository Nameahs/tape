"""Per-day activity summary for a quick look from a terminal."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from tape import db

ET = ZoneInfo("America/New_York")
ROUTES = ("now", "digest", "log")


@dataclass
class DayStats:
    day: date
    filings: int = 0
    scored: int = 0
    routes: dict[str, int] = field(default_factory=lambda: dict.fromkeys(ROUTES, 0))
    cost_usd: float = 0.0


@dataclass
class Stats:
    days: list[DayStats]
    spend_usd: float  # Jev cost of every score made in the window, whatever day the filing is from
    calls: int
    # Filings scoring gave up on (any day, not just the window): they need a look, and stay unscored
    # until their score_failures row is deleted.
    given_up: int = 0
    # Filings ingest stopped fetching (no text, or EDGAR kept erroring); never stored, so not in `days`.
    fetch_given_up: int = 0


def collect(conn: sqlite3.Connection, days: int = 7, today: date | None = None) -> Stats:
    """Filings, scores and routes for the last `days` days (today included), bucketed by EDGAR's
    acceptance date in Eastern time so a filing lands on the day the market saw it.

    Backfills score old filings, so spend per filing day understates what was paid this week; the
    window total is taken from scored_at instead.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    today = today or datetime.now(ET).date()
    first = today - timedelta(days=days - 1)
    start_utc = datetime.combine(first, time(), ET).astimezone(UTC)
    end_utc = datetime.combine(today + timedelta(days=1), time(), ET).astimezone(UTC)

    buckets = {first + timedelta(days=i): DayStats(first + timedelta(days=i)) for i in range(days)}
    rows = conn.execute(
        """
        SELECT f.accepted_at, s.accession_no IS NOT NULL AS scored, s.cost_usd, b.route
        FROM filings f LEFT JOIN scores s USING (accession_no) LEFT JOIN briefs b USING (accession_no)
        WHERE julianday(f.accepted_at) >= julianday(?) AND julianday(f.accepted_at) < julianday(?)
        """,
        (start_utc.isoformat(), end_utc.isoformat()),
    )
    for r in rows:
        d = buckets.get(datetime.fromisoformat(r["accepted_at"]).astimezone(ET).date())
        if d is None:
            continue
        d.filings += 1
        d.scored += r["scored"]
        d.cost_usd += r["cost_usd"] or 0.0
        if r["route"] in d.routes:
            d.routes[r["route"]] += 1

    spend = conn.execute(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS cost FROM scores
        WHERE julianday(scored_at) >= julianday(?) AND julianday(scored_at) < julianday(?)
        """,
        (start_utc.isoformat(), end_utc.isoformat()),
    ).fetchone()
    return Stats(days=[buckets[k] for k in sorted(buckets)], spend_usd=float(spend["cost"]), calls=int(spend["n"]),
                 given_up=len(db.given_up(conn)), fetch_given_up=len(db.fetch_given_up(conn)))


def format_stats(s: Stats) -> str:
    header = f"{'day':<10}  {'filings':>7}  {'scored':>6}  {'now':>4}  {'digest':>6}  {'log':>5}  {'cost':>8}"
    lines = [header]
    for d in s.days:
        lines.append(
            f"{d.day.isoformat():<10}  {d.filings:>7,}  {d.scored:>6,}  {d.routes['now']:>4,}  "
            f"{d.routes['digest']:>6,}  {d.routes['log']:>5,}  {f'${d.cost_usd:.2f}':>8}"
        )
    total_filings = sum(d.filings for d in s.days)
    total_scored = sum(d.scored for d in s.days)
    lines.append(f"{'total':<10}  {total_filings:>7,}  {total_scored:>6,}")
    lines.append(f"Jev spend in window: ${s.spend_usd:.2f} over {s.calls:,} calls")
    if s.given_up:
        lines.append(
            f"Gave up scoring {s.given_up:,} filing(s) after {db.MAX_SCORE_ATTEMPTS} failed attempts "
            "(listed in score_failures; delete a row to retry it)"
        )
    if s.fetch_given_up:
        lines.append(
            f"Gave up fetching {s.fetch_given_up:,} filing(s) after {db.MAX_FETCH_ATTEMPTS} failed attempts "
            "(listed in fetch_failures; delete a row to retry it)"
        )
    return "\n".join(lines)
