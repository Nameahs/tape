"""A process-wide lock so two pipeline passes never work on the same store at once."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class PipelineBusy(Exception):
    """Another process holds the pipeline lock for this store."""


def lock_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".lock")


@contextmanager
def pipeline_lock(db_path: Path) -> Iterator[None]:
    """Hold an exclusive lock on `<db>.lock` for the duration of the block.

    The lock is non-blocking: if another `tape run` or `tape backfill` holds it, PipelineBusy is
    raised at once instead of queueing behind a pass that may take an hour. flock is released by
    the OS when the process exits, so a crash or a killed job can't leave a stale lock behind.
    """
    path = lock_path(Path(db_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise PipelineBusy(str(path)) from e
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
