import argparse
import asyncio
import json
import logging
import signal
import time
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime
from threading import Event
from uuid import UUID
from zoneinfo import ZoneInfo

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from tutor_lead_monitor.application.collect import run_collector
from tutor_lead_monitor.application.failed import inspect_failed, reset_failed, validate_selection
from tutor_lead_monitor.application.process import process_pending
from tutor_lead_monitor.application.retention import run_retention
from tutor_lead_monitor.collectors.fixture import FixtureCollector
from tutor_lead_monitor.config import FixtureOptions, Settings, load_config
from tutor_lead_monitor.db.repositories import sync_source
from tutor_lead_monitor.db.session import check_database, create_db_engine, session_factory
from tutor_lead_monitor.domain.models import CollectionContext, utc
from tutor_lead_monitor.logging import configure_logging

logger = logging.getLogger(__name__)


def check_ready(engine: Engine) -> None:
    check_database(engine)
    expected = set(ScriptDirectory.from_config(Config("alembic.ini")).get_heads())
    with engine.connect() as connection:
        actual = set(MigrationContext.configure(connection).get_current_heads())
    if not expected or actual != expected:
        raise RuntimeError("Database migrations are not current")


async def fixture_preview(collector: FixtureCollector) -> None:
    async for page in collector.collect(CollectionContext(None, None, "fixture-preview")):
        print(
            json.dumps(
                {
                    "source_key": collector.key,
                    "external_ids": [item.external_id for item in page.items],
                    "next_cursor": page.next_cursor,
                    "has_more": page.has_more,
                }
            )
        )


