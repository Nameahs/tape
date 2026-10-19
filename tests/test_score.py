import json
import threading
import time

import pytest
from typesafe_sdk import Choice, Noul, Score, TypeSafeAPIError

from tape import db
from tape.score import EVENTS, MATERIALITY_LEVELS, QUESTIONS, score_many, score_one, state_for
from tests.conftest import make_filing
from tests.fakes import FakeJev


def _row(conn, n=1, **kw):
    db.upsert_filing(conn, make_filing(n, **kw))
    return conn.execute("SELECT * FROM filings WHERE accession_no = ?", (make_filing(n).accession_no,)).fetchone()


def test_question_set_shape():
    assert set(QUESTIONS) == {"materiality", "event", "direction", "surprise", "boilerplate"}
    assert isinstance(QUESTIONS["materiality"], Score) and len(MATERIALITY_LEVELS) == 5
    assert isinstance(QUESTIONS["event"], Choice) and "other" in EVENTS
    assert isinstance(QUESTIONS["surprise"], Noul)
    # Serializes to the wire format the API expects.
    assert QUESTIONS["materiality"].model_dump()["type"] == "score"


def test_state_for_splits_items(conn):
    state = state_for(_row(conn))
    assert state["items"] == ["2.02", "9.01"]
    assert state["company"] == "Example Corp"


def test_score_one_maps_answers_and_cost(conn):
    row = _row(conn, clean_text="The CEO tendered her resignation.")
    s = score_one(FakeJev(), row, "jev-latest", price_per_m=0.042)
    assert s.materiality == 4.6  # 3.6 on Jev's 0..4 scale
    assert s.event == "leadership_change" and s.direction == "bad"
    assert s.surprise == 0.9
    assert s.cost_usd == 1000 * 0.042 / 1_000_000
    assert s.model == "jev-fake"
    assert (s.materiality_confidence, s.event_confidence, s.direction_confidence) == (0.7, 0.9, 0.8)
    assert json.loads(s.materiality_probs) == [0.0, 0.05, 0.1, 0.1, 0.75]


def test_score_one_tolerates_answers_without_confidence(conn):
    class Bare(FakeJev):
        def system_one(self, state, questions, *, model=None):
            resp = super().system_one(state, questions, model=model)
            for answer in resp.answers.values():
                vars(answer).pop("confidence", None)
                vars(answer).pop("probabilities", None)
            return resp

    s = score_one(Bare(), _row(conn), "m", 0.042)
    assert s.materiality_confidence is None and s.materiality_probs is None


def test_score_many_skips_failures(conn):
    rows = [_row(conn, 1, clean_text="fine"), _row(conn, 2, clean_text="explode please")]
    out = list(score_many(FakeJev(fail_on={"explode"}), rows, "m", 0.042, workers=2))
    assert [s.accession_no for s in out] == [rows[0]["accession_no"]]


def test_score_many_yields_before_the_batch_finishes(conn):
    release = threading.Event()

    class Slow(FakeJev):
        def system_one(self, state, questions, *, model=None):
            if "slow" in state["text"]:
                release.wait(timeout=2)
            return super().system_one(state, questions, model=model)

    rows = [_row(conn, 1, clean_text="slow one"), _row(conn, 2, clean_text="quick one")]
    results = score_many(Slow(), rows, "m", 0.042, workers=2)
    first = next(results)
    released_early = not release.is_set()
    release.set()
    rest = list(results)
    assert released_early, "first result only arrived once the slow filing finished"
    assert first.accession_no == rows[1]["accession_no"]
    assert [s.accession_no for s in rest] == [rows[0]["accession_no"]]


def test_score_many_stops_sending_when_the_caller_stops(conn):
    started, release = threading.Event(), threading.Event()

    class Blocking(FakeJev):
        def system_one(self, state, questions, *, model=None):
            if "second" in state["text"]:
                started.set()
                release.wait(timeout=1)
            return super().system_one(state, questions, model=model)

    jev = Blocking()
    rows = [_row(conn, n, clean_text=text) for n, text in enumerate(["first", "second", "third"], 1)]
    results = score_many(jev, rows, "m", 0.042, workers=1)
    assert next(results).accession_no == rows[0]["accession_no"]
    assert started.wait(timeout=1)  # the second filing is in flight, the third still queued
    results.close()  # what a KeyboardInterrupt in the caller's loop does to the generator
    release.set()
    time.sleep(0.1)
    assert [state["text"] for state, _, _ in jev.calls] == ["first", "second"]


