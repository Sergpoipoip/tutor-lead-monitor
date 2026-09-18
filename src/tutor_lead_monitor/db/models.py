from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tutor_lead_monitor.db.base import Base, Timestamps, UUIDPrimaryKey
from tutor_lead_monitor.domain.models import JSONValue


class Source(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(
            "policy_status IN ('pending','approved','paused','blocked')", name="policy"
        ),
        CheckConstraint("NOT enabled OR policy_status = 'approved'", name="enabled_approved"),
    )

    key: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(50))
    display_name: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("false"))
    config: Mapped[dict[str, JSONValue]] = mapped_column(
        JSONB, server_default=sql_text("'{}'::jsonb")
    )
    access_method: Mapped[str] = mapped_column(String(50))
    policy_status: Mapped[str] = mapped_column(String(20), server_default="pending")
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CollectorState(Timestamps, Base):
    __tablename__ = "collector_states"
    __table_args__ = (CheckConstraint("consecutive_failures >= 0", name="failures_nonnegative"),)

    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    cursor: Mapped[dict[str, JSONValue] | None] = mapped_column(JSONB)
    high_water_mark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default="0")
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CollectionRun(UUIDPrimaryKey, Base):
    __tablename__ = "collection_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running','succeeded','partial','failed')", name="status"),
        CheckConstraint(
            "items_seen >= 0 AND items_inserted >= 0 AND items_failed >= 0", name="counts"
        ),
    )

    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), server_default="running")
    items_seen: Mapped[int] = mapped_column(Integer, server_default="0")
    items_inserted: Mapped[int] = mapped_column(Integer, server_default="0")
    items_failed: Mapped[int] = mapped_column(Integer, server_default="0")
    error_category: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)


class RawItem(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "raw_items"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_raw_items_source_external"),
        CheckConstraint(
            "processing_status IN ('pending','processed','rejected','failed')", name="status"
        ),
        CheckConstraint(
            "exact_fingerprint IS NULL OR exact_fingerprint ~ '^[0-9a-f]{64}$'", name="fingerprint"
        ),
    )

    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"))
    external_id: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    text: Mapped[str] = mapped_column(Text)
    author_label: Mapped[str | None] = mapped_column(Text)
    normalized_text: Mapped[str | None] = mapped_column(Text)
    # Computed by Milestone 2 normalization, not by collectors.
    exact_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    metadata_: Mapped[dict[str, JSONValue]] = mapped_column(
        "metadata", JSONB, server_default=sql_text("'{}'::jsonb")
    )
    processing_status: Mapped[str] = mapped_column(String(20), server_default="pending", index=True)
    processing_error: Mapped[str | None] = mapped_column(Text)


class Lead(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "leads"
    __table_args__ = (
        CheckConstraint("grade BETWEEN 1 AND 11", name="grade"),
        CheckConstraint("score BETWEEN 0 AND 100", name="score"),
        CheckConstraint(
            "intent IN ('seeking_tutor','offering_tutoring','school_or_agency_ad',"
            "'teaching_job','informational','uncertain')",
            name="intent",
        ),
        CheckConstraint(
            "subject IN ('literature','russian_and_literature',"
            "'russian_language','other','unknown')",
            name="subject",
        ),
        CheckConstraint("format IN ('online','offline','either','unknown')", name="format"),
        CheckConstraint("urgency IN ('low','normal','high','unknown')", name="urgency"),
        CheckConstraint(
            "status IN ('new','notified','interested','rejected','duplicate','closed','expired')",
            name="status",
        ),
    )

    canonical_raw_item_id: Mapped[UUID] = mapped_column(ForeignKey("raw_items.id"), unique=True)
    intent: Mapped[str] = mapped_column(String(30))
    subject: Mapped[str] = mapped_column(String(30))
    grade: Mapped[int | None] = mapped_column(SmallInteger)
    goals: Mapped[list[str]] = mapped_column(JSONB, server_default=sql_text("'[]'::jsonb"))
    format: Mapped[str] = mapped_column(String(20), server_default="unknown")
    location_text: Mapped[str | None] = mapped_column(Text)
    budget_text: Mapped[str | None] = mapped_column(Text)
    urgency: Mapped[str] = mapped_column(String(20), server_default="unknown")
    contact_available: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("false"))
    score: Mapped[int] = mapped_column(SmallInteger)
    score_reasons: Mapped[list[dict[str, JSONValue]]] = mapped_column(
        JSONB, server_default=sql_text("'[]'::jsonb")
    )
    classification_version: Mapped[str] = mapped_column(String(100))
    scoring_version: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), server_default="new", index=True)
    first_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class LeadOccurrence(Base):
    __tablename__ = "lead_occurrences"
    __table_args__ = (
        CheckConstraint("similarity BETWEEN 0 AND 100", name="similarity"),
        CheckConstraint(
            "match_method IN ('exact_id','exact_url','exact_text','fuzzy_text','manual')",
            name="method",
        ),
    )

    lead_id: Mapped[UUID] = mapped_column(ForeignKey("leads.id"), primary_key=True)
    raw_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("raw_items.id"), primary_key=True, unique=True
    )
    similarity: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    match_method: Mapped[str] = mapped_column(String(20))


