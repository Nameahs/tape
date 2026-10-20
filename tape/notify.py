"""Telegram delivery for the digest when it is sent from a script instead of the Agent37 agent."""

from __future__ import annotations

import os
import time

import httpx

API = "https://api.telegram.org/bot{token}/sendMessage"
LIMIT = 4096  # Telegram's max message length
MAX_ATTEMPTS = 4  # per chunk, so one rate limit doesn't cut a digest off halfway
MAX_RETRY_AFTER = 30.0  # never wait longer than this on one 429


def chunks(text: str, limit: int = LIMIT) -> list[str]:
    """Split on line breaks so no message exceeds Telegram's limit."""
    out, cur = [], ""
    for line in text.splitlines():
        line = line[:limit]
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def send_telegram(text: str, token: str | None = None, chat_id: str | None = None,
                  client: httpx.Client | None = None) -> int:
    token = token or os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = chat_id or os.environ["TELEGRAM_CHAT_ID"]
    owned = client is None
    http = httpx.Client(timeout=15.0) if client is None else client
    try:
        sent = 0
        for part in chunks(text):
            _post(http, API.format(token=token), {
                "chat_id": chat_id, "text": part, "disable_web_page_preview": True,
            })
            sent += 1
        return sent
    finally:
        if owned:
            http.close()


def _post(client: httpx.Client, url: str, payload: dict[str, object]) -> httpx.Response:
    """POST one message, waiting out Telegram's 429s before giving up."""
    for attempt in range(MAX_ATTEMPTS):
        resp = client.post(url, json=payload)
        if resp.status_code == 429 and attempt < MAX_ATTEMPTS - 1:
            time.sleep(_retry_after(resp, attempt))
            continue
        resp.raise_for_status()
        return resp
    raise AssertionError("unreachable")


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    """Telegram puts the wait in the JSON body (parameters.retry_after); fall back to backoff."""
    try:
        delay = float(resp.json()["parameters"]["retry_after"])
    except (ValueError, KeyError, TypeError):
        delay = float(2**attempt)
    return min(max(delay, 0.0), MAX_RETRY_AFTER)
