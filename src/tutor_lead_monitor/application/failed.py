"""Bounded, explicit maintenance of failed processing records; no raw data output."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from tutor_lead_monitor.db.locks import MAINTENANCE, transaction_lock
from tutor_lead_monitor.db.models import RawItem

MAX_FAILED_BATCH = 1000


def validate_selection(record_id: UUID | None, limit: int | None) -> None:
    if (record_id is None) == (limit is None):
        raise ValueError("Specify exactly one record ID or explicit limit")
    if limit is not None and not 1 <= limit <= MAX_FAILED_BATCH:
        raise ValueError("Failed-record limit must be between 1 and 1000")


@dataclass(frozen=True)
class FailedRecords:
    total: int
    record_ids: tuple[UUID, ...]


def inspect_failed(engine: Engine, *, limit: int = 100) -> FailedRecords:
    validate_selection(None, limit)
    with Session(engine) as session:
        failed = RawItem.processing_status == "failed"
        total = session.scalar(select(func.count()).select_from(RawItem).where(failed)) or 0
        ids = session.scalars(
            select(RawItem.id).where(failed).order_by(RawItem.collected_at, RawItem.id).limit(limit)
        )
        return FailedRecords(total, tuple(ids))


def reset_failed(
    engine: Engine, *, record_id: UUID | None = None, limit: int | None = None
) -> tuple[UUID, ...]:
    validate_selection(record_id, limit)
    with Session(engine) as session, session.begin():
        transaction_lock(session, MAINTENANCE, shared=True)
        query = select(RawItem).where(RawItem.processing_status == "failed")
        if record_id is not None:
            query = query.where(RawItem.id == record_id)
        records = session.scalars(
            query.order_by(RawItem.collected_at, RawItem.id)
            .limit(limit or 1)
            .with_for_update(skip_locked=True)
        ).all()
        for raw in records:
            raw.processing_status = "pending"
            raw.processing_error = None
        return tuple(raw.id for raw in records)
