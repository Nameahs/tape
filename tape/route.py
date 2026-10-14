"""Decide what happens to a scored filing: brief it now, save it for the digest, or just log it."""

from __future__ import annotations

from tape.config import Thresholds
from tape.db import ScoreRow

NOW, DIGEST, LOG = "now", "digest", "log"


def route(s: ScoreRow, t: Thresholds) -> str:
    if s.boilerplate > t.boilerplate:
        return LOG
    if s.materiality >= t.brief_now:
        return NOW
    if s.materiality >= t.brief_now_with_surprise and s.surprise > t.surprise:
        return NOW
    if s.materiality >= t.digest:
        return DIGEST
    return LOG
