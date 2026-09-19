import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from tutor_lead_monitor.collectors.base import Collector
from tutor_lead_monitor.config import SourceConfig
from tutor_lead_monitor.db.locks import MAINTENANCE, source_lock, transaction_lock
from tutor_lead_monitor.db.models import CollectionRun, CollectorState, Source
from tutor_lead_monitor.db.repositories import persist_page, sync_source
from tutor_lead_monitor.domain.models import CollectionContext, CollectionPage, utc
from tutor_lead_monitor.search.base import SearchError
from tutor_lead_monitor.vk_api import VKError, VKFailure

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CollectionResult:
    run_id: UUID
    status: str
    items_seen: int
    items_inserted: int
    items_failed: int


async def run_collector(
    engine: Engine, config: SourceConfig, collector: Collector
) -> CollectionResult:
    if collector.key != config.key or not config.enabled or config.policy_status != "approved":
        raise ValueError("Collector must match an enabled approved source")
    if config.kind not in {"fixture", "web_search", "vk_api"}:
        raise ValueError("Unsupported collector kind")
    config.check_authorization()
    now = datetime.now(UTC)
    run_id = uuid4()
    with source_lock(engine, config.key) as connection:
        with Session(connection) as session, session.begin():
            transaction_lock(session, MAINTENANCE, shared=True)
            source = session.scalar(select(Source).where(Source.key == config.key))
            source_id = sync_source(session, config) if source is None else source.id
            source = session.get(Source, source_id)
            if source is None or not source.enabled or source.policy_status != "approved":
                raise ValueError("Stored source is disabled or paused; review before re-enabling")
            state = session.get(CollectorState, source_id)
            if state is None or (state.paused_until is not None and state.paused_until > now):
                raise ValueError("Collector state is unavailable or paused")
            session.execute(
                update(CollectionRun)
                .where(CollectionRun.source_id == source_id, CollectionRun.status == "running")
                .values(status="failed", finished_at=now, error_category="interrupted")
            )
            # A truncated first VK window has no durable lower boundary. Retrying
            # it after new arrivals could skip a previously failed item forever.
            # Preserve evidence/state and require owner catch-up review instead.
            incomplete_vk_bootstrap = (
                config.kind == "vk_api"
                and state.cursor is None
                and session.scalar(
                    select(CollectionRun.id)
                    .where(
                        CollectionRun.source_id == source_id,
                        CollectionRun.status.in_(("failed", "partial")),
                        CollectionRun.items_seen > 0,
                    )
                    .limit(1)
                )
                is not None
            )
            context = CollectionContext(
                None if state.cursor is not None else state.high_water_mark,
                state.cursor,
                str(run_id),
            )
            session.add(CollectionRun(id=run_id, source_id=source_id, started_at=now))
        seen = inserted = failed = 0
        error_category: str | None = None
        checkpoint = context.cursor
        retry_after: int | None = None
        try:
            if incomplete_vk_bootstrap:
                raise VKError(VKFailure.OVERFLOW)
            async for page in collector.collect(context):
                seen += len(page.items)
                page_failed = 0
                try:
                    with Session(connection) as session, session.begin():
                        transaction_lock(session, MAINTENANCE, shared=True)
                        added = 0
                        for item in page.items:
                            try:
                                added += persist_page(
                                    session,
                                    source_id=source_id,
                                    source_key=config.key,
                                    page=CollectionPage((item,), checkpoint, False),
                                )
                            except Exception as error:
                                page_failed += 1
                                error_category = type(error).__name__
                        # A failed item is replayable: never checkpoint past its page.
                        # Later valid items still persist, and conflicts make the replay safe.
                        if failed == 0 and page_failed == 0:
                            persist_page(
                                session,
                                source_id=source_id,
                                source_key=config.key,
                                page=CollectionPage((), page.next_cursor, page.has_more),
                                high_water_mark=max(
                                    (
                                        utc(i.published_at)
                                        for i in page.items
                                        if i.published_at is not None
                                    ),
                                    default=None,
                                ),
                            )
                        session.execute(
                            update(CollectionRun)
                            .where(CollectionRun.id == run_id)
                            .values(
                                items_seen=seen,
                                items_inserted=inserted + added,
                                items_failed=failed + page_failed,
                            )
                        )
                    inserted += added
                    failed += page_failed
                    if failed == 0:
                        checkpoint = page.next_cursor
                except Exception:
                    failed += len(page.items)
                    raise
        except Exception as error:
            error_category = (
                error.category.value
                if isinstance(error, (SearchError, VKError))
                else type(error).__name__
            )
            if isinstance(error, SearchError):
                retry_after = error.retry_after
        status = "succeeded" if error_category is None else "partial" if seen > failed else "failed"
        with Session(connection) as session, session.begin():
            transaction_lock(session, MAINTENANCE, shared=True)
            session.execute(
                update(CollectionRun)
                .where(CollectionRun.id == run_id)
                .values(
                    status=status,
                    finished_at=datetime.now(UTC),
                    items_seen=seen,
                    items_inserted=inserted,
                    items_failed=failed,
                    error_category=error_category,
                )
            )
            state = session.get(CollectorState, source_id)
            assert state is not None
            if error_category is None:
                state.last_success_at = datetime.now(UTC)
                state.consecutive_failures = 0
            else:
                state.consecutive_failures += 1
                if retry_after is not None:
                    state.paused_until = datetime.now(UTC) + timedelta(seconds=retry_after)
        logger.info(
            "collection_finished",
            extra={
                "run_id": str(run_id),
                "source_key": config.key,
                "status": status,
                "items_seen": seen,
                "items_inserted": inserted,
                "items_failed": failed,
                "error_category": error_category,
            },
        )
        return CollectionResult(run_id, status, seen, inserted, failed)
