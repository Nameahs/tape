import subprocess
import sys

import pytest

from tape.lock import PipelineBusy, lock_path, pipeline_lock


def test_lock_file_sits_next_to_the_database(tmp_path):
    assert lock_path(tmp_path / "tape.db") == tmp_path / "tape.db.lock"


def test_second_holder_is_refused_until_released(tmp_path):
    db = tmp_path / "data" / "tape.db"
    with pipeline_lock(db):
        assert lock_path(db).exists()
        with pytest.raises(PipelineBusy), pipeline_lock(db):
            pass
    with pipeline_lock(db):
        pass


def test_lock_is_released_when_the_block_raises(tmp_path):
    db = tmp_path / "tape.db"
    with pytest.raises(RuntimeError), pipeline_lock(db):
        raise RuntimeError("pass failed")
    with pipeline_lock(db):
        pass


def test_lock_is_held_across_processes(tmp_path):
    db = tmp_path / "tape.db"
    probe = (
        "import sys\n"
        "from tape.lock import PipelineBusy, pipeline_lock\n"
        "try:\n"
        "    with pipeline_lock(sys.argv[1]):\n"
        "        pass\n"
        "except PipelineBusy:\n"
        "    sys.exit(3)\n"
    )
    with pipeline_lock(db):
        held = subprocess.run([sys.executable, "-c", probe, str(db)])
    free = subprocess.run([sys.executable, "-c", probe, str(db)])
    assert held.returncode == 3
    assert free.returncode == 0
