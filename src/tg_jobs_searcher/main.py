"""Application CLI and lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tg_jobs_searcher.bot.app import BotApplication, create_bot_application
from tg_jobs_searcher.config import ConfigurationError, Settings, TelegramSettings, TelethonSettings
from tg_jobs_searcher.db.monitoring_repository import MonitoringRepository
from tg_jobs_searcher.db.scan_repository import ScanRepository
from tg_jobs_searcher.db.session import (
    InstanceAlreadyRunningError,
    PostgresAdvisoryLock,
    check_database_connection,
    create_engine,
    create_session_factory,
)
from tg_jobs_searcher.logging import configure_logging
from tg_jobs_searcher.services.live_messages import LiveMessageProcessor
from tg_jobs_searcher.services.scanning import HistoryScanWorker, PeriodicRecoveryScheduler
from tg_jobs_searcher.telegram.authorize import run_authorisation
from tg_jobs_searcher.telegram.client import (
    connect_authorized_client,
    create_telegram_client,
)
from tg_jobs_searcher.telegram.reader import LiveMessageMonitor

logger = logging.getLogger(__name__)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram jobs searcher")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and PostgreSQL connectivity, then exit",
    )
    subparsers = parser.add_subparsers(dest="command")
    auth_parser = subparsers.add_parser(
        "auth", help="authorise the Telegram account and save a session"
    )
    auth_parser.add_argument("--phone", help="phone number in international format")
    return parser.parse_args(argv)


async def run(*, check_only: bool) -> int:
    try:
        settings = Settings.from_env()
        telegram_settings = None if check_only else TelegramSettings.from_env()
    except ConfigurationError as exc:
        logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")
        logging.getLogger(__name__).error("configuration_invalid: %s", exc)
        return 2

    configure_logging(settings.log_level)
    engine = create_engine(settings)
    advisory_lock = PostgresAdvisoryLock(engine, settings.app_lock_key)
    try:
        await check_database_connection(engine)
        await advisory_lock.acquire()
        logger.info("database_ready")
        if check_only:
            logger.info("health_check_passed")
            return 0

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        _register_signal_handlers(loop, stop_event)
        logger.info("application_started")
        assert telegram_settings is not None
        return await _run_telegram_application(
            telegram_settings,
            stop_event,
            create_session_factory(engine),
        )
    except InstanceAlreadyRunningError:
        logger.error("instance_already_running")
        return 3
    except Exception:
        logger.exception("application_initialization_failed")
        return 1
    finally:
        await advisory_lock.release()
        await _dispose_engine(engine)
        logger.info("application_stopped")


def _register_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop_event.set)
        except (NotImplementedError, RuntimeError):
            # Windows and embedded event loops may not expose Unix signal handlers.
            pass


async def _dispose_engine(engine: AsyncEngine) -> None:
    await engine.dispose()


async def _run_telegram_application(
    settings: TelegramSettings,
    stop_event: asyncio.Event,
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    client = create_telegram_client(settings.telethon)
    application: BotApplication | None = None
    scan_task: asyncio.Task[None] | None = None
    recovery_task: asyncio.Task[None] | None = None
    monitor = LiveMessageMonitor(client, LiveMessageProcessor(MonitoringRepository(session_factory)))
    try:
        await connect_authorized_client(client)
        # Register before polling so a newly activated group has no monitoring gap.
        await monitor.start()
        application = create_bot_application(settings.bot, client, session_factory)
        scan_repository = ScanRepository(session_factory)
        await scan_repository.requeue_running()
        recovered_jobs = await scan_repository.schedule_recovery_jobs()
        if recovered_jobs:
            logger.info("recovery_scan_jobs_scheduled", extra={"count": recovered_jobs})
        scan_task = asyncio.create_task(
            HistoryScanWorker(scan_repository, client, application.bot).run(stop_event),
            name="history-scan-worker",
        )
        recovery_task = asyncio.create_task(
            PeriodicRecoveryScheduler(scan_repository).run(stop_event),
            name="periodic-recovery-scheduler",
        )
        await application.run_until_stopped(stop_event, (scan_task, recovery_task))
        logger.info("shutdown_requested")
        return 0
    finally:
        background_tasks = [task for task in (scan_task, recovery_task) if task is not None]
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        if application is not None:
            await application.close()
        await monitor.stop()
        await client.disconnect()


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.command == "auth":
        try:
            exit_code = run_authorisation(TelethonSettings.from_env(), phone=args.phone)
        except ConfigurationError as exc:
            logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")
            logging.getLogger(__name__).error("configuration_invalid: %s", exc)
            exit_code = 2
        raise SystemExit(exit_code)
    raise SystemExit(asyncio.run(run(check_only=args.check)))
