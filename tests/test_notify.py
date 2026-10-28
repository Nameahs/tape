import json

import httpx
import pytest
import respx

from tape import notify
from tape.notify import API, MAX_ATTEMPTS, RETRY_STATUSES, chunks, send_telegram


def test_chunks_respects_limit():
    text = "\n".join(["x" * 30] * 10)
    parts = chunks(text, limit=100)
    assert all(len(p) <= 100 for p in parts)
    assert "\n".join(parts) == text


def test_chunks_splits_overlong_line_without_losing_text():
    assert chunks("y" * 250, limit=100) == ["y" * 100, "y" * 100, "y" * 50]


def test_chunks_breaks_overlong_line_at_a_space():
    words = " ".join(f"word{i:04d}" for i in range(1100))  # ~9,900 characters on one line
    text = f"header\n{words}\nfooter"
    parts = chunks(text)
    assert len(parts) == 4  # header alone (the next piece fills a message), three pieces, footer on the last
    assert all(len(p) <= 4096 for p in parts)
    # Every word arrives intact and in order; only the spaces the line was broken on are gone.
    assert " ".join(parts).replace("\n", " ").split() == text.split()
    assert parts[0] == "header" and parts[-1].endswith("\nfooter")


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


@pytest.mark.parametrize("status", sorted(RETRY_STATUSES))
@respx.mock
def test_send_telegram_retries_server_errors(sleeps, status):
    ok = httpx.Response(200, json={"ok": True})
    route = respx.post(API.format(token="T")).mock(side_effect=[httpx.Response(status), ok])
    assert send_telegram("hi", token="T", chat_id="42", client=httpx.Client()) == 1
    assert route.call_count == 2
    assert sleeps == [1.0]


@respx.mock
def test_send_telegram_retries_transport_errors_mid_digest(sleeps):
    ok = httpx.Response(200, json={"ok": True})
    route = respx.post(API.format(token="T")).mock(
        side_effect=[ok, httpx.ReadTimeout("slow"), httpx.ConnectError("reset"), ok]
    )
    text = "\n".join(["a" * 3000, "b" * 3000])
    assert send_telegram(text, token="T", chat_id="42", client=httpx.Client()) == 2
    assert route.call_count == 4
    assert sleeps == [1.0, 2.0]


@respx.mock
def test_send_telegram_raises_server_error_after_retry_limit(sleeps):
    route = respx.post(API.format(token="T")).respond(502)
    with pytest.raises(httpx.HTTPStatusError):
        send_telegram("hi", token="T", chat_id="42", client=httpx.Client())
    assert route.call_count == MAX_ATTEMPTS
    assert sleeps == [1.0, 2.0, 4.0]


@respx.mock
def test_send_telegram_raises_transport_error_after_retry_limit(sleeps):
    route = respx.post(API.format(token="T")).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(httpx.ConnectError):
        send_telegram("hi", token="T", chat_id="42", client=httpx.Client())
    assert route.call_count == MAX_ATTEMPTS
    assert len(sleeps) == MAX_ATTEMPTS - 1


class _TrackingClient(httpx.Client):
    instances: list["_TrackingClient"] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _TrackingClient.instances.append(self)


@respx.mock
def test_send_telegram_closes_the_client_it_creates(monkeypatch):
    _TrackingClient.instances.clear()
    monkeypatch.setattr(notify.httpx, "Client", _TrackingClient)
    respx.post(API.format(token="T")).respond(401)
    with pytest.raises(httpx.HTTPStatusError):
        send_telegram("hi", token="T", chat_id="42")
    assert len(_TrackingClient.instances) == 1
    assert _TrackingClient.instances[0].is_closed


@respx.mock
def test_send_telegram_leaves_a_passed_client_open():
    respx.post(API.format(token="T")).respond(200, json={"ok": True})
    client = httpx.Client()
    send_telegram("hi", token="T", chat_id="42", client=client)
    assert not client.is_closed
    client.close()
