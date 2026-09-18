from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from tutor_lead_monitor.application.retention import run_retention
from tutor_lead_monitor.config import load_config
from tutor_lead_monitor.db.models import (
    CollectionRun,
    Feedback,
    Lead,
    LeadOccurrence,
    Notification,
    NotificationItem,
    RawItem,
    Source,
)
from tutor_lead_monitor.db.repositories import sync_source

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 17, tzinfo=UTC)


def add_lead(session: Session, source_id: UUID, key: str, age: int) -> Lead:
    raw = RawItem(
        source_id=source_id,
        external_id=key,
        text="synthetic",
        collected_at=NOW - timedelta(days=age),
        processing_status="processed",
    )
    session.add(raw)
    session.flush()
    lead = Lead(
        canonical_raw_item_id=raw.id,
        intent="seeking_tutor",
        subject="literature",
        score=90,
        classification_version="test",
        scoring_version="test",
        last_seen_at=raw.collected_at,
    )
    session.add(lead)
    session.flush()
    session.add(LeadOccurrence(lead_id=lead.id, raw_item_id=raw.id, match_method="exact_id"))
    return lead


def test_retention_dry_run_apply_and_protected_evidence(migrated_engine: Engine) -> None:
    config = load_config(Path("config"))
    with Session(migrated_engine) as session, session.begin():
        source_id = sync_source(session, config.registry.sources[0])
        expired = add_lead(session, source_id, "expired", 200)
        protected = add_lead(session, source_id, "feedback", 200)
        recent = add_lead(session, source_id, "recent", 10)
        pending = add_lead(session, source_id, "pending_notification", 200)
        session.add(Feedback(lead_id=protected.id, recipient_key="synthetic", value="interested"))
        old_digest = Notification(
            kind="digest",
            recipient_key="synthetic",
            period_key="old",
            status="sent",
            created_at=NOW - timedelta(days=181),
        )
        session.add(old_digest)
        session.flush()
        session.add(NotificationItem(notification_id=old_digest.id, lead_id=expired.id))
        session.add(
            Notification(
                lead_id=pending.id,
                kind="immediate",
                recipient_key="synthetic",
                status="pending",
                created_at=NOW - timedelta(days=10),
            )
        )
        session.add_all(
            [
                RawItem(
                    source_id=source_id,
                    external_id="rejected_old",
                    text="ad",
                    processing_status="rejected",
                    collected_at=NOW - timedelta(days=31),
                ),
                RawItem(
                    source_id=source_id,
                    external_id="rejected_boundary",
                    text="ad",
                    processing_status="rejected",
                    collected_at=NOW - timedelta(days=30),
                ),
                RawItem(
                    source_id=source_id,
                    external_id="failed",
                    text="failed",
                    processing_status="failed",
                    collected_at=NOW - timedelta(days=200),
                ),
                CollectionRun(
                    source_id=source_id,
                    started_at=NOW - timedelta(days=200),
                    finished_at=NOW - timedelta(days=200),
                    status="succeeded",
                ),
                CollectionRun(
                    source_id=source_id, started_at=NOW - timedelta(days=200), status="running"
                ),
            ]
        )
        keep_ids = {protected.id, recent.id, pending.id}
    preview = run_retention(migrated_engine, config.business.retention, now=NOW)
    assert preview.dry_run
    assert (
        preview.notifications,
        preview.notification_items,
        preview.leads,
        preview.lead_occurrences,
        preview.raw_items,
        preview.collection_runs,
    ) == (1, 1, 1, 1, 2, 1)
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(Lead)) == 4
        assert session.scalar(select(func.count()).select_from(RawItem)) == 7
    applied = run_retention(migrated_engine, config.business.retention, now=NOW, dry_run=False)
    assert applied.raw_items == preview.raw_items and applied.leads == preview.leads
    with Session(migrated_engine) as session:
        assert set(session.scalars(select(Lead.id))) == keep_ids
        assert session.scalar(select(func.count()).select_from(Feedback)) == 1
        assert session.scalar(select(func.count()).select_from(RawItem)) == 5
        assert session.scalar(select(func.count()).select_from(CollectionRun)) == 1
    assert run_retention(migrated_engine, config.business.retention, now=NOW).raw_items == 0


def test_retention_configured_period_and_naive_time(migrated_engine: Engine) -> None:
    config = load_config(Path("config"))
    with Session(migrated_engine) as session, session.begin():
        source_id = sync_source(session, config.registry.sources[0])
        session.add(
            RawItem(
                source_id=source_id,
                external_id="rejected",
                text="ad",
                processing_status="rejected",
                collected_at=NOW - timedelta(days=10),
            )
        )
    short = config.business.retention.model_copy(update={"raw_rejected_days": 5})
    assert run_retention(migrated_engine, short, now=NOW).raw_items == 1
    with pytest.raises(ValueError, match="Timezone-aware"):
        run_retention(migrated_engine, short, now=NOW.replace(tzinfo=None))


def test_retention_accepts_older_registry_snapshots(migrated_engine: Engine) -> None:
    config = load_config(Path("config"))
    with Session(migrated_engine) as session, session.begin():
        source_id = sync_source(session, config.registry.sources[0])
        source = session.get(Source, source_id)
        assert source is not None
        source.config = {"retention_days": 5}
        session.add(
            RawItem(
                source_id=source_id,
                external_id="old",
                text="synthetic",
                processing_status="rejected",
                collected_at=NOW - timedelta(days=10),
            )
        )
    assert run_retention(migrated_engine, config.business.retention, now=NOW).raw_items == 1
