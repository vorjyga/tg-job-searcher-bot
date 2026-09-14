import pytest

from tg_jobs_searcher.config import BotSettings, ConfigurationError, Settings, TelethonSettings


def test_loads_valid_postgresql_asyncpg_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://app:secret@localhost:5432/tg_jobs")

    settings = Settings.from_env()

    assert settings.database_connect_timeout_seconds == 10
    assert settings.log_level == "INFO"


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///local.db",
        "postgresql://app:secret@localhost:5432/tg_jobs",
        "postgresql+asyncpg://app:secret@/tg_jobs",
    ],
)
def test_rejects_non_asyncpg_postgresql_url(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("DATABASE_URL", url)

    with pytest.raises(ConfigurationError):
        Settings.from_env()


def test_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ConfigurationError, match="DATABASE_URL must be set"):
        Settings.from_env()


def test_loads_telegram_settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    session_path = tmp_path / "telethon.session"
    monkeypatch.setenv("BOT_TOKEN", "12345:token")
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_ID", "456")
    monkeypatch.setenv("TELEGRAM_API_HASH", "a" * 32)
    monkeypatch.setenv("TELEGRAM_SESSION_PATH", str(session_path))

    assert BotSettings.from_env().owner_telegram_id == 123
    assert TelethonSettings.from_env().session_path == session_path


def test_rejects_relative_telethon_session_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_API_ID", "456")
    monkeypatch.setenv("TELEGRAM_API_HASH", "a" * 32)
    monkeypatch.setenv("TELEGRAM_SESSION_PATH", "telethon.session")

    with pytest.raises(ConfigurationError, match="must be an absolute path"):
        TelethonSettings.from_env()
