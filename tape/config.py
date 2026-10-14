"""Settings, read from the environment so the same code runs locally and on Agent37."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


@dataclass(frozen=True)
class Thresholds:
    """Router cutoffs on the 1 to 5 materiality scale."""

    brief_now: float = 4.0
    brief_now_with_surprise: float = 3.0
    surprise: float = 0.7
    digest: float = 2.75
    boilerplate: float = 0.8

    @classmethod
    def from_env(cls) -> Thresholds:
        return cls(
            brief_now=_float("TAPE_BRIEF_NOW", cls.brief_now),
            brief_now_with_surprise=_float("TAPE_BRIEF_NOW_WITH_SURPRISE", cls.brief_now_with_surprise),
            surprise=_float("TAPE_SURPRISE", cls.surprise),
            digest=_float("TAPE_DIGEST", cls.digest),
            boilerplate=_float("TAPE_BOILERPLATE", cls.boilerplate),
        )


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
    thresholds: Thresholds = field(default_factory=Thresholds)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            db_path=Path(os.environ.get("TAPE_DB_PATH", str(cls.db_path))),
            user_agent=os.environ.get("TAPE_USER_AGENT", cls.user_agent),
            jev_model=os.environ.get("TAPE_JEV_MODEL", cls.jev_model),
            max_chars=int(_float("TAPE_MAX_CHARS", cls.max_chars)),
            jev_input_price_per_m=_float("TAPE_JEV_PRICE_PER_M", cls.jev_input_price_per_m),
            thresholds=Thresholds.from_env(),
        )
