from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from conftest import alembic_config
from sqlalchemy import Engine, func, inspect, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from tutor_lead_monitor.cli import check_ready
from tutor_lead_monitor.collectors.fixture import TEXTS, FixtureCollector
from tutor_lead_monitor.config import SourceConfig
from tutor_lead_monitor.db.base import Base
from tutor_lead_monitor.db.models import (
    CollectorState,
    Feedback,
    Lead,
    LeadOccurrence,
    Notification,
    NotificationItem,
    RawItem,
    Source,
)
from tutor_lead_monitor.db.repositories import persist_page, sync_source
from tutor_lead_monitor.domain.models import CollectionContext, CollectionPage

pytestmark = pytest.mark.integration


def test_migration_round_trip_and_metadata(migrated_engine: Engine) -> None:
    with migrated_engine.begin() as connection:
        assert set(inspect(connection).get_table_names()) == set(Base.metadata.tables) | {
            "alembic_version"
        }
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        command.upgrade(alembic_config(connection), "head")
        command.check(alembic_config(connection))
        command.downgrade(alembic_config(connection), "base")
        assert set(inspect(connection).get_table_names()) == {"alembic_version"}
        command.upgrade(alembic_config(connection), "head")
    check_ready(migrated_engine)


async def pages() -> list[CollectionPage]:
    return [
        page async for page in FixtureCollector().collect(CollectionContext(None, None, "test"))
    ]


async def seed_raw(session: Session, config: SourceConfig) -> tuple[UUID, RawItem]:
    source_id = sync_source(session, config)
    persist_page(session, source_id=source_id, source_key=config.key, page=(await pages())[0])
    raw = session.scalars(select(RawItem).order_by(RawItem.external_id)).first()
    assert raw is not None
    return source_id, raw


async def seed_lead(session: Session, config: SourceConfig) -> Lead:
    _, raw = await seed_raw(session, config)
    lead = Lead(
        canonical_raw_item_id=raw.id,
        intent="seeking_tutor",
        subject="literature",
        score=94,
        grade=10,
        classification_version="test",
        scoring_version="test",
        last_seen_at=datetime.now(UTC),
    )
    session.add(lead)
    session.flush()
    return lead


async def test_raw_retry_preserves_evidence_and_checkpoint(
    session: Session,
    source_config: SourceConfig,
) -> None:
    source_id = sync_source(session, source_config)
    assert sync_source(session, source_config) == source_id
    collected_pages = await pages()
    for page in collected_pages:
        assert persist_page(session, source_id=source_id, source_key="fixture", page=page) == len(
            page.items
        )
    replay = replace(
        collected_pages[0],
        items=tuple(
            replace(item, text="An edited re-fetch must not overwrite original evidence")
            for item in collected_pages[0].items
        ),
    )
    assert persist_page(session, source_id=source_id, source_key="fixture", page=replay) == 0
    assert session.scalar(select(func.count()).select_from(RawItem)) == len(TEXTS)
    raw = session.scalars(select(RawItem).order_by(RawItem.external_id)).first()
    assert raw is not None and raw.text == collected_pages[0].items[0].text
    assert raw.metadata_ == {"fixture_version": 1}
    assert raw.processing_status == "pending"
    assert raw.exact_fingerprint is None
    assert raw.collected_at.utcoffset() == UTC.utcoffset(None)


async def test_page_failure_rolls_back_items_and_cursor(
    session: Session,
    source_config: SourceConfig,
) -> None:
    source_id = sync_source(session, source_config)
    page = (await pages())[0]
    broken = replace(
        page, items=(page.items[0], replace(page.items[1], text="invalid\x00postgres"))
    )
    with pytest.raises(DBAPIError):
        persist_page(session, source_id=source_id, source_key="fixture", page=broken)
    assert session.scalar(select(func.count()).select_from(RawItem)) == 0
    state = session.get(CollectorState, source_id)
    assert state is not None and state.cursor is None
    assert persist_page(session, source_id=source_id, source_key="fixture", page=page) == 2
    assert state.cursor == page.next_cursor


