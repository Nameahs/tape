"""Turn filing HTML into compact text that fits Jev's input budget."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

_WS = re.compile(r"[ \t ]+")
_BLANKS = re.compile(r"\n{3,}")
# 8-K item headings, e.g. "Item 5.02 Departure of Directors or Certain Officers".
_ITEM = re.compile(r"^\s*item\s+(\d\.\d{2})\b", re.IGNORECASE | re.MULTILINE)
# Boilerplate that adds tokens without adding signal.
_BOILERPLATE_START = re.compile(
    r"^\s*(signatures?|forward[- ]looking statements|cautionary (note|statement)|safe harbor)\b",
    re.IGNORECASE | re.MULTILINE,
)


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "title", "meta"]):
        tag.decompose()
    # Inline XBRL wraps facts in hidden headers; drop them.
    for tag in soup.find_all(style=re.compile(r"display:\s*none", re.IGNORECASE)):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    text = soup.get_text("\n")
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return _BLANKS.sub("\n\n", text).strip()


def drop_boilerplate(text: str) -> str:
    """Cut everything from the first signature or safe-harbor section onward."""
    m = _BOILERPLATE_START.search(text)
    # Only cut if the boilerplate starts after some real content.
    if m and m.start() > 80:
        return text[: m.start()].rstrip()
    return text


def items_in(text: str) -> list[str]:
    seen: list[str] = []
    for m in _ITEM.finditer(text):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def build_state_text(main_html: str, exhibit_html: str | None, max_chars: int) -> str:
    """The 8-K body first (it says what happened), then the press release (it says how much)."""
    main = drop_boilerplate(html_to_text(main_html))
    parts = [main]
    if exhibit_html:
        parts.append("EXHIBIT 99.1 (press release)\n" + drop_boilerplate(html_to_text(exhibit_html)))
    text = "\n\n".join(parts)
    if len(text) <= max_chars:
        return text
    # The 8-K body gets at least two thirds of the budget, plus whatever a short (or missing)
    # exhibit leaves unused; the exhibit fills the rest.
    exhibit = parts[1] if len(parts) > 1 else ""
    exhibit_room = len(exhibit) + 2 if exhibit else 0
    main_budget = min(len(main), max((max_chars * 2) // 3, max_chars - exhibit_room))
    head = main[:main_budget]
    rest = max_chars - len(head) - 2
    tail = exhibit[:rest] if exhibit and rest > 0 else ""
    return (head + "\n\n" + tail).strip()[:max_chars]
