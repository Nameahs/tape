import json

import httpx
import pytest
import respx

from tape import notify
from tape.notify import API, MAX_ATTEMPTS, chunks, send_telegram


def test_chunks_respects_limit():
    text = "\n".join(["x" * 30] * 10)
    parts = chunks(text, limit=100)
    assert all(len(p) <= 100 for p in parts)
    assert "\n".join(parts) == text


def test_chunks_truncates_overlong_line():
    assert chunks("y" * 250, limit=100) == ["y" * 100]


@respx.mock
def test_send_telegram():
    route = respx.post(API.format(token="T")).respond(200, json={"ok": True})
    n = send_telegram("hello\nworld", token="T", chat_id="42", client=httpx.Client())
    assert n == 1
    body = json.loads(route.calls[0].request.content)
    assert body == {"chat_id": "42", "text": "hello\nworld", "disable_web_page_preview": True}


@pytest.fixture
def sleeps(monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr(notify.time, "sleep", waited.append)
    return waited


def _rate_limited(retry_after=3):
    return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": retry_after}})


@respx.mock
def test_send_telegram_retries_429_and_sends_every_chunk(sleeps):
    ok = httpx.Response(200, json={"ok": True})
    route = respx.post(API.format(token="T")).mock(side_effect=[ok, _rate_limited(3), ok])
    text = "\n".join(["a" * 3000, "b" * 3000])
    n = send_telegram(text, token="T", chat_id="42", client=httpx.Client())
    assert n == 2
    assert route.call_count == 3
    assert sleeps == [3.0]
    texts = [json.loads(c.request.content)["text"] for c in route.calls]
    assert texts == ["a" * 3000, "b" * 3000, "b" * 3000]


@respx.mock
def test_send_telegram_caps_retry_after(sleeps):
    ok = httpx.Response(200, json={"ok": True})
    respx.post(API.format(token="T")).mock(side_effect=[_rate_limited(600), ok])
    send_telegram("hi", token="T", chat_id="42", client=httpx.Client())
    assert sleeps == [notify.MAX_RETRY_AFTER]


@respx.mock
def test_send_telegram_backs_off_without_retry_after(sleeps):
    ok = httpx.Response(200, json={"ok": True})
    respx.post(API.format(token="T")).mock(side_effect=[httpx.Response(429, text="slow down"), ok])
    send_telegram("hi", token="T", chat_id="42", client=httpx.Client())
    assert sleeps == [1.0]


@respx.mock
def test_send_telegram_raises_after_retry_limit(sleeps):
    route = respx.post(API.format(token="T")).mock(return_value=_rate_limited(1))
    with pytest.raises(httpx.HTTPStatusError):
        send_telegram("hi", token="T", chat_id="42", client=httpx.Client())
    assert route.call_count == MAX_ATTEMPTS
    assert len(sleeps) == MAX_ATTEMPTS - 1


@respx.mock
def test_send_telegram_does_not_retry_other_errors(sleeps):
    route = respx.post(API.format(token="T")).respond(400, json={"ok": False})
    with pytest.raises(httpx.HTTPStatusError):
        send_telegram("hi", token="T", chat_id="42", client=httpx.Client())
    assert route.call_count == 1
    assert sleeps == []
