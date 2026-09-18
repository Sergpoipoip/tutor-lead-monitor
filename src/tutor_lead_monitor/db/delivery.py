"""ORM boundary for delivery snapshots and short transactions. Never performs network I/O."""

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session

from tutor_lead_monitor.config import AppConfig
from tutor_lead_monitor.db.locks import MAINTENANCE, transaction_lock
from tutor_lead_monitor.db.models import (
    Lead,
    Notification,
    NotificationChunk,
    NotificationItem,
    RawItem,
    RecipientState,
    Source,
)
from tutor_lead_monitor.db.notification_markers import marked_lead, marked_period
from tutor_lead_monitor.domain.models import JSONValue
from tutor_lead_monitor.notifications.base import (
    Button,
    DeliveryError,
    FailureKind,
    LeadView,
    Message,
)
from tutor_lead_monitor.notifications.digest import day_bounds
from tutor_lead_monitor.notifications.formatting import alert, digest_messages

ACTIVE = ("new", "notified", "interested")
MAX_ATTEMPTS = 3


def view(lead: Lead, raw: RawItem, source: Source) -> LeadView:
    return LeadView(
        lead.id,
        lead.score,
        tuple(str(r["explanation"]) for r in lead.score_reasons if "explanation" in r),
        lead.subject,
        lead.grade,
        tuple(lead.goals),
        lead.format,
        lead.location_text,
        lead.urgency,
        lead.budget_text,
        source.display_name,
        raw.published_at,
        raw.normalized_text or raw.text,
        raw.canonical_url or raw.url,
    )


def paused(session: Session, recipient: int) -> bool:
    state = session.get(RecipientState, str(recipient))
    return state.paused if state else False


def add_chunks(session: Session, notification: Notification, messages: tuple[Message, ...]) -> None:
    session.flush()
    for position, message in enumerate(messages):
        buttons: list[dict[str, JSONValue]] = [
            {"label": b.label, "data": b.data} for b in message.buttons
        ]
        session.add(
            NotificationChunk(
                notification_id=notification.id,
                position=position,
                text=message.html,
                buttons=buttons,
            )
        )


def reserve_immediate(
    engine: Engine, recipient: int, config: AppConfig, now: datetime, limit: int
) -> tuple[UUID, ...]:
    with Session(engine) as session, session.begin():
        transaction_lock(session, MAINTENANCE, shared=True)
        if paused(session, recipient):
            return ()
        # Existing failed/pending envelopes retain their original identity and snapshot.
        existing = list(
            session.scalars(
                select(Notification.id)
                .where(
                    Notification.recipient_key == str(recipient),
                    Notification.kind == "immediate",
                    Notification.status.in_(["pending", "sending", "failed"]),
                )
                .order_by(Notification.created_at, Notification.id)
            )
        )
        rows = session.execute(
            select(Lead, RawItem, Source)
            .join(RawItem, Lead.canonical_raw_item_id == RawItem.id)
            .join(Source, RawItem.source_id == Source.id)
            .where(
                Lead.score >= config.scoring.thresholds.immediate,
                Lead.status.in_(ACTIVE),
                ~marked_lead(str(recipient), "immediate"),
                ~select(Notification.id)
                .where(
                    Notification.lead_id == Lead.id,
                    Notification.recipient_key == str(recipient),
                    Notification.kind == "immediate",
                )
                .exists(),
            )
            .order_by(Lead.score.desc(), Lead.last_seen_at.desc(), Lead.id)
            .limit(limit)
        ).all()
        for lead, raw, source in rows:
            notification = Notification(
                lead_id=lead.id, recipient_key=str(recipient), kind="immediate"
            )
            session.add(notification)
            add_chunks(session, notification, (alert(view(lead, raw, source), now),))
            existing.append(notification.id)
        return tuple(existing)


def reserve_digest(
    engine: Engine, recipient: int, config: AppConfig, day: date, now: datetime, explicit: bool
) -> UUID | None:
    start, end = day_bounds(day, config.business.timezone)
    with Session(engine) as session, session.begin():
        transaction_lock(session, MAINTENANCE, shared=True)
        if paused(session, recipient) and not explicit:
            return None
        existing = session.scalar(
            select(Notification).where(
                Notification.recipient_key == str(recipient),
                Notification.kind == "digest",
                Notification.period_key == day.isoformat(),
            )
        )
        if existing:
            return existing.id
        if marked_period(session, str(recipient), day.isoformat()):
            return None
        rows = session.execute(
            select(Lead, RawItem, Source)
            .join(RawItem, Lead.canonical_raw_item_id == RawItem.id)
            .join(Source, RawItem.source_id == Source.id)
            .where(
                Lead.score >= config.scoring.thresholds.digest,
                Lead.status.in_(ACTIVE),
                ~select(NotificationItem.lead_id)
                .join(Notification, NotificationItem.notification_id == Notification.id)
                .where(
                    NotificationItem.lead_id == Lead.id,
                    Notification.recipient_key == str(recipient),
                    Notification.kind == "digest",
                )
                .exists(),
                ~marked_lead(str(recipient), "digest"),
            )
            .order_by(
                Lead.score.desc(),
                func.coalesce(RawItem.published_at, RawItem.collected_at).desc(),
                Lead.id,
            )
        ).all()
        if not rows and not config.business.send_empty_digest:
            return None
        raw_count = (
            session.scalar(
                select(func.count())
                .select_from(RawItem)
                .where(RawItem.collected_at >= start, RawItem.collected_at < end)
            )
            or 0
        )
        rejected = (
            session.scalar(
                select(func.count())
                .select_from(RawItem)
                .where(
                    RawItem.collected_at >= start,
                    RawItem.collected_at < end,
                    RawItem.processing_status == "rejected",
                )
            )
            or 0
        )
        scores = list(
            session.scalars(
                select(Lead.score).where(Lead.created_at >= start, Lead.created_at < end)
            )
        )
        t = config.scoring.thresholds
        bands = (
            sum(s >= t.immediate for s in scores),
            sum(t.digest <= s < t.immediate for s in scores),
            sum(t.review <= s < t.digest for s in scores),
        )
        header = (
            f"Digest {day} · {config.business.timezone}\n"
            f"Statistics: local calendar day {day} (snapshot)\n"
            f"Collected: {raw_count}; rejected: {rejected}; new leads: {len(scores)}\n"
            f"Immediate: {bands[0]}; digest: {bands[1]}; review: {bands[2]}; eligible: {len(rows)}"
        )
        notification = Notification(
            recipient_key=str(recipient), kind="digest", period_key=day.isoformat()
        )
        session.add(notification)
        messages = digest_messages(header, tuple(view(*row) for row in rows), now)
        add_chunks(session, notification, messages)
        for lead, _, _ in rows:
            session.add(NotificationItem(notification_id=notification.id, lead_id=lead.id))
        return notification.id