async def test_empty_page_checkpoint_and_monotonic_high_water_mark(
    session: Session,
    source_config: SourceConfig,
) -> None:
    source_id = sync_source(session, source_config)
    checkpoint = {"version": 1, "offset": 4}
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    for high_water_mark in (timestamp, timestamp - timedelta(days=1)):
        assert (
            persist_page(
                session,
                source_id=source_id,
                source_key="fixture",
                page=CollectionPage((), checkpoint, False),
                high_water_mark=high_water_mark,
            )
            == 0
        )
    state = session.get(CollectorState, source_id)
    assert state is not None
    assert state.cursor == checkpoint
    assert state.high_water_mark == timestamp


async def test_outer_transaction_owns_page_commit(
    migrated_engine: Engine,
    source_config: SourceConfig,
) -> None:
    with Session(migrated_engine) as session, session.begin():
        source_id = sync_source(session, source_config)
    with Session(migrated_engine) as session:
        persist_page(session, source_id=source_id, source_key="fixture", page=(await pages())[0])
        session.rollback()
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(RawItem)) == 0
        state = session.get(CollectorState, source_id)
        assert state is not None and state.cursor is None


async def test_source_key_and_enablement_enforced(
    session: Session,
    source_config: SourceConfig,
) -> None:
    source_id = sync_source(session, source_config)
    page = (await pages())[0]
    with pytest.raises(ValueError, match="source"):
        persist_page(session, source_id=source_id, source_key="wrong", page=page)
    source = session.get(Source, source_id)
    assert source is not None
    source.enabled = False
    with pytest.raises(ValueError, match="enabled"):
        persist_page(session, source_id=source_id, source_key="fixture", page=page)


@pytest.mark.parametrize("score,grade", [(101, 10), (-1, 10), (90, 0), (90, 12)])
async def test_lead_constraints(
    session: Session,
    source_config: SourceConfig,
    score: int,
    grade: int,
) -> None:
    _, raw = await seed_raw(session, source_config)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            Lead(
                canonical_raw_item_id=raw.id,
                intent="seeking_tutor",
                subject="literature",
                grade=grade,
                score=score,
                classification_version="test",
                scoring_version="test",
                last_seen_at=datetime.now(UTC),
            )
        )
        session.flush()


async def test_occurrence_is_unique_and_preserves_foreign_keys(
    session: Session,
    source_config: SourceConfig,
) -> None:
    lead = await seed_lead(session, source_config)
    session.add(
        LeadOccurrence(
            lead_id=lead.id,
            raw_item_id=lead.canonical_raw_item_id,
            match_method="exact_id",
        )
    )
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            LeadOccurrence(
                lead_id=uuid4(),
                raw_item_id=lead.canonical_raw_item_id,
                match_method="manual",
            )
        )
        session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.query(RawItem).filter_by(id=lead.canonical_raw_item_id).delete()
        session.flush()


async def test_notification_idempotency_and_digest_membership(
    session: Session,
    source_config: SourceConfig,
) -> None:
    lead = await seed_lead(session, source_config)
    session.add(
        Notification(lead_id=lead.id, recipient_key="synthetic-recipient", kind="immediate")
    )
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            Notification(
                lead_id=lead.id,
                recipient_key="synthetic-recipient",
                kind="immediate",
            )
        )
        session.flush()
    digest = Notification(
        recipient_key="synthetic-recipient", kind="digest", period_key="2026-01-01"
    )
    session.add(digest)
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            Notification(
                recipient_key="synthetic-recipient",
                kind="digest",
                period_key="2026-01-01",
            )
        )
        session.flush()
    session.add(NotificationItem(notification_id=digest.id, lead_id=lead.id))
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(NotificationItem(notification_id=digest.id, lead_id=lead.id))
        session.flush()
    # Different days and recipients are separate idempotency scopes.
    session.add_all(
        [
            Notification(
                recipient_key="synthetic-recipient", kind="digest", period_key="2026-01-02"
            ),
            Notification(lead_id=lead.id, recipient_key="another-recipient", kind="immediate"),
        ]
    )
    session.flush()


@pytest.mark.parametrize("kind,period", [("immediate", None), ("digest", None)])
def test_invalid_notification_envelope(session: Session, kind: str, period: str | None) -> None:
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(Notification(recipient_key="synthetic", kind=kind, period_key=period))
        session.flush()


async def test_feedback_prevents_accidental_cascade_deletion(
    session: Session,
    source_config: SourceConfig,
) -> None:
    lead = await seed_lead(session, source_config)
    session.add(Feedback(lead_id=lead.id, recipient_key="synthetic", value="interested"))
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.delete(lead)
        session.flush()
