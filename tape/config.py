"""Settings, read from the environment so the same code runs locally and on Agent37."""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

log = logging.getLogger(__name__)


class ConfigError(ValueError):
    """A setting is missing or invalid. The fix is in the environment, not the code."""


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
        raise ConfigError(f"{name}: expected a whole number, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name}: must be at least {minimum}, got {value}")
    return value


def _parse_float(name: str, raw: str) -> float:
    """Parse a number from the environment, naming the variable if it isn't one.

    float() also accepts "nan" and "inf". Every comparison with NaN is false, so a NaN cutoff would
    quietly route every filing to the log; refuse both.
    """
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name}: expected a number, got {raw!r}") from None
    if not math.isfinite(value):
        raise ConfigError(f"{name}: expected a finite number, got {raw!r}")
    return value


# The cutoffs Jev's scores can actually reach: materiality runs 1 to 5, the others are probabilities. A value
# outside its scale is a typo, and it fails silently: it looks like a run of quiet days or briefs every filing.
_CUTOFF_RANGES = {
    "brief_now": (1.0, 5.0),
    "brief_now_with_surprise": (1.0, 5.0),
    "digest": (1.0, 5.0),
    "surprise": (0.0, 1.0),
    "boilerplate": (0.0, 1.0),
}


def _check_cutoff(setting: str, cutoff: str, value: float) -> None:
    low, high = _CUTOFF_RANGES[cutoff]
    if not low <= value <= high:
        raise ConfigError(f"{setting}: must be between {low:g} and {high:g}, got {value:g}")


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
                raise ConfigError(f"unknown threshold(s) for {event}: {', '.join(sorted(unknown))}")
            for cutoff, value in values.items():
                _check_cutoff(f"TAPE_{cutoff.upper()}__{event.upper()}", cutoff, value)
        for cutoff in names:
            _check_cutoff(f"TAPE_{cutoff.upper()}", cutoff, getattr(self, cutoff))

    def for_event(self, event: str | None) -> Thresholds:
        """The cutoffs that apply to one event label, with its overrides folded in."""
        values = self.overrides.get(event or "")
        if not values:
            return self
        return replace(self, overrides={}, **values)

    @classmethod
    def from_env(cls) -> Thresholds:
        t = cls(
            brief_now=_float("TAPE_BRIEF_NOW", cls.brief_now),
            brief_now_with_surprise=_float("TAPE_BRIEF_NOW_WITH_SURPRISE", cls.brief_now_with_surprise),
            surprise=_float("TAPE_SURPRISE", cls.surprise),
            digest=_float("TAPE_DIGEST", cls.digest),
            boilerplate=_float("TAPE_BOILERPLATE", cls.boilerplate),
            overrides=_event_overrides(os.environ),
        )
        for problem in t.unreachable():
            log.warning("%s", problem)
        return t

    def unreachable(self) -> list[str]:
        """Describe each routing rule these cutoffs leave dead, globally and per event.

        route() checks brief_now first, so a digest bar at or above it empties the digest band, and a
        surprise bar at or above it means the surprise rule never decides anything. Both are legal (a
        digest bar at brief-now is how you turn the digest off), but done by accident they look like
        a run of quiet days, so the caller warns rather than refuses. An event is only reported when
        its own overrides cause the problem; one the global cutoffs already cause is reported once.
        """
        problems = _unreachable(self, "", set())
        for event, values in sorted(self.overrides.items()):
            problems += _unreachable(self.for_event(event), event, set(values))
        return problems


def _unreachable(t: Thresholds, event: str, overridden: set[str]) -> list[str]:
    def var(cutoff: str) -> str:
        name = f"TAPE_{cutoff.upper()}"
        return f"{name}__{event.upper()}" if cutoff in overridden else name

    where = f" for {event}" if event else ""
    out = []
    for cutoff, lost in (("digest", "no filing will be routed to the digest"),
                         ("brief_now_with_surprise", "the surprise rule can never brief a filing")):
        if event and not {cutoff, "brief_now"} & overridden:
            continue
        value = getattr(t, cutoff)
        if value >= t.brief_now:
            out.append(f"{var(cutoff)} ({value:g}) is at or above {var('brief_now')} ({t.brief_now:g}): "
                       f"{lost}{where}")
    return out


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
            # Only a known event marks this as a misspelled override; anything else isn't ours to judge.
            if event in EVENTS:
                raise ConfigError(f"{key}: unknown threshold {cutoff!r}; expected one of {', '.join(sorted(names))}")
            continue
        if event not in EVENTS:
            raise ConfigError(f"{key}: unknown event {event!r}; expected one of {', '.join(EVENTS)}")
        out.setdefault(event, {})[cutoff] = _parse_float(key, raw.strip())
    return out


def require_contact(user_agent: str) -> str:
    """Return the EDGAR user agent, or fail if it doesn't declare a contact address.

    The SEC's fair-access policy wants automated clients to send a name and an email. EDGAR answers
    anything else with 403 "Undeclared Automated Tool", which otherwise surfaces as a generic HTTP
    error on every request and looks like an outage. Checking for an "@" is enough to catch the
    unset placeholder and obvious typos without trying to validate email syntax.
    """
    name, _, domain = user_agent.strip().rpartition("@")
    if not name.strip() or "." not in domain or " " in domain:
        raise ConfigError(
            f"TAPE_USER_AGENT must include a contact email for the SEC, e.g. "
            f"'Tape you@example.com'; got {user_agent!r}"
        )
    return user_agent.strip()


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
            raise ConfigError(f"jev_retry_budget_s must be positive, got {self.jev_retry_budget_s}")
        if self.jev_input_price_per_m < 0:
            raise ConfigError(f"jev_input_price_per_m must not be negative, got {self.jev_input_price_per_m}")
