import pytest

from tape import db


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def make_filing(n: int = 1, **kw) -> db.Filing:
    base = dict(
        accession_no=f"0000000001-26-{n:06d}",
        cik="1",
        company="Example Corp",
        form="8-K",
        accepted_at="2026-10-13T16:05:00-04:00",
        url="https://www.sec.gov/Archives/edgar/data/1/x-index.htm",
        items=("2.02", "9.01"),
        ticker="EXMP",
        clean_text="Example Corp reports third quarter results.",
    )
    base.update(kw)
    return db.Filing(**base)


def make_score(accession_no: str, materiality: float = 3.0, **kw) -> db.ScoreRow:
    base = dict(
        accession_no=accession_no, model="jev-test", materiality=materiality, event="earnings_guidance",
        direction="good", surprise=0.2, boilerplate=0.1, raw_json="{}",
    )
    base.update(kw)
    return db.ScoreRow(**base)