def serve(engine: Engine, interval: int) -> None:
    """Foundation lifecycle only. Business job scheduling starts in later milestones."""
    stop = Event()

    def stop_service(signum: int, frame: object) -> None:
        stop.set()

    previous = {sig: signal.signal(sig, stop_service) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        logger.info("service_started")
        while not stop.is_set():
            started = time.monotonic()
            try:
                check_ready(engine)
                logger.info(
                    "database_health",
                    extra={
                        "status": "ready",
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    },
                )
            except Exception as error:
                logger.error(
                    "database_health",
                    extra={
                        "status": "unavailable",
                        "error_category": type(error).__name__,
                    },
                )
            stop.wait(interval)
        logger.info("service_stopped")
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tutor Lead Monitor — local pipeline")
    parser.add_argument(
        "command",
        choices=[
            "check-config",
            "healthcheck",
            "migrate",
            "sync-sources",
            "fixture",
            "serve",
            "collect",
            "process",
            "pipeline",
            "retention",
            "failed",
            "retry-failed",
            "telegram-bot",
            "notify-immediate",
            "send-digest",
        ],
    )
    parser.add_argument("--source", default="fixture", help="Fixture registry key")
    parser.add_argument(
        "--local-date", type=date.fromisoformat, help="Digest date in configured timezone"
    )
    parser.add_argument("--limit", type=int, help="Maximum records (failed commands: 1..1000)")
    parser.add_argument("--record-id", type=UUID, help="Internal failed-record UUID to reset")
    parser.add_argument(
        "--as-of",
        type=datetime.fromisoformat,
        help="Aware ISO timestamp for controlled scoring/retention",
    )
    retention_mode = parser.add_mutually_exclusive_group()
    retention_mode.add_argument("--apply", action="store_true", help="Apply retention deletions")
    retention_mode.add_argument(
        "--dry-run", action="store_true", help="Preview retention (default)"
    )
    args = parser.parse_args(argv)
    configure_logging()
    engine: Engine | None = None
    try:
        if args.as_of is not None:
            args.as_of = utc(args.as_of)
        if args.command == "retry-failed":
            validate_selection(args.record_id, args.limit)
        elif args.record_id is not None:
            raise ValueError("Record ID is only supported for retry-failed")
        if args.command == "failed":
            validate_selection(None, args.limit if args.limit is not None else 100)
        if args.command != "retention" and (args.apply or args.dry_run):
            raise ValueError("Apply/dry-run flags are only supported for retention")
        if args.limit is not None and args.limit < 1:
            raise ValueError("Processing limit must be positive")
        settings = Settings()
        configure_logging(settings.log_level)
        config = load_config(settings.config_dir)
        telegram_access = (
            settings.telegram_access()
            if args.command in {"telegram-bot", "notify-immediate", "send-digest"}
            else None
        )
        if args.command == "check-config":
            logger.info("configuration_valid")
            return 0
        if args.command == "fixture":
            source = next(s for s in config.registry.sources if s.key == args.source)
            if not source.enabled or source.policy_status != "approved" or source.kind != "fixture":
                raise ValueError("Fixture source must be enabled and approved")
            options = FixtureOptions.model_validate(source.config)
            asyncio.run(
                fixture_preview(FixtureCollector(source.key, options.page_size, options.dataset))
            )
            return 0
        engine = create_db_engine(settings)
        if args.command == "migrate":
            with engine.begin() as connection:
                migration_config = Config("alembic.ini")
                migration_config.attributes["connection"] = connection
                command.upgrade(migration_config, "head")
            logger.info("migrations_applied")
            return 0
        check_ready(engine)
        if telegram_access is not None:
            from telegram import Bot

            from tutor_lead_monitor.application.bot import OwnerAccess
            from tutor_lead_monitor.application.notify import send_digest, send_immediate
            from tutor_lead_monitor.notifications.telegram import TelegramNotifier, run_bot

            token, allowed, recipient = telegram_access
            if args.command == "telegram-bot":
                run_bot(engine, config, token, OwnerAccess(allowed, recipient))
                return 0

            async def notify() -> int:
                assert engine is not None
                async with Bot(token) as bot:
                    notifier = TelegramNotifier(bot)
                    now = args.as_of or datetime.now(UTC)
                    if args.command == "notify-immediate":
                        result = await send_immediate(
                            engine, config, recipient, notifier, now=now, limit=args.limit or 100
                        )
                    else:
                        day = (
                            args.local_date
                            or now.astimezone(ZoneInfo(config.business.timezone)).date()
                        )
                        result = await send_digest(
                            engine, config, recipient, notifier, day, now=now, explicit=True
                        )
                    print(json.dumps(asdict(result)))
                    return int(result.failed > 0)

            return asyncio.run(notify())
        if args.command == "failed":
            print(json.dumps(asdict(inspect_failed(engine, limit=args.limit or 100)), default=str))
            return 0
        if args.command == "retry-failed":
            ids = reset_failed(engine, record_id=args.record_id, limit=args.limit)
            print(json.dumps({"reset": len(ids), "record_ids": ids}, default=str))
            return 0
        unsuccessful = False
        if args.command in {"collect", "pipeline"}:
            sources = (
                config.registry.sources
                if args.command == "pipeline"
                else [s for s in config.registry.sources if s.key == args.source]
            )
            if not sources:
                raise ValueError("Unknown source")
            for source in sources:
                if args.command == "pipeline" and not source.enabled:
                    continue
                try:
                    options = FixtureOptions.model_validate(source.config)
                    outcome = asyncio.run(
                        run_collector(
                            engine,
                            source,
                            FixtureCollector(source.key, options.page_size, options.dataset),
                        )
                    )
                    print(json.dumps(asdict(outcome), default=str))
                    unsuccessful |= outcome.status != "succeeded"
                except Exception as error:
                    if args.command == "collect":
                        raise
                    unsuccessful = True
                    logger.error(
                        "collection_failed",
                        extra={
                            "source_key": source.key,
                            "error_category": type(error).__name__,
                        },
                    )
            if args.command == "collect":
                return int(unsuccessful)
        if args.command in {"process", "pipeline"}:
            processed = process_pending(engine, config, limit=args.limit or 1000, now=args.as_of)
            print(json.dumps(asdict(processed)))
            return int(processed.failed > 0 or (args.command == "pipeline" and unsuccessful))
        if args.command == "retention":
            print(
                json.dumps(
                    asdict(
                        run_retention(
                            engine,
                            config.business.retention,
                            dry_run=not args.apply,
                            now=args.as_of,
                        )
                    )
                )
            )
            return 0
        if args.command == "sync-sources":
            with session_factory(engine).begin() as session:
                for source in config.registry.sources:
                    sync_source(session, source)
            logger.info("sources_synchronized")
        elif args.command == "serve":
            serve(engine, settings.health_interval_seconds)
        else:
            logger.info("database_ready")
        return 0
    except Exception as error:
        # SQLAlchemy/provider/validation exceptions may embed credentials or input text.
        logger.error("command_failed", extra={"error_category": type(error).__name__})
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
