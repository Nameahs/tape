"""One pass of the pipeline: discover new 8-Ks, fetch and clean them, score with Jev, route."""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta

from tape import db
from tape.config import Settings
from tape.edgar import Edgar, FilingRef
from tape.route import route
from tape.score import SystemOneClient, score_many

log = logging.getLogger(__name__)


@dataclass
class PassStats:
    discovered: int = 0
    fetched: int = 0
    fetch_errors: int = 0
    scored: int = 0
    routes: Counter = field(default_factory=Counter)
    cost_usd: float = 0.0

    def summary(self) -> str:
        r = self.routes
        return (
            f"discovered={self.discovered} fetched={self.fetched} errors={self.fetch_errors} "
            f"scored={self.scored} now={r['now']} digest={r['digest']} log={r['log']} cost=${self.cost_usd:.4f}"
        )


def ingest(conn: sqlite3.Connection, edgar: Edgar, refs: Iterable[FilingRef], settings: Settings,
           stats: PassStats) -> None:
    refs = list(refs)
    stats.discovered += len(refs)
    known = db.known_accessions(conn, [r.accession_no for r in refs])
    for ref in refs:
        if ref.accession_no in known:
            continue
        try:
            filing = edgar.fetch(ref, settings.max_chars)
        except Exception:
            stats.fetch_errors += 1
            log.exception("fetch failed for %s", ref.accession_no)
            continue
        with db.transaction(conn):
            db.upsert_filing(conn, filing)
        stats.fetched += 1


def score_and_route(conn: sqlite3.Connection, jev: SystemOneClient, settings: Settings, stats: PassStats) -> None:
    rows = db.unscored(conn)
    if not rows:
        return
    for s in score_many(jev, rows, settings.jev_model, settings.jev_input_price_per_m):
        with db.transaction(conn):
            db.upsert_score(conn, s)
            r = route(s, settings.thresholds)
            db.set_route(conn, s.accession_no, r)
        stats.scored += 1
        stats.routes[r] += 1
        stats.cost_usd += s.cost_usd or 0.0


def run_pass(conn: sqlite3.Connection, edgar: Edgar, jev: SystemOneClient, settings: Settings) -> PassStats:
    stats = PassStats()
    ingest(conn, edgar, edgar.latest_8k(), settings, stats)
    score_and_route(conn, jev, settings, stats)
    return stats


def backfill(conn: sqlite3.Connection, edgar: Edgar, jev: SystemOneClient, settings: Settings,
             days: int, end: date | None = None) -> PassStats:
    stats = PassStats()
    end = end or date.today()
    for offset in range(days, 0, -1):
        day = end - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        ingest(conn, edgar, edgar.daily_8k(day), settings, stats)
        # Score as we go so a long backfill can be interrupted without losing work.
        score_and_route(conn, jev, settings, stats)
        log.info("%s %s", day, stats.summary())
    return stats


def rescore_routes(conn: sqlite3.Connection, settings: Settings) -> Counter:
    """Re-apply thresholds to stored scores (after tuning) without calling Jev again."""
    counts: Counter = Counter()
    for row in conn.execute("SELECT * FROM scores").fetchall():
        s = db.ScoreRow(**{k: row[k] for k in row.keys() if k != "scored_at"})
        r = route(s, settings.thresholds)
        db.set_route(conn, s.accession_no, r)
        counts[r] += 1
    conn.commit()
    return counts
