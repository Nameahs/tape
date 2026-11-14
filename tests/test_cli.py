import json
import logging
import sys
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import httpx
import pandas as pd
import pytest
import respx

from tape import db
from tape.cli import EXIT_TEMPFAIL, format_digest, format_reroute, main
from tape.edgar import FEED_URL, EdgarBlockedError
from tape.evaluate import PRICE_LOOKBACK_DAYS
from tape.notify import API
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
        ["evaluate", "--days", "0"],
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


def _config_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TAPE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TAPE_USER_AGENT", "Tape test@example.com")
    monkeypatch.setenv("TYPESAFE_API_KEY", "key")


@pytest.mark.parametrize(
    ("argv", "unset", "setenv", "expected"),
    [
        (["stats"], None, ("TAPE_MAX_CHARS", "abc"), "error: TAPE_MAX_CHARS: expected a whole number, got 'abc'"),
        (["stats"], None, ("TAPE_BRIEF_NOW", "nan"), "error: TAPE_BRIEF_NOW: expected a finite number, got 'nan'"),
        (["run"], None, ("TAPE_USER_AGENT", "Tape"), "error: TAPE_USER_AGENT must include a contact email"),
        (["backfill", "--days", "1"], None, ("TAPE_USER_AGENT", "Tape"), "error: TAPE_USER_AGENT must include"),
        (["run"], "TYPESAFE_API_KEY", None, "error: No API key was provided"),
    ],
)
def test_bad_configuration_is_one_line_and_status_2(argv, unset, setenv, expected, tmp_path, monkeypatch, capsys):
    _config_env(tmp_path, monkeypatch)
    if unset:
        monkeypatch.delenv(unset)
    if setenv:
        monkeypatch.setenv(*setenv)
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert err.startswith(expected)
    assert len(err.strip().splitlines()) == 1


def test_verbose_keeps_the_configuration_traceback(tmp_path, monkeypatch, caplog):
    _config_env(tmp_path, monkeypatch)
    monkeypatch.setenv("TAPE_MAX_CHARS", "abc")
    caplog.set_level(logging.INFO)
    assert main(["-v", "stats"]) == 2
    assert "Traceback" in caplog.text


def test_other_value_errors_are_not_mistaken_for_configuration(tmp_path, monkeypatch):
    _config_env(tmp_path, monkeypatch)
    assert main(["init"]) == 0

    def broken(*_args):
        raise ValueError("a bug")

    monkeypatch.setattr("tape.stats.collect", broken)
    with pytest.raises(ValueError, match="a bug"):
        main(["stats"])


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


def _digested(path):
    with closing(db.connect(path)) as conn:
        return {r["accession_no"]: r["digested_at"] for r in conn.execute("SELECT * FROM briefs")}


def _send_digest(monkeypatch, send):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr("tape.notify.send_telegram", send)
    return main(["digest", "--send"])


