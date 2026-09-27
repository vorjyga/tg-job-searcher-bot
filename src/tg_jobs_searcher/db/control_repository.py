"""Persist the administrator-controlled bot pause."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_jobs_searcher.db.models import BotAccessSettings


class BotControlRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def is_paused(self) -> bool:
        async with self._session_factory() as session:
            value = await session.scalar(
                select(BotAccessSettings.is_paused).where(BotAccessSettings.id == 1)
            )
        if value is None:
            raise RuntimeError("Bot settings are missing; run database migrations")
        return value

    async def set_paused(self, paused: bool) -> bool:
        """Set the state and report whether it changed."""
        async with self._session_factory() as session, session.begin():
            settings = await session.scalar(
                select(BotAccessSettings).where(BotAccessSettings.id == 1).with_for_update()
            )
            if settings is None:
                raise RuntimeError("Bot settings are missing; run database migrations")
            changed = settings.is_paused != paused
            settings.is_paused = paused
            return changed
