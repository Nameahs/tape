from pathlib import Path

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


def test_state_text_includes_exhibit_and_respects_budget():
    main = (FIX / "main_8k.htm").read_text()
    ex = (FIX / "ex991.htm").read_text()
    full = build_state_text(main, ex, max_chars=100_000)
    assert "EXHIBIT 99.1" in full and "withdrew its full-year guidance" in full
    assert "risks" not in full  # forward-looking boilerplate trimmed
    short = build_state_text(main, ex, max_chars=300)
    assert len(short) <= 300
    assert short.startswith("UNITED STATES")


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
