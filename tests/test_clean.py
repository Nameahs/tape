from pathlib import Path

import pytest

from tape.clean import build_state_text, drop_boilerplate, html_to_text, items_in

FIX = Path(__file__).parent / "fixtures"


def test_html_to_text_drops_hidden_and_styles():
    text = html_to_text((FIX / "main_8k.htm").read_text())
    assert "hidden xbrl" not in text
    assert "p{}" not in text
    assert "Jane Doe notified the Board" in text


def test_items_in():
    text = html_to_text((FIX / "main_8k.htm").read_text())
    assert items_in(text) == ["5.02", "9.01"]


def test_drop_boilerplate_cuts_signatures():
    text = drop_boilerplate(html_to_text((FIX / "main_8k.htm").read_text()))
    assert "SIGNATURES" not in text
    assert "interim Chief Executive Officer" in text


def test_drop_boilerplate_leaves_short_text_alone():
    assert drop_boilerplate("Signatures\nshort") == "Signatures\nshort"


LEAD = "Item 1.01 Entry into a Material Definitive Agreement with a counterparty described below.\n"


@pytest.mark.parametrize("line", [
    "Signature Aviation agreed to buy the company for $4.6 billion in cash.",
    "Signatures from 40% of holders were received, above the 25% needed to call a meeting.",
    "Safe Harbor Financial will serve 400 new clients under the agreement.",
    "Safe harbor provisions of the agreement cap the buyer's indemnity at $50 million.",
    "Cautionary notes issued by the lender were withdrawn on October 1.",
    "Forward-looking statements were not updated after the merger closed.",
])
def test_drop_boilerplate_keeps_sentences_that_open_with_a_heading_word(line):
    text = LEAD + line + "\nThe deal is expected to close in the first quarter."
    assert drop_boilerplate(text) == text


@pytest.mark.parametrize("heading", [
    "SIGNATURES",
    "Signature:",
    "Forward-Looking Statements",
    "Forward-Looking Statements: This release contains statements about future results.",
    "FORWARD LOOKING INFORMATION",
    "Cautionary Note Regarding Forward-Looking Statements",
    "Special Note About Forward-Looking Statements",
    "Cautionary Statement — this release contains projections.",
    "Safe Harbor Statement",
    "Safe Harbor Statement under the Private Securities Litigation Reform Act of 1995",
    "Safe Harbor for Forward-Looking Statements",
])
def test_drop_boilerplate_cuts_at_section_headings(heading):
    text = LEAD + "The buyer will pay $4.6 billion in cash.\n" + heading + "\nRisks include..."
    assert drop_boilerplate(text) == LEAD + "The buyer will pay $4.6 billion in cash."


def test_state_text_includes_exhibit_and_respects_budget():
    main = (FIX / "main_8k.htm").read_text()
    ex = (FIX / "ex991.htm").read_text()
    full = build_state_text(main, ex, max_chars=100_000)
    assert "EXHIBIT 99.1" in full and "withdrew its full-year guidance" in full
    assert "risks" not in full  # forward-looking boilerplate trimmed
    short = build_state_text(main, ex, max_chars=300)
    assert len(short) <= 300
    assert short.startswith("UNITED STATES")