def test_digest_send_marks_what_it_sent_so_the_next_digest_skips_it(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    acc = _seed(path)

    assert _send_digest(monkeypatch, lambda text: 1) == 0
    assert _digested(path)[acc] is not None
    capsys.readouterr()

    sent = []
    assert _send_digest(monkeypatch, sent.append) == 0
    assert sent == ["Tape digest: nothing material today."]


def test_digest_send_failure_leaves_filings_for_the_next_digest(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    acc = _seed(path)

    def boom(text):
        raise httpx.ConnectError("connection refused")

    assert _send_digest(monkeypatch, boom) == 1
    assert _digested(path) == {acc: None}
    capsys.readouterr()

    sent = []
    assert _send_digest(monkeypatch, sent.append) == 0
    assert "4.4  EXMP  mna" in sent[0]


def test_digest_send_without_credentials_marks_nothing(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    acc = _seed(path)
    assert main(["digest", "--send"]) == 1
    assert _digested(path) == {acc: None}


def test_digest_marks_only_with_mark(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    acc = _seed(path)

    assert main(["digest"]) == 0
    assert _digested(path) == {acc: None}
    assert main(["digest", "--mark"]) == 0
    assert "4.4  EXMP  mna" in capsys.readouterr().out
    assert _digested(path)[acc] is not None

    assert main(["digest"]) == 0
    assert capsys.readouterr().out == "Tape digest: nothing material today.\n"


def test_digest_lists_a_filing_routed_after_the_previous_digest(tmp_path, monkeypatch, capsys):
    # Accepted two days ago, before the last digest went out, but only routed since: it used to fall
    # between the two digests' 24-hour windows.
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    with closing(db.connect(path)) as conn:
        f = make_filing(1, accepted_at=(datetime.now(UTC) - timedelta(hours=48)).isoformat())
        db.upsert_filing(conn, f)
        db.upsert_score(conn, make_score(f.accession_no, 3.2, event="guidance"))
        db.set_route(conn, f.accession_no, "digest")
        conn.commit()

    assert main(["digest"]) == 0
    assert "3.2  EXMP  guidance" in capsys.readouterr().out
    assert main(["digest", "--hours", "24"]) == 0
    assert "nothing material" in capsys.readouterr().out


@respx.mock
@pytest.mark.parametrize("status, code", [(200, 0), (401, 1)])
def test_digest_send_keeps_the_token_out_of_verbose_logs(tmp_path, monkeypatch, caplog, status, code):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    _seed(path)
    token = "123456:SECRET"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    respx.post(API.format(token=token)).respond(status, json={"ok": status == 200})
    caplog.set_level(logging.INFO)

    assert main(["-v", "digest", "--send"]) == code
    assert token not in caplog.text


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


@pytest.mark.parametrize("token", ["", "   ", "\n"])
def test_digest_send_treats_blank_token_as_unset(tmp_path, monkeypatch, capsys, caplog, token):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    _seed(path)
    called = []
    monkeypatch.setattr("tape.notify.send_telegram", lambda text: called.append(text))

    assert main(["digest", "--send"]) == 1
    assert called == []
    assert "TELEGRAM_BOT_TOKEN not set" in caplog.text


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


def test_dashboard_survives_a_truncated_report(tmp_path, monkeypatch, capsys, caplog):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    _seed(path)
    report = tmp_path / "eval.json"
    report.write_text('{"n": 25, "gap')
    out = tmp_path / "dash.html"

    assert main(["dashboard", "--out", str(out), "--report", str(report)]) == 0
    assert out.read_text().startswith("<!doctype html>")
    assert "ignoring unreadable eval report" in caplog.text


def _seed_outcomes(path, n=25):
    conn = db.connect(path)
    for i in range(n):
        f = make_filing(100 + i)
        db.upsert_filing(conn, f)
        db.upsert_score(conn, make_score(f.accession_no, 1.0 + i % 5))
        ar = 0.01 * (1 + i % 5)
        conn.execute("INSERT INTO outcomes VALUES (?, '2026-10-06', ?, 0.0, ?, ?)", (f.accession_no, ar, ar, ar))
    conn.commit()
    conn.close()


class _RaisingPrices:
    def closes(self, tickers, start, end):
        raise httpx.ConnectError("yahoo is down")


class _EmptyPrices:
    def closes(self, tickers, start, end):
        return pd.DataFrame()


class _NoMarketPrices:
    def closes(self, tickers, start, end):
        return pd.DataFrame({"EXMP": [50.0, 45.0]}, index=[date(2026, 10, 5), date(2026, 10, 6)])


@pytest.mark.parametrize("source", [_RaisingPrices, _EmptyPrices, _NoMarketPrices])
def test_evaluate_reports_from_stored_outcomes_when_prices_fail(source, tmp_path, monkeypatch, capsys, caplog):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    _seed(path)  # scored and unpriced, so the run has to ask for prices
    _seed_outcomes(path)
    monkeypatch.setattr("tape.evaluate.YFinancePrices", source)
    out = tmp_path / "eval.json"

    with caplog.at_level(logging.WARNING, logger="tape.cli"):
        assert main(["evaluate", "--out", str(out)]) == 0

    assert "prices unavailable; no outcomes added" in capsys.readouterr().out
    assert "price download failed" in caplog.text
    assert json.loads(out.read_text())["n"] == 25


def test_evaluate_without_yfinance_is_a_configuration_error(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    _seed(path)
    monkeypatch.setitem(sys.modules, "yfinance", None)  # makes the import raise ImportError

    assert main(["evaluate", "--out", str(tmp_path / "eval.json")]) == 2

    captured = capsys.readouterr()
    assert captured.err.startswith("error: yfinance is not installed")
    assert "prices unavailable" not in captured.out


class _Stop(Exception):
    pass


@pytest.mark.parametrize(("argv", "expected"), [([], PRICE_LOOKBACK_DAYS), (["--days", "140"], 140)])
def test_evaluate_passes_lookback_to_compute_outcomes(argv, expected, tmp_path, monkeypatch):
    monkeypatch.setenv("TAPE_DB_PATH", str(tmp_path / "t.db"))
    assert main(["init"]) == 0
    seen = {}

    def fake_compute(conn, prices, lookback_days):
        seen["days"] = lookback_days
        raise _Stop

    monkeypatch.setattr("tape.evaluate.compute_outcomes", fake_compute)
    with pytest.raises(_Stop):
        main(["evaluate", *argv])
    assert seen["days"] == expected


@pytest.mark.parametrize("argv", [["run"], ["backfill", "--days", "1"]])
def test_edgar_commands_require_a_contact_user_agent(argv, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TAPE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("TAPE_USER_AGENT", raising=False)

    def no_network(*_a, **_k):
        raise AssertionError("EDGAR should not be contacted")

    monkeypatch.setattr("tape.edgar.Edgar.get", no_network)
    assert main(argv) == 2
    assert "TAPE_USER_AGENT" in capsys.readouterr().err


def test_commands_without_edgar_ignore_the_user_agent(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    monkeypatch.delenv("TAPE_USER_AGENT", raising=False)
    _seed(path)
    assert main(["stats"]) == 0
    assert main(["pending"]) == 0
    assert main(["digest"]) == 0


@pytest.mark.parametrize(("argv", "expected"), [(["run"], 0), (["backfill", "--days", "1"], 1)])
def test_pipeline_commands_skip_when_another_pass_holds_the_lock(argv, expected, tmp_path, monkeypatch,
                                                                  capsys):
    from tape.lock import pipeline_lock

    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))

    def no_pass(*_a, **_k):
        raise AssertionError("no EDGAR or Jev work while another pass holds the lock")

    monkeypatch.setattr("tape.cli._edgar", no_pass)
    monkeypatch.setattr("tape.cli._jev", no_pass)
    with pipeline_lock(path):
        assert main(argv) == expected
    assert capsys.readouterr().err.strip() == (
        "another tape run or backfill is in progress; skipping this pass")


def test_pipeline_lock_is_released_after_a_pass_that_raises(tmp_path, monkeypatch):
    from tape.lock import pipeline_lock

    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))

    def boom(*_a, **_k):
        raise RuntimeError("EDGAR exploded")

    monkeypatch.setattr("tape.cli._edgar", boom)
    with pytest.raises(RuntimeError):
        main(["run"])
    with pipeline_lock(path):
        pass


def test_read_only_commands_run_while_a_pass_holds_the_lock(tmp_path, monkeypatch, capsys):
    from tape.lock import pipeline_lock

    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    acc = _seed(path)
    with pipeline_lock(path):
        assert main(["stats"]) == 0
        assert main(["pending"]) == 0
        assert main(["digest"]) == 0
        assert main(["brief", acc, "--text", "Example Corp agreed to be acquired."]) == 0


BLOCK_PAGE = "<html><body>Request Rate Threshold Exceeded</body></html>"


def _edgar_env(tmp_path, monkeypatch):
    from tests.fakes import FakeJev

    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    monkeypatch.setenv("TAPE_USER_AGENT", "Tape test@example.com")
    monkeypatch.setattr("tape.edgar.time.sleep", lambda _s: None)
    jev = FakeJev()
    monkeypatch.setattr("tape.cli._jev", lambda _settings: jev)
    return path


def _runs(path):
    conn = db.connect(path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id")]
    finally:
        conn.close()


@respx.mock
def test_blocked_backfill_is_one_line_and_tempfail(tmp_path, monkeypatch, capsys):
    path = _edgar_env(tmp_path, monkeypatch)
    respx.get(url__regex=r"/daily-index/").respond(403, text=BLOCK_PAGE)

    assert main(["backfill", "--days", "7"]) == EXIT_TEMPFAIL

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "EDGAR is blocking requests from this host; try again in about 10 minutes\n"
    (run,) = _runs(path)
    assert run["kind"] == "backfill" and run["blocked"] == 1
    assert run["error"].startswith("EdgarBlockedError:")


@respx.mock
def test_blocked_backfill_keeps_traceback_in_verbose_log(tmp_path, monkeypatch, capsys, caplog):
    _edgar_env(tmp_path, monkeypatch)
    respx.get(url__regex=r"/daily-index/").respond(403, text=BLOCK_PAGE)

    with caplog.at_level(logging.INFO, logger="tape.cli"):
        assert main(["-v", "backfill", "--days", "7"]) == EXIT_TEMPFAIL
    (record,) = [r for r in caplog.records if r.name == "tape.cli"]
    assert record.exc_info and record.exc_info[0] is EdgarBlockedError


def test_blocked_run_is_one_line_and_tempfail(tmp_path, monkeypatch, capsys):
    path = _edgar_env(tmp_path, monkeypatch)

    def blocked_pass(*_a, **_k):
        raise EdgarBlockedError("blocked", request=httpx.Request("GET", FEED_URL),
                                response=httpx.Response(403, text=BLOCK_PAGE))

    monkeypatch.setattr("tape.pipeline.run_pass", blocked_pass)
    assert main(["run"]) == EXIT_TEMPFAIL
    captured = capsys.readouterr()
    assert captured.out == "" and "EDGAR is blocking requests" in captured.err
    assert "Traceback" not in captured.err
    assert path.exists()


def test_other_errors_still_raise(tmp_path, monkeypatch):
    _edgar_env(tmp_path, monkeypatch)

    def broken(*_a, **_k):
        raise RuntimeError("bug")

    monkeypatch.setattr("tape.pipeline.backfill", broken)
    with pytest.raises(RuntimeError, match="bug"):
        main(["backfill", "--days", "1"])


@respx.mock
def test_run_with_feed_down_is_one_line_and_tempfail(tmp_path, monkeypatch, capsys):
    path = _edgar_env(tmp_path, monkeypatch)
    feed = respx.get(FEED_URL).respond(503)

    assert main(["run"]) == EXIT_TEMPFAIL

    assert feed.call_count > 1  # Edgar.get retried before giving up
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("EDGAR request failed (") and captured.err.endswith("; try again later\n")
    assert "Traceback" not in captured.err
    (run,) = _runs(path)
    assert run["kind"] == "pass" and run["error"].startswith("HTTPStatusError:") and not run["blocked"]


@respx.mock
def test_run_with_feed_down_scores_stored_filings_and_is_tempfail(tmp_path, monkeypatch, capsys):
    path = _edgar_env(tmp_path, monkeypatch)
    with closing(db.connect(path)) as conn:
        db.upsert_filing(conn, make_filing(1))
        conn.commit()
    respx.get(FEED_URL).mock(side_effect=httpx.ReadTimeout("timed out"))

    assert main(["run"]) == EXIT_TEMPFAIL

    assert capsys.readouterr().err.startswith("EDGAR request failed (timed out)")
    (run,) = _runs(path)
    assert run["scored"] == 1 and run["error"].startswith("ReadTimeout:")


@respx.mock
def test_backfill_with_edgar_unreachable_is_tempfail(tmp_path, monkeypatch, capsys):
    path = _edgar_env(tmp_path, monkeypatch)
    respx.get(url__regex=r"/daily-index/").mock(side_effect=httpx.ConnectTimeout("timed out"))

    assert main(["backfill", "--days", "7"]) == EXIT_TEMPFAIL

    assert "EDGAR request failed (timed out)" in capsys.readouterr().err
    (run,) = _runs(path)
    assert run["error"].startswith("ConnectTimeout:")


def _read_commands(tmp_path):
    out = tmp_path / "out"
    return [
        (["pending"], 0),
        (["digest"], 0),
        (["stats"], 0),
        (["reroute"], 0),
        (["evaluate", "--skip-prices", "--out", str(out / "eval.json")], 0),
        (["dashboard", "--out", str(out / "d.html"), "--report", str(out / "eval.json")], 0),
        # Reaches the store and reports the unknown filing, rather than the missing store.
        (["brief", "0000000001-26-000001", "--text", "x"], 1),
    ]


@pytest.mark.parametrize("index", range(7))
def test_read_commands_refuse_a_missing_store(index, tmp_path, monkeypatch, capsys):
    path = tmp_path / "wrong" / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    argv, _ = _read_commands(tmp_path)[index]

    assert main(argv) == 2

    captured = capsys.readouterr()
    assert captured.err == (f"error: no Tape store at {path}; check TAPE_DB_PATH, or run tape init to create one\n")
    assert captured.out == ""
    assert not path.parent.exists()


def test_digest_send_with_a_missing_store_tells_nobody(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TAPE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")

    with respx.mock(assert_all_called=False) as router:
        assert main(["digest", "--send"]) == 2
    assert not router.calls
    assert "nothing material" not in capsys.readouterr().out


@pytest.mark.parametrize("index", range(7))
def test_read_commands_work_after_init(index, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TAPE_DB_PATH", str(tmp_path / "t.db"))
    assert main(["init"]) == 0
    capsys.readouterr()
    argv, status = _read_commands(tmp_path)[index]

    assert main(argv) == status
    assert "no Tape store" not in capsys.readouterr().err


def test_init_creates_the_store_and_its_folder(tmp_path, monkeypatch, capsys):
    path = tmp_path / "data" / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))

    assert main(["init"]) == 0

    assert path.is_file()
    assert f"store ready at {path}" in capsys.readouterr().out


@respx.mock
def test_run_creates_a_missing_store(tmp_path, monkeypatch):
    path = _edgar_env(tmp_path, monkeypatch)
    assert not path.exists()
    respx.get(FEED_URL).mock(return_value=httpx.Response(503))
    # Status doesn't matter here: the store has to exist for the run to be recorded either way.
    main(["run"])
    assert path.is_file()


def test_format_reroute_lists_moves_then_totals():
    from collections import Counter

    from tape.pipeline import RerouteReport

    report = RerouteReport(
        totals=Counter(now=37, digest=412, log=8123),
        moves=Counter({("digest", "now"): 12, (None, "log"): 3, ("now", "digest"): 12}),
    )

    assert format_reroute(report).splitlines() == [
        "digest -> now: 12",
        "now -> digest: 12",
        "new -> log: 3",
        "27 changed; routes now: now 37, digest 412, log 8123",
    ]


def test_format_reroute_says_when_nothing_moved():
    from collections import Counter

    from tape.pipeline import RerouteReport

    out = format_reroute(RerouteReport(totals=Counter(log=4)))

    assert out.splitlines() == ["no routes changed", "0 changed; routes now: log 4"]


def test_reroute_prints_the_moves(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    _seed(path)  # materiality 4.4, routed 'now'
    monkeypatch.setenv("TAPE_BRIEF_NOW", "4.5")

    assert main(["reroute"]) == 0

    assert capsys.readouterr().out.splitlines()[0] == "now -> digest: 1"


def test_reroute_dry_run_prints_moves_without_writing(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    acc = _seed(path)
    monkeypatch.setenv("TAPE_BRIEF_NOW", "4.5")

    assert main(["reroute", "--dry-run"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[:2] == ["dry run, nothing written", "now -> digest: 1"]
    with closing(db.connect(path)) as conn:
        assert conn.execute("SELECT route FROM briefs WHERE accession_no = ?", (acc,)).fetchone()[0] == "now"


def test_reroute_waits_for_a_running_pass(tmp_path, monkeypatch, capsys):
    from tape.lock import pipeline_lock

    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    acc = _seed(path)  # materiality 4.4, routed 'now'
    monkeypatch.setenv("TAPE_BRIEF_NOW", "4.5")

    with pipeline_lock(path):
        assert main(["reroute"]) == 1

    assert capsys.readouterr().err.strip() == (
        "another tape run or backfill is in progress; nothing rerouted; try again when it finishes")
    with closing(db.connect(path)) as conn:
        assert conn.execute("SELECT route FROM briefs WHERE accession_no = ?", (acc,)).fetchone()[0] == "now"


def test_reroute_dry_run_needs_no_lock(tmp_path, monkeypatch, capsys):
    from tape.lock import pipeline_lock

    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    _seed(path)
    monkeypatch.setenv("TAPE_BRIEF_NOW", "4.5")

    with pipeline_lock(path):
        assert main(["reroute", "--dry-run"]) == 0

    assert capsys.readouterr().out.splitlines()[:2] == ["dry run, nothing written", "now -> digest: 1"]


def _given_up_store(path):
    conn = db.connect(path)
    stuck = make_filing(1)
    db.upsert_filing(conn, stuck)
    for _ in range(db.MAX_SCORE_ATTEMPTS):
        db.record_score_failure(conn, stuck.accession_no, "KeyError: 'materiality'", at="2026-11-05T15:00:00Z")
    gone = "0000000001-26-000099"
    for _ in range(db.MAX_FETCH_ATTEMPTS):
        db.record_fetch_failure(conn, gone, "HTTPStatusError: 404", at="2026-11-05T16:00:00Z")
    conn.commit()
    conn.close()
    return stuck.accession_no, gone


def _counts(path):
    with closing(db.connect(path)) as conn:
        return len(db.given_up(conn)), len(db.fetch_given_up(conn))


def test_retry_lists_given_up_filings_without_clearing_them(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    stuck, gone = _given_up_store(path)

    assert main(["retry"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"score  {stuck}  attempts=3  2026-11-05T15:00:00Z  KeyError: 'materiality'",
        f"fetch  {gone}  attempts=3  2026-11-05T16:00:00Z  HTTPStatusError: 404",
    ]
    assert main(["retry", "--accession", gone]) == 0
    assert capsys.readouterr().out.split()[:2] == ["fetch", gone]
    assert _counts(path) == (1, 1)


def test_retry_prints_each_given_up_filing_on_one_line(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    stuck, gone = _given_up_store(path)
    with closing(db.connect(path)) as conn, conn:
        # Stored before errors were collapsed on the way in.
        conn.execute("UPDATE fetch_failures SET last_error = ?",
                     ("HTTPStatusError: Client error '404 Not Found'\nFor more information check: https://x",))

    assert main(["retry"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert lines[1] == (f"fetch  {gone}  attempts=3  2026-11-05T16:00:00Z  "
                        "HTTPStatusError: Client error '404 Not Found' For more information check: https://x")


def test_retry_with_nothing_given_up(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    db.connect(path).close()
    assert main(["retry"]) == 0
    assert capsys.readouterr().out.strip() == "nothing given up"


@pytest.mark.parametrize(("flags", "message", "left"), [
    (["--score"], "cleared 1 to score;", (0, 1)),
    (["--fetch"], "cleared 1 to fetch;", (1, 0)),
    (["--score", "--fetch"], "cleared 1 to score, 1 to fetch;", (0, 0)),
])
def test_retry_clears_each_kind(flags, message, left, tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    _given_up_store(path)
    assert main(["retry", *flags]) == 0
    assert capsys.readouterr().out.startswith(message)
    assert _counts(path) == left


def test_retry_one_accession(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    stuck, gone = _given_up_store(path)
    assert main(["retry", "--score", "--fetch", "--accession", gone]) == 0
    assert capsys.readouterr().out.startswith("cleared 0 to score, 1 to fetch;")
    assert _counts(path) == (1, 0)


@pytest.mark.parametrize("flags", [[], ["--score"], ["--fetch"], ["--score", "--fetch"]])
def test_retry_unknown_accession_is_an_error(flags, tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    _given_up_store(path)
    assert main(["retry", *flags, "--accession", "0000000000-00-000000"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("error: 0000000000-00-000000 is not a given-up filing")
    assert _counts(path) == (1, 1)


def test_retry_needs_an_existing_store(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    assert main(["retry", "--score"]) == 2
    assert not path.exists()


@respx.mock
def test_digest_send_logs_why_telegram_refused(tmp_path, monkeypatch, caplog):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    _seed(path)
    token = "123456:SECRET"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    respx.post(API.format(token=token)).respond(
        400, json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})

    assert main(["digest", "--send"]) == 1
    assert "digest not sent" in caplog.text
    assert "Bad Request: chat not found" in caplog.text
    assert token not in caplog.text


@respx.mock
def test_digest_send_logs_a_partial_send(tmp_path, monkeypatch, caplog):
    path = tmp_path / "t.db"
    monkeypatch.setenv("TAPE_DB_PATH", str(path))
    _seed(path)
    token = "123456:SECRET"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr("tape.notify.chunks", lambda text: ["first", "second", "third"])
    respx.post(API.format(token=token)).mock(side_effect=[
        httpx.Response(200, json={"ok": True}),
        httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"}),
    ])

    assert main(["digest", "--send"]) == 1
    assert "digest partly sent (1 of 3 messages)" in caplog.text
    assert "bot was blocked by the user" in caplog.text
    assert "digest not sent" not in caplog.text
    assert token not in caplog.text
    # Which filings went out isn't known, so none is marked and the next digest lists them all again.
    assert all(v is None for v in _digested(path).values())
