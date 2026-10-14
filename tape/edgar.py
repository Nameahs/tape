"""EDGAR access: the latest-filings feed, daily indexes, filing index pages, and the ticker map.

Fair access: the SEC allows at most 10 requests per second and requires a User-Agent that
identifies the caller. We stay at 5 per second.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import feedparser
import httpx
from bs4 import BeautifulSoup

from tape.clean import build_state_text
from tape.db import Filing

SEC = "https://www.sec.gov"
ET = ZoneInfo("America/New_York")
FEED_URL = (
    SEC + "/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include"
    "&start=0&count=100&output=atom"
)
TICKERS_URL = SEC + "/files/company_tickers.json"

_TITLE = re.compile(r"^(?P<form>\S+) - (?P<company>.+?) \((?P<cik>\d{10})\)")
_ACC = re.compile(r"(\d{10}-\d{2}-\d{6})")
_ITEM = re.compile(r"Item (\d\.\d{2})")


@dataclass(frozen=True)
class FilingRef:
    """What we know about a filing before opening it."""

    accession_no: str
    cik: str
    company: str
    form: str
    index_url: str


class Throttle:
    def __init__(self, per_second: float = 5.0) -> None:
        self._interval = 1.0 / per_second
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = self._next
            self._next = now + self._interval


class Edgar:
    def __init__(self, user_agent: str, client: httpx.Client | None = None, per_second: float = 5.0) -> None:
        self._client = client or httpx.Client(timeout=20.0, follow_redirects=True)
        self._client.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self._throttle = Throttle(per_second)
        self._tickers: dict[str, str] | None = None

    def get(self, url: str) -> httpx.Response:
        for attempt in range(4):
            self._throttle.wait()
            resp = self._client.get(url)
            # 429 and 503 mean slow down; back off and retry.
            if resp.status_code in (429, 503):
                time.sleep(2**attempt)
                continue
            resp.raise_for_status()
            return resp
        resp.raise_for_status()
        return resp

    # Discovery ---------------------------------------------------------------------------

    def latest_8k(self) -> list[FilingRef]:
        return parse_feed(self.get(FEED_URL).text)

    def daily_8k(self, day: date) -> list[FilingRef]:
        q = (day.month - 1) // 3 + 1
        url = f"{SEC}/Archives/edgar/daily-index/{day.year}/QTR{q}/form.{day:%Y%m%d}.idx"
        try:
            return parse_form_index(self.get(url).text)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (403, 404):  # weekends and holidays have no index
                return []
            raise

    # Details -----------------------------------------------------------------------------

    def ticker_for(self, cik: str) -> str | None:
        if self._tickers is None:
            data = self.get(TICKERS_URL).json()
            self._tickers = {}
            for row in data.values():
                # First listing wins; it is usually the common stock.
                self._tickers.setdefault(f"{int(row['cik_str']):010d}", row["ticker"])
        return self._tickers.get(cik)

    def fetch(self, ref: FilingRef, max_chars: int) -> Filing:
        index = parse_index_page(self.get(ref.index_url).text, ref.index_url)
        main_html = self.get(index.main_url).text if index.main_url else ""
        exhibit_html = self.get(index.exhibit_url).text if index.exhibit_url else None
        return Filing(
            accession_no=ref.accession_no,
            cik=ref.cik,
            company=ref.company,
            form=ref.form,
            accepted_at=index.accepted_at,
            url=ref.index_url,
            items=tuple(index.items),
            ticker=self.ticker_for(ref.cik),
            clean_text=build_state_text(main_html, exhibit_html, max_chars),
        )


# Parsers (pure functions, tested against saved pages) --------------------------------------


def parse_feed(xml: str) -> list[FilingRef]:
    refs = []
    for e in feedparser.parse(xml).entries:
        m = _TITLE.match(e.get("title", ""))
        acc = _ACC.search(e.get("id", "") + " " + e.get("summary", ""))
        if not m or not acc or m["form"] != "8-K":  # skip 8-K/A amendments
            continue
        refs.append(FilingRef(acc[1], m["cik"], m["company"].strip(), m["form"], e.link))
    return _dedupe(refs)


def parse_form_index(text: str) -> list[FilingRef]:
    """Parse an EDGAR daily form.idx file (fixed-width columns after a dashed rule)."""
    refs = []
    body = text.split("\n-", 1)[-1].split("\n", 1)[-1]
    for line in body.splitlines():
        if not line.startswith("8-K "):
            continue
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5:
            continue
        form, company, cik, _filed, path = parts[:5]
        acc = _ACC.search(path)
        if not acc:
            continue
        acc_no = acc[1]
        folder = f"{SEC}/Archives/edgar/data/{int(cik)}/{acc_no.replace('-', '')}"
        refs.append(FilingRef(acc_no, f"{int(cik):010d}", company, form, f"{folder}/{acc_no}-index.htm"))
    return _dedupe(refs)


def _dedupe(refs: list[FilingRef]) -> list[FilingRef]:
    """Drop repeat accession numbers, keeping the first listing.

    A filing made jointly by several registrants (a parent and its operating subsidiary, say)
    is listed once per filer under the same accession number. It is one document, so we only
    want to fetch and score it once.
    """
    seen: set[str] = set()
    out = []
    for ref in refs:
        if ref.accession_no not in seen:
            seen.add(ref.accession_no)
            out.append(ref)
    return out


@dataclass(frozen=True)
class IndexPage:
    accepted_at: str
    items: list[str]
    main_url: str | None
    exhibit_url: str | None


def parse_index_page(html: str, base_url: str) -> IndexPage:
    soup = BeautifulSoup(html, "html.parser")
    info: dict[str, str] = {}
    for head in soup.select("div.infoHead"):
        value = head.find_next_sibling("div", class_="info")
        if value is not None:
            info[head.get_text(strip=True)] = value.get_text("\n", strip=True)

    accepted = datetime.strptime(info["Accepted"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
    items = _ITEM.findall(info.get("Items", ""))

    main_url = exhibit_url = None
    for row in soup.select("table.tableFile tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        link = cells[2].find("a")
        doc_type = cells[3].get_text(strip=True).upper()
        if link is None or not link.get("href"):
            continue
        href = link["href"].replace("/ix?doc=", "")
        url = urljoin(base_url, href)
        if doc_type == "8-K" and main_url is None:
            main_url = url
        elif doc_type.startswith("EX-99") and exhibit_url is None:
            exhibit_url = url
    return IndexPage(accepted.isoformat(), items, main_url, exhibit_url)
