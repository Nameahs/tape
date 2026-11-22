import json
import threading
import time
from types import SimpleNamespace

import pytest
from typesafe_sdk import (
    Choice,
    Noul,
    Score,
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
)

from tape import db
from tape.score import (
    EVENTS,
    MATERIALITY_LEVELS,
    OUTAGE_STOP_AFTER,
    QUESTIONS,
    InvalidAnswerError,
    _confidence,
    _level_probs,
    is_outage,
    score_many,
    score_one,
    state_for,
)
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


@pytest.mark.parametrize("usage", [None, SimpleNamespace(output_tokens=5)])
def test_score_one_without_usage_stores_unknown_cost(conn, usage):
    class NoUsage(FakeJev):
        def system_one(self, state, questions, *, model=None):
            resp = super().system_one(state, questions, model=model)
            resp.usage = usage
            return resp

    s = score_one(NoUsage(), _row(conn), "m", 0.042)
    assert s.input_tokens is None and s.cost_usd is None
    db.upsert_score(conn, s)
    stored = conn.execute("SELECT input_tokens, cost_usd FROM scores WHERE accession_no = ?",
                          (s.accession_no,)).fetchone()
    assert tuple(stored) == (None, None)


def test_score_one_with_zero_tokens_costs_zero(conn):
    class Empty(FakeJev):
        def system_one(self, state, questions, *, model=None):
            resp = super().system_one(state, questions, model=model)
            resp.usage = SimpleNamespace(input_tokens=0)
            return resp

    assert score_one(Empty(), _row(conn), "m", 0.042).cost_usd == 0.0


class Usage(FakeJev):
    def __init__(self, input_tokens):
        super().__init__()
        self.input_tokens = input_tokens

    def system_one(self, state, questions, *, model=None):
        resp = super().system_one(state, questions, model=model)
        resp.usage = SimpleNamespace(input_tokens=self.input_tokens, output_tokens=5)
        return resp


@pytest.mark.parametrize("tokens", [-120, 12.5, float("nan"), float("inf"), "812", True])
def test_score_one_treats_an_impossible_token_count_as_unknown_cost(conn, caplog, tokens):
    s = score_one(Usage(tokens), _row(conn), "m", 0.042)
    assert s.input_tokens is None and s.cost_usd is None
    assert "Ignoring input_tokens" in caplog.text
    db.upsert_score(conn, s)
    stored = conn.execute("SELECT input_tokens, cost_usd FROM scores WHERE accession_no = ?",
                          (s.accession_no,)).fetchone()
    assert tuple(stored) == (None, None)


def test_score_one_accepts_a_whole_token_count_sent_as_a_float(conn):
    s = score_one(Usage(1000.0), _row(conn), "m", 0.042)
    assert s.input_tokens == 1000 and isinstance(s.input_tokens, int)
    assert s.cost_usd == pytest.approx(0.000042)


class Answering(FakeJev):
    """FakeJev with one answer field overridden, e.g. Answering("materiality", "score", 9.0)."""

    def __init__(self, question, attr, value):
        super().__init__()
        self.question, self.attr, self.value = question, attr, value

    def system_one(self, state, questions, *, model=None):
        resp = super().system_one(state, questions, model=model)
        setattr(resp.answers[self.question], self.attr, self.value)
        return resp


@pytest.mark.parametrize("question, attr, value", [
    ("materiality", "score", 4.01),
    ("materiality", "score", -0.5),
    ("materiality", "score", float("nan")),
    ("event", "choice", "bogus"),
    ("event", "choice", None),
    ("direction", "choice", "sideways"),
    ("surprise", "noul", 1.5),
    ("boilerplate", "noul", -0.1),
], ids=repr)
def test_score_one_rejects_answers_outside_the_question(conn, question, attr, value):
    with pytest.raises(InvalidAnswerError, match=question):
        score_one(Answering(question, attr, value), _row(conn), "m", 0.042)


