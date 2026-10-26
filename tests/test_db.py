import sqlite3

import pytest

from tape import db
from tests.conftest import make_filing, make_score


def test_upsert_is_idempotent(conn):
    f = make_filing()
    db.upsert_filing(conn, f)
    db.upsert_filing(conn, f)
    assert conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 1


def test_upsert_keeps_existing_text_when_new_is_missing(conn):
    db.upsert_filing(conn, make_filing())
    db.upsert_filing(conn, make_filing(clean_text=None, ticker=None))
    row = conn.execute("SELECT clean_text, ticker FROM filings").fetchone()
    assert row["clean_text"] and row["ticker"] == "EXMP"


def test_known_accessions(conn):
    db.upsert_filing(conn, make_filing(1))
    assert db.known_accessions(conn, [make_filing(1).accession_no, "nope"]) == {make_filing(1).accession_no}
    assert db.known_accessions(conn, []) == set()


def test_unscored_skips_missing_and_blank_text(conn):
    keep = make_filing(1)
    for f in (keep, make_filing(2, clean_text=None), make_filing(3, clean_text=""), make_filing(4, clean_text=" \n ")):
        db.upsert_filing(conn, f)
    assert [r["accession_no"] for r in db.unscored(conn)] == [keep.accession_no]


def test_unscored_and_pending_flow(conn):
    a, b = make_filing(1), make_filing(2)
    db.upsert_filing(conn, a)
    db.upsert_filing(conn, b)
    assert len(db.unscored(conn)) == 2

    db.upsert_score(conn, make_score(a.accession_no, 4.6))
    db.upsert_score(conn, make_score(b.accession_no, 1.2))
    assert db.unscored(conn) == []

    db.set_route(conn, a.accession_no, "now")
    db.set_route(conn, b.accession_no, "log")
    pending = db.pending_briefs(conn)
    assert [r["accession_no"] for r in pending] == [a.accession_no]
    assert "materiality_confidence" in pending[0].keys()

    db.save_brief(conn, a.accession_no, "Big news.", "2026-10-13T20:10:00Z")
    assert db.pending_briefs(conn) == []


def test_pending_briefs_since_skips_old_filings(conn):
    old = make_filing(1, accepted_at="2026-07-01T10:00:00-04:00")
    new = make_filing(2, accepted_at="2026-10-13T16:05:00-04:00")
    for f in (old, new):
        db.upsert_filing(conn, f)
        db.upsert_score(conn, make_score(f.accession_no, 4.8))
        db.set_route(conn, f.accession_no, "now")

    assert len(db.pending_briefs(conn)) == 2
    # Cutoff in UTC, filing times in ET: 20:05Z is exactly the newer filing's acceptance time.
    assert [r["accession_no"] for r in db.pending_briefs(conn, "2026-10-13T20:05:00+00:00")] == [new.accession_no]
    assert db.pending_briefs(conn, "2026-10-13T20:05:01+00:00") == []


def test_save_brief_requires_route(conn):
    db.upsert_filing(conn, make_filing())
    with pytest.raises(KeyError):
        db.save_brief(conn, make_filing().accession_no, "x", None)


def test_route_constraint(conn):
    db.upsert_filing(conn, make_filing())
    with pytest.raises(sqlite3.IntegrityError):
        db.set_route(conn, make_filing().accession_no, "maybe")


def test_digest_window_compares_instants_not_strings(conn):
    # 2026-10-13 21:00 ET is 2026-10-14 01:00 UTC, so it is inside a window starting 00:30 UTC,
    # even though the raw string "2026-10-13T21:00" sorts before "2026-10-14T00:30".
    f = make_filing(1, accepted_at="2026-10-13T21:00:00-04:00")
    db.upsert_filing(conn, f)
    db.upsert_score(conn, make_score(f.accession_no, 3.0))
    db.set_route(conn, f.accession_no, "digest")
    assert len(db.digest_since(conn, "2026-10-14T00:30:00+00:00")) == 1
    assert db.digest_since(conn, "2026-10-14T01:30:00+00:00") == []


