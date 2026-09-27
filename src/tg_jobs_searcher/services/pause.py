"""Coordinate pause transitions with in-flight bot updates and deliveries."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from tg_jobs_searcher.db.control_repository import BotControlRepository


class PauseCoordinator:
    def __init__(self, repository: BotControlRepository) -> None:
        self._repository = repository
        self._condition = asyncio.Condition()
        self._transition_lock = asyncio.Lock()
        self._active = 0
        self._pausing = False
        self._paused = False

    @property
    def is_paused(self) -> bool:
        return self._paused or self._pausing

    async def initialize(self) -> None:
        self._paused = await self._repository.is_paused()

    @asynccontextmanager
    async def activity(self) -> AsyncIterator[bool]:
        async with self._condition:
            allowed = not self.is_paused
            if allowed:
                self._active += 1
        try:
            yield allowed
        finally:
            if allowed:
                async with self._condition:
                    self._active -= 1
                    self._condition.notify_all()

    async def pause(self) -> bool:
        async with self._transition_lock:
            async with self._condition:
                if self._paused:
                    return False
                self._pausing = True
            try:
                async with self._condition:
                    while self._active:
                        await self._condition.wait()
                changed = await self._repository.set_paused(True)
            except BaseException:
                async with self._condition:
                    self._pausing = False
                    self._condition.notify_all()
                raise
            async with self._condition:
                self._paused = True
                self._pausing = False
                self._condition.notify_all()
            return changed

    async def resume(self) -> bool:
        async with self._transition_lock:
            async with self._condition:
                if not self._paused:
                    return False
            changed = await self._repository.set_paused(False)
            async with self._condition:
                self._paused = False
                self._condition.notify_all()
            return changed
