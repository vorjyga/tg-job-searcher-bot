"""Persisted bot conversation state."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_jobs_searcher.db.models import (
    ConversationState,
)
from tg_jobs_searcher.db.repository_types import (
    CONVERSATION_TTL,
    ConversationStep,
)


class ConversationRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def set(
        self,
        owner_id: uuid.UUID,
        step: ConversationStep,
        draft: dict[str, Any],
    ) -> None:
        expires_at = datetime.now(UTC) + CONVERSATION_TTL
        statement = (
            insert(ConversationState)
            .values(owner_id=owner_id, step=step.value, draft=draft, expires_at=expires_at)
            .on_conflict_do_update(
                index_elements=[ConversationState.owner_id],
                set_={
                    "step": step.value,
                    "draft": draft,
                    "expires_at": expires_at,
                    "updated_at": func.now(),
                },
            )
        )
        async with self._session_factory() as session:
            await session.execute(statement)
            await session.commit()

    async def get(self, owner_id: uuid.UUID) -> ConversationState | None:
        async with self._session_factory() as session:
            state = await session.scalar(
                select(ConversationState).where(ConversationState.owner_id == owner_id)
            )
            if state is None:
                return None
            if state.expires_at > datetime.now(UTC):
                return state
            await session.delete(state)
            await session.commit()
            return None

    async def clear(self, owner_id: uuid.UUID) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(ConversationState).where(ConversationState.owner_id == owner_id)
            )
            await session.commit()
            return result.rowcount > 0
