"""SQLite store. Every table is keyed on the EDGAR accession number so each stage is safe to rerun."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS filings (
    accession_no TEXT PRIMARY KEY,
    cik          TEXT NOT NULL,
    ticker       TEXT,
    company      TEXT NOT NULL,
    form         TEXT NOT NULL,
    items        TEXT NOT NULL DEFAULT '',   -- comma separated, e.g. "2.02,9.01"
    accepted_at  TEXT NOT NULL,              -- ISO 8601 with UTC offset, as EDGAR reports it (ET)
    url          TEXT NOT NULL,
    clean_text   TEXT,
    ingested_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS scores (
    accession_no TEXT PRIMARY KEY REFERENCES filings(accession_no),
    model        TEXT NOT NULL,
    materiality  REAL NOT NULL,              -- 1 to 5
    event        TEXT NOT NULL,
    direction    TEXT NOT NULL,
    surprise     REAL NOT NULL,
    boilerplate  REAL NOT NULL,
    raw_json     TEXT NOT NULL,
    input_tokens INTEGER,
    cost_usd     REAL,
    latency_ms   REAL,
    scored_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS briefs (
    accession_no TEXT PRIMARY KEY REFERENCES filings(accession_no),
    route        TEXT NOT NULL CHECK (route IN ('now', 'digest', 'log')),
    brief_text   TEXT,
    sent_at      TEXT
);

CREATE TABLE IF NOT EXISTS outcomes (
    accession_no        TEXT PRIMARY KEY REFERENCES filings(accession_no),
    event_day           TEXT NOT NULL,
    stock_return        REAL NOT NULL,
    market_return       REAL NOT NULL,
    abnormal_return     REAL NOT NULL,
    abs_abnormal_return REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_filings_accepted ON filings(accepted_at);
CREATE INDEX IF NOT EXISTS idx_briefs_route ON briefs(route, sent_at);
"""

# Schema changes after the first release. SCHEMA above is version 0 and is never edited; each entry here
# moves an existing database one version forward. PRAGMA user_version records how far a file has got, so
# a workspace that predates a change picks it up the next time any command connects.
MIGRATIONS: list[str] = [
    # 1: Jev's confidence per answer, and the materiality level probabilities as a JSON list (levels 1..5),
    # so uncertain scores can be reviewed or routed differently without re-parsing raw_json.
    """
    ALTER TABLE scores ADD COLUMN materiality_confidence REAL;
    ALTER TABLE scores ADD COLUMN event_confidence REAL;
    ALTER TABLE scores ADD COLUMN direction_confidence REAL;
    ALTER TABLE scores ADD COLUMN materiality_probs TEXT;
    """,
    # 2: one row per pipeline pass or backfill. finished_at stays NULL if the process died mid-pass, so gaps
    # and crashes are visible; error holds the exception when a pass failed but could record it.
    """
    CREATE TABLE runs (
        id           INTEGER PRIMARY KEY,
        kind         TEXT NOT NULL CHECK (kind IN ('pass', 'backfill')),
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        discovered   INTEGER NOT NULL DEFAULT 0,
        fetched      INTEGER NOT NULL DEFAULT 0,
        fetch_errors INTEGER NOT NULL DEFAULT 0,
        scored       INTEGER NOT NULL DEFAULT 0,
        routed_now   INTEGER NOT NULL DEFAULT 0,
        routed_digest INTEGER NOT NULL DEFAULT 0,
        routed_log   INTEGER NOT NULL DEFAULT 0,
        cost_usd     REAL NOT NULL DEFAULT 0,
        error        TEXT
    );
    CREATE INDEX idx_runs_started ON runs(started_at);
    """,
    # 3: filings Jev failed to score. A transient error clears on the next pass; a filing Jev rejects every
    # time would otherwise be resent on every pass forever. After MAX_SCORE_ATTEMPTS, unscored() stops
    # offering it until the row is cleared by hand.
    """
    CREATE TABLE score_failures (
        accession_no    TEXT PRIMARY KEY REFERENCES filings(accession_no) ON DELETE CASCADE,
        attempts        INTEGER NOT NULL,
        last_error      TEXT NOT NULL,
        last_attempt_at TEXT NOT NULL
    );
    """,
    # 4: filings EDGAR failed to give us text for (no 8-K or EX-99 document, only images, or a permanent
    # HTTP error). These are never stored in filings, so without a record ingest would treat them as new
    # and fetch them again on every pass. No foreign key: the filing row usually doesn't exist.
    """
    CREATE TABLE fetch_failures (
        accession_no    TEXT PRIMARY KEY,
        attempts        INTEGER NOT NULL,
        last_error      TEXT NOT NULL,
        last_attempt_at TEXT NOT NULL
    );
    """,
    # 5: date-range queries compare julianday(accepted_at), since EDGAR's offsets vary (-04:00/-05:00) and a
    # plain text comparison would misorder them. idx_filings_accepted is on the bare column, so those
    # queries scanned the whole table; an index on the same expression lets them seek instead.
    """
    CREATE INDEX idx_filings_accepted_jd ON filings(julianday(accepted_at));
    """,
]