def test_contract_with_real_sdk(conn):
    """Run the real TypeSafe client over a mock transport: request shape in, typed answers out."""
    import httpx2
    from typesafe_sdk import TypeSafeClient

    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        levels = {"4": 0.3, "0": 0.1, "1": 0.1, "2": 0.2, "3": 0.3}  # out of order on purpose
        return httpx2.Response(200, json={
            "model": "jev-1.13",
            "usage": {"input_tokens": 812, "output_tokens": 5},
            "answers": {
                "materiality": {"type": "score", "score": 3.2, "confidence": 0.8,
                                "legend": {k: "x" for k in levels}, "probabilities": levels},
                "event": {"type": "choice", "choice": "mna", "confidence": 0.9, "probabilities": {"mna": 0.9}},
                "direction": {"type": "choice", "choice": "good", "confidence": 0.7, "probabilities": {"good": 0.7}},
                "surprise": {"type": "noul", "noul": 0.6},
                "boilerplate": {"type": "noul", "noul": 0.1},
            },
        })

    client = TypeSafeClient(api_key="test", transport=httpx2.MockTransport(handler))
    s = score_one(client, _row(conn), "jev-latest", 0.042)
    assert sent["model"] == "jev-latest"
    assert sent["questions"]["materiality"]["type"] == "score"
    assert len(sent["questions"]["materiality"]["criteria"]) == 5
    assert sent["state"]["items"] == ["2.02", "9.01"]
    assert (s.materiality, s.event, s.input_tokens) == (4.2, "mna", 812)
    assert '"jev-1.13"' in s.raw_json
    assert (s.materiality_confidence, s.event_confidence, s.direction_confidence) == (0.8, 0.9, 0.7)
    assert json.loads(s.materiality_probs) == [0.1, 0.1, 0.2, 0.3, 0.3]


def _counting_client(statuses, policy):
    import httpx2
    from typesafe_sdk import TypeSafeClient

    calls = []

    def handler(request):
        calls.append(request)
        status = statuses[min(len(calls), len(statuses)) - 1]
        return httpx2.Response(status, json={"error": "nope"})

    transport = httpx2.MockTransport(handler)
    client = TypeSafeClient(api_key="k", base_url="https://jev.test", transport=transport, retry=policy)
    return client, calls


def _fast(**kw):
    from dataclasses import replace

    from tape.score import retry_policy

    # Same retry rules, no sleeping in tests.
    return replace(retry_policy(**kw), backoff_initial=0.0, backoff_max=0.0)


def test_retry_policy_defaults_are_more_patient_than_the_sdk():
    from typesafe_sdk import RetryPolicy

    from tape.score import retry_policy

    p, sdk = retry_policy(), RetryPolicy()
    assert p.max_retries > sdk.max_retries and p.timeout > sdk.timeout
    assert {429, 503} <= p.http_statuses and 400 not in p.http_statuses and 401 not in p.http_statuses
    assert p.respect_retry_after and p.api_timeout_error and p.api_connection_error


def test_rate_limits_are_retried_until_the_limit():
    client, calls = _counting_client([429], _fast(max_retries=3))
    with pytest.raises(TypeSafeAPIError, match="429"):
        client.system_one("x", {"q": Noul(instructions="?")}, model="m")
    assert len(calls) == 4


def test_bad_requests_fail_without_retrying():
    client, calls = _counting_client([400], _fast(max_retries=3))
    with pytest.raises(TypeSafeAPIError, match="400"):
        client.system_one("x", {"q": Noul(instructions="?")}, model="m")
    assert len(calls) == 1


def test_default_client_uses_tape_retry_policy(monkeypatch):
    import typesafe_sdk

    from tape.score import default_client, retry_policy

    seen = {}

    class Spy:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", Spy)
    default_client()
    assert seen["retry"] == retry_policy()
    custom = retry_policy(max_retries=1)
    default_client(custom)
    assert seen["retry"] is custom
