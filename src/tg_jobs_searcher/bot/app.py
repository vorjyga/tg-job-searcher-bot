"""aiogram application lifecycle bound to an authorised Telethon client."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telethon import TelegramClient

from tg_jobs_searcher.bot.handlers import (
    create_public_router,
    create_router,
    make_telegram_group_joiner,
    make_telegram_group_lister,
    make_telegram_group_resolver,
)
from tg_jobs_searcher.bot.management import create_management_router
from tg_jobs_searcher.bot.users import create_admin_router
from tg_jobs_searcher.config import BotSettings
from tg_jobs_searcher.db.repositories import NotificationRepository, OwnerRepository
from tg_jobs_searcher.services.notifications import NotificationWorker


@dataclass(slots=True)
class BotApplication:
    bot: Bot
    dispatcher: Dispatcher
    notification_worker: NotificationWorker

    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        worker_task = asyncio.create_task(
            self.notification_worker.run(stop_event), name="notification-outbox"
        )
        polling_task = asyncio.create_task(
            self.dispatcher.start_polling(
                self.bot,
                allowed_updates=self.dispatcher.resolve_used_update_types(),
                handle_signals=False,
                close_bot_session=False,
            )
        )
        stop_task = asyncio.create_task(stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                {polling_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if polling_task in done:
                await polling_task
                return
            try:
                await self.dispatcher.stop_polling()
            except RuntimeError:
                # A shutdown signal can arrive before aiogram marks polling as started.
                polling_task.cancel()
                await asyncio.gather(polling_task, return_exceptions=True)
            else:
                await polling_task
        finally:
            stop_task.cancel()
            worker_task.cancel()
            await asyncio.gather(stop_task, worker_task, return_exceptions=True)

    async def close(self) -> None:
        await self.bot.session.close()


def create_bot_application(
    settings: BotSettings,
    client: TelegramClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> BotApplication:
    bot = Bot(token=settings.token)
    dispatcher = Dispatcher()
    owner_repository = OwnerRepository(session_factory, settings.owner_telegram_id)
    dispatcher.include_router(
        create_public_router(
            admin_telegram_id=settings.owner_telegram_id,
            owners=owner_repository,
            register_owner=owner_repository.ensure_owner,
        )
    )
    dispatcher.include_router(
        create_router(
            admin_telegram_id=settings.owner_telegram_id,
            owners=owner_repository,
            resolve_group=make_telegram_group_resolver(client),
            list_groups=make_telegram_group_lister(client),
        )
    )
    dispatcher.include_router(
        create_management_router(
            admin_telegram_id=settings.owner_telegram_id,
            owners=owner_repository,
            session_factory=session_factory,
            resolve_group=make_telegram_group_joiner(client),
            check_group_access=make_telegram_group_resolver(client),
        )
    )
    dispatcher.include_router(
        create_admin_router(settings.owner_telegram_id, owner_repository)
    )
    return BotApplication(
        bot=bot,
        dispatcher=dispatcher,
        notification_worker=NotificationWorker(NotificationRepository(session_factory), bot),
    )
