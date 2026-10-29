import json

import httpx
import pytest

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


def test_pending_skips_old_filings(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    conn = db.connect(path)
    old = make_filing(1, accepted_at="2020-01-02T10:00:00-05:00")
    db.upsert_filing(conn, old)
    db.upsert_score(conn, make_score(old.accession_no, 4.9))
    db.set_route(conn, old.accession_no, "now")
    conn.commit()
    conn.close()

    assert main(["pending"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["pending", "--max-age-hours", "100000"]) == 0
    assert [p["accession_no"] for p in json.loads(capsys.readouterr().out)] == [old.accession_no]


def test_pending_rejects_zero_max_age(capsys):
    with pytest.raises(SystemExit):
        main(["pending", "--max-age-hours", "0"])


def test_format_digest_empty():
    assert "nothing material" in format_digest([])


def test_stats_command(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    _seed(path)

    assert main(["stats", "--days", "3"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:3] == ["day", "filings", "scored"]
    assert len(lines) == 1 + 3 + 2  # header, one row per day, total, spend


def test_stats_rejects_zero_days(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TAPE_DB_PATH", str(tmp_path / "t.db"))
    with pytest.raises(SystemExit):
        main(["stats", "--days", "0"])
    assert "must be at least 1" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["backfill", "--days", "0"],
        ["backfill", "--days", "-5"],
        ["digest", "--hours", "0"],
        ["digest", "--hours", "-24"],
    ],
)
def test_rejects_non_positive_windows_before_touching_store(argv, tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert "must be at least 1" in capsys.readouterr().err
    assert not path.exists()


def test_jev_client_gets_retry_settings(monkeypatch):
    from tape import cli, score
    from tape.config import Settings

    seen = {}
    monkeypatch.setattr(score, "default_client", lambda retry=None: seen.setdefault("retry", retry))
    cli._jev(Settings(jev_max_retries=1, jev_retry_budget_s=10.0))
    assert seen["retry"].max_retries == 1 and seen["retry"].timeout == 10.0


def test_digest_send_delivers_printed_text(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    _seed(path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    sent = []
    monkeypatch.setattr("tape.notify.send_telegram", lambda text: sent.append(text) or 1)

    assert main(["digest", "--send"]) == 0
    out = capsys.readouterr().out
    assert sent == [out.rstrip("\n")]


def test_digest_send_failure_still_prints(tmp_path, monkeypatch, capsys, caplog):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    _seed(path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")

    def boom(text):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("tape.notify.send_telegram", boom)

    assert main(["digest", "--send"]) == 1
    assert "4.4  EXMP  mna" in capsys.readouterr().out
    assert "digest not sent" in caplog.text
    assert "connection refused" in caplog.text


def test_digest_send_without_credentials(tmp_path, monkeypatch, capsys, caplog):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    _seed(path)
    called = []
    monkeypatch.setattr("tape.notify.send_telegram", lambda text: called.append(text))

    assert main(["digest", "--send"]) == 1
    assert "4.4  EXMP  mna" in capsys.readouterr().out
    assert called == []
    assert "TELEGRAM_CHAT_ID not set" in caplog.text


@pytest.mark.parametrize("text", ["", "  \n\t"])
def test_brief_rejects_empty_text(tmp_path, monkeypatch, capsys, text):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    acc = _seed(path)

    assert main(["brief", acc, "--text", text]) == 1
    assert "empty brief" in capsys.readouterr().err
    main(["pending"])
    assert [p["accession_no"] for p in json.loads(capsys.readouterr().out)] == [acc]


def test_brief_rejects_empty_stdin(tmp_path, monkeypatch, capsys):
    import io

    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    acc = _seed(path)
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))

    assert main(["brief", acc]) == 1
    capsys.readouterr()
    main(["pending"])
    assert [p["accession_no"] for p in json.loads(capsys.readouterr().out)] == [acc]


def test_brief_reads_stdin(tmp_path, monkeypatch, capsys):
    import io

    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    acc = _seed(path)
    monkeypatch.setattr("sys.stdin", io.StringIO("  Example Corp raised guidance.\n"))

    assert main(["brief", acc]) == 0
    conn = db.connect(path)
    row = conn.execute("SELECT brief_text FROM briefs WHERE accession_no = ?", (acc,)).fetchone()
    assert row["brief_text"] == "Example Corp raised guidance."


def test_brief_unknown_accession_is_one_line_error(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    acc = _seed(path)

    assert main(["brief", "0000000000-99-999999", "--text", "Example Corp raised guidance."]) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "error: no routed filing 0000000000-99-999999; nothing saved"
    assert "Traceback" not in captured.err
    main(["pending"])
    assert [p["accession_no"] for p in json.loads(capsys.readouterr().out)] == [acc]


def test_evaluate_with_too_few_outcomes_exits_cleanly(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    _seed(path)
    out = tmp_path / "eval.json"

    assert main(["evaluate", "--skip-prices", "--out", str(out)]) == 0
    assert "not enough outcomes to evaluate yet: 0 of 20" in capsys.readouterr().out
    assert not out.exists()


def test_evaluate_with_too_few_outcomes_keeps_earlier_report(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    _seed(path)
    out = tmp_path / "eval.json"
    out.write_text('{"n": 25}')

    assert main(["evaluate", "--skip-prices", "--out", str(out)]) == 0
    assert out.read_text() == '{"n": 25}'
