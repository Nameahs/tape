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
