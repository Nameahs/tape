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
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
