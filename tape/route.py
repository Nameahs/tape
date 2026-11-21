"""Decide what happens to a scored filing: brief it now, save it for the digest, or just log it."""

from __future__ import annotations

from typing import Protocol

from tape.config import Thresholds

NOW, DIGEST, LOG = "now", "digest", "log"


class Routable(Protocol):
    """The parts of a score routing reads. A ScoreRow is one; so is anything holding just these columns."""

    @property
    def materiality(self) -> float: ...
    @property
    def surprise(self) -> float: ...
    @property
    def boilerplate(self) -> float: ...
    @property
    def event(self) -> str | None: ...


def route(s: Routable, t: Thresholds) -> str:
    t = t.for_event(s.event)
    if s.boilerplate > t.boilerplate:
        return LOG
    if s.materiality >= t.brief_now:
        return NOW
    if s.materiality >= t.brief_now_with_surprise and s.surprise > t.surprise:
        return NOW
    if s.materiality >= t.digest:
        return DIGEST
    return LOG
