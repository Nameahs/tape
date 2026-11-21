import os
import threading
import time
from datetime import date
from pathlib import Path

import feedparser
import httpx
import pytest
import respx

from tape.edgar import (
    FEED_SIZE,
    FEED_URL,
    MAX_ATTEMPTS,
    TICKERS_STALE_RETRY_AFTER,
    TICKERS_URL,
    Edgar,
    EdgarBlockedError,
    EmptyFilingError,
    FilingRef,
    IndexPageError,
    NotAnIndexPageError,
    Throttle,
    is_outage,
    parse_feed,
    parse_form_index,
    parse_index_page,
    parse_tickers,
)

FIX = Path(__file__).parent / "fixtures"
FOLDER = "https://www.sec.gov/Archives/edgar/data/1/000000000126000101"
INDEX = f"{FOLDER}/0000000001-26-000101-index.htm"


def test_parse_feed_skips_amendments():
    refs = parse_feed((FIX / "feed.atom").read_text())
    assert refs == [FilingRef("0000000001-26-000101", "0000000001", "Example Corp", "8-K", INDEX)]


@pytest.mark.parametrize("link", ["", '<link rel="alternate" href="https://example.org/filing"/>'])
def test_parse_feed_reconstructs_unusable_index_url(link):
    xml = f"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <title>8-K - Example Corp (0000000001) (Filer)</title>
    <id>urn:tag:sec.gov,2008:accession-number=0000000001-26-000101</id>
    {link}
    </entry></feed>"""
    assert parse_feed(xml) == [FilingRef("0000000001-26-000101", "0000000001", "Example Corp", "8-K", INDEX)]


def test_parse_form_index():
    refs = parse_form_index((FIX / "form.idx").read_text())
    assert [r.accession_no for r in refs] == ["0000000001-26-000101"]
    assert refs[0].index_url == INDEX
    assert refs[0].cik == "0000000001"


def test_parse_index_page():
    page = parse_index_page((FIX / "index.htm").read_text(), INDEX)
    assert page.accepted_at == "2026-10-13T16:31:02-04:00"
    assert page.items == ["5.02", "9.01"]
    assert page.main_url == f"{FOLDER}/exmp-20261013.htm"
    assert page.exhibit_url == f"{FOLDER}/ex991.htm"
    assert page.exhibit_type == "EX-99.1"


EX991_ROW = ('<tr><td>2</td><td>PRESS RELEASE</td><td><a href="/Archives/edgar/data/1/000000000126000101/ex991.htm">'
             "ex991.htm</a></td><td>EX-99.1</td><td>12000</td></tr>")


def _doc_row(name, doc_type):
    return (f'<tr><td>3</td><td>EXHIBIT</td><td><a href="/Archives/edgar/data/1/000000000126000101/{name}">'
            f"{name}</a></td><td>{doc_type}</td><td>9000</td></tr>")


def _index_with_exhibits(*rows):
    html = (FIX / "index.htm").read_text()
    assert EX991_ROW in html
    return html.replace(EX991_ROW, "".join(rows))


def test_parse_index_page_prefers_the_press_release_wherever_it_is_listed():
    html = _index_with_exhibits(_doc_row("ex992.htm", "EX-99.2"), EX991_ROW)
    page = parse_index_page(html, INDEX)
    assert (page.exhibit_type, page.exhibit_url) == ("EX-99.1", f"{FOLDER}/ex991.htm")


@pytest.mark.parametrize("doc_type", ["EX-99.2", "EX-99", "ex-99.3"])
def test_parse_index_page_falls_back_to_the_first_other_ex99(doc_type):
    html = _index_with_exhibits(_doc_row("deck.htm", doc_type), _doc_row("later.htm", "EX-99.4"))
    page = parse_index_page(html, INDEX)
    assert (page.exhibit_type, page.exhibit_url) == (doc_type.upper(), f"{FOLDER}/deck.htm")


def test_parse_index_page_accepts_a_padded_press_release_type():
    html = _index_with_exhibits(_doc_row("deck.htm", "EX-99.2"), _doc_row("pr.htm", "EX-99.01"))
    assert parse_index_page(html, INDEX).exhibit_type == "EX-99.01"


def test_parse_index_page_without_an_exhibit():
    page = parse_index_page(_index_with_exhibits(), INDEX)
    assert (page.exhibit_type, page.exhibit_url) == (None, None)


MAINTENANCE_PAGE = "<html><body><h1>EDGAR is undergoing maintenance</h1><p>Please try again later.</p></body></html>"
ACCEPTED_LINE = '<div class="infoHead">Accepted</div><div class="info">2026-10-13 16:31:02</div>'


def _index_with_accepted(line):
    html = (FIX / "index.htm").read_text()
    assert ACCEPTED_LINE in html
    return html.replace(ACCEPTED_LINE, line)


@pytest.mark.parametrize("html", [MAINTENANCE_PAGE, "", (FIX / "index.htm").read_text()[:40]],
                         ids=["maintenance", "empty", "truncated"])
def test_parse_index_page_rejects_a_page_that_is_not_a_filing_index(html):
    with pytest.raises(NotAnIndexPageError, match="not a filing index page"):
        parse_index_page(html, INDEX)


@pytest.mark.parametrize(("line", "message"), [
    ("", "has no Accepted time"),
    ('<div class="infoHead">Accepted</div><div class="info">soon</div>', "unreadable Accepted time: 'soon'"),
], ids=["missing", "garbled"])
def test_parse_index_page_names_a_missing_or_unreadable_accepted_time(line, message):
    with pytest.raises(IndexPageError, match=message) as info:
        parse_index_page(_index_with_accepted(line), INDEX)
    assert not isinstance(info.value, NotAnIndexPageError)
    assert INDEX in str(info.value)


@pytest.mark.parametrize(("exc", "outage"), [
    (NotAnIndexPageError("x"), True),
    (IndexPageError("x"), False),
    (EmptyFilingError("x"), False),
    (httpx.ReadTimeout("slow"), True),
])
def test_is_outage_for_index_page_errors(exc, outage):
    assert is_outage(exc) is outage


def _mock_filing(router):
    router.get(INDEX).respond(200, text=(FIX / "index.htm").read_text())
    router.get(f"{FOLDER}/exmp-20261013.htm").respond(200, text=(FIX / "main_8k.htm").read_text())
    router.get(f"{FOLDER}/ex991.htm").respond(200, text=(FIX / "ex991.htm").read_text())
    router.get(TICKERS_URL).respond(200, text=(FIX / "company_tickers.json").read_text())


@respx.mock
def test_end_to_end_fetch_sends_user_agent():
    respx.get(FEED_URL).respond(200, text=(FIX / "feed.atom").read_text())
    _mock_filing(respx)
    edgar = Edgar("Tape test test@example.com", per_second=1000)
    [ref] = edgar.latest_8k().refs
    filing = edgar.fetch(ref, max_chars=5000)
    assert filing.ticker == "EXMP"  # first listing wins over the preferred share
    assert filing.items == ("5.02", "9.01")
    assert "interim Chief Executive Officer" in filing.clean_text
    assert all(c.request.headers["User-Agent"] == "Tape test test@example.com" for c in respx.calls)


@respx.mock
def test_fetch_labels_the_exhibit_it_used():
    respx.get(INDEX).respond(200, text=_index_with_exhibits(_doc_row("deck.htm", "EX-99.2")))
    respx.get(f"{FOLDER}/exmp-20261013.htm").respond(200, text=(FIX / "main_8k.htm").read_text())
    respx.get(f"{FOLDER}/deck.htm").respond(200, text="<html><body><p>Investor day slides.</p></body></html>")
    respx.get(TICKERS_URL).respond(200, text=(FIX / "company_tickers.json").read_text())
    ref = FilingRef("0000000001-26-000101", "0000000001", "Example Corp", "8-K", INDEX)
    text = Edgar("Tape test test@example.com", per_second=1000).fetch(ref, max_chars=100_000).clean_text
    assert "EXHIBIT 99.2\nInvestor day slides." in text
    assert "press release" not in text


def _index_without_documents():
    """The fixture index page with its document table emptied."""
    html = (FIX / "index.htm").read_text()
    start = html.index('<table class="tableFile"')
    end = html.index("</table>", start) + len("</table>")
    return html[:start] + html[end:]


@respx.mock
def test_fetch_rejects_a_filing_with_no_documents():
    respx.get(INDEX).respond(200, text=_index_without_documents())
    edgar = Edgar("ua", per_second=1000)
    ref = FilingRef("0000000001-26-000101", "0000000001", "Example Corp", "8-K", INDEX)
    with pytest.raises(EmptyFilingError, match="0000000001-26-000101"):
        edgar.fetch(ref, max_chars=5000)


@respx.mock
def test_fetch_rejects_a_main_document_with_no_text():
    _mock_filing(respx)
    respx.get(f"{FOLDER}/exmp-20261013.htm").respond(200, text="<html><body><img src='p1.jpg'></body></html>")
    respx.get(f"{FOLDER}/ex991.htm").respond(200, text="<html><body></body></html>")
    edgar = Edgar("ua", per_second=1000)
    ref = FilingRef("0000000001-26-000101", "0000000001", "Example Corp", "8-K", INDEX)
    with pytest.raises(EmptyFilingError):
        edgar.fetch(ref, max_chars=5000)


@respx.mock
def test_retries_on_429(monkeypatch):
    monkeypatch.setattr("tape.edgar.time.sleep", lambda s: None)
    route = respx.get(FEED_URL)
    route.side_effect = [httpx.Response(429), httpx.Response(200, text=(FIX / "feed.atom").read_text())]
    assert len(Edgar("ua", per_second=1000).latest_8k().refs) == 1


@respx.mock
def test_retries_transport_errors(monkeypatch):
    monkeypatch.setattr("tape.edgar.time.sleep", lambda s: None)
    route = respx.get(FEED_URL)
    route.side_effect = [
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("reset"),
        httpx.Response(200, text=(FIX / "feed.atom").read_text()),
    ]
    assert len(Edgar("ua", per_second=1000).latest_8k().refs) == 1
    assert route.call_count == 3


@respx.mock
def test_transport_error_raised_after_last_attempt(monkeypatch):
    monkeypatch.setattr("tape.edgar.time.sleep", lambda s: None)
    route = respx.get(FEED_URL)
    route.side_effect = httpx.ConnectError("down")
    with pytest.raises(httpx.ConnectError):
        Edgar("ua", per_second=1000).latest_8k()
    assert route.call_count == 4

@respx.mock
def test_honours_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr("tape.edgar.Throttle.wait", lambda self: None)
    monkeypatch.setattr("tape.edgar.time.sleep", sleeps.append)
    route = respx.get(FEED_URL)
    route.side_effect = [
        httpx.Response(503, headers={"Retry-After": "7"}),
        httpx.Response(429, headers={"Retry-After": "3600"}),
        httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        httpx.Response(200, text=(FIX / "feed.atom").read_text()),
    ]
    assert len(Edgar("ua", per_second=1000).latest_8k().refs) == 1
    assert sleeps == [7.0, 60.0, 4.0]  # as asked, capped, then backoff for a date we don't parse


@respx.mock
def test_gives_up_on_429_without_a_final_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr("tape.edgar.Throttle.wait", lambda self: None)
    monkeypatch.setattr("tape.edgar.time.sleep", sleeps.append)
    route = respx.get(FEED_URL).respond(429)
    with pytest.raises(httpx.HTTPStatusError):
        Edgar("ua", per_second=1000).latest_8k()
    assert route.call_count == 4
    assert sleeps == [1.0, 2.0, 4.0]

@respx.mock
@pytest.mark.parametrize("status", [500, 502, 504])
def test_retries_server_errors(monkeypatch, status):
    monkeypatch.setattr("tape.edgar.time.sleep", lambda s: None)
    route = respx.get(FEED_URL)
    route.side_effect = [httpx.Response(status), httpx.Response(200, text=(FIX / "feed.atom").read_text())]
    assert len(Edgar("ua", per_second=1000).latest_8k().refs) == 1
    assert route.call_count == 2


@respx.mock
def test_gives_up_on_repeated_504(monkeypatch):
    sleeps = []
    monkeypatch.setattr("tape.edgar.Throttle.wait", lambda self: None)
    monkeypatch.setattr("tape.edgar.time.sleep", sleeps.append)
    route = respx.get(FEED_URL).respond(504)
    with pytest.raises(httpx.HTTPStatusError):
        Edgar("ua", per_second=1000).latest_8k()
    assert route.call_count == 4
    assert sleeps == [1.0, 2.0, 4.0]


@respx.mock
@pytest.mark.parametrize("status", [400, 404, 501])
def test_does_not_retry_permanent_errors(monkeypatch, status):
    sleeps = []
    monkeypatch.setattr("tape.edgar.time.sleep", sleeps.append)
    route = respx.get(FEED_URL).respond(status)
    with pytest.raises(httpx.HTTPStatusError):
        Edgar("ua", per_second=1000).latest_8k()
    assert route.call_count == 1
    assert sleeps == []


RATE_LIMIT_PAGE = (
    "<html><head><title>SEC.gov | Request Rate Threshold Exceeded</title></head>"
    "<body><h1>Your Request Originates from an Undeclared Automated Tool</h1></body></html>"
)


@respx.mock
@pytest.mark.parametrize("marker", ["Request Rate Threshold Exceeded", "Undeclared Automated Tool"])
def test_block_page_raises_blocked_error(marker):
    route = respx.get(FEED_URL).respond(403, text=f"<html><body><h1>{marker}</h1></body></html>")
    with pytest.raises(EdgarBlockedError) as exc:
        Edgar("ua", per_second=1000).latest_8k()
    assert isinstance(exc.value, httpx.HTTPStatusError)
    assert exc.value.response.status_code == 403
    assert route.call_count == 1  # a block lasts minutes; retrying within the request only extends it


@respx.mock
def test_plain_403_is_an_ordinary_status_error():
    respx.get(FEED_URL).respond(403, text="<html><body>Forbidden</body></html>")
    with pytest.raises(httpx.HTTPStatusError) as exc:
        Edgar("ua", per_second=1000).latest_8k()
    assert not isinstance(exc.value, EdgarBlockedError)


@respx.mock
def test_daily_index_missing_is_none():
    respx.get("https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.20261011.idx").respond(404)
    assert Edgar("ua", per_second=1000).daily_8k(date(2026, 10, 11)) is None


@respx.mock
def test_daily_index_plain_403_is_none(caplog):
    respx.get("https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.20261012.idx").respond(
        403, text="<html><body>Forbidden</body></html>")
    with caplog.at_level("INFO", logger="tape.edgar"):
        assert Edgar("ua", per_second=1000).daily_8k(date(2026, 10, 12)) is None
    assert "no daily index for 2026-10-12 (HTTP 403)" in caplog.text


@respx.mock
def test_daily_index_block_page_raises():
    respx.get("https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.20261013.idx").respond(
        403, text=RATE_LIMIT_PAGE)
    with pytest.raises(EdgarBlockedError):
        Edgar("ua", per_second=1000).daily_8k(date(2026, 10, 13))


def _tickers_text():
    return (FIX / "company_tickers.json").read_text()


@respx.mock
def test_ticker_map_is_cached_on_disk(tmp_path):
    route = respx.get(TICKERS_URL).respond(200, text=_tickers_text())
    assert Edgar("ua", per_second=1000, cache_dir=tmp_path).ticker_for("0000000001") == "EXMP"
    assert (tmp_path / "company_tickers.json").exists()
    # A new process (new Edgar) reads the cache instead of downloading again.
    assert Edgar("ua", per_second=1000, cache_dir=tmp_path).ticker_for("0000000001") == "EXMP"
    assert route.call_count == 1


@respx.mock
def test_stale_ticker_cache_is_refreshed(tmp_path):
    cache = tmp_path / "company_tickers.json"
    cache.write_text('{"0": {"cik_str": 1, "ticker": "OLD", "title": "Example Corp"}}')
    day_old = time.time() - 25 * 3600
    os.utime(cache, (day_old, day_old))
    route = respx.get(TICKERS_URL).respond(200, text=_tickers_text())
    assert Edgar("ua", per_second=1000, cache_dir=tmp_path).ticker_for("0000000001") == "EXMP"
    assert route.call_count == 1
    assert "EXMP" in cache.read_text()


@respx.mock
def test_stale_ticker_cache_used_when_refresh_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("tape.edgar.time.sleep", lambda s: None)
    cache = tmp_path / "company_tickers.json"
    cache.write_text('{"0": {"cik_str": 1, "ticker": "OLD", "title": "Example Corp"}}')
    day_old = time.time() - 25 * 3600
    os.utime(cache, (day_old, day_old))
    respx.get(TICKERS_URL).respond(500)
    assert Edgar("ua", per_second=1000, cache_dir=tmp_path).ticker_for("0000000001") == "OLD"


@respx.mock
def test_ticker_download_failure_on_cold_cache_does_not_fail_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr("tape.edgar.time.sleep", lambda s: None)
    _mock_filing(respx)
    respx.get(TICKERS_URL).respond(500)
    edgar = Edgar("ua", per_second=1000, cache_dir=tmp_path)
    filing = edgar.fetch(FilingRef("0000000001-26-000101", "0000000001", "Example Corp", "8-K", INDEX), 5000)
    assert filing.ticker is None
    assert filing.clean_text


@respx.mock
def test_ticker_map_is_retried_after_a_failed_download(tmp_path, monkeypatch):
    monkeypatch.setattr("tape.edgar.time.sleep", lambda s: None)
    clock = [1000.0]
    monkeypatch.setattr("tape.edgar.time.monotonic", lambda: clock[0])
    route = respx.get(TICKERS_URL)
    route.side_effect = [httpx.Response(503)] * 4 + [httpx.Response(200, text=_tickers_text())]
    edgar = Edgar("ua", per_second=1000, cache_dir=tmp_path)
    assert edgar.ticker_for("0000000001") is None
    assert edgar.ticker_for("0000000001") is None  # within the retry window: no new download
    assert route.call_count == 4
    clock[0] += 301
    assert edgar.ticker_for("0000000001") == "EXMP"
    assert route.call_count == 5


def _stale_cache(tmp_path, now):
    cache = tmp_path / "company_tickers.json"
    cache.write_text('{"0": {"cik_str": 1, "ticker": "OLD", "title": "Example Corp"}}')
    os.utime(cache, (now - 25 * 3600, now - 25 * 3600))
    return cache


@respx.mock
def test_failed_refresh_with_stale_cache_backs_off_across_processes(tmp_path, monkeypatch):
    monkeypatch.setattr("tape.edgar.time.sleep", lambda s: None)
    now = time.time()
    clock = [now]
    monkeypatch.setattr("tape.edgar.time.time", lambda: clock[0])
    _stale_cache(tmp_path, now)
    route = respx.get(TICKERS_URL)
    route.side_effect = [httpx.Response(503)] * MAX_ATTEMPTS + [httpx.Response(200, text=_tickers_text())]

    assert Edgar("ua", per_second=1000, cache_dir=tmp_path).ticker_for("0000000001") == "OLD"
    assert route.call_count == MAX_ATTEMPTS
    # The next pass is a new process: it uses the stale map without asking the SEC again.
    clock[0] += TICKERS_STALE_RETRY_AFTER - 1
    assert Edgar("ua", per_second=1000, cache_dir=tmp_path).ticker_for("0000000001") == "OLD"
    assert route.call_count == MAX_ATTEMPTS
    # Once the back-off has passed, a pass tries again, and a success clears the stamp.
    clock[0] += 2
    assert Edgar("ua", per_second=1000, cache_dir=tmp_path).ticker_for("0000000001") == "EXMP"
    assert route.call_count == MAX_ATTEMPTS + 1
    assert not (tmp_path / "company_tickers.json.failed").exists()


@respx.mock
def test_failure_stamp_from_the_future_does_not_block_refresh(tmp_path):
    _stale_cache(tmp_path, time.time())
    (tmp_path / "company_tickers.json.failed").write_text(repr(time.time() + 3600))
    route = respx.get(TICKERS_URL).respond(200, text=_tickers_text())
    assert Edgar("ua", per_second=1000, cache_dir=tmp_path).ticker_for("0000000001") == "EXMP"
    assert route.call_count == 1


@respx.mock
def test_unreadable_failure_stamp_is_ignored(tmp_path):
    _stale_cache(tmp_path, time.time())
    (tmp_path / "company_tickers.json.failed").write_text("not a time")
    route = respx.get(TICKERS_URL).respond(200, text=_tickers_text())
    assert Edgar("ua", per_second=1000, cache_dir=tmp_path).ticker_for("0000000001") == "EXMP"
    assert route.call_count == 1


@respx.mock
def test_failure_stamp_does_not_block_a_cold_cache(tmp_path, monkeypatch):
    # With no cached map there is nothing to fall back on, so the stamp must not stop a download.
    (tmp_path / "company_tickers.json.failed").write_text(repr(time.time()))
    route = respx.get(TICKERS_URL).respond(200, text=_tickers_text())
    assert Edgar("ua", per_second=1000, cache_dir=tmp_path).ticker_for("0000000001") == "EXMP"
    assert route.call_count == 1


@respx.mock
def test_corrupt_ticker_cache_is_replaced(tmp_path):
    (tmp_path / "company_tickers.json").write_text('{"0": {"cik_str"')
    respx.get(TICKERS_URL).respond(200, text=_tickers_text())
    assert Edgar("ua", per_second=1000, cache_dir=tmp_path).ticker_for("0000000001") == "EXMP"
    assert "EXMP" in (tmp_path / "company_tickers.json").read_text()


_CO_REG_ENTRY = """<entry>
<title>8-K - {company} ({cik}) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/{n}/000000000426000011/0000000004-26-000011-index.htm"/>
<summary type="html"> &lt;b&gt;AccNo:&lt;/b&gt; 0000000004-26-000011</summary>
<id>urn:tag:sec.gov,2008:accession-number=0000000004-26-000011</id>
</entry>"""


@respx.mock
@pytest.mark.parametrize(("entries", "full"), [(FEED_SIZE - 1, False), (FEED_SIZE, True)])
def test_latest_8k_reports_a_full_feed_counting_every_entry(entries, full):
    # Amendments are dropped from refs but still take up room on the page.
    body = "".join(
        f"<entry><title>8-K/A - Example Corp (0000000001) (Filer)</title><id>0000000001-26-{n:06d}</id></entry>"
        for n in range(entries)
    )
    respx.get(FEED_URL).respond(200, text=f'<feed xmlns="http://www.w3.org/2005/Atom">{body}</feed>')
    page = Edgar("ua", per_second=1000).latest_8k()
    assert page.refs == [] and page.full is full


@respx.mock
def test_latest_8k_parses_the_feed_once(monkeypatch):
    calls = []
    real_parse = feedparser.parse

    def counting_parse(*args, **kwargs):
        calls.append(args)
        return real_parse(*args, **kwargs)

    monkeypatch.setattr("tape.edgar.feedparser.parse", counting_parse)
    respx.get(FEED_URL).respond(200, text=(FIX / "feed.atom").read_text())
    page = Edgar("ua", per_second=1000).latest_8k()
    assert [r.accession_no for r in page.refs] == ["0000000001-26-000101"] and page.full is False
    assert len(calls) == 1


def test_parse_feed_dedupes_co_registrants():
    entries = _CO_REG_ENTRY.format(company="Parent Utility Inc", cik="0000000004", n=4) + _CO_REG_ENTRY.format(
        company="Parent Utility Operating Co", cik="0000000005", n=5
    )
    xml = f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'
    refs = parse_feed(xml)
    assert len(refs) == 1
    assert refs[0].accession_no == "0000000004-26-000011"
    assert refs[0].company == "Parent Utility Inc"  # first listing wins


def _form_index(*lines: str) -> str:
    return "Form Type   Company Name\n" + "-" * 80 + "\n" + "\n".join(lines) + "\n"


def _index_line(company: str, cik: str = "7") -> str:
    # The column widths of EDGAR's form.idx: a name of 62 characters leaves no space before the CIK.
    return f"{'8-K':<12}{company:<62}{cik:<12}{'20261013':<12}edgar/data/{cik}/0000000007-26-000001.txt"


@pytest.mark.parametrize(
    ("listed", "company"),
    [
        ("ACME CORP", "ACME CORP"),
        ("ACME  CORP", "ACME CORP"),  # a double space inside the name
        ("A" * 61, "A" * 61),  # one space left before the CIK
        ("A" * 62, "A" * 62),  # runs straight into the CIK
        ("A" * 57 + " 2026", "A" * 57 + " 2026"),  # digits at the end of a name that runs into the CIK
    ],
)
def test_parse_form_index_reads_any_company_name(listed, company, caplog):
    [ref] = parse_form_index(_form_index(_index_line(listed)))
    assert ref.company == company
    assert ref.cik == "0000000007"
    assert ref.accession_no == "0000000007-26-000001"
    assert ref.index_url.endswith("/data/7/000000000726000001/0000000007-26-000001-index.htm")
    assert "can't read" not in caplog.text


@pytest.mark.parametrize(
    "line",
    [
        "8-K         ACME CORP",
        "8-K         ACME CORP      7           20261013    edgar/data/7/no-accession.txt",
        "8-K         ACME CORP      8           20261013    edgar/data/7/0000000007-26-000001.txt",
        "8-K         7              20261013    edgar/data/7/0000000007-26-000001.txt",
    ],
)
def test_parse_form_index_warns_about_an_unreadable_8k(line, caplog):
    good = _index_line("ACME CORP", cik="9").replace("0000000007", "0000000009")
    refs = parse_form_index(_form_index(line, good))
    assert [r.cik for r in refs] == ["0000000009"]
    assert "can't read" in caplog.text
    assert line in caplog.text


def test_parse_form_index_ignores_other_forms_quietly(caplog):
    assert parse_form_index(_form_index("10-Q        Other Co", "8-K/A       Sample Holdings")) == []
    assert caplog.text == ""


def test_parse_form_index_dedupes_co_registrants():
    rule = "-" * 80
    lines = [
        "8-K         Parent Utility Inc                                            4           20261013    "
        "edgar/data/4/0000000004-26-000011.txt",
        "8-K         Parent Utility Operating Co                                   5           20261013    "
        "edgar/data/5/0000000004-26-000011.txt",
    ]
    refs = parse_form_index("Form Type   Company Name\n" + rule + "\n" + "\n".join(lines) + "\n")
    assert [(r.accession_no, r.cik) for r in refs] == [("0000000004-26-000011", "0000000004")]


@respx.mock
@pytest.mark.parametrize("body", [
    '{"message": "Service temporarily unavailable"}',
    '[{"cik_str": 1, "ticker": "EXMP"}]',
    '{"0": {"ticker": "EXMP"}}',
    '{"0": {"cik_str": 1, "ticker": null}}',
])
def test_error_shaped_ticker_file_on_cold_cache_gives_no_ticker_and_is_not_cached(tmp_path, body):
    respx.get(TICKERS_URL).respond(200, text=body)
    assert Edgar("ua", per_second=1000, cache_dir=tmp_path).ticker_for("0000000001") is None
    assert not (tmp_path / "company_tickers.json").exists()


@respx.mock
def test_error_shaped_ticker_file_does_not_fail_fetch(tmp_path):
    _mock_filing(respx)
    respx.get(TICKERS_URL).respond(200, text='{"message": "Service temporarily unavailable"}')
    edgar = Edgar("ua", per_second=1000, cache_dir=tmp_path)
    filing = edgar.fetch(FilingRef("0000000001-26-000101", "0000000001", "Example Corp", "8-K", INDEX), 5000)
    assert filing.ticker is None


@respx.mock
def test_error_shaped_ticker_file_falls_back_to_stale_cache(tmp_path):
    cache = tmp_path / "company_tickers.json"
    cache.write_text('{"0": {"cik_str": 1, "ticker": "OLD", "title": "Example Corp"}}')
    day_old = time.time() - 25 * 3600
    os.utime(cache, (day_old, day_old))
    respx.get(TICKERS_URL).respond(200, text='{"message": "Service temporarily unavailable"}')
    assert Edgar("ua", per_second=1000, cache_dir=tmp_path).ticker_for("0000000001") == "OLD"
    assert "OLD" in cache.read_text()


@respx.mock
def test_fresh_but_error_shaped_ticker_cache_is_replaced(tmp_path):
    (tmp_path / "company_tickers.json").write_text('{"message": "Service temporarily unavailable"}')
    route = respx.get(TICKERS_URL).respond(200, text=_tickers_text())
    assert Edgar("ua", per_second=1000, cache_dir=tmp_path).ticker_for("0000000001") == "EXMP"
    assert route.call_count == 1
    assert "EXMP" in (tmp_path / "company_tickers.json").read_text()


def test_parse_tickers_skips_malformed_rows():
    data = {
        "0": {"cik_str": 1, "ticker": "EXMP"},
        "1": {"cik_str": "2", "ticker": "STR"},  # CIK as a string of digits is fine
        "2": {"cik_str": "abc", "ticker": "BAD"},
        "3": {"cik_str": 4},
        "4": "not a row",
        "5": {"cik_str": True, "ticker": "BOOL"},
        "6": {"cik_str": 1, "ticker": "EXMP-PA"},  # first listing for a CIK wins
    }
    assert parse_tickers(data) == {"0000000001": "EXMP", "0000000002": "STR"}


def test_parse_tickers_rejects_a_file_with_no_usable_rows():
    with pytest.raises(ValueError):
        parse_tickers({})
    with pytest.raises(ValueError):
        parse_tickers({"0": {"cik_str": "x", "ticker": 5}})


_real_sleep = time.sleep


class FakeClock:
    """Stands in for time.monotonic and time.sleep: sleeping advances the clock by the requested amount
    but only really sleeps for a moment, long enough to let other threads run in between."""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start
        self.sleeps: list[float] = []
        self.wakes: list[float] = []  # the time each sleeper asked to be woken at

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.wakes.append(self.now + seconds)
        _real_sleep(0.001)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr("tape.edgar.time.monotonic", fake.monotonic)
    monkeypatch.setattr("tape.edgar.time.sleep", fake.sleep)
    return fake


def test_throttle_spaces_consecutive_waits(clock):
    throttle = Throttle(per_second=5)
    granted = []
    for _ in range(4):
        throttle.wait()
        granted.append(clock.now)
    assert granted == pytest.approx([100.0, 100.2, 100.4, 100.6])
    assert clock.sleeps == pytest.approx([0.2, 0.2, 0.2])


def test_throttle_does_not_sleep_once_the_interval_has_passed(clock):
    throttle = Throttle(per_second=5)
    throttle.wait()
    clock.now += 0.5  # the caller did other work for longer than one interval
    throttle.wait()
    assert clock.sleeps == []
    clock.now += 0.1  # but the next slot is still counted from the late call
    throttle.wait()
    assert clock.sleeps == pytest.approx([0.1])


def test_throttle_gives_concurrent_callers_evenly_spaced_slots(clock):
    throttle = Throttle(per_second=10)
    start = threading.Barrier(8)

    def worker():
        start.wait()
        throttle.wait()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # The first caller goes straight through and each of the other seven is woken one interval after
    # the one before it, so the eight requests are spread over 0.7s rather than sharing a slot.
    assert sorted(clock.wakes) == pytest.approx([100.1, 100.2, 100.3, 100.4, 100.5, 100.6, 100.7])
