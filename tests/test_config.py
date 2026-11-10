import pytest

from tape.config import ConfigError, Settings, Thresholds


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


@pytest.mark.parametrize("name, attr", [
    ("TAPE_DB_PATH", "db_path"),
    ("TAPE_USER_AGENT", "user_agent"),
    ("TAPE_JEV_MODEL", "jev_model"),
])
@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_string_setting_falls_back_to_default(monkeypatch, name, attr, raw):
    monkeypatch.setenv(name, raw)
    assert getattr(Settings.from_env(), attr) == getattr(Settings(), attr)


def test_string_settings_are_stripped(monkeypatch):
    monkeypatch.setenv("TAPE_JEV_MODEL", " jev-2 \n")
    monkeypatch.setenv("TAPE_DB_PATH", " /tmp/x.db ")
    s = Settings.from_env()
    assert s.jev_model == "jev-2"
    assert str(s.db_path) == "/tmp/x.db"


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


def test_event_override_with_misspelled_cutoff_fails_loudly(monkeypatch):
    monkeypatch.setenv("TAPE_BRIEF_NOWW__MNA", "4.5")
    with pytest.raises(ValueError, match=r"TAPE_BRIEF_NOWW__MNA: unknown threshold 'brief_noww'; expected .*brief_now"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("name", "raw", "allowed"),
    [
        ("TAPE_BRIEF_NOW", "45", "between 1 and 5"),
        ("TAPE_BRIEF_NOW_WITH_SURPRISE", "0.99", "between 1 and 5"),
        ("TAPE_DIGEST", "0", "between 1 and 5"),
        ("TAPE_SURPRISE", "70", "between 0 and 1"),
        ("TAPE_BOILERPLATE", "-0.1", "between 0 and 1"),
        ("TAPE_BRIEF_NOW__MNA", "5.1", "between 1 and 5"),
        ("TAPE_SURPRISE__EARNINGS_GUIDANCE", "1.5", "between 0 and 1"),
    ],
)
def test_threshold_out_of_range_names_the_variable(monkeypatch, name, raw, allowed):
    monkeypatch.setenv(name, raw)
    with pytest.raises(ValueError, match=f"{name}: must be {allowed}, got"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("TAPE_BRIEF_NOW", "1"),
        ("TAPE_BRIEF_NOW", "5"),
        ("TAPE_DIGEST", "1"),
        ("TAPE_DIGEST", "5.0"),
        ("TAPE_SURPRISE", "0"),
        ("TAPE_SURPRISE", "1"),
        ("TAPE_BOILERPLATE", "0"),
        ("TAPE_BOILERPLATE", "1"),
        ("TAPE_BRIEF_NOW__MNA", "5"),
        ("TAPE_SURPRISE__MNA", "0"),
    ],
)
def test_threshold_boundary_values_are_accepted(monkeypatch, name, raw):
    monkeypatch.setenv(name, raw)
    Settings.from_env()


def test_thresholds_built_in_code_are_checked_too():
    with pytest.raises(ValueError, match="TAPE_DIGEST: must be between 1 and 5"):
        Thresholds(digest=0.0)
    with pytest.raises(ValueError, match="TAPE_BRIEF_NOW__MNA: must be between 1 and 5"):
        Thresholds(overrides={"mna": {"brief_now": 45.0}})


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


@pytest.mark.parametrize("raw", ["0", "-5"])
def test_retry_budget_must_be_positive(monkeypatch, raw):
    monkeypatch.setenv("TAPE_JEV_RETRY_BUDGET_S", raw)
    with pytest.raises(ConfigError, match="^TAPE_JEV_RETRY_BUDGET_S: must be positive"):
        Settings.from_env()


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity"])
@pytest.mark.parametrize("name", ["TAPE_DIGEST", "TAPE_BRIEF_NOW__MNA", "TAPE_JEV_RETRY_BUDGET_S"])
def test_non_finite_numbers_are_rejected(monkeypatch, name, raw):
    monkeypatch.setenv(name, raw)
    with pytest.raises(ValueError, match=f"{name}: expected a finite number"):
        Settings.from_env()


