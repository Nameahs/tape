from pathlib import Path

import bs4.element
import pytest

from tape.clean import build_state_text, drop_boilerplate, exhibit_label, html_to_text, items_in

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


def test_drop_boilerplate_looks_past_a_heading_at_the_top():
    body = "Revenue grew 12 percent on strong demand in every segment. " * 3
    text = (
        "Forward-Looking Statements\nThis release contains forward-looking statements.\n"
        "Item 2.02 Results of Operations and Financial Condition\n" + body
        + "\nSIGNATURES\nPursuant to the requirements of the Securities Exchange Act of 1934"
    )
    out = drop_boilerplate(text)
    assert "SIGNATURES" not in out and "Pursuant" not in out
    assert out.startswith("Forward-Looking Statements") and out.endswith("every segment.")


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


@pytest.mark.parametrize(("exhibit_type", "label"), [
    (None, "EXHIBIT 99.1 (press release)"),
    ("EX-99.1", "EXHIBIT 99.1 (press release)"),
    ("EX-99.01", "EXHIBIT 99.1 (press release)"),
    ("EX-99.2", "EXHIBIT 99.2"),
    ("EX-99", "EXHIBIT 99"),
    (" ex-99.3 ", "EXHIBIT 99.3"),
])
def test_exhibit_label_names_the_document_type(exhibit_type, label):
    assert exhibit_label(exhibit_type) == label


def test_state_text_labels_a_non_press_release_exhibit_by_its_type():
    text = build_state_text(_page("Item 7.01 Regulation FD."), _page("Investor presentation."), 1000, "EX-99.2")
    assert text == "Item 7.01 Regulation FD.\n\nEXHIBIT 99.2\nInvestor presentation."
    assert "press release" not in text


def test_empty_exhibit_adds_no_label():
    assert build_state_text("<p>Item 8.01 Other Events.</p>", "<html><body></body></html>", 1000) == (
        "Item 8.01 Other Events."
    )
    assert build_state_text("", "<html><img src='scan.jpg'></html>", 1000) == ""


def _page(body: str) -> str:
    return f"<html><body><p>{body}</p></body></html>"


def test_state_text_uses_whole_budget_without_exhibit():
    main = _page("Item 5.02 " + "word " * 400)
    text = build_state_text(main, None, max_chars=600)
    assert len(text) >= 590


def test_state_text_gives_body_the_room_a_short_exhibit_leaves():
    main = _page("Item 1.01 " + "word " * 400)
    ex = _page("Short press release.")
    text = build_state_text(main, ex, max_chars=600)
    assert len(text) <= 600
    assert text.endswith("Short press release.")
    assert len(text) >= 590


def test_inline_tags_stay_inside_their_sentence():
    html = (
        "<p>The merger closed after the company obtained all required shareholder "
        '<a href="#">signatures</a> and regulatory approvals.</p>'
        "<p>Revenue rose 40% to $5 billion, and guidance was raised.</p>"
    )
    text = drop_boilerplate(html_to_text(html))
    assert "shareholder signatures and regulatory approvals." in text
    assert "Revenue rose 40% to $5 billion" in text


@pytest.mark.parametrize("wrap", ["<p>{}</p>", "<div>{}</div>", "<p><b>{}</b></p>", "<br>{}<br>"])
def test_a_signatures_heading_on_its_own_still_cuts(wrap):
    html = (
        "<p>Item 5.02 Departure of Directors. Jane Doe notified the Board of her resignation "
        "effective immediately.</p>" + wrap.format("SIGNATURES") +
        "<p>Pursuant to the requirements of the Securities Exchange Act of 1934...</p>"
    )
    text = drop_boilerplate(html_to_text(html))
    assert text.endswith("effective immediately.")


def test_inline_xbrl_spans_join_into_one_line():
    html = (
        '<div><span style="font-family:Times">Net sales were </span>'
        '<ix:nonFraction name="us-gaap:Revenues" scale="6">412.5</ix:nonFraction>'
        '<span> million, up </span><span>12%</span><span>.</span></div>'
        "<div><span>Second paragraph.</span></div>"
    )
    assert html_to_text(html) == "Net sales were 412.5 million, up 12%.\nSecond paragraph."


def test_source_newlines_inside_a_paragraph_are_spaces():
    html = "<p>The Board approved\nthe plan on\r\nMarch 3.</p><p>Next.</p>"
    assert html_to_text(html) == "The Board approved the plan on March 3.\nNext."


def test_table_rows_and_list_items_stay_on_separate_lines():
    html = (
        "<table><tr><td>Revenue</td><td>$5.0</td></tr><tr><td>Costs</td><td>$3.0</td></tr></table>"
        "<ul><li>First</li><li>Second</li></ul>"
    )
    assert html_to_text(html).splitlines() == ["Revenue | $5.0", "Costs | $3.0", "First", "Second"]


def test_income_statement_row_keeps_labels_and_periods_in_order():
    html = (
        "<p>Results:</p><table>"
        "<tr><th></th><th>Q3 2026</th><th></th><th>Q3 2025</th></tr>"
        "<tr><td>Revenue</td><td>$</td><td>412.5</td><td>&#160;</td><td>$</td><td>368.1</td></tr>"
        "<tr><td>Net income (loss)</td><td>$</td><td><b>51.2</b></td><td></td><td>$</td><td>(4.7</td><td>)</td></tr>"
        "<tr><td>Gross margin</td><td>41.0</td><td>%</td><td></td><td>38.2</td><td>%</td></tr>"
        "</table>"
    )
    assert html_to_text(html).splitlines() == [
        "Results:",
        "Q3 2026 | Q3 2025",
        "Revenue | $412.5 | $368.1",
        "Net income (loss) | $51.2 | $(4.7)",
        "Gross margin | 41.0% | 38.2%",
    ]


