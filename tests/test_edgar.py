from datetime import date
from pathlib import Path

import httpx
import respx

from tape.edgar import FEED_URL, TICKERS_URL, Edgar, FilingRef, parse_feed, parse_form_index, parse_index_page

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


@respx.mock
def test_retries_on_429(monkeypatch):
    monkeypatch.setattr("tape.edgar.time.sleep", lambda s: None)
    route = respx.get(FEED_URL)
    route.side_effect = [httpx.Response(429), httpx.Response(200, text=(FIX / "feed.atom").read_text())]
    assert len(Edgar("ua", per_second=1000).latest_8k()) == 1


@respx.mock
def test_daily_index_missing_on_weekend_is_empty():
    respx.get("https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.20261011.idx").respond(404)
    assert Edgar("ua", per_second=1000).daily_8k(date(2026, 10, 11)) == []
