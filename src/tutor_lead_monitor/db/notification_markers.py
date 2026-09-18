import hashlib
from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import String, cast, select, union
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from tutor_lead_monitor.db.models import Lead, Notification, NotificationItem, NotificationMarker


def recipient_hash(recipient: str) -> str:
    return hashlib.sha256(f"tlm:recipient:{recipient}".encode()).hexdigest()


def period_hash(period: str) -> str:
    return hashlib.sha256(f"tlm:digest_period:{period}".encode()).hexdigest()


def marked_lead(recipient: str, kind: str) -> ColumnElement[bool]:
    return (
        select(NotificationMarker.scope_key)
        .where(
            NotificationMarker.recipient_hash == recipient_hash(recipient),
            NotificationMarker.kind == kind,
            NotificationMarker.scope_key == cast(Lead.id, String),
        )
        .exists()
    )


def marked_period(session: Session, recipient: str, period: str) -> bool:
    return (
        session.get(
            NotificationMarker, (recipient_hash(recipient), "digest_period", period_hash(period))
        )
        is not None
    )


def preserve_expired_keys(session: Session, ids: Iterable[UUID]) -> None:
    """Copy only recipient scope, kind and opaque lead/period key before payload deletion."""
    expired = tuple(ids)
    keys = union(
        select(
            Notification.recipient_key, Notification.kind, cast(Notification.lead_id, String)
        ).where(Notification.id.in_(expired), Notification.kind == "immediate"),
        select(
            Notification.recipient_key, Notification.kind, cast(NotificationItem.lead_id, String)
        )
        .join(NotificationItem, NotificationItem.notification_id == Notification.id)
        .where(Notification.id.in_(expired), Notification.kind == "digest"),
    )
    for recipient, kind, scope in session.execute(keys):
        session.execute(
            insert(NotificationMarker)
            .values(recipient_hash=recipient_hash(recipient), kind=kind, scope_key=scope)
            .on_conflict_do_nothing()
        )
    for recipient, period in session.execute(
        select(Notification.recipient_key, Notification.period_key).where(
            Notification.id.in_(expired), Notification.kind == "digest"
        )
    ):
        session.execute(
            insert(NotificationMarker)
            .values(
                recipient_hash=recipient_hash(recipient),
                kind="digest_period",
                scope_key=period_hash(period),
            )
            .on_conflict_do_nothing()
        )