def test_spacer_rows_and_cells_are_dropped():
    html = "<table><tr><td>&#160;</td><td></td></tr><tr><td>A</td><td> </td><td>B</td></tr></table>"
    assert html_to_text(html) == "A | B"


def test_source_newlines_inside_a_cell_are_spaces():
    html = "<table><tr><td>Diluted earnings\nper share</td><td>$1.02</td></tr></table>"
    assert html_to_text(html) == "Diluted earnings per share | $1.02"


def test_layout_table_keeps_paragraphs_on_their_own_lines():
    html = (
        "<table><tr><td><p>First paragraph of the release.</p><p>Second paragraph.</p></td>"
        "<td><div>Sidebar note.</div></td></tr></table>"
    )
    assert html_to_text(html).splitlines() == [
        "First paragraph of the release.", "Second paragraph.", "Sidebar note.",
    ]


def test_row_holding_a_nested_table_keeps_the_inner_rows():
    html = (
        "<table><tr><td>Outer</td><td>"
        "<table><tr><td>Revenue</td><td>$5.0</td></tr></table>"
        "</td></tr></table>"
    )
    assert html_to_text(html).splitlines() == ["Outer", "Revenue | $5.0"]


def test_plain_text_filing_keeps_its_lines():
    txt = "ITEM 8.01 OTHER EVENTS\n\nThe company announced a dividend.\nSIGNATURES\nBy: /s/ CFO"
    assert html_to_text(txt).splitlines() == [
        "ITEM 8.01 OTHER EVENTS", "The company announced a dividend.", "SIGNATURES", "By: /s/ CFO",
    ]


def test_html_comments_do_not_leak_into_text():
    assert html_to_text("<p>Kept<!-- a\nnote --> text.</p>") == "Kept text."


def test_zero_width_spacer_paragraphs_are_dropped():
    html = "<div><p>Revenue rose.</p><p>&#8203;</p><p>&#xFEFF;</p><p>Costs fell.</p></div>"
    assert html_to_text(html) == "Revenue rose.\nCosts fell."


def test_zero_width_characters_inside_a_word_are_removed():
    assert html_to_text("<p>Net\u200b sales\u2060 rose.</p>") == "Net sales rose."


def test_unicode_spaces_collapse_to_one_space():
    html = "<p>Net\u2002sales\u2009rose\u202f12%\u3000in\u00a0\u2003Q3.</p>"
    assert html_to_text(html) == "Net sales rose 12% in Q3."


def test_zero_width_and_unicode_spaced_cells_flatten_like_plain_ones():
    html = (
        "<table><tr><td>Net sales</td><td>&#8203;</td><td>$</td>"
        "<td>&#8203;412.5</td><td>&#xFEFF;</td><td> %</td></tr></table>"
    )
    assert html_to_text(html) == "Net sales | $412.5%"


def test_pre_blocks_keep_their_source_lines():
    html = "<p>Intro\ntext.</p><pre>Line one\n  Line two\n\nLine three</pre><p>After.</p>"
    assert html_to_text(html).splitlines() == ["Intro text.", "Line one", "Line two", "Line three", "After."]


def test_br_breaks_a_line_in_html_and_plain_text():
    assert html_to_text("<p>Acme Corp.<br>Investor Relations<br/>(555) 010-0100</p>").splitlines() == [
        "Acme Corp.", "Investor Relations", "(555) 010-0100",
    ]
    assert html_to_text("Dividend declared<br>Payable May 1").splitlines() == [
        "Dividend declared", "Payable May 1",
    ]


def _sibling_heavy_doc(blocks: int) -> str:
    paras = "".join(f"<p>Paragraph {i} of the\nrelease.</p>" for i in range(blocks))
    rows = "".join(f"<tr><td>Item {i}</td><td>$</td><td>1,234</td><td>(56</td><td>)</td></tr>" for i in range(blocks))
    return f"<html><body>{paras}<table>{rows}</table><span style='display:none'>hidden</span></body></html>"


def test_html_to_text_does_not_scan_siblings_per_block(monkeypatch):
    # bs4 locates a node for insert/replace/extract with Tag.index, a linear scan of the parent's
    # children. Calling it once per block made big exhibits quadratic (a 4 MB filing took ~13 s),
    # so the number of scans must not grow with the document.
    calls = 0
    index = bs4.element.Tag.index

    def counting_index(self, element):
        nonlocal calls
        calls += 1
        return index(self, element)

    monkeypatch.setattr(bs4.element.Tag, "index", counting_index)
    counts = []
    for blocks in (100, 400):
        calls = 0
        text = html_to_text(_sibling_heavy_doc(blocks))
        counts.append(calls)
        assert text.splitlines()[-1] == f"Item {blocks - 1} | $1,234 | (56)"
    assert counts[0] == counts[1] <= 5


def test_html_to_text_handles_deep_nesting():
    html = "<div>" * 5000 + "<p>Deep.</p>" + "</div>" * 5000
    assert html_to_text(html) == "Deep."