@dataclass(frozen=True, repr=False)
class Attempt:
    notification_id: UUID
    position: int
    message: Message


def claim_chunk(
    engine: Engine, recipient: int, notification_id: UUID, now: datetime, explicit: bool
) -> Attempt | None:
    with Session(engine) as session, session.begin():
        transaction_lock(session, MAINTENANCE, shared=True)
        envelope = session.get(Notification, notification_id)
        if envelope is None or envelope.status in {"sent", "skipped"}:
            return None
        if paused(session, recipient) and not explicit:
            return None
        if envelope.lead_id is not None:
            lead = session.get(Lead, envelope.lead_id)
            if lead is None or lead.status not in ACTIVE:
                envelope.status = "skipped"
                return None
        chunk = session.scalar(
            select(NotificationChunk)
            .where(
                NotificationChunk.notification_id == notification_id,
                NotificationChunk.status != "sent",
            )
            .order_by(NotificationChunk.position)
            .limit(1)
        )
        if chunk is None:
            count = session.scalar(
                select(func.count())
                .select_from(NotificationChunk)
                .where(NotificationChunk.notification_id == notification_id)
            )
            if count:
                envelope.status, envelope.sent_at = "sent", now
            else:
                envelope.status, envelope.last_error = "failed", "missing_snapshot"
            return None
        # The recipient session lock proves no live cooperating sender owns this attempt.
        if chunk.status == "sending":
            chunk.status, chunk.last_error = "ambiguous", "ambiguous"
            envelope.status, envelope.last_error = "failed", "ambiguous"
            return None
        if chunk.status == "ambiguous" or chunk.attempt_count >= MAX_ATTEMPTS:
            return None
        if chunk.status == "failed" and chunk.last_error not in {"retryable", "rate_limited"}:
            return None
        if chunk.next_attempt_at is not None and chunk.next_attempt_at > now:
            return None
        chunk.status = envelope.status = "sending"
        chunk.attempt_count += 1
        envelope.attempt_count += 1
        message = Message(
            chunk.text, tuple(Button(str(b["label"]), str(b["data"])) for b in chunk.buttons)
        )
        return Attempt(notification_id, chunk.position, message)


def finish_chunk(
    engine: Engine,
    attempt: Attempt,
    now: datetime,
    message_id: str | None = None,
    error: DeliveryError | None = None,
) -> None:
    with Session(engine) as session, session.begin():
        transaction_lock(session, MAINTENANCE, shared=True)
        chunk = session.get(NotificationChunk, (attempt.notification_id, attempt.position))
        envelope = session.get(Notification, attempt.notification_id)
        assert chunk is not None and envelope is not None
        if error:
            chunk.status = "ambiguous" if error.kind == FailureKind.AMBIGUOUS else "failed"
            chunk.last_error = envelope.last_error = error.kind.value
            envelope.status = "failed"
            if error.kind in {FailureKind.RETRYABLE, FailureKind.RATE_LIMITED}:
                delay = max(
                    error.retry_after, min(300, 2**chunk.attempt_count) + random.uniform(0, 1)
                )
                chunk.next_attempt_at = now + timedelta(seconds=delay)
            return
        chunk.status, chunk.provider_message_id, chunk.sent_at = "sent", message_id, now
        chunk.last_error, chunk.next_attempt_at = None, None
        envelope.provider_message_id = message_id
        envelope.last_error = None
        session.flush()
        remaining = session.scalar(
            select(func.count())
            .select_from(NotificationChunk)
            .where(
                NotificationChunk.notification_id == envelope.id, NotificationChunk.status != "sent"
            )
        )
        if not remaining:
            envelope.status, envelope.sent_at = "sent", now
            if envelope.lead_id:
                session.execute(
                    update(Lead)
                    .where(Lead.id == envelope.lead_id, Lead.status == "new")
                    .values(status="notified")
                )


def notification_status(engine: Engine, notification_id: UUID) -> str:
    with Session(engine) as session:
        return (
            session.scalar(select(Notification.status).where(Notification.id == notification_id))
            or "missing"
        )