class Notification(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint("kind IN ('immediate','digest')", name="kind"),
        CheckConstraint("status IN ('pending','sending','sent','failed','skipped')", name="status"),
        CheckConstraint("attempt_count >= 0", name="attempt_count"),
        CheckConstraint(
            "(kind = 'immediate' AND lead_id IS NOT NULL AND period_key IS NULL) OR "
            "(kind = 'digest' AND lead_id IS NULL AND period_key IS NOT NULL)",
            name="envelope",
        ),
        Index(
            "uq_notifications_immediate",
            "lead_id",
            "recipient_key",
            "kind",
            unique=True,
            postgresql_where=sql_text("kind = 'immediate'"),
        ),
        Index(
            "uq_notifications_digest",
            "recipient_key",
            "kind",
            "period_key",
            unique=True,
            postgresql_where=sql_text("kind = 'digest'"),
        ),
    )

    lead_id: Mapped[UUID | None] = mapped_column(ForeignKey("leads.id"))
    recipient_key: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20))
    period_key: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), server_default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0")
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationItem(Base):
    __tablename__ = "notification_items"

    notification_id: Mapped[UUID] = mapped_column(
        ForeignKey("notifications.id", ondelete="CASCADE"), primary_key=True
    )
    lead_id: Mapped[UUID] = mapped_column(ForeignKey("leads.id"), primary_key=True)


class Feedback(UUIDPrimaryKey, Base):
    __tablename__ = "feedback"
    __table_args__ = (
        CheckConstraint(
            "value IN ('interested','not_relevant','duplicate','closed')", name="value"
        ),
    )

    lead_id: Mapped[UUID] = mapped_column(ForeignKey("leads.id"), index=True)
    recipient_key: Mapped[str] = mapped_column(String(100))
    value: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("now()")
    )
    metadata_: Mapped[dict[str, JSONValue]] = mapped_column(
        "metadata", JSONB, server_default=sql_text("'{}'::jsonb")
    )


class RecipientState(Timestamps, Base):
    __tablename__ = "recipient_states"

    recipient_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    paused: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("false"))


class NotificationChunk(Base):
    __tablename__ = "notification_chunks"
    __table_args__ = (
        CheckConstraint("position >= 0 AND attempt_count >= 0", name="counts"),
        CheckConstraint(
            "status IN ('pending','sending','sent','failed','ambiguous')", name="status"
        ),
    )

    notification_id: Mapped[UUID] = mapped_column(ForeignKey("notifications.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    buttons: Mapped[list[dict[str, JSONValue]]] = mapped_column(
        JSONB, server_default=sql_text("'[]'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0")
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(30))


class CallbackReceipt(Base):
    __tablename__ = "callback_receipts"

    callback_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    feedback_id: Mapped[UUID] = mapped_column(ForeignKey("feedback.id"))
