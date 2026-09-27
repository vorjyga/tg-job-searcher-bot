"""Configuration loaded exclusively from process environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url


class ConfigurationError(ValueError):
    """Raised when application configuration is absent or invalid."""


DEFAULT_LOCK_KEY = 742109436318294807
VALID_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    log_level: str
    database_connect_timeout_seconds: float
    app_lock_key: int

    @classmethod
    def from_env(cls) -> Settings:
        database_url = _normalise_database_url(_required("DATABASE_URL"))
        _validate_database_url(database_url)

        log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        if log_level not in VALID_LOG_LEVELS:
            allowed = ", ".join(sorted(VALID_LOG_LEVELS))
            raise ConfigurationError(f"LOG_LEVEL must be one of: {allowed}")

        timeout = _positive_float(
            "DATABASE_CONNECT_TIMEOUT_SECONDS",
            os.getenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "10"),
        )
        lock_key = _signed_bigint("APP_LOCK_KEY", os.getenv("APP_LOCK_KEY", str(DEFAULT_LOCK_KEY)))
        return cls(
            database_url=database_url,
            log_level=log_level,
            database_connect_timeout_seconds=timeout,
            app_lock_key=lock_key,
        )


@dataclass(frozen=True, slots=True)
class TelethonSettings:
    """Credentials for the Telegram client logged in as the account owner."""

    api_id: int
    api_hash: str
    session_path: Path

    @classmethod
    def from_env(cls) -> TelethonSettings:
        api_id = _positive_int("TELEGRAM_API_ID", _required("TELEGRAM_API_ID"))
        api_hash = _required("TELEGRAM_API_HASH")
        session_path = Path(_required("TELEGRAM_SESSION_PATH")).expanduser()
        if not session_path.is_absolute():
            raise ConfigurationError("TELEGRAM_SESSION_PATH must be an absolute path")
        if session_path.exists() and session_path.is_dir():
            raise ConfigurationError("TELEGRAM_SESSION_PATH must name a file, not a directory")
        return cls(api_id=api_id, api_hash=api_hash, session_path=session_path)


@dataclass(frozen=True, slots=True)
class BotSettings:
    """Credentials and access policy for the Bot API interface."""

    token: str
    owner_telegram_id: int

    @classmethod
    def from_env(cls) -> BotSettings:
        return cls(
            token=_required("BOT_TOKEN"),
            owner_telegram_id=_positive_int("OWNER_TELEGRAM_ID", _required("OWNER_TELEGRAM_ID")),
        )


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    bot: BotSettings
    telethon: TelethonSettings

    @classmethod
    def from_env(cls) -> TelegramSettings:
        return cls(bot=BotSettings.from_env(), telethon=TelethonSettings.from_env())


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} must be set")
    return value


def _validate_database_url(value: str) -> None:
    try:
        url = make_url(value)
    except Exception as exc:  # SQLAlchemy provides several concrete parser exceptions.
        raise ConfigurationError("DATABASE_URL is not a valid SQLAlchemy URL") from exc

    if url.get_backend_name() != "postgresql":
        raise ConfigurationError("DATABASE_URL must use PostgreSQL")
    if url.get_driver_name() != "asyncpg":
        raise ConfigurationError(
            "DATABASE_URL must use the asyncpg driver (postgresql+asyncpg://...)"
        )
    if not url.host or not url.database:
        raise ConfigurationError("DATABASE_URL must include a host and database name")


def _normalise_database_url(value: str) -> str:
    """Translate libpq's sslmode URL option to asyncpg's ssl argument.

    Supabase exposes PostgreSQL URLs with ``sslmode=require``.  SQLAlchemy's
    asyncpg dialect forwards that name unchanged, while asyncpg expects
    ``ssl=require`` when it receives individual connection keyword arguments.
    """
    try:
        url = make_url(value)
    except Exception as exc:  # SQLAlchemy provides several concrete parser exceptions.
        raise ConfigurationError("DATABASE_URL is not a valid SQLAlchemy URL") from exc
    sslmode = url.query.get("sslmode")
    if sslmode is None:
        return value
    ssl = url.query.get("ssl")
    if ssl is not None and ssl != sslmode:
        raise ConfigurationError("DATABASE_URL cannot specify different ssl and sslmode values")
    query = dict(url.query)
    query.pop("sslmode")
    query["ssl"] = sslmode
    return url.set(query=query).render_as_string(hide_password=False)


def _positive_float(name: str, value: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a positive number") from exc
    if result <= 0:
        raise ConfigurationError(f"{name} must be a positive number")
    return result


def _positive_int(name: str, value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a positive integer") from exc
    if result <= 0:
        raise ConfigurationError(f"{name} must be a positive integer")
    return result


def _signed_bigint(name: str, value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a signed 64-bit integer") from exc
    if not -(2**63) <= result < 2**63:
        raise ConfigurationError(f"{name} must be a signed 64-bit integer")
    return result
