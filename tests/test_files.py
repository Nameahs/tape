import os

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
