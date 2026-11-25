"""File helpers shared across the package."""

from __future__ import annotations

import os
from pathlib import Path


def write_atomic(path: Path, text: str) -> None:
    """Write via a temp file and rename, so readers and a killed job never leave half a file at path.

    Always UTF-8: the dashboard declares that charset, and a scheduled job's locale may be ASCII.

    Raises OSError on failure, with any existing file at path untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            # Without this the rename can reach the journal before the data, and a crash leaves path empty.
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def _fsync_dir(folder: Path) -> None:
    """Make a rename in folder durable. Best effort: some platforms and filesystems can't open or sync a folder."""
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
