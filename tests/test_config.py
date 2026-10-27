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


def test_bad_number_names_the_variable(monkeypatch):
    monkeypatch.setenv("TAPE_BRIEF_NOW", "high")
    with pytest.raises(ValueError, match=r"TAPE_BRIEF_NOW: expected a number, got 'high'"):
        Settings.from_env()


def test_bad_event_override_names_the_variable(monkeypatch):
    monkeypatch.setenv("TAPE_DIGEST__EARNINGS_GUIDANCE", "3,5")
    with pytest.raises(ValueError, match=r"TAPE_DIGEST__EARNINGS_GUIDANCE: expected a number"):
        Settings.from_env()


def test_int_settings_from_env(monkeypatch):
    monkeypatch.setenv("TAPE_MAX_CHARS", "8000")
    monkeypatch.setenv("TAPE_JEV_MAX_RETRIES", "0")
    s = Settings.from_env()
    assert (s.max_chars, s.jev_max_retries) == (8000, 0)


@pytest.mark.parametrize("raw", ["2.9", "12k"])
def test_int_setting_rejects_non_integers(monkeypatch, raw):
    monkeypatch.setenv("TAPE_JEV_MAX_RETRIES", raw)
    with pytest.raises(ValueError, match="TAPE_JEV_MAX_RETRIES: expected a whole number"):
        Settings.from_env()


@pytest.mark.parametrize(("name", "raw"), [("TAPE_MAX_CHARS", "0"), ("TAPE_JEV_MAX_RETRIES", "-1")])
def test_int_setting_rejects_out_of_range(monkeypatch, name, raw):
    monkeypatch.setenv(name, raw)
    with pytest.raises(ValueError, match=f"{name}: must be at least"):
        Settings.from_env()


def test_retry_budget_must_be_positive(monkeypatch):
    monkeypatch.setenv("TAPE_JEV_RETRY_BUDGET_S", "0")
    with pytest.raises(ValueError, match="jev_retry_budget_s must be positive"):
        Settings.from_env()


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity"])
@pytest.mark.parametrize("name", ["TAPE_DIGEST", "TAPE_BRIEF_NOW__MNA", "TAPE_JEV_RETRY_BUDGET_S"])
def test_non_finite_numbers_are_rejected(monkeypatch, name, raw):
    monkeypatch.setenv(name, raw)
    with pytest.raises(ValueError, match=f"{name}: expected a finite number"):
        Settings.from_env()


def test_jev_price_must_not_be_negative(monkeypatch):
    monkeypatch.setenv("TAPE_JEV_PRICE_PER_M", "-0.042")
    with pytest.raises(ValueError, match="jev_input_price_per_m must not be negative"):
        Settings.from_env()


def test_jev_price_may_be_zero(monkeypatch):
    monkeypatch.setenv("TAPE_JEV_PRICE_PER_M", "0")
    assert Settings.from_env().jev_input_price_per_m == 0.0
