"""Personal catalog searches and administrator source management."""

from __future__ import annotations

import uuid

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_jobs_searcher.bot.handlers import AuthorizedUserMiddleware
from tg_jobs_searcher.bot.management_dialogs import (
    RULE_HELP,
    _ensure_owner,
    _ensure_owner_from_callback,
)
from tg_jobs_searcher.bot.management_views import _format_keyword_values
from tg_jobs_searcher.db.catalog_repository import CatalogRepository
from tg_jobs_searcher.db.conversation_repository import ConversationRepository
from tg_jobs_searcher.db.owner_repository import OwnerRepository
from tg_jobs_searcher.db.repository_types import ConversationStep
from tg_jobs_searcher.services.keywords import KeywordInputError, parse_keyword_input


async def save_catalog_keywords(
    message: Message,
    owner_id: uuid.UUID,
    catalog: CatalogRepository,
    conversations: ConversationRepository,
    text: str,
) -> None:
    try:
        keywords = parse_keyword_input(text)
    except KeywordInputError as exc:
        await message.answer(f"{exc}\n\n{RULE_HELP}")
        return
    await catalog.set_search(owner_id, keywords)
    await conversations.clear(owner_id)
    count = await catalog.source_count()
    await message.answer(
        f"Поиск по всем источникам включён. Активных источников: {count}.\n"
        "Условия:\n" + _format_keyword_values([k.value for k in keywords]) + "\n\n"
        "Будут приходить новые сообщения с момента сохранения условий. "
        "Изменить условия — /search; остановить — /search_stop.\n"
        + (
            "Каталог пока пуст; поиск начнёт работать после добавления источников администратором."
            if not count
            else ""
        ),
    )


def _page(args: str | None) -> int | None:
    if not args:
        return 1
    value = args.strip()
    return int(value) if value.isdecimal() and 1 <= int(value) <= 10000 else None


