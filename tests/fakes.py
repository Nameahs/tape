"""A stand-in for the Jev client that answers from simple keyword rules."""

from types import SimpleNamespace


class FakeJev:
    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.calls = []
        self.fail_on = fail_on or set()

    def system_one(self, state, questions, *, model=None):
        self.calls.append((state, questions, model))
        text = state["text"].lower()
        if any(word in text for word in self.fail_on):
            raise RuntimeError("boom")
        big = "resignation" in text or "withdrew" in text
        answers = {
            "materiality": SimpleNamespace(
                score=3.6 if big else 0.4,
                confidence=0.7 if big else 0.95,
                probabilities={0: 0.0, 1: 0.05, 2: 0.1, 3: 0.1, 4: 0.75} if big else
                {0: 0.65, 1: 0.3, 2: 0.05, 3: 0.0, 4: 0.0},
            ),
            "event": SimpleNamespace(choice="leadership_change" if big else "routine_governance", confidence=0.9),
            "direction": SimpleNamespace(choice="bad" if big else "neutral", confidence=0.8),
            "surprise": SimpleNamespace(noul=0.9 if big else 0.1),
            "boilerplate": SimpleNamespace(noul=0.05 if big else 0.9),
        }
        return SimpleNamespace(
            model="jev-fake", answers=answers, usage=SimpleNamespace(input_tokens=1000, output_tokens=5)
        )