@pytest.mark.parametrize("question, attr, value, field, stored", [
    ("materiality", "score", 0.0, "materiality", 1.0),
    ("materiality", "score", 4.0, "materiality", 5.0),
    ("surprise", "noul", 1.0, "surprise", 1.0),
    ("boilerplate", "noul", 0.0, "boilerplate", 0.0),
])
def test_score_one_accepts_the_ends_of_each_scale(conn, question, attr, value, field, stored):
    s = score_one(Answering(question, attr, value), _row(conn), "m", 0.042)
    assert getattr(s, field) == stored


def test_an_invalid_answer_is_not_an_outage():
    assert not is_outage(InvalidAnswerError("x"))


def test_level_probs_keeps_each_probability_at_its_level():
    sparse = type("A", (), {"probabilities": {0: 0.2, 4: 0.8}})()
    assert json.loads(_level_probs(sparse)) == [0.2, 0.0, 0.0, 0.0, 0.8]

    wire = type("A", (), {"probabilities": {"3": 0.6, "1": 0.4}})()
    assert json.loads(_level_probs(wire)) == [0.0, 0.4, 0.0, 0.6, 0.0]


def test_level_probs_drops_unknown_levels(caplog):
    answer = type("A", (), {"probabilities": {0: 0.5, 1: 0.3, 5: 0.1, "high": 0.1}})()
    with caplog.at_level("WARNING", logger="tape.score"):
        assert json.loads(_level_probs(answer)) == [0.5, 0.3, 0.0, 0.0, 0.0]
    assert "unknown levels" in caplog.text and "'5'" in caplog.text and "'high'" in caplog.text


def test_score_one_keeps_the_score_when_side_data_is_impossible(conn):
    class Odd(FakeJev):
        def system_one(self, state, questions, *, model=None):
            resp = super().system_one(state, questions, model=model)
            resp.answers["event"].confidence = float("nan")
            resp.answers["materiality"].probabilities = {0: 0.5, 4: 1.5}
            return resp

    s = score_one(Odd(), _row(conn, clean_text="The CEO tendered her resignation."), "m", 0.042)
    assert s.materiality == 4.6 and s.event == "leadership_change"
    assert s.event_confidence is None and s.materiality_probs is None
    assert (s.materiality_confidence, s.direction_confidence) == (0.7, 0.8)


@pytest.mark.parametrize("value", [1.7, -0.2, float("nan"), float("inf"), "high"])
def test_level_probs_drops_the_list_for_an_impossible_probability(caplog, value):
    answer = type("A", (), {"probabilities": {0: 0.5, 1: value}})()
    with caplog.at_level("WARNING", logger="tape.score"):
        assert _level_probs(answer) is None
    assert "outside 0..1" in caplog.text


@pytest.mark.parametrize("value", [1.7, -0.2, float("nan"), float("-inf"), "sure"])
def test_confidence_drops_an_impossible_value(caplog, value):
    answer = type("A", (), {"confidence": value})()
    with caplog.at_level("WARNING", logger="tape.score"):
        assert _confidence(answer) is None
    assert "outside 0..1" in caplog.text


@pytest.mark.parametrize("value", [0, 1, 0.123456])
def test_confidence_keeps_the_ends_of_the_range(value):
    assert _confidence(type("A", (), {"confidence": value})()) == round(value, 4)


def test_score_many_skips_failures(conn):
    rows = [_row(conn, 1, clean_text="fine"), _row(conn, 2, clean_text="explode please")]
    out = list(score_many(FakeJev(fail_on={"explode"}), rows, "m", 0.042, workers=2))
    assert [s.accession_no for s in out] == [rows[0]["accession_no"]]


@pytest.mark.parametrize(
    ("exc", "outage"),
    [
        (TypeSafeAPIError(401, None, {}), True),
        (TypeSafeAPIError(403, None, {}), True),
        (TypeSafeAPIError(408, None, {}), True),
        (TypeSafeAPIError(429, None, {}), True),
        (TypeSafeAPIError(500, None, {}), True),
        (TypeSafeAPIError(503, None, {}), True),
        (TypeSafeAPIConnectionError("refused"), True),
        (TypeSafeAPITimeoutError(60.0), True),
        (TypeSafeAPIError(400, None, {}), False),
        (TypeSafeAPIError(404, None, {}), False),
        (TypeSafeAPIError(422, None, {}), False),
        (TypeSafeAPIResponseValidationError(200, None, {}, "answers.event"), False),
        (KeyError("materiality"), False),
    ],
    ids=repr,
)
def test_is_outage(exc, outage):
    assert is_outage(exc) is outage


