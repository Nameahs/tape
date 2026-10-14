"""Telegram delivery for the digest when it is sent from a script instead of the Agent37 agent."""

from __future__ import annotations

import os

import httpx

API = "https://api.telegram.org/bot{token}/sendMessage"
LIMIT = 4096  # Telegram's max message length


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
    client = client or httpx.Client(timeout=15.0)
    sent = 0
    for part in chunks(text):
        resp = client.post(API.format(token=token), json={
            "chat_id": chat_id, "text": part, "disable_web_page_preview": True,
        })
        resp.raise_for_status()
        sent += 1
    return sent
