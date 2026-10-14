import json

from tape import db
from tape.cli import format_digest, main
from tests.conftest import make_filing, make_score


def _seed(path):
    conn = db.connect(path)
    f = make_filing(1, accepted_at="2999-01-01T09:00:00-05:00")
    db.upsert_filing(conn, f)
    db.upsert_score(conn, make_score(f.accession_no, 4.4, event="mna"))
    db.set_route(conn, f.accession_no, "now")
    conn.commit()
    conn.close()
    return f.accession_no


def test_pending_brief_digest(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    acc = _seed(path)

    assert main(["pending"]) == 0
    pending = json.loads(capsys.readouterr().out)
    assert [p["accession_no"] for p in pending] == [acc]

    assert main(["brief", acc, "--text", "Example Corp agreed to be acquired."]) == 0
    capsys.readouterr()
    main(["pending"])
    assert json.loads(capsys.readouterr().out) == []

    main(["digest", "--hours", "24"])
    out = capsys.readouterr().out
    assert "4.4  EXMP  mna" in out


def test_format_digest_empty():
    assert "nothing material" in format_digest([])
