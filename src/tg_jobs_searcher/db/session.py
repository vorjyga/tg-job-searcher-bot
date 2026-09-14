"""Async PostgreSQL engine, health check and single-instance lock."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from tg_jobs_searcher.config import Settings


class InstanceAlreadyRunningError(RuntimeError):
    """Raised when another process holds this application's PostgreSQL advisory lock."""


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={"timeout": settings.database_connect_timeout_seconds},
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def check_database_connection(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


class PostgresAdvisoryLock:
    """A session-scoped PostgreSQL lock held on a dedicated pooled connection."""

    def __init__(self, engine: AsyncEngine, key: int) -> None:
        self._engine = engine
        self._key = key
        self._connection: AsyncConnection | None = None

    async def acquire(self) -> None:
        if self._connection is not None:
            raise RuntimeError("PostgreSQL advisory lock is already acquired by this process")

        connection = await self._engine.connect()
        try:
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": self._key}
            )
            if not acquired:
                raise InstanceAlreadyRunningError(
                    "another application instance already holds the lock"
                )
            self._connection = connection
        except BaseException:
            await connection.close()
            raise

    async def release(self) -> None:
        if self._connection is None:
            return
        connection, self._connection = self._connection, None
        try:
            await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": self._key})
        finally:
            await connection.close()
