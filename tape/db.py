"""SQLite store. Every table is keyed on the EDGAR accession number so each stage is safe to rerun."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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
]


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
    updated = conn.execute(
        "UPDATE briefs SET brief_text = ?, sent_at = ? WHERE accession_no = ?",
        (text, sent_at, accession_no),
    ).rowcount
    if not updated:
        raise KeyError(f"no routed filing {accession_no}")


def unscored(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT f.* FROM filings f LEFT JOIN scores s USING (accession_no)
        WHERE s.accession_no IS NULL AND f.clean_text IS NOT NULL
        ORDER BY f.accepted_at
        """
    ).fetchall()


def pending_briefs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT f.accession_no, f.ticker, f.company, f.items, f.accepted_at, f.url,
               s.materiality, s.event, s.direction, s.surprise
        FROM briefs b JOIN filings f USING (accession_no) JOIN scores s USING (accession_no)
        WHERE b.route = 'now' AND b.brief_text IS NULL
        ORDER BY s.materiality DESC
        """
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
