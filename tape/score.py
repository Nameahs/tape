"""Ask Jev five questions about each filing in a single request."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

from typesafe_sdk import Choice, Noul, RetryPolicy, Score

from tape.db import ScoreRow

log = logging.getLogger(__name__)

MATERIALITY_LEVELS = [
    "Routine: administrative or procedural, no bearing on value (bylaw edits, vote tallies, auditor letters).",
    "Minor: small, expected business update unlikely to change any valuation.",
    "Moderate: real news investors would note, but within the range of expectations.",
    "Significant: likely to change some investors' view of value (a large deal, guidance change, "
    "senior executive exit).",
    "Market-moving: a major surprise likely to move the stock sharply (sudden CEO exit, guidance withdrawn, "
    "transformative M&A, going-concern or default).",
]

EVENTS = {
    "leadership_change": "Executive or director appointments, departures or compensation changes.",
    "earnings_guidance": "Quarterly results, preliminary results, or changes to guidance.",
    "mna": "Acquisitions, mergers, divestitures or other changes of control.",
    "financing_debt": "Debt or equity offerings, credit agreements, defaults, buybacks or dividends.",
    "legal_regulatory": "Litigation, investigations, regulatory actions or approvals.",
    "restructuring": "Layoffs, impairments, bankruptcy, restructuring or delisting.",
    "routine_governance": "Shareholder votes, bylaw or charter changes, auditor changes, other procedure.",
    "other": "Anything else.",
}

DIRECTIONS = {
    "good": "Good news for shareholders.",
    "bad": "Bad news for shareholders.",
    "mixed": "Both good and bad elements, or unclear.",
    "neutral": "No meaningful effect on shareholders.",
}

QUESTIONS = {
    "materiality": Score(
        instructions="How likely is this SEC 8-K filing to change how investors value the company?",
        criteria=MATERIALITY_LEVELS,
    ),
    "event": Choice(instructions="What kind of event does this filing report?", criteria=EVENTS),
    "direction": Choice(instructions="Is this news good, bad or mixed for shareholders?", criteria=DIRECTIONS),
    "surprise": Noul(
        instructions="Does this filing reveal something investors likely did not expect?",
        criteria={
            "true": "Unscheduled or unexpected news, such as a sudden departure or a guidance cut.",
            "false": "Scheduled or expected news, such as a regular earnings release or routine vote.",
        },
    ),
    "boilerplate": Noul(instructions="Is this filing mostly procedural or legal boilerplate?"),
}


class SystemOneClient(Protocol):
    def system_one(self, state: Any, questions: Any, *, model: str | None = None) -> Any: ...


def state_for(row: Any) -> dict[str, Any]:
    return {
        "company": row["company"],
        "form": row["form"],
        "items": [i for i in (row["items"] or "").split(",") if i],
        "text": row["clean_text"],
    }


def _raw_json(resp: Any) -> str:
    dump = getattr(resp, "model_dump_json", None)
    return dump() if callable(dump) else json.dumps(resp, default=str)


def _confidence(answer: Any) -> float | None:
    value = getattr(answer, "confidence", None)
    return None if value is None else round(float(value), 4)


def _level_probs(answer: Any) -> str | None:
    """Materiality level probabilities as a JSON list ordered by level (index 0 is level 1).

    The typed SDK keys them by int, the wire format by string; accept either.
    """
    probs = getattr(answer, "probabilities", None)
    if not probs:
        return None
    ordered = sorted(probs.items(), key=lambda kv: int(kv[0]))
    return json.dumps([round(float(p), 4) for _, p in ordered])


def score_one(client: SystemOneClient, row: Any, model: str, price_per_m: float) -> ScoreRow:
    started = time.perf_counter()
    resp = client.system_one(state_for(row), QUESTIONS, model=model)
    latency_ms = (time.perf_counter() - started) * 1000
    a = resp.answers
    input_tokens = getattr(resp.usage, "input_tokens", None)
    return ScoreRow(
        accession_no=row["accession_no"],
        model=resp.model,
        # Jev's expected score runs 0..4 for five levels; store it as 1..5.
        materiality=round(float(a["materiality"].score) + 1.0, 4),
        event=a["event"].choice,
        direction=a["direction"].choice,
        surprise=float(a["surprise"].noul),
        boilerplate=float(a["boilerplate"].noul),
        raw_json=_raw_json(resp),
        input_tokens=input_tokens,
        cost_usd=(input_tokens or 0) * price_per_m / 1_000_000,
        latency_ms=round(latency_ms, 1),
        materiality_confidence=_confidence(a["materiality"]),
        event_confidence=_confidence(a["event"]),
        direction_confidence=_confidence(a["direction"]),
        materiality_probs=_level_probs(a["materiality"]),
    )


def score_many(
    client: SystemOneClient, rows: Iterable[Any], model: str, price_per_m: float, workers: int = 8
) -> list[ScoreRow]:
    """Score in parallel. A failed filing is logged and left unscored so the next pass retries it."""

    def safe(row: Any) -> ScoreRow | None:
        try:
            return score_one(client, row, model, price_per_m)
        except Exception:
            log.exception("scoring failed for %s", row["accession_no"])
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [s for s in pool.map(safe, list(rows)) if s is not None]


# Status codes worth another attempt: request timeout, rate limit, and server-side failures.
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def retry_policy(max_retries: int = 4, budget_s: float = 60.0) -> RetryPolicy:
    """How hard to retry one filing before leaving it for the next pass.

    The SDK default (2 retries, 5s max backoff, 30s budget) gives up quickly, and a pass scores
    many filings in parallel, so a rate-limit burst would otherwise drop a whole batch. Back off
    longer and honour Retry-After, but cap the total so one stuck filing can't stall the pass.
    Other 4xx errors (bad request, auth) are not transient and fail on the first attempt.
    """
    return RetryPolicy(
        max_retries=max_retries,
        backoff_initial=1.0,
        backoff_max=20.0,
        http_statuses=set(RETRY_STATUSES),
        timeout=budget_s,
    )


def default_client(retry: RetryPolicy | None = None) -> SystemOneClient:
    from typesafe_sdk import TypeSafeClient

    # Reads TYPESAFE_API_KEY from the environment.
    return TypeSafeClient(retry=retry or retry_policy())
