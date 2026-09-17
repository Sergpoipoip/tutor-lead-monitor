import argparse
import asyncio
import json
import logging
import signal
import time
from collections.abc import Sequence
from threading import Event

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from tutor_lead_monitor.collectors.fixture import FixtureCollector
from tutor_lead_monitor.config import FixtureOptions, Settings, load_config
from tutor_lead_monitor.db.repositories import sync_source
from tutor_lead_monitor.db.session import check_database, create_db_engine, session_factory
from tutor_lead_monitor.domain.models import CollectionContext
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
    parser = argparse.ArgumentParser(description="Tutor Lead Monitor — Milestone 1 foundation")
    parser.add_argument(
        "command",
        choices=[
            "check-config",
            "healthcheck",
            "migrate",
            "sync-sources",
            "fixture",
            "serve",
        ],
    )
    parser.add_argument("--source", default="fixture", help="Fixture registry key")
    args = parser.parse_args(argv)
    configure_logging()
    engine: Engine | None = None
    try:
        settings = Settings()
        configure_logging(settings.log_level)
        config = load_config(settings.config_dir)
        if args.command == "check-config":
            logger.info("configuration_valid")
            return 0
        if args.command == "fixture":
            source = next(s for s in config.registry.sources if s.key == args.source)
            if not source.enabled or source.policy_status != "approved" or source.kind != "fixture":
                raise ValueError("Fixture source must be enabled and approved")
            options = FixtureOptions.model_validate(source.config)
            asyncio.run(fixture_preview(FixtureCollector(source.key, options.page_size)))
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
