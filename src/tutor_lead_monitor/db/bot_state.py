from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from tutor_lead_monitor.db.delivery import paused
from tutor_lead_monitor.db.locks import MAINTENANCE, source_lock, transaction_lock
from tutor_lead_monitor.db.models import (
    CallbackReceipt,
    CollectionRun,
    Feedback,
    Lead,
    Notification,
    RawItem,
    RecipientState,
    Source,
)


@dataclass(frozen=True)
class StatusView:
    paused: bool
    pending_processing: int
    failed_processing: int
    pending_notifications: int
    failed_notifications: int
    collectors: tuple[tuple[str, str, datetime | None], ...]


def status_view(engine: Engine, recipient: int) -> StatusView:
    with Session(engine) as session:
        processing = dict(
            session.execute(
                select(RawItem.processing_status, func.count()).group_by(RawItem.processing_status)
            )
            .tuples()
            .all()
        )
        notifications = dict(
            session.execute(
                select(Notification.status, func.count())
                .where(Notification.recipient_key == str(recipient))
                .group_by(Notification.status)
            )
            .tuples()
            .all()
        )
        collectors: list[tuple[str, str, datetime | None]] = []
        for source in session.scalars(select(Source).order_by(Source.key).limit(20)):
            run = session.scalar(
                select(CollectionRun)
                .where(CollectionRun.source_id == source.id)
                .order_by(CollectionRun.started_at.desc(), CollectionRun.id)
                .limit(1)
            )
            collectors.append(
                (source.key, run.status if run else "never_run", run.finished_at if run else None)
            )
        return StatusView(
            paused(session, recipient),
            processing.get("pending", 0),
            processing.get("failed", 0),
            notifications.get("pending", 0) + notifications.get("sending", 0),
            notifications.get("failed", 0),
            tuple(collectors),
        )


def set_paused(engine: Engine, recipient: int, value: bool) -> None:
    with source_lock(engine, f"delivery:{recipient}"), Session(engine) as session, session.begin():
        transaction_lock(session, MAINTENANCE, shared=True)
        state = session.get(RecipientState, str(recipient))
        if state is None:
            session.add(RecipientState(recipient_key=str(recipient), paused=value))
        else:
            state.paused = value


FEEDBACK_VALUES = {
    "i": ("interested", "interested"),
    "n": ("not_relevant", "rejected"),
    "d": ("duplicate", "duplicate"),
    "c": ("closed", "closed"),
}


def feedback(engine: Engine, actor: int, callback_id: str, action: str, lead_id: UUID) -> bool:
    value, state = FEEDBACK_VALUES[action]
    with Session(engine) as session, session.begin():
        transaction_lock(session, MAINTENANCE, shared=True)
        transaction_lock(session, "tlm:feedback")
        if session.get(CallbackReceipt, callback_id) is not None:
            return True
        lead = session.get(Lead, lead_id)
        if lead is None:
            return False
        previous = session.scalar(
            select(Feedback)
            .where(Feedback.lead_id == lead_id, Feedback.recipient_key == str(actor))
            .order_by(Feedback.created_at.desc(), Feedback.id.desc())
            .limit(1)
        )
        if previous is None or previous.value != value or lead.status != state:
            previous = Feedback(
                lead_id=lead_id,
                recipient_key=str(actor),
                value=value,
                created_at=func.clock_timestamp(),
            )
            session.add(previous)
            session.flush()
            lead.status = state
        session.add(CallbackReceipt(callback_id=callback_id, feedback_id=previous.id))
        return True
