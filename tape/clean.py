"""Turn filing HTML into compact text that fits Jev's input budget."""

from __future__ import annotations

import re
from collections.abc import Iterable

from bs4 import BeautifulSoup, NavigableString, Tag

# Spaces, tabs and the Unicode space separators (NBSP, en/em/thin/hair spaces, narrow NBSP,
# ideographic space) all collapse to one ASCII space.
_WS = re.compile(r"[ \t\u00a0\u2000-\u200a\u202f\u205f\u3000]+")
# Zero-width characters (spaces, joiners, word joiner, BOM) carry no text. Spacer paragraphs in
# Workiva filings hold nothing but one, and str.strip() doesn't treat them as blank.
_ZERO_WIDTH = re.compile(r"[\u200b-\u200d\u2060\ufeff]")
_BLANKS = re.compile(r"\n{3,}")
# 8-K item headings, e.g. "Item 5.02 Departure of Directors or Certain Officers".
_ITEM = re.compile(r"^\s*item\s+(\d\.\d{2})\b", re.IGNORECASE | re.MULTILINE)
# Section headings that start boilerplate which adds tokens without adding signal. A heading
# must stand alone on its line or end in a colon or dash ("Forward-Looking Statements: This
# release..."), so a sentence that merely opens with "Signature Aviation" or "Safe Harbor
# Financial" is kept.
_BOILERPLATE_START = re.compile(
    r"^[ \t]*(?:"
    r"signatures?"
    r"|(?:(?:cautionary |special )?(?:note|statement)s? (?:regarding|concerning|about|on) )?"
    r"forward[- ]looking (?:statements?|information)"
    r"|cautionary (?:note|statement)s?"
    r"|safe harbor(?: statement)?"
    r"(?: (?:under the private securities litigation reform act(?: of 1995)?"
    r"|for forward[- ]looking statements))?"
    r")[ \t]*(?:[:\-–—]|$)",
    re.IGNORECASE | re.MULTILINE,
)
# Elements that start a new line. Everything else (a, b, i, span, font, ix:* ...) is inline and
# its text runs on with its neighbours, so a linked or bolded word stays inside its sentence.
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "body", "caption", "center", "dd", "div", "dl",
    "dt", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "hr", "html", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "td",
    "tfoot", "th", "thead", "tr", "ul",
})
_CELLS = ("td", "th")
# Cells that are only a currency sign or an opening paren belong to the value after them; cells
# that are only a closing paren or a percent sign belong to the value before them. Financial
# tables (Workiva, inline XBRL) put these in cells of their own.
_LEADS = frozenset("$€£¥(")
_TRAILS = frozenset(")%")
_CELL_SEP = " | "


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "title", "meta"]):
        tag.decompose()
    # Inline XBRL wraps facts in hidden headers; drop them.
    for tag in soup.find_all(style=re.compile(r"display:\s*none", re.IGNORECASE)):
        tag.decompose()
    # Real HTML: a newline in the source is just whitespace, like a space. (A plain-text filing
    # has no block tags, and there the newlines are the only structure we get.)
    is_html = soup.find(_BLOCK_TAGS) is not None
    text = _ZERO_WIDTH.sub("", "".join(_text_pieces(soup, is_html)))
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return _BLANKS.sub("\n\n", text).strip()


def _text_pieces(soup: BeautifulSoup, is_html: bool) -> list[str]:
    """The document's text in one walk of the tree, with a newline around each block element and
    for each <br>. A table row of plain cells comes out as one line (see `_flat_row`).

    The tree is read, never edited: bs4's insert/replace find a node by a linear scan of its
    parent's children, so editing a parent with thousands of block children is quadratic.
    """
    strings = soup.interesting_string_types
    pieces: list[str] = []
    # Each entry is (children still to visit, closing piece, inside <pre>). An explicit stack, so
    # deeply nested markup can't hit the recursion limit.
    stack = [(iter(soup.contents), "", False)]
    while stack:
        children, closing, in_pre = stack[-1]
        node = next(children, None)
        if node is None:
            stack.pop()
            pieces.append(closing)
            continue
        if isinstance(node, Tag):
            if node.name == "br":
                pieces.append("\n")
            elif is_html and node.name in _BLOCK_TAGS:
                row = _flat_row(node) if node.name == "tr" else None
                if row is not None:
                    pieces += ("\n", row, "\n")
                else:
                    pieces.append("\n")
                    stack.append((iter(node.contents), "\n", in_pre or node.name == "pre"))
            else:
                stack.append((iter(node.contents), "", in_pre))
        elif type(node) in strings:  # what get_text() reads: no comments, doctypes, ...
            text = str(node)
            if is_html and not in_pre and type(node) is NavigableString and "\n" in text:
                text = text.replace("\r", " ").replace("\n", " ")
            pieces.append(text)
    return pieces


def _flat_row(row: Tag) -> str | None:
    """A data row of a table as one line, its non-empty cells joined by " | ".

    None for a row with no cells, or whose cells hold block content (a layout table wrapping
    paragraphs, or a nested table): that row is walked like any other element, so its
    paragraphs still come out one per line.
    """
    cells = row.find_all(_CELLS, recursive=False)
    if not cells or any(cell.find(_BLOCK_TAGS) is not None for cell in cells):
        return None
    return _join_cells(_cell_text(cell) for cell in cells)


def _cell_text(cell: Tag) -> str:
    # A cell holding only a zero-width space is a spacer, like an empty one.
    return " ".join(_ZERO_WIDTH.sub("", cell.get_text()).split())


def _join_cells(texts: Iterable[str]) -> str:
    parts: list[str] = []
    glue_next = False
    for text in texts:
        if not text:  # spacer cell
            continue
        if parts and (glue_next or set(text) <= _TRAILS):
            parts[-1] += text
        else:
            parts.append(text)
        glue_next = set(text) <= _LEADS
    return _CELL_SEP.join(parts)


def drop_boilerplate(text: str) -> str:
    """Cut everything from the first signature or safe-harbor section onward."""
    # Only cut where the boilerplate starts after some real content. A notice at the very top
    # (press releases often open with one) is skipped, not treated as the end of the search.
    for m in _BOILERPLATE_START.finditer(text):
        if m.start() > 80:
            return text[: m.start()].rstrip()
    return text


def items_in(text: str) -> list[str]:
    seen: list[str] = []
    for m in _ITEM.finditer(text):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def exhibit_label(exhibit_type: str | None) -> str:
    """The heading for the exhibit in the state text, named after its document type (EX-99.2 ->
    EXHIBIT 99.2). Only EX-99.1 is called a press release: Jev reads the label, and calling an
    investor deck or a shareholder letter a press release suggests a results announcement.
    """
    kind = (exhibit_type or "EX-99.1").strip().upper()
    number = kind.removeprefix("EX-")
    if number in ("99.1", "99.01"):
        return "EXHIBIT 99.1 (press release)"
    return f"EXHIBIT {number}"


def build_state_text(main_html: str, exhibit_html: str | None, max_chars: int,
                     exhibit_type: str | None = None) -> str:
    """The 8-K body first (it says what happened), then the exhibit, usually the press release (it
    says how much). `exhibit_type` is the exhibit's document type from the index; None means EX-99.1.
    """
    main = drop_boilerplate(html_to_text(main_html))
    parts = [main]
    exhibit = drop_boilerplate(html_to_text(exhibit_html)) if exhibit_html else ""
    if exhibit:  # a label with nothing under it would make an empty filing look non-empty
        parts.append(exhibit_label(exhibit_type) + "\n" + exhibit)
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
