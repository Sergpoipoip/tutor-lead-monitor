"""Foundation persistence primitives; callers own transactions and scheduling."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tutor_lead_monitor.config import SourceConfig
from tutor_lead_monitor.db.models import CollectorState, RawItem, Source
from tutor_lead_monitor.domain.models import CollectionPage, utc


def sync_source(session: Session, config: SourceConfig) -> UUID:
    """Explicitly synchronize registry state, retaining operational history."""
    values = {
        "key": config.key,
        "kind": config.kind,
        "display_name": config.display_name,
        "enabled": config.enabled,
        "access_method": config.access_method,
        "policy_status": config.policy_status,
        "last_reviewed_at": config.policy.reviewed_at,
        "config": config.model_dump(mode="json"),
    }
    statement = insert(Source).values(**values)
    source_id = session.execute(
        statement.on_conflict_do_update(
            index_elements=[Source.key], set_={**values, "updated_at": func.now()}
        ).returning(Source.id)
    ).scalar_one()
    session.execute(insert(CollectorState).values(source_id=source_id).on_conflict_do_nothing())
    return source_id


def persist_page(
    session: Session,
    *,
    source_id: UUID,
    source_key: str,
    page: CollectionPage,
    high_water_mark: datetime | None = None,
) -> int:
    """Insert raw evidence and checkpoint together; never commit internally.

    A savepoint rolls back the whole page on failure, even when the caller
    catches the exception. Retrying preserves the original evidence. Concurrent
    collection orchestration and per-item failure policy belong to Milestone 2.
    """
    if any(item.source_key != source_key for item in page.items):
        raise ValueError("Collected item source does not match collector key")
    source = session.get(Source, source_id)
    if source is None or source.key != source_key:
        raise ValueError("Source ID and collector key must agree")
    if not source.enabled or source.policy_status != "approved":
        raise ValueError("Source must be enabled and approved")
    inserted = 0
    with session.begin_nested():
        for item in page.items:
            result = session.execute(
                insert(RawItem)
                .values(
                    source_id=source_id,
                    external_id=item.external_id,
                    url=item.url,
                    published_at=item.published_at,
                    collected_at=item.collected_at,
                    text=item.text,
                    author_label=item.author_label,
                    metadata_=dict(item.metadata),
                )
                .on_conflict_do_nothing(constraint="uq_raw_items_source_external")
                .returning(RawItem.id)
            ).scalar_one_or_none()
            inserted += result is not None
        state = session.get(CollectorState, source_id, with_for_update=True)
        if state is None:
            raise ValueError("Collector state is missing; synchronize the source first")
        state.cursor = dict(page.next_cursor) if page.next_cursor is not None else None
        if high_water_mark is not None:
            value = utc(high_water_mark)
            if state.high_water_mark is None or value > state.high_water_mark:
                state.high_water_mark = value
        session.flush()
    return inserted
