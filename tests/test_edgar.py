import os
import time
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from tape.edgar import (
    FEED_URL,
    TICKERS_URL,
    Edgar,
    EmptyFilingError,
    FilingRef,
    parse_feed,
    parse_form_index,
    parse_index_page,
)

FIX = Path(__file__).parent / "fixtures"
FOLDER = "https://www.sec.gov/Archives/edgar/data/1/000000000126000101"
INDEX = f"{FOLDER}/0000000001-26-000101-index.htm"


def test_parse_feed_skips_amendments():
    refs = parse_feed((FIX / "feed.atom").read_text())
    assert refs == [FilingRef("0000000001-26-000101", "0000000001", "Example Corp", "8-K", INDEX)]


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
    [ref] = edgar.latest_8k()
    filing = edgar.fetch(ref, max_chars=5000)
    assert filing.ticker == "EXMP"  # first listing wins over the preferred share
    assert filing.items == ("5.02", "9.01")
    assert "interim Chief Executive Officer" in filing.clean_text
    assert all(c.request.headers["User-Agent"] == "Tape test test@example.com" for c in respx.calls)


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
    assert len(Edgar("ua", per_second=1000).latest_8k()) == 1


@respx.mock
def test_retries_transport_errors(monkeypatch):
    monkeypatch.setattr("tape.edgar.time.sleep", lambda s: None)
    route = respx.get(FEED_URL)
    route.side_effect = [
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("reset"),
        httpx.Response(200, text=(FIX / "feed.atom").read_text()),
    ]
    assert len(Edgar("ua", per_second=1000).latest_8k()) == 1
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
    assert len(Edgar("ua", per_second=1000).latest_8k()) == 1
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
def test_daily_index_missing_on_weekend_is_empty():
    respx.get("https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.20261011.idx").respond(404)
    assert Edgar("ua", per_second=1000).daily_8k(date(2026, 10, 11)) == []


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


def test_parse_feed_dedupes_co_registrants():
    entries = _CO_REG_ENTRY.format(company="Parent Utility Inc", cik="0000000004", n=4) + _CO_REG_ENTRY.format(
        company="Parent Utility Operating Co", cik="0000000005", n=5
    )
    xml = f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'
    refs = parse_feed(xml)
    assert len(refs) == 1
    assert refs[0].accession_no == "0000000004-26-000011"
    assert refs[0].company == "Parent Utility Inc"  # first listing wins


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
