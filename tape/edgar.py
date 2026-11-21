"""EDGAR access: the latest-filings feed, daily indexes, filing index pages, and the ticker map.

Fair access: the SEC allows at most 10 requests per second and requires a User-Agent that
identifies the caller. We stay at 5 per second.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import feedparser
import httpx
from bs4 import BeautifulSoup

from tape.clean import build_state_text
from tape.db import Filing
from tape.files import write_atomic

SEC = "https://www.sec.gov"
ET = ZoneInfo("America/New_York")
FEED_SIZE = 100  # the most entries one feed request asks for
FEED_URL = (
    SEC + "/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include"
    f"&start=0&count={FEED_SIZE}&output=atom"
)
TICKERS_URL = SEC + "/files/company_tickers.json"
TICKERS_FILE = "company_tickers.json"
TICKERS_MAX_AGE = 24 * 3600  # the SEC regenerates the file daily
TICKERS_RETRY_AFTER = 300.0  # after a failed download, don't try again in this process for this long
# After a failed refresh that fell back to a stale cache, skip the download in every process for this
# long. Passes are separate processes, and each failed refresh can spend minutes in retries; a map a few
# hours older than usual costs almost nothing, since tickers rarely change.
TICKERS_STALE_RETRY_AFTER = 1800.0
TICKERS_FAILED_FILE = TICKERS_FILE + ".failed"
MAX_ATTEMPTS = 4
MAX_RETRY_AFTER = 60.0  # never wait longer than this on one Retry-After
# 429 and 503 mean slow down. 500, 502 and 504 come from EDGAR's front end during busy
# periods and usually clear within seconds, so they get the same backoff.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Phrases on the page EDGAR serves with a 403 when it blocks a client, as opposed to a plain 403
# for a path that doesn't exist (daily indexes for weekends and holidays).
BLOCK_MARKERS = ("Request Rate Threshold Exceeded", "Undeclared Automated Tool")

log = logging.getLogger(__name__)

_TITLE = re.compile(r"^(?P<form>\S+) - (?P<company>.+?) \((?P<cik>\d{10})\)")
_ACC = re.compile(r"(\d{10}-\d{2}-\d{6})")
_ITEM = re.compile(r"Item (\d\.\d{2})")
_INDEX_LINE = re.compile(r"^8-K\s+(?P<listing>.+?)\s+\d{8}\s+(?P<path>edgar/data/(?P<cik>\d+)/\S+)\s*$")


class EdgarBlockedError(httpx.HTTPStatusError):
    """EDGAR refused the request because it has blocked this client.

    The SEC blocks a host for about 10 minutes after it exceeds the fair-access rate, and
    refuses user agents without a contact outright. Every request fails the same way until
    the block lifts, so callers should stop rather than treat it as a per-URL failure.
    """


def is_block_page(resp: httpx.Response) -> bool:
    """True if `resp` is EDGAR's 403 block page rather than an ordinary 403."""
    if resp.status_code != 403:
        return False
    try:
        body = resp.text
    except (UnicodeDecodeError, httpx.ResponseNotRead):
        return False
    return any(marker in body for marker in BLOCK_MARKERS)


