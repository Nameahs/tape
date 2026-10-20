"""Settings, read from the environment so the same code runs locally and on Agent37."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return _parse_float(name, raw) if raw else default


def _int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name}: expected a whole number, got {raw!r}") from None
    if value < minimum:
        raise ValueError(f"{name}: must be at least {minimum}, got {value}")
    return value


def _parse_float(name: str, raw: str) -> float:
    """Parse a number from the environment, naming the variable if it isn't one."""
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name}: expected a number, got {raw!r}") from None


@dataclass(frozen=True)
class Thresholds:
    """Router cutoffs on the 1 to 5 materiality scale."""

    brief_now: float = 4.0
    brief_now_with_surprise: float = 3.0
    surprise: float = 0.7
    digest: float = 2.75
    boilerplate: float = 0.8
    # Per-event overrides, e.g. {"earnings_guidance": {"brief_now": 4.5}}. Scheduled, frequent
    # events such as earnings releases can need a stricter bar than the global one.
    overrides: Mapping[str, Mapping[str, float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        names = _cutoff_names()
        for event, values in self.overrides.items():
            unknown = set(values) - names
            if unknown:
                raise ValueError(f"unknown threshold(s) for {event}: {', '.join(sorted(unknown))}")

    def for_event(self, event: str | None) -> Thresholds:
        """The cutoffs that apply to one event label, with its overrides folded in."""
        values = self.overrides.get(event or "")
        if not values:
            return self
        return replace(self, overrides={}, **values)

    @classmethod
    def from_env(cls) -> Thresholds:
        return cls(
            brief_now=_float("TAPE_BRIEF_NOW", cls.brief_now),
            brief_now_with_surprise=_float("TAPE_BRIEF_NOW_WITH_SURPRISE", cls.brief_now_with_surprise),
            surprise=_float("TAPE_SURPRISE", cls.surprise),
            digest=_float("TAPE_DIGEST", cls.digest),
            boilerplate=_float("TAPE_BOILERPLATE", cls.boilerplate),
            overrides=_event_overrides(os.environ),
        )


def _cutoff_names() -> set[str]:
    return {f.name for f in fields(Thresholds) if f.name != "overrides"}


def _event_overrides(env: Mapping[str, str]) -> dict[str, dict[str, float]]:
    """Read TAPE_<CUTOFF>__<EVENT> variables, e.g. TAPE_BRIEF_NOW__EARNINGS_GUIDANCE=4.5."""
    from tape.score import EVENTS

    names = _cutoff_names()
    out: dict[str, dict[str, float]] = {}
    for key, raw in env.items():
        if not key.startswith("TAPE_") or "__" not in key or not raw.strip():
            continue
        cutoff, _, event = key.removeprefix("TAPE_").lower().partition("__")
        if cutoff not in names:
            continue
        if event not in EVENTS:
            raise ValueError(f"{key}: unknown event {event!r}; expected one of {', '.join(EVENTS)}")
        out.setdefault(event, {})[cutoff] = _parse_float(key, raw.strip())
    return out


@dataclass(frozen=True)
class Settings:
    db_path: Path = Path("data/tape.db")
    # The SEC asks automated clients to identify themselves with a contact address.
    user_agent: str = "Tape research bot (set TAPE_USER_AGENT)"
    jev_model: str = "jev-latest"
    # Characters of filing text sent to Jev. About 4 characters per token.
    max_chars: int = 12_000
    # USD per million input tokens. Output tokens are free.
    jev_input_price_per_m: float = 0.042
    # Retries per filing for transient Jev errors, and the total seconds spent on one filing.
    jev_max_retries: int = 4
    jev_retry_budget_s: float = 60.0
    thresholds: Thresholds = field(default_factory=Thresholds)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            db_path=Path(os.environ.get("TAPE_DB_PATH", str(cls.db_path))),
            user_agent=os.environ.get("TAPE_USER_AGENT", cls.user_agent),
            jev_model=os.environ.get("TAPE_JEV_MODEL", cls.jev_model),
            max_chars=_int("TAPE_MAX_CHARS", cls.max_chars, minimum=1),
            jev_input_price_per_m=_float("TAPE_JEV_PRICE_PER_M", cls.jev_input_price_per_m),
            jev_max_retries=_int("TAPE_JEV_MAX_RETRIES", cls.jev_max_retries, minimum=0),
            jev_retry_budget_s=_float("TAPE_JEV_RETRY_BUDGET_S", cls.jev_retry_budget_s),
            thresholds=Thresholds.from_env(),
        )

    def __post_init__(self) -> None:
        if self.jev_retry_budget_s <= 0:
            raise ValueError(f"jev_retry_budget_s must be positive, got {self.jev_retry_budget_s}")