def test_score_many_reports_failures_on_the_callers_thread(conn):
    rows = [_row(conn, 1, clean_text="fine"), _row(conn, 2, clean_text="explode please")]
    seen = []

    def on_failure(row, exc):
        seen.append((row["accession_no"], str(exc), threading.current_thread() is threading.main_thread()))

    out = list(score_many(FakeJev(fail_on={"explode"}), rows, "m", 0.042, workers=2, on_failure=on_failure))
    assert [s.accession_no for s in out] == [rows[0]["accession_no"]]
    assert seen == [(rows[1]["accession_no"], "boom", True)]


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


def _plain_rows(count):
    return [{"accession_no": str(n), "company": "Acme", "form": "8-K", "items": "", "clean_text": "fine"}
            for n in range(1, count + 1)]


class Scripted(FakeJev):
    """Answers each call with the next error in `script` (None means a normal answer), repeating it."""

    def __init__(self, script):
        super().__init__()
        self.script = script

    def system_one(self, state, questions, *, model=None):
        self.error = self.script[len(self.calls) % len(self.script)]
        return super().system_one(state, questions, model=model)


OUTAGE, REJECTED = TypeSafeAPIError(503, None, {}), TypeSafeAPIError(400, None, {})


def _run_batch(jev, count, workers):
    failures, stops = [], []
    out = list(score_many(jev, _plain_rows(count), "m", 0.042, workers=workers,
                          on_failure=lambda row, exc: failures.append(exc), on_stop=stops.append))
    return out, failures, stops


def test_score_many_stops_sending_during_an_outage():
    jev = FakeJev(error=OUTAGE)
    out, failures, stops = _run_batch(jev, 40, workers=4)
    # Four in flight at the start, and one more after each of the first two failures.
    assert len(jev.calls) == 6
    assert out == [] and len(failures) == 6
    assert stops == [34]


def test_score_many_stops_at_the_limit_with_a_single_worker():
    jev = FakeJev(error=OUTAGE)
    _, failures, stops = _run_batch(jev, 5, workers=1)
    assert len(jev.calls) == len(failures) == OUTAGE_STOP_AFTER
    assert stops == [2]


def test_score_many_does_not_report_a_stop_with_nothing_left_to_send():
    _, _, stops = _run_batch(FakeJev(error=OUTAGE), OUTAGE_STOP_AFTER, workers=1)
    assert stops == []


@pytest.mark.parametrize("between", [None, REJECTED], ids=["success", "rejected"])
def test_score_many_keeps_going_when_jev_answers_between_outage_errors(between):
    jev = Scripted([OUTAGE] * (OUTAGE_STOP_AFTER - 1) + [between])
    out, failures, stops = _run_batch(jev, 30, workers=1)
    assert len(jev.calls) == 30 and stops == []
    assert len(out) + len(failures) == 30


def test_score_many_sends_everything_jev_rejects():
    jev = FakeJev(error=REJECTED)
    _, failures, stops = _run_batch(jev, 20, workers=2)
    assert len(jev.calls) == len(failures) == 20
    assert stops == []


def test_score_many_only_takes_filings_it_has_a_worker_for():
    taken = []

    def rows():
        for row in _plain_rows(50):
            taken.append(row)
            yield row

    results = score_many(FakeJev(), rows(), "m", 0.042, workers=2)
    next(results)
    assert len(taken) <= 4  # two in flight, and up to two more if both finished before the first result
    results.close()
    assert len(taken) <= 4


def test_score_many_scores_a_whole_batch_larger_than_its_workers():
    rows = _plain_rows(20)
    out = list(score_many(FakeJev(), rows, "m", 0.042, workers=3))
    assert sorted(s.accession_no for s in out) == sorted(r["accession_no"] for r in rows)


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