def is_outage(exc: Exception) -> bool:
    """Whether a failed request is about EDGAR rather than the URL that happened to hit it: a timeout, a
    dropped connection, a status that still means "slow down" or "try later" after Edgar.get's retries, or
    a 200 page that isn't a filing index at all (a maintenance or truncated page). A block is reported
    separately as EdgarBlockedError, and a 404 or an index page missing a field is about the filing.
    """
    if isinstance(exc, NotAnIndexPageError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRY_STATUSES
    return isinstance(exc, httpx.TransportError)


class EmptyFilingError(ValueError):
    """A filing whose documents yielded no text to score."""


class IndexPageError(ValueError):
    """A filing index page that is missing something every filing index has, such as its acceptance time."""


class NotAnIndexPageError(IndexPageError):
    """EDGAR answered a filing index URL with a page that has none of a filing index's header blocks:
    its maintenance page, or a page cut off under load. That is about EDGAR, not the filing."""


@dataclass(frozen=True)
class FilingRef:
    """What we know about a filing before opening it."""

    accession_no: str
    cik: str
    company: str
    form: str
    index_url: str


@dataclass(frozen=True)
class FeedPage:
    refs: list[FilingRef]
    # Whether EDGAR returned as many entries as we asked for, amendments and co-registrants included,
    # so older entries may have been cut off.
    full: bool


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
    def __init__(self, user_agent: str, client: httpx.Client | None = None, per_second: float = 5.0,
                 cache_dir: Path | None = None) -> None:
        self._cache_dir = cache_dir
        self._client = client or httpx.Client(timeout=20.0, follow_redirects=True)
        self._client.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self._throttle = Throttle(per_second)
        self._tickers: dict[str, str] | None = None
        self._tickers_failed_at: float | None = None

    def get(self, url: str) -> httpx.Response:
        for attempt in range(MAX_ATTEMPTS):
            self._throttle.wait()
            try:
                resp = self._client.get(url)
            except httpx.TransportError:
                # Connection resets and read timeouts are usually a blip; one shouldn't fail a pass.
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                log.warning("EDGAR request failed, retrying: %s", url, exc_info=True)
                time.sleep(2**attempt)
                continue
            # Back off and retry transient statuses, unless this was the last try.
            if resp.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS - 1:
                time.sleep(_retry_delay(resp, attempt))
                continue
            if is_block_page(resp):
                raise EdgarBlockedError(f"EDGAR blocked the request for {url}: 403 block page",
                                        request=resp.request, response=resp)
            resp.raise_for_status()
            return resp
        raise AssertionError("unreachable")  # the last attempt always returns or raises

    # Discovery ---------------------------------------------------------------------------

    def latest_8k(self) -> FeedPage:
        entries = feedparser.parse(self.get(FEED_URL).text).entries
        return FeedPage(_feed_refs(entries), len(entries) >= FEED_SIZE)

    def daily_8k(self, day: date) -> list[FilingRef] | None:
        """The 8-Ks in `day`'s index, or None if EDGAR has no index for that day. None is not the same
        as an empty list: an index with no 8-Ks is a quiet day, a missing one may be a late one.
        """
        q = (day.month - 1) // 3 + 1
        url = f"{SEC}/Archives/edgar/daily-index/{day.year}/QTR{q}/form.{day:%Y%m%d}.idx"
        try:
            return parse_form_index(self.get(url).text)
        except EdgarBlockedError:
            # Not a missing index: returning [] here would quietly drop a business day from a backfill.
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (403, 404):  # weekends and holidays have no index
                log.info("no daily index for %s (HTTP %d)", day, e.response.status_code)
                return None
            raise

    # Details -----------------------------------------------------------------------------

    def ticker_for(self, cik: str) -> str | None:
        """The ticker for `cik`, or None if it has none or the ticker map can't be loaded right now.

        A failed download (with no cached copy to fall back on) must not fail the filing that
        happened to ask first, and must not leave an empty map behind for the rest of the
        process. We return None and try again after TICKERS_RETRY_AFTER.
        """
        if self._tickers is None:
            failed_at = self._tickers_failed_at
            if failed_at is not None and time.monotonic() - failed_at < TICKERS_RETRY_AFTER:
                return None
            try:
                tickers = self._load_tickers()
            except (httpx.HTTPError, ValueError):
                self._tickers_failed_at = time.monotonic()
                log.warning("could not load the ticker map; storing filings without tickers for now",
                            exc_info=True)
                return None
            self._tickers = tickers
            self._tickers_failed_at = None
        return self._tickers.get(cik)

    def _load_tickers(self) -> dict[str, str]:
        """CIK -> ticker from the SEC ticker file, using the on-disk cache when it is less than a day old.

        A scheduled pass runs every few minutes in a fresh process, so without the cache we
        would download the same ~1 MB file a few hundred times a day. If the refresh fails we
        fall back to a stale copy rather than losing tickers for the whole pass. A download that
        parses as JSON but isn't a ticker map (an error body served with a 200) counts as a
        failed refresh, and is never written to the cache.

        A failed refresh with a stale copy is recorded next to the cache, and later processes use
        the stale copy without asking again until TICKERS_STALE_RETRY_AFTER has passed.
        """
        path = self._cache_dir / TICKERS_FILE if self._cache_dir is not None else None
        cached = _cached_tickers(path) if path is not None else None
        if path is not None and cached is not None:
            if time.time() - path.stat().st_mtime < TICKERS_MAX_AGE:
                return cached
            if _recent_refresh_failure(path.with_name(TICKERS_FAILED_FILE)):
                return cached
        try:
            resp = self.get(TICKERS_URL)
            tickers = parse_tickers(resp.json())
        except (httpx.HTTPError, ValueError):
            if cached is None:
                raise
            log.warning("ticker map refresh failed; using stale cache at %s", path, exc_info=True)
            if path is not None:
                _write_ticker_cache(path.with_name(TICKERS_FAILED_FILE), repr(time.time()))
            return cached
        if path is not None:
            _write_ticker_cache(path, resp.text)
            try:
                path.with_name(TICKERS_FAILED_FILE).unlink(missing_ok=True)
            except OSError:
                log.warning("could not clear the ticker refresh failure stamp in %s", path.parent)
        return tickers

    def fetch(self, ref: FilingRef, max_chars: int) -> Filing:
        index = parse_index_page(decode_document(self.get(ref.index_url)), ref.index_url)
        main_html = decode_document(self.get(index.main_url)) if index.main_url else ""
        exhibit_html = decode_document(self.get(index.exhibit_url)) if index.exhibit_url else None
        text = build_state_text(main_html, exhibit_html, max_chars, index.exhibit_type)
        if not text.strip():
            # Nothing to score: no 8-K or EX-99 document on the index page, or only images. Raise so
            # the filing isn't stored and a later pass can try again.
            raise EmptyFilingError(f"no text found for {ref.accession_no} at {ref.index_url}")
        return Filing(
            accession_no=ref.accession_no,
            cik=ref.cik,
            company=ref.company,
            form=ref.form,
            accepted_at=index.accepted_at,
            url=ref.index_url,
            items=tuple(index.items),
            ticker=self.ticker_for(ref.cik),
            clean_text=text,
        )


# Charsets browsers read as Windows-1252, which agrees with them on every printable character and also
# has the curly quotes and dashes filers paste from word processors in 0x80-0x9F.
_CP1252_ALIASES = frozenset({"ascii", "us-ascii", "iso-8859-1", "iso8859-1", "latin-1", "latin1", "l1"})


def decode_document(resp: httpx.Response) -> str:
    """The text of a filing document or index page.

    EDGAR serves documents as text/html without a charset, and filers don't all write UTF-8: many
    documents carry Windows-1252 or Latin-1 bytes (a raw NBSP, curly quotes, an accented name). httpx
    would decode those as UTF-8 with replacement characters, and a run of them can even form a valid
    UTF-8 sequence for an unrelated character. So use the declared charset when there is one, else
    UTF-8 if the bytes are valid UTF-8, else Windows-1252.
    """
    content = resp.content
    declared = (resp.charset_encoding or "").strip().lower()
    if declared in _CP1252_ALIASES:
        declared = "cp1252"
    if declared:
        try:
            return content.decode(declared, errors="replace")
        except LookupError:
            pass  # a charset Python doesn't know; work it out from the bytes instead
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("cp1252", errors="replace")


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying: the server's Retry-After if it gave one, else backoff.

    Only the delta-seconds form is honoured; an HTTP-date or garbage falls back to backoff.
    """
    try:
        delay = float(resp.headers["Retry-After"])
    except (KeyError, ValueError):
        delay = math.nan
    if not math.isfinite(delay):
        return float(2**attempt)
    return min(max(delay, 0.0), MAX_RETRY_AFTER)


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cached_tickers(path: Path) -> dict[str, str] | None:
    data = _read_json(path)
    if data is None:
        return None
    try:
        return parse_tickers(data)
    except ValueError:
        log.warning("ignoring ticker cache at %s: not a ticker map", path)
        return None


def _recent_refresh_failure(stamp: Path) -> bool:
    """Whether a ticker refresh failed less than TICKERS_STALE_RETRY_AFTER ago, per the stamp file.

    The stamp holds the failure time rather than relying on its mtime, so a missing or unreadable
    stamp, or one from the future (a clock step), simply allows a refresh.
    """
    try:
        failed_at = float(stamp.read_text())
    except (OSError, ValueError):
        return False
    return 0 <= time.time() - failed_at < TICKERS_STALE_RETRY_AFTER


def _write_ticker_cache(path: Path, text: str) -> None:
    try:
        write_atomic(path, text)
    except OSError:
        log.warning("could not write ticker cache to %s", path, exc_info=True)


# Parsers (pure functions, tested against saved pages) --------------------------------------


def parse_tickers(data: object) -> dict[str, str]:
    """CIK (10 digits) -> ticker from the parsed company_tickers.json.

    The file is {"0": {"cik_str": 320193, "ticker": "AAPL", "title": ...}, ...}. Malformed rows are
    skipped; ValueError if the file isn't that shape at all or has no usable row.
    """
    if not isinstance(data, dict):
        raise ValueError(f"ticker file is a {type(data).__name__}, not an object")
    tickers: dict[str, str] = {}
    bad = 0
    for row in data.values():
        cik = row.get("cik_str") if isinstance(row, dict) else None
        ticker = row.get("ticker") if isinstance(row, dict) else None
        if isinstance(cik, str) and cik.strip().isdigit():
            cik = int(cik)
        if isinstance(cik, bool) or not isinstance(cik, int) or cik <= 0 or not isinstance(ticker, str) \
                or not ticker.strip():
            bad += 1
            continue
        # First listing wins; it is usually the common stock.
        tickers.setdefault(f"{cik:010d}", ticker.strip())
    if not tickers:
        raise ValueError(f"ticker file has no usable rows ({bad} malformed)")
    if bad:
        log.warning("skipped %d malformed rows in the ticker file", bad)
    return tickers


def parse_feed(xml: str) -> list[FilingRef]:
    return _feed_refs(feedparser.parse(xml).entries)


def _feed_refs(entries: list) -> list[FilingRef]:
    refs = []
    for e in entries:
        m = _TITLE.match(e.get("title", ""))
        acc = _ACC.search(e.get("id", "") + " " + e.get("summary", ""))
        if not m or not acc or m["form"] != "8-K":  # skip 8-K/A amendments
            continue
        refs.append(FilingRef(acc[1], m["cik"], m["company"].strip(), m["form"], e.link))
    return _dedupe(refs)


def parse_form_index(text: str) -> list[FilingRef]:
    """Parse the 8-Ks out of an EDGAR daily form.idx file (fixed-width columns after a dashed rule).

    Fields are read from the ends of the line, not split on spacing: a company name can hold a double
    space, or fill its column and run straight into the CIK. The path's folder gives the CIK, so a name
    ending in digits can't be mistaken for part of it. A line we can't read is a filing lost for good,
    since the backfill is what repairs the feed, so it gets a warning.
    """
    refs = []
    body = text.split("\n-", 1)[-1].split("\n", 1)[-1]
    for line in body.splitlines():
        if line.startswith("8-K "):
            ref = _parse_index_line(line)
            if ref is None:
                log.warning("skipping an 8-K line we can't read in the daily index: %r", line)
            else:
                refs.append(ref)
    return _dedupe(refs)


def _parse_index_line(line: str) -> FilingRef | None:
    m = _INDEX_LINE.match(line)
    acc = _ACC.search(m["path"]) if m else None
    if not m or not acc or not m["listing"].endswith(m["cik"]):
        return None
    company = " ".join(m["listing"].removesuffix(m["cik"]).split())
    if not company:
        return None
    acc_no = acc[1]
    cik = int(m["cik"])
    folder = f"{SEC}/Archives/edgar/data/{cik}/{acc_no.replace('-', '')}"
    return FilingRef(acc_no, f"{cik:010d}", company, "8-K", f"{folder}/{acc_no}-index.htm")


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


# Document types EDGAR filers use for the press release exhibit. Most write EX-99.1; a few pad it.
PRESS_RELEASE_TYPES = frozenset({"EX-99.1", "EX-99.01"})


@dataclass(frozen=True)
class IndexPage:
    accepted_at: str
    items: list[str]
    main_url: str | None
    exhibit_url: str | None
    # The exhibit's document type as the index lists it (EX-99.1, EX-99.2, EX-99), so the state
    # text can say which exhibit it is rather than assume a press release.
    exhibit_type: str | None = None


def parse_index_page(html: str, base_url: str) -> IndexPage:
    soup = BeautifulSoup(html, "html.parser")
    info: dict[str, str] = {}
    for head in soup.select("div.infoHead"):
        value = head.find_next_sibling("div", class_="info")
        if value is not None:
            info[head.get_text(strip=True)] = value.get_text("\n", strip=True)

    if not info:
        raise NotAnIndexPageError(f"{base_url} is not a filing index page")
    try:
        accepted = datetime.strptime(info["Accepted"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
    except KeyError:
        raise IndexPageError(f"{base_url} has no Accepted time") from None
    except ValueError:
        raise IndexPageError(f"{base_url} has an unreadable Accepted time: {info['Accepted']!r}") from None
    items = _ITEM.findall(info.get("Items", ""))

    main_url = None
    exhibits: list[tuple[str, str]] = []  # (type, url) of each EX-99* in index order
    for row in soup.select("table.tableFile tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        link = cells[2].find("a")
        doc_type = cells[3].get_text(strip=True).upper()
        href = link.get("href") if link is not None else None
        if not isinstance(href, str) or not href:
            continue
        href = href.replace("/ix?doc=", "")
        url = urljoin(base_url, href)
        if doc_type == "8-K" and main_url is None:
            main_url = url
        elif doc_type.startswith("EX-99"):
            exhibits.append((doc_type, url))
    # The press release when there is one, wherever the index lists it; otherwise the first EX-99
    # (an investor presentation, a shareholder letter) is still the best text we have.
    exhibit = next((e for e in exhibits if e[0] in PRESS_RELEASE_TYPES), exhibits[0] if exhibits else None)
    exhibit_type, exhibit_url = exhibit if exhibit else (None, None)
    return IndexPage(accepted.isoformat(), items, main_url, exhibit_url, exhibit_type)
