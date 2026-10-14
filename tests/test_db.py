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

    db.save_brief(conn, a.accession_no, "Big news.", "2026-10-13T20:10:00Z")
    assert db.pending_briefs(conn) == []


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
