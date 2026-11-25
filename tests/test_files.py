import os
import subprocess
import sys

import pytest

from tape.files import write_atomic


def test_write_atomic_creates_folders_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "a" / "b.txt"
    write_atomic(path, "hello")
    assert path.read_text() == "hello"
    assert os.listdir(path.parent) == ["b.txt"]


def test_failed_write_keeps_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "b.txt"
    path.write_text("old")

    def fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        write_atomic(path, "new")
    assert path.read_text() == "old"
    assert os.listdir(tmp_path) == ["b.txt"]


def test_write_atomic_syncs_the_data_before_the_rename(tmp_path, monkeypatch):
    path = tmp_path / "b.txt"
    calls = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        calls.append(("fsync", os.fstat(fd).st_size))
        real_fsync(fd)

    def replace(src, dst):
        calls.append(("replace", None))
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    write_atomic(path, "hello")
    assert calls[:2] == [("fsync", 5), ("replace", None)]
    assert path.read_text() == "hello"


def test_write_atomic_syncs_the_folder_after_the_rename(tmp_path, monkeypatch):
    path = tmp_path / "b.txt"
    synced = []
    real_fsync = os.fsync

    def fsync(fd):
        synced.append(os.path.samestat(os.fstat(fd), os.stat(tmp_path)))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    write_atomic(path, "hello")
    assert synced == [False, True]


def test_folder_that_cannot_be_synced_does_not_fail_the_write(tmp_path, monkeypatch):
    path = tmp_path / "b.txt"
    real_fsync = os.fsync

    def fsync(fd):
        if os.path.samestat(os.fstat(fd), os.stat(tmp_path)):
            raise OSError("Invalid argument")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    write_atomic(path, "hello")
    assert path.read_text() == "hello"
    assert os.listdir(tmp_path) == ["b.txt"]


def test_failed_fsync_keeps_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "b.txt"
    path.write_text("old")

    def fail(fd):
        raise OSError("I/O error")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="I/O error"):
        write_atomic(path, "new")
    assert path.read_text() == "old"
    assert os.listdir(tmp_path) == ["b.txt"]


def run_in_ascii_locale(code: str, *args: str) -> subprocess.CompletedProcess:
    """Run `code` in a fresh interpreter whose locale encoding is ASCII, the way a bare scheduled job can be."""
    env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    probe = subprocess.run([sys.executable, "-c", "import locale; print(locale.getencoding())"],
                           env=env, capture_output=True, text=True, check=True)
    if "utf" in probe.stdout.lower():
        pytest.skip("this platform has no non-UTF-8 C locale")
    return subprocess.run([sys.executable, "-c", code, *args], env=env, capture_output=True, text=True)


def test_write_atomic_writes_utf8_whatever_the_locale(tmp_path):
    path = tmp_path / "page.html"
    code = "import sys; from pathlib import Path; from tape.files import write_atomic; " \
           "write_atomic(Path(sys.argv[1]), 'Soci\\u00e9t\\u00e9 G\\u00e9n\\u00e9rale \\u2014 8-K')"
    result = run_in_ascii_locale(code, str(path))
    assert result.returncode == 0, result.stderr
    assert path.read_bytes() == "Société Générale — 8-K".encode()


def test_ticker_cache_with_non_ascii_titles_is_read_under_an_ascii_locale(tmp_path):
    # SEC's company_tickers.json carries raw UTF-8 titles. Decoding it as ASCII raised a ValueError the cache
    # reader treats as "no cache", so every pass downloaded the ticker file again.
    path = tmp_path / "tickers.json"
    path.write_bytes('{"0": {"cik_str": 1234, "ticker": "SGE", "title": "Société Générale"}}'.encode())
    code = "import sys; from pathlib import Path; from tape.edgar import _cached_tickers; " \
           "print(_cached_tickers(Path(sys.argv[1])))"
    result = run_in_ascii_locale(code, str(path))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "{'0000001234': 'SGE'}"


def test_eval_report_with_non_ascii_text_is_read_under_an_ascii_locale(tmp_path):
    path = tmp_path / "eval.json"
    path.write_bytes('{"n": 1, "rule_flagged": 0, "by_score": {}, "note": "Société — n/a"}'.encode())
    code = "import sys; from pathlib import Path; from tape.dashboard import _read_report; " \
           "print(_read_report(Path(sys.argv[1])) is not None)"
    result = run_in_ascii_locale(code, str(path))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True", result.stderr
