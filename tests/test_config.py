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