def test_jev_price_must_not_be_negative(monkeypatch):
    monkeypatch.setenv("TAPE_JEV_PRICE_PER_M", "-0.042")
    with pytest.raises(ConfigError, match="^TAPE_JEV_PRICE_PER_M: must be at least 0, got -0.042"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("kwargs", "variable"),
    [
        ({"jev_retry_budget_s": 0.0}, "TAPE_JEV_RETRY_BUDGET_S"),
        ({"jev_input_price_per_m": -1.0}, "TAPE_JEV_PRICE_PER_M"),
    ],
)
def test_settings_built_in_code_name_both_field_and_variable(kwargs, variable):
    (field_name,) = kwargs
    with pytest.raises(ConfigError, match=rf"{field_name} \({variable}\)"):
        Settings(**kwargs)


def test_jev_price_may_be_zero(monkeypatch):
    monkeypatch.setenv("TAPE_JEV_PRICE_PER_M", "0")
    assert Settings.from_env().jev_input_price_per_m == 0.0


@pytest.mark.parametrize("ua", ["Tape you@example.com", "  Acme Research ops@acme.co  ", "a@b.io"])
def test_require_contact_accepts_a_name_and_email(ua):
    from tape.config import require_contact

    assert require_contact(ua) == ua.strip()


@pytest.mark.parametrize(
    "ua",
    [Settings().user_agent, "", "   ", "Tape research bot", "@example.com", "Tape you@localhost", "Tape you@"],
)
def test_require_contact_rejects_a_missing_contact(ua):
    from tape.config import require_contact

    with pytest.raises(ValueError, match="TAPE_USER_AGENT"):
        require_contact(ua)


def test_sensible_thresholds_have_nothing_unreachable(monkeypatch, caplog):
    monkeypatch.setenv("TAPE_BRIEF_NOW__EARNINGS_GUIDANCE", "4.5")
    with caplog.at_level("WARNING", logger="tape.config"):
        assert Thresholds.from_env().unreachable() == []
    assert caplog.records == []


def test_digest_above_brief_now_warns(monkeypatch, caplog):
    monkeypatch.setenv("TAPE_DIGEST", "4.2")
    with caplog.at_level("WARNING", logger="tape.config"):
        t = Thresholds.from_env()
    assert t.digest == 4.2  # a warning, not an error
    assert [r.getMessage() for r in caplog.records] == [
        "TAPE_DIGEST (4.2) is at or above TAPE_BRIEF_NOW (4): no filing will be routed to the digest"]


def test_surprise_bar_above_brief_now_warns():
    t = Thresholds(brief_now=3.5, brief_now_with_surprise=3.5)
    assert t.unreachable() == [
        "TAPE_BRIEF_NOW_WITH_SURPRISE (3.5) is at or above TAPE_BRIEF_NOW (3.5): "
        "the surprise rule can never brief a filing"]


def test_event_override_that_empties_a_band_names_the_event_variable():
    t = Thresholds(overrides={"mna": {"digest": 4.5}, "earnings_guidance": {"brief_now": 2.5}})
    assert t.unreachable() == [
        "TAPE_DIGEST (2.75) is at or above TAPE_BRIEF_NOW__EARNINGS_GUIDANCE (2.5): "
        "no filing will be routed to the digest for earnings_guidance",
        "TAPE_BRIEF_NOW_WITH_SURPRISE (3) is at or above TAPE_BRIEF_NOW__EARNINGS_GUIDANCE (2.5): "
        "the surprise rule can never brief a filing for earnings_guidance",
        "TAPE_DIGEST__MNA (4.5) is at or above TAPE_BRIEF_NOW (4): "
        "no filing will be routed to the digest for mna",
    ]


def test_global_problem_is_not_repeated_for_unrelated_event_overrides():
    t = Thresholds(digest=4.0, overrides={"mna": {"boilerplate": 0.5}, "earnings_guidance": {"digest": 3.0}})
    assert t.unreachable() == [
        "TAPE_DIGEST (4) is at or above TAPE_BRIEF_NOW (4): no filing will be routed to the digest"]
