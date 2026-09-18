from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from tutor_lead_monitor.config import PositiveInt, RetentionConfig
from tutor_lead_monitor.db.locks import MAINTENANCE, transaction_lock
from tutor_lead_monitor.db.models import (
    CollectionRun,
    Feedback,
    Lead,
    LeadOccurrence,
    Notification,
    NotificationChunk,
    NotificationItem,
    RawItem,
    Source,
)
from tutor_lead_monitor.domain.models import utc


@dataclass(frozen=True)
class RetentionResult:
    dry_run: bool
    notifications: int
    notification_items: int
    leads: int
    lead_occurrences: int
    raw_items: int
    collection_runs: int
    notification_chunks: int = 0


def run_retention(
    engine: Engine, config: RetentionConfig, *, dry_run: bool = True, now: datetime | None = None
) -> RetentionResult:
    instant = utc(now) if now is not None else datetime.now(UTC)
    with Session(engine) as session, session.begin():
        # Cooperating ingestion/processing transactions hold the shared counterpart.
        transaction_lock(session, MAINTENANCE)
        notification_ids = set(
            session.scalars(
                select(Notification.id).where(
                    Notification.created_at < instant - timedelta(days=config.notifications_days),
                    Notification.status.in_(["sent", "failed", "skipped"]),
                )
            )
        )
        protected: set[UUID] = set(session.scalars(select(Feedback.lead_id)))
        protected.update(
            session.scalars(
                select(Notification.lead_id).where(
                    Notification.id.not_in(notification_ids), Notification.lead_id.is_not(None)
                )
            )
        )
        protected.update(
            session.scalars(
                select(NotificationItem.lead_id).where(
                    NotificationItem.notification_id.not_in(notification_ids)
                )
            )
        )
        # Keep idempotency envelopes for retained leads, including feedback-protected leads.
        # Digest envelopes link several leads, so preserve their dependency closure too.
        protected.update(
            session.scalars(
                select(Lead.id).where(
                    Lead.last_seen_at >= instant - timedelta(days=config.leads_days)
                )
            )
        )
        links = list(
            session.execute(
                select(Notification.id, Notification.lead_id).where(
                    Notification.lead_id.is_not(None)
                )
            ).tuples()
        )
        links.extend(
            session.execute(
                select(NotificationItem.notification_id, NotificationItem.lead_id)
            ).tuples()
        )
        while True:
            keep_envelopes = {
                notification_id for notification_id, lead_id in links if lead_id in protected
            }
            notification_ids.difference_update(keep_envelopes)
            expanded = {
                lead_id
                for notification_id, lead_id in links
                if notification_id not in notification_ids and lead_id is not None
            }
            if expanded <= protected:
                break
            protected.update(expanded)
        lead_ids = set(
            session.scalars(
                select(Lead.id).where(
                    Lead.last_seen_at < instant - timedelta(days=config.leads_days),
                    Lead.id.not_in(protected),
                )
            )
        )
        occurrences = session.execute(
            select(LeadOccurrence.lead_id, LeadOccurrence.raw_item_id)
        ).all()
        retained_raw = {raw_id for lead_id, raw_id in occurrences if lead_id not in lead_ids}
        retained_raw.update(
            session.scalars(select(Lead.canonical_raw_item_id).where(Lead.id.not_in(lead_ids)))
        )
        raw_ids: set[UUID] = set()
        retention_days = TypeAdapter(PositiveInt)
        source_days = {
            source.id: retention_days.validate_python(
                source.config.get("retention_days", config.leads_days)
            )
            for source in session.scalars(select(Source))
        }
        for raw in session.scalars(
            select(RawItem).where(
                RawItem.processing_status.in_(["rejected", "processed"]),
                RawItem.id.not_in(retained_raw),
            )
        ):
            duration = (
                config.raw_rejected_days
                if raw.processing_status == "rejected"
                else config.leads_days
            )
            if raw.collected_at < instant - timedelta(
                days=min(duration, source_days[raw.source_id])
            ):
                raw_ids.add(raw.id)
        run_ids = set(
            session.scalars(
                select(CollectionRun.id).where(
                    CollectionRun.finished_at < instant - timedelta(days=config.notifications_days),
                    CollectionRun.status != "running",
                )
            )
        )
        membership_count = len(
            session.scalars(
                select(NotificationItem.lead_id).where(
                    NotificationItem.notification_id.in_(notification_ids)
                )
            ).all()
        )
        result = RetentionResult(
            dry_run,
            len(notification_ids),
            membership_count,
            len(lead_ids),
            sum(lead_id in lead_ids for lead_id, _ in occurrences),
            len(raw_ids),
            len(run_ids),
            len(
                session.scalars(
                    select(NotificationChunk.position).where(
                        NotificationChunk.notification_id.in_(notification_ids)
                    )
                ).all()
            ),
        )
        if not dry_run:
            session.execute(
                delete(NotificationChunk).where(
                    NotificationChunk.notification_id.in_(notification_ids)
                )
            )
            # Explicit dependency order; never delete feedback or rely on cascades.
            session.execute(
                delete(NotificationItem).where(
                    NotificationItem.notification_id.in_(notification_ids)
                )
            )
            session.execute(delete(Notification).where(Notification.id.in_(notification_ids)))
            session.execute(delete(LeadOccurrence).where(LeadOccurrence.lead_id.in_(lead_ids)))
            session.execute(delete(Lead).where(Lead.id.in_(lead_ids)))
            session.execute(delete(RawItem).where(RawItem.id.in_(raw_ids)))
            session.execute(delete(CollectionRun).where(CollectionRun.id.in_(run_ids)))
        return result
