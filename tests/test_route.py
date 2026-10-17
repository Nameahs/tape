import pytest

from tape.config import Thresholds
from tape.route import DIGEST, LOG, NOW, route
from tests.conftest import make_score

T = Thresholds()


@pytest.mark.parametrize(
    ("materiality", "surprise", "boilerplate", "expected"),
    [
        (4.5, 0.1, 0.1, NOW),        # clearly material
        (4.0, 0.0, 0.0, NOW),        # threshold is inclusive
        (3.2, 0.8, 0.1, NOW),        # moderate but surprising
        (3.2, 0.5, 0.1, DIGEST),     # moderate, expected
        (2.75, 0.0, 0.0, DIGEST),
        (2.5, 0.9, 0.1, LOG),        # surprising but minor
        (4.8, 0.9, 0.85, LOG),       # boilerplate wins
    ],
)
def test_route(materiality, surprise, boilerplate, expected):
    s = make_score("a", materiality, surprise=surprise, boilerplate=boilerplate)
    assert route(s, T) == expected


def test_event_override_applies_only_to_that_event():
    t = Thresholds(overrides={"earnings_guidance": {"brief_now": 4.5}})
    earnings = make_score("a", 4.2, event="earnings_guidance", surprise=0.1)
    exit_ = make_score("b", 4.2, event="leadership_change", surprise=0.1)
    assert route(earnings, t) == DIGEST
    assert route(exit_, t) == NOW


def test_event_override_keeps_other_cutoffs():
    t = Thresholds(overrides={"earnings_guidance": {"brief_now": 4.5}})
    # The surprise path still uses the global brief_now_with_surprise.
    assert route(make_score("a", 3.2, event="earnings_guidance", surprise=0.8), t) == NOW


def test_missing_event_uses_global_thresholds():
    t = Thresholds(overrides={"earnings_guidance": {"brief_now": 4.5}})
    assert route(make_score("a", 4.2, event=None), t) == NOW


def test_unknown_cutoff_in_override_is_rejected():
    with pytest.raises(ValueError, match="brief_soon"):
        Thresholds(overrides={"mna": {"brief_soon": 4.0}})
