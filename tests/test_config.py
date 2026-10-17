import pytest

from tape.config import Settings


def test_defaults():
    s = Settings()
    assert s.thresholds.brief_now == 4.0
    assert s.max_chars == 12_000


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("TAPE_DB_PATH", "/tmp/x.db")
    monkeypatch.setenv("TAPE_BRIEF_NOW", "4.5")
    s = Settings.from_env()
    assert str(s.db_path) == "/tmp/x.db"
    assert s.thresholds.brief_now == 4.5


def test_event_override_from_env(monkeypatch):
    monkeypatch.setenv("TAPE_BRIEF_NOW__EARNINGS_GUIDANCE", "4.5")
    monkeypatch.setenv("TAPE_DIGEST__ROUTINE_GOVERNANCE", "3.5")
    t = Settings.from_env().thresholds
    assert t.brief_now == 4.0
    assert t.overrides == {"earnings_guidance": {"brief_now": 4.5}, "routine_governance": {"digest": 3.5}}
    assert t.for_event("earnings_guidance").brief_now == 4.5


def test_event_override_with_unknown_event_fails_loudly(monkeypatch):
    monkeypatch.setenv("TAPE_BRIEF_NOW__EARNINGS", "4.5")
    with pytest.raises(ValueError, match="TAPE_BRIEF_NOW__EARNINGS"):
        Settings.from_env()


def test_unrelated_double_underscore_vars_are_ignored(monkeypatch):
    monkeypatch.setenv("TAPE_SOMETHING__ELSE", "x")
    monkeypatch.setenv("TAPE_DIGEST__MNA", "")
    assert Settings.from_env().thresholds.overrides == {}


def test_jev_retry_settings_from_env(monkeypatch):
    assert (Settings().jev_max_retries, Settings().jev_retry_budget_s) == (4, 60.0)
    monkeypatch.setenv("TAPE_JEV_MAX_RETRIES", "0")
    monkeypatch.setenv("TAPE_JEV_RETRY_BUDGET_S", "15")
    s = Settings.from_env()
    assert (s.jev_max_retries, s.jev_retry_budget_s) == (0, 15.0)