# Failed scoring attempts before a filing is left out of unscored(). Enough to ride out a Jev incident
# spanning a couple of passes, few enough that a filing Jev always rejects stops costing requests.
MAX_SCORE_ATTEMPTS = 3

# Failed fetches before ingest stops asking EDGAR for a filing. The latest-filings feed holds a bad filing
# for hours, so this caps the cost at a few requests instead of one round per pass.
MAX_FETCH_ATTEMPTS = 3


@dataclass(frozen=True)
class Filing:
    accession_no: str
    cik: str
    company: str
    form: str
    accepted_at: str
    url: str
    items: tuple[str, ...] = ()
    ticker: str | None = None
    clean_text: str | None = None


@dataclass(frozen=True)
class ScoreRow:
    accession_no: str
    model: str
    materiality: float
    event: str
    direction: str
    surprise: float
    boilerplate: float
    raw_json: str
    input_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: float | None = None
    materiality_confidence: float | None = None
    event_confidence: float | None = None
    direction_confidence: float | None = None
    materiality_probs: str | None = None  # JSON list, probability of levels 1..5


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply any MIGRATIONS this database hasn't seen yet. Returns the resulting schema version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for target, script in enumerate(MIGRATIONS[version:], start=version + 1):
        # executescript commits first, so wrap each step ourselves: a failed ALTER leaves the version alone.
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {target};\nCOMMIT;")
    return max(version, len(MIGRATIONS))


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def known_accessions(conn: sqlite3.Connection, accessions: list[str]) -> set[str]:
    if not accessions:
        return set()
    marks = ",".join("?" * len(accessions))
    rows = conn.execute(f"SELECT accession_no FROM filings WHERE accession_no IN ({marks})", accessions)
    return {r[0] for r in rows}


def upsert_filing(conn: sqlite3.Connection, f: Filing) -> None:
    conn.execute(
        """
        INSERT INTO filings (accession_no, cik, ticker, company, form, items, accepted_at, url, clean_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(accession_no) DO UPDATE SET
            ticker = COALESCE(excluded.ticker, filings.ticker),
            clean_text = COALESCE(excluded.clean_text, filings.clean_text)
        """,
        (f.accession_no, f.cik, f.ticker, f.company, f.form, ",".join(f.items), f.accepted_at, f.url,
         f.clean_text),
    )


def missing_tickers(conn: sqlite3.Connection, since_iso: str) -> list[sqlite3.Row]:
    """Filings accepted at or after `since_iso` that were stored without a ticker."""
    return conn.execute(
        """
        SELECT accession_no, cik FROM filings
        WHERE ticker IS NULL AND julianday(accepted_at) >= julianday(?)
        ORDER BY accepted_at
        """,
        (since_iso,),
    ).fetchall()


def set_ticker(conn: sqlite3.Connection, accession_no: str, ticker: str) -> None:
    conn.execute("UPDATE filings SET ticker = ? WHERE accession_no = ? AND ticker IS NULL", (ticker, accession_no))