def create_catalog_router(
    *,
    admin_telegram_id: int,
    owners: OwnerRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="catalog_search")
    router.message.middleware(AuthorizedUserMiddleware(owners))
    router.callback_query.middleware(AuthorizedUserMiddleware(owners))
    catalog = CatalogRepository(session_factory)
    conversations = ConversationRepository(session_factory)

    async def begin_search(message: Message, owner_id: uuid.UUID) -> None:
        await conversations.set(owner_id, ConversationStep.AWAITING_CATALOG_KEYWORDS, {})
        await message.answer(
            "Введите ключевые слова для поиска по всем источникам общего каталога.\n\n"
            f"{RULE_HELP}\n\nНапример: python & вакансия, frontend & remote\n"
            "Для отмены — /cancel. Ваши группы настраиваются отдельно через /groups."
        )

    @router.message(Command("search"))
    async def search(message: Message, command: CommandObject) -> None:
        owner = await _ensure_owner(owners, message)
        if command.args:
            await save_catalog_keywords(message, owner.id, catalog, conversations, command.args)
        else:
            await begin_search(message, owner.id)

    @router.callback_query(F.data == "catalog:search")
    async def search_button(callback: CallbackQuery) -> None:
        owner = await _ensure_owner_from_callback(owners, callback)
        if owner is None or not isinstance(callback.message, Message):
            await callback.answer("Отправьте /search.", show_alert=True)
            return
        await callback.answer()
        await begin_search(callback.message, owner.id)

    @router.message(Command("search_status"))
    async def status(message: Message) -> None:
        owner = await _ensure_owner(owners, message)
        search = await catalog.get_search(owner.id)
        count = await catalog.source_count()
        if search is None:
            await message.answer(f"Поиск ещё не настроен. Источников: {count}. Начать — /search.")
            return
        values = _format_keyword_values([k["value"] for k in search.keywords])
        await message.answer(
            f"Поиск по всем источникам: {'включён' if search.is_enabled else 'остановлен'}.\n"
            f"Источников: {count}.\nУсловия:\n{values}\n"
            "Изменить или включить — /search; остановить — /search_stop."
        )

    @router.message(Command("search_stop"))
    async def stop(message: Message) -> None:
        owner = await _ensure_owner(owners, message)
        await catalog.stop_search(owner.id)
        state = await conversations.get(owner.id)
        if state is not None and state.step == ConversationStep.AWAITING_CATALOG_KEYWORDS.value:
            await conversations.clear(owner.id)
        await message.answer(
            "Поиск по всем источникам остановлен. Условия сохранены. Включить — /search."
        )

    async def admin(message: Message) -> bool:
        if message.from_user is not None and message.from_user.id == admin_telegram_id:
            return True
        await message.answer("Управление общим каталогом доступно только администратору.")
        return False

    @router.message(Command("sources_add"))
    async def add_sources(message: Message, command: CommandObject) -> None:
        if not await admin(message):
            return
        if not command.args:
            await message.answer(
                "Формат: /sources_add и ссылки, @username или ID, по одному на строку.\n"
                "Можно отправлять список частями. Аккаунт попробует вступить по ссылкам; "
                "числовой ID подходит только для уже доступного чата."
            )
            return
        references = [line.strip() for line in command.args.splitlines() if line.strip()]
        if len(references) > 400 or any(len(ref) > 500 for ref in references):
            await message.answer(
                "За раз допускается до 400 источников; каждая ссылка — до 500 символов."
            )
            return
        count = await catalog.enqueue_imports(references)
        await message.answer(
            f"В очередь добавлено: {count}. Вступление и проверка идут в фоне. Статус — /sources_status."
        )

    @router.message(Command("sources"))
    async def sources(message: Message, command: CommandObject) -> None:
        if not await admin(message):
            return
        page = _page(command.args)
        if page is None:
            await message.answer("Формат: /sources <номер страницы>")
            return
        sources = await catalog.list_sources(page)
        lines = [
            f"Общий каталог, страница {page}. Активных источников: {await catalog.source_count()}."
        ]
        for source in sources:
            state = "включён" if source.is_enabled else "отключён"
            lines.append(
                f"• {source.title.replace(chr(10), ' ')[:90]} — {source.telegram_chat_id} ({state})"
            )
            if source.last_error:
                lines.append(f"  {source.last_error[:80]}")
        if not sources:
            lines.append("На этой странице источников нет.")
        lines.append(
            f"Следующая страница: /sources {page + 1}\nУдалить: /sources_remove <ID>; добавить или восстановить: /sources_add <ссылка или ID>."
        )
        await message.answer("\n".join(lines))

    @router.message(Command("sources_remove"))
    async def remove_source(message: Message, command: CommandObject) -> None:
        if not await admin(message):
            return
        try:
            chat_id = int(command.args or "")
        except ValueError:
            await message.answer("Формат: /sources_remove <ID из /sources>")
            return
        removed = await catalog.remove_source(chat_id)
        await message.answer(
            "Источник отключён в общем каталоге. Аккаунт остаётся в чате."
            if removed
            else "Активный источник с таким ID не найден."
        )

    @router.message(Command("sources_status"))
    async def sources_status(message: Message, command: CommandObject) -> None:
        if not await admin(message):
            return
        page = _page(command.args)
        if page is None:
            await message.answer("Формат: /sources_status <номер страницы ошибок>")
            return
        labels = {
            "pending": "Ожидают",
            "running": "Обрабатываются",
            "completed": "Добавлены",
            "failed": "Ошибки",
        }
        lines = ["Импорт источников:"]
        lines.extend(
            f"{labels.get(status, status)}: {count}"
            for status, count in await catalog.import_status()
        )
        lines.append(f"Ошибки, страница {page}:")
        for job in await catalog.failed_imports(page):
            lines.append(f"• {job.reference[:100]}: {(job.last_error or 'Ошибка')[:160]}")
        lines.append(
            "После исправления причины повторите /sources_add для нужных ссылок. Следующая страница ошибок: "
            f"/sources_status {page + 1}"
        )
        await message.answer("\n".join(lines))

    return router


def search_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Поиск по всем источникам", callback_data="catalog:search")],
            [InlineKeyboardButton(text="Мои группы", callback_data="groups:list")],
        ]
    )
