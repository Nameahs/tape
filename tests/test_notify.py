import json

import httpx
import respx

from tape.notify import API, chunks, send_telegram


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