def upsert_score(conn: sqlite3.Connection, s: ScoreRow) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO scores (accession_no, model, materiality, event, direction, surprise,
            boilerplate, raw_json, input_tokens, cost_usd, latency_ms, materiality_confidence,
            event_confidence, direction_confidence, materiality_probs)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (s.accession_no, s.model, s.materiality, s.event, s.direction, s.surprise, s.boilerplate,
         s.raw_json, s.input_tokens, s.cost_usd, s.latency_ms, s.materiality_confidence, s.event_confidence,
         s.direction_confidence, s.materiality_probs),
    )


def set_route(conn: sqlite3.Connection, accession_no: str, route: str) -> None:
    conn.execute(
        """
        INSERT INTO briefs (accession_no, route) VALUES (?, ?)
        ON CONFLICT(accession_no) DO UPDATE SET route = excluded.route
        """,
        (accession_no, route),
    )


def save_brief(conn: sqlite3.Connection, accession_no: str, text: str, sent_at: str | None) -> None:
    if not text.strip():
        raise ValueError(f"empty brief for {accession_no}")
    updated = conn.execute(
        "UPDATE briefs SET brief_text = ?, sent_at = ? WHERE accession_no = ?",
        (text, sent_at, accession_no),
    ).rowcount
    if not updated:
        raise KeyError(f"no routed filing {accession_no}")


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def start_run(conn: sqlite3.Connection, kind: str, started_at: str | None = None) -> int:
    with transaction(conn):
        cur = conn.execute("INSERT INTO runs (kind, started_at) VALUES (?, ?)", (kind, started_at or _utc_now()))
    if cur.lastrowid is None:
        raise RuntimeError("runs insert returned no row id")
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, *, discovered: int = 0, fetched: int = 0,
               fetch_errors: int = 0, scored: int = 0, routes: Mapping[str, int] | None = None,
               cost_usd: float = 0.0, error: str | None = None, finished_at: str | None = None) -> None:
    routes = routes or {}
    with transaction(conn):
        conn.execute(
            """
            UPDATE runs SET finished_at = ?, discovered = ?, fetched = ?, fetch_errors = ?, scored = ?,
                routed_now = ?, routed_digest = ?, routed_log = ?, cost_usd = ?, error = ?
            WHERE id = ?
            """,
            (finished_at or _utc_now(), discovered, fetched, fetch_errors, scored, routes.get("now", 0),
             routes.get("digest", 0), routes.get("log", 0), cost_usd, error, run_id),
        )


def unscored(conn: sqlite3.Connection, max_attempts: int = MAX_SCORE_ATTEMPTS) -> list[sqlite3.Row]:
    """Filings with text that Jev hasn't scored yet. Blank text (stored before fetch rejected it) is skipped,
    and so is any filing that has already failed `max_attempts` times (see given_up()).
    """
    return conn.execute(
        """
        SELECT f.* FROM filings f LEFT JOIN scores s USING (accession_no)
        LEFT JOIN score_failures sf USING (accession_no)
        WHERE s.accession_no IS NULL AND trim(COALESCE(f.clean_text, ''), ' ' || char(9, 10, 13)) != ''
          AND COALESCE(sf.attempts, 0) < ?
        ORDER BY f.accepted_at
        """,
        (max_attempts,),
    ).fetchall()


def record_score_failure(conn: sqlite3.Connection, accession_no: str, error: str,
                         at: str | None = None) -> int:
    """Count one more failed scoring attempt for a filing. Returns the attempts so far."""
    row = conn.execute(
        """
        INSERT INTO score_failures (accession_no, attempts, last_error, last_attempt_at) VALUES (?, 1, ?, ?)
        ON CONFLICT (accession_no) DO UPDATE SET
            attempts = attempts + 1, last_error = excluded.last_error, last_attempt_at = excluded.last_attempt_at
        RETURNING attempts
        """,
        (accession_no, error, at or _utc_now()),
    ).fetchone()
    return int(row["attempts"])