def test_new_database_is_at_latest_schema_version(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(scores)")}
    assert {"materiality_confidence", "event_confidence", "direction_confidence", "materiality_probs"} <= cols


def test_migrates_a_version_zero_database_in_place(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA)  # what a workspace created before any migrations looks like
    old.execute(
        "INSERT INTO filings (accession_no, cik, company, form, accepted_at, url) "
        "VALUES ('a', '1', 'X', '8-K', 't', 'u')"
    )
    old.execute(
        "INSERT INTO scores (accession_no, model, materiality, event, direction, surprise, boilerplate, raw_json) "
        "VALUES ('a', 'm', 2.0, 'other', 'neutral', 0.1, 0.2, '{}')"
    )
    old.commit()
    old.close()

    conn = db.connect(path)
    row = conn.execute("SELECT materiality, materiality_confidence FROM scores").fetchone()
    assert (row["materiality"], row["materiality_confidence"]) == (2.0, None)
    assert db.migrate(conn) == len(db.MIGRATIONS)  # idempotent on reconnect
    conn.close()
    assert db.connect(path).execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_score_confidence_round_trips(conn):
    f = make_filing()
    db.upsert_filing(conn, f)
    db.upsert_score(conn, make_score(f.accession_no, materiality_confidence=0.42, event_confidence=0.9,
                                     direction_confidence=0.6, materiality_probs="[0.1, 0.2, 0.4, 0.2, 0.1]"))
    row = conn.execute("SELECT * FROM scores").fetchone()
    assert (row["materiality_confidence"], row["event_confidence"], row["direction_confidence"]) == (0.42, 0.9, 0.6)
    assert row["materiality_probs"] == "[0.1, 0.2, 0.4, 0.2, 0.1]"


def test_run_lifecycle(conn):
    run_id = db.start_run(conn, "pass", started_at="2026-10-15T14:00:00Z")
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert (row["kind"], row["finished_at"], row["scored"]) == ("pass", None, 0)  # in flight, or crashed

    db.finish_run(conn, run_id, discovered=5, fetched=4, fetch_errors=1, scored=4,
                  routes={"now": 1, "log": 3}, cost_usd=0.012, finished_at="2026-10-15T14:00:09Z")
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert dict(row) | {"id": 0} == {
        "id": 0, "kind": "pass", "started_at": "2026-10-15T14:00:00Z", "finished_at": "2026-10-15T14:00:09Z",
        "discovered": 5, "fetched": 4, "fetch_errors": 1, "scored": 4, "routed_now": 1, "routed_digest": 0,
        "routed_log": 3, "cost_usd": 0.012, "error": None,
    }


def test_run_kind_constraint(conn):
    with pytest.raises(sqlite3.IntegrityError):
        db.start_run(conn, "nightly")


def test_score_failures_count_up_and_stop_unscored_at_the_limit(conn):
    stuck, fine = make_filing(1), make_filing(2)
    db.upsert_filing(conn, stuck)
    db.upsert_filing(conn, fine)
    for n in range(1, db.MAX_SCORE_ATTEMPTS):
        assert db.record_score_failure(conn, stuck.accession_no, "HTTPStatusError: 400") == n
        assert len(db.unscored(conn)) == 2  # still retried below the limit
    db.record_score_failure(conn, stuck.accession_no, "KeyError: 'materiality'", at="2026-10-26T15:00:00Z")

    assert [r["accession_no"] for r in db.unscored(conn)] == [fine.accession_no]
    [row] = db.given_up(conn)
    assert row["accession_no"] == stuck.accession_no
    assert row["attempts"] == db.MAX_SCORE_ATTEMPTS
    assert row["last_error"] == "KeyError: 'materiality'"
    assert row["last_attempt_at"] == "2026-10-26T15:00:00Z"


def test_clear_score_failure_puts_a_filing_back_in_the_queue(conn):
    f = make_filing()
    db.upsert_filing(conn, f)
    for _ in range(db.MAX_SCORE_ATTEMPTS):
        db.record_score_failure(conn, f.accession_no, "boom")
    assert db.unscored(conn) == []
    db.clear_score_failure(conn, f.accession_no)
    assert [r["accession_no"] for r in db.unscored(conn)] == [f.accession_no]
    assert db.given_up(conn) == []


def test_given_up_ignores_filings_scored_since(conn):
    f = make_filing()
    db.upsert_filing(conn, f)
    for _ in range(db.MAX_SCORE_ATTEMPTS):
        db.record_score_failure(conn, f.accession_no, "boom")
    db.upsert_score(conn, make_score(f.accession_no, 2.0))  # e.g. rescored by hand
    assert db.given_up(conn) == []