def clear_score_failure(conn: sqlite3.Connection, accession_no: str) -> None:
    conn.execute("DELETE FROM score_failures WHERE accession_no = ?", (accession_no,))


def given_up(conn: sqlite3.Connection, max_attempts: int = MAX_SCORE_ATTEMPTS) -> list[sqlite3.Row]:
    """Unscored filings that unscored() no longer offers. Delete a row from score_failures to retry one."""
    return conn.execute(
        """
        SELECT sf.* FROM score_failures sf LEFT JOIN scores s USING (accession_no)
        WHERE s.accession_no IS NULL AND sf.attempts >= ?
        ORDER BY sf.last_attempt_at
        """,
        (max_attempts,),
    ).fetchall()


def record_fetch_failure(conn: sqlite3.Connection, accession_no: str, error: str,
                         at: str | None = None) -> int:
    """Count one more failed fetch for a filing. Returns the attempts so far."""
    row = conn.execute(
        """
        INSERT INTO fetch_failures (accession_no, attempts, last_error, last_attempt_at) VALUES (?, 1, ?, ?)
        ON CONFLICT (accession_no) DO UPDATE SET
            attempts = attempts + 1, last_error = excluded.last_error, last_attempt_at = excluded.last_attempt_at
        RETURNING attempts
        """,
        (accession_no, error, at or _utc_now()),
    ).fetchone()
    return int(row["attempts"])


def clear_fetch_failure(conn: sqlite3.Connection, accession_no: str) -> None:
    conn.execute("DELETE FROM fetch_failures WHERE accession_no = ?", (accession_no,))


def fetch_given_up(conn: sqlite3.Connection, accessions: list[str] | None = None,
                   max_attempts: int = MAX_FETCH_ATTEMPTS) -> list[sqlite3.Row]:
    """Filings ingest no longer fetches, oldest attempt first; with `accessions`, only those among them.
    Filings stored since (fetched by hand, say) are left out. Delete a row from fetch_failures to retry one.
    """
    if accessions is not None and not accessions:
        return []
    only = f"AND ff.accession_no IN ({','.join('?' * len(accessions))})" if accessions else ""
    return conn.execute(
        f"""
        SELECT ff.* FROM fetch_failures ff LEFT JOIN filings f USING (accession_no)
        WHERE f.accession_no IS NULL AND ff.attempts >= ? {only}
        ORDER BY ff.last_attempt_at
        """,
        (max_attempts, *(accessions or ())),
    ).fetchall()


def pending_briefs(conn: sqlite3.Connection, since_iso: str | None = None) -> list[sqlite3.Row]:
    """Filings routed 'now' with no brief yet (a blank brief_text, saved before briefs were checked,
    counts as none). With `since_iso`, only those EDGAR accepted at or after it:
    a backfill or reroute marks old filings 'now' too, and those are not news any more.
    """
    return conn.execute(
        """
        SELECT f.accession_no, f.ticker, f.company, f.items, f.accepted_at, f.url,
               s.materiality, s.event, s.direction, s.surprise, s.materiality_confidence
        FROM briefs b JOIN filings f USING (accession_no) JOIN scores s USING (accession_no)
        WHERE b.route = 'now' AND trim(COALESCE(b.brief_text, ''), ' ' || char(9, 10, 13)) = ''
          AND (? IS NULL OR julianday(f.accepted_at) >= julianday(?))
        ORDER BY s.materiality DESC
        """,
        (since_iso, since_iso),
    ).fetchall()


def digest_since(conn: sqlite3.Connection, since_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT f.accession_no, f.ticker, f.company, f.url, s.materiality, s.event, s.direction
        FROM briefs b JOIN filings f USING (accession_no) JOIN scores s USING (accession_no)
        WHERE b.route IN ('now', 'digest') AND julianday(f.accepted_at) >= julianday(?)
        ORDER BY s.materiality DESC
        """,
        (since_iso,),
    ).fetchall()
