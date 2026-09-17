"""Initial foundation schema

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("access_method", sa.String(length=50), nullable=False),
        sa.Column("policy_status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "NOT enabled OR policy_status = 'approved'", name=op.f("ck_sources_enabled_approved")
        ),
        sa.CheckConstraint(
            "policy_status IN ('pending','approved','paused','blocked')",
            name=op.f("ck_sources_policy"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
        sa.UniqueConstraint("key", name=op.f("uq_sources_key")),
    )
    op.create_table(
        "collection_runs",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="running", nullable=False),
        sa.Column("items_seen", sa.Integer(), server_default="0", nullable=False),
        sa.Column("items_inserted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("items_failed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_category", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('running','succeeded','partial','failed')",
            name=op.f("ck_collection_runs_status"),
        ),
        sa.CheckConstraint(
            "items_seen >= 0 AND items_inserted >= 0 AND items_failed >= 0",
            name=op.f("ck_collection_runs_counts"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("fk_collection_runs_source_id_sources")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collection_runs")),
    )
    op.create_index(
        op.f("ix_collection_runs_source_id"), "collection_runs", ["source_id"], unique=False
    )
    op.create_table(
        "collector_states",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("cursor", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("high_water_mark", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("paused_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0", name=op.f("ck_collector_states_failures_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("fk_collector_states_source_id_sources")
        ),
        sa.PrimaryKeyConstraint("source_id", name=op.f("pk_collector_states")),
    )
    op.create_table(
        "raw_items",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("author_label", sa.Text(), nullable=True),
        sa.Column("normalized_text", sa.Text(), nullable=True),
        sa.Column("exact_fingerprint", sa.String(length=64), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "processing_status", sa.String(length=20), server_default="pending", nullable=False
        ),
        sa.Column("processing_error", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "exact_fingerprint IS NULL OR exact_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_raw_items_fingerprint"),
        ),
        sa.CheckConstraint(
            "processing_status IN ('pending','processed','rejected','failed')",
            name=op.f("ck_raw_items_status"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("fk_raw_items_source_id_sources")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_raw_items")),
        sa.UniqueConstraint("source_id", "external_id", name="uq_raw_items_source_external"),
    )
    op.create_index(
        op.f("ix_raw_items_exact_fingerprint"), "raw_items", ["exact_fingerprint"], unique=False
    )
    op.create_index(
        op.f("ix_raw_items_processing_status"), "raw_items", ["processing_status"], unique=False
    )
    op.create_index(op.f("ix_raw_items_published_at"), "raw_items", ["published_at"], unique=False)
    op.create_table(
        "leads",
        sa.Column("canonical_raw_item_id", sa.Uuid(), nullable=False),
        sa.Column("intent", sa.String(length=30), nullable=False),
        sa.Column("subject", sa.String(length=30), nullable=False),
        sa.Column("grade", sa.SmallInteger(), nullable=True),
        sa.Column(
            "goals",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("format", sa.String(length=20), server_default="unknown", nullable=False),
        sa.Column("location_text", sa.Text(), nullable=True),
        sa.Column("budget_text", sa.Text(), nullable=True),
        sa.Column("urgency", sa.String(length=20), server_default="unknown", nullable=False),
        sa.Column(
            "contact_available", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("score", sa.SmallInteger(), nullable=False),
        sa.Column(
            "score_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("classification_version", sa.String(length=100), nullable=False),
        sa.Column("scoring_version", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="new", nullable=False),
        sa.Column("first_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "format IN ('online','offline','either','unknown')", name=op.f("ck_leads_format")
        ),
        sa.CheckConstraint(
            "intent IN ('seeking_tutor','offering_tutoring','school_or_agency_ad',"
            "'teaching_job','informational','uncertain')",
            name=op.f("ck_leads_intent"),
        ),
        sa.CheckConstraint(
            "status IN ('new','notified','interested','rejected','duplicate','closed','expired')",
            name=op.f("ck_leads_status"),
        ),
        sa.CheckConstraint(
            "subject IN ('literature','russian_and_literature',"
            "'russian_language','other','unknown')",
            name=op.f("ck_leads_subject"),
        ),
        sa.CheckConstraint(
            "urgency IN ('low','normal','high','unknown')", name=op.f("ck_leads_urgency")
        ),
        sa.CheckConstraint("grade BETWEEN 1 AND 11", name=op.f("ck_leads_grade")),
        sa.CheckConstraint("score BETWEEN 0 AND 100", name=op.f("ck_leads_score")),
        sa.ForeignKeyConstraint(
            ["canonical_raw_item_id"],
            ["raw_items.id"],
            name=op.f("fk_leads_canonical_raw_item_id_raw_items"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leads")),
        sa.UniqueConstraint("canonical_raw_item_id", name=op.f("uq_leads_canonical_raw_item_id")),
    )
    op.create_index(op.f("ix_leads_last_seen_at"), "leads", ["last_seen_at"], unique=False)
    op.create_index(op.f("ix_leads_status"), "leads", ["status"], unique=False)
    op.create_table(
        "feedback",
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "value IN ('interested','not_relevant','duplicate','closed')",
            name=op.f("ck_feedback_value"),
        ),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], name=op.f("fk_feedback_lead_id_leads")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback")),
    )
    op.create_index(op.f("ix_feedback_lead_id"), "feedback", ["lead_id"], unique=False)
    op.create_table(
        "lead_occurrences",
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("raw_item_id", sa.Uuid(), nullable=False),
        sa.Column("similarity", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("match_method", sa.String(length=20), nullable=False),
        sa.CheckConstraint(
            "match_method IN ('exact_id','exact_url','exact_text','fuzzy_text','manual')",
            name=op.f("ck_lead_occurrences_method"),
        ),
        sa.CheckConstraint(
            "similarity BETWEEN 0 AND 100", name=op.f("ck_lead_occurrences_similarity")
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name=op.f("fk_lead_occurrences_lead_id_leads")
        ),
        sa.ForeignKeyConstraint(
            ["raw_item_id"],
            ["raw_items.id"],
            name=op.f("fk_lead_occurrences_raw_item_id_raw_items"),
        ),
        sa.PrimaryKeyConstraint("lead_id", "raw_item_id", name=op.f("pk_lead_occurrences")),
        sa.UniqueConstraint("raw_item_id", name=op.f("uq_lead_occurrences_raw_item_id")),
    )
    op.create_table(
        "notifications",
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("recipient_key", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("period_key", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(kind = 'immediate' AND lead_id IS NOT NULL AND period_key IS NULL) OR "
            "(kind = 'digest' AND lead_id IS NULL AND period_key IS NOT NULL)",
            name=op.f("ck_notifications_envelope"),
        ),
        sa.CheckConstraint("kind IN ('immediate','digest')", name=op.f("ck_notifications_kind")),
        sa.CheckConstraint(
            "status IN ('pending','sending','sent','failed','skipped')",
            name=op.f("ck_notifications_status"),
        ),
        sa.CheckConstraint("attempt_count >= 0", name=op.f("ck_notifications_attempt_count")),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name=op.f("fk_notifications_lead_id_leads")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
    )
    op.create_index(op.f("ix_notifications_status"), "notifications", ["status"], unique=False)
    op.create_index(
        "uq_notifications_digest",
        "notifications",
        ["recipient_key", "kind", "period_key"],
        unique=True,
        postgresql_where=sa.text("kind = 'digest'"),
    )
    op.create_index(
        "uq_notifications_immediate",
        "notifications",
        ["lead_id", "recipient_key", "kind"],
        unique=True,
        postgresql_where=sa.text("kind = 'immediate'"),
    )
    op.create_table(
        "notification_items",
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name=op.f("fk_notification_items_lead_id_leads")
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name=op.f("fk_notification_items_notification_id_notifications"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("notification_id", "lead_id", name=op.f("pk_notification_items")),
    )


def downgrade() -> None:
    op.drop_table("notification_items")
    op.drop_index(
        "uq_notifications_immediate",
        table_name="notifications",
        postgresql_where=sa.text("kind = 'immediate'"),
    )
    op.drop_index(
        "uq_notifications_digest",
        table_name="notifications",
        postgresql_where=sa.text("kind = 'digest'"),
    )
    op.drop_index(op.f("ix_notifications_status"), table_name="notifications")
    op.drop_table("notifications")
    op.drop_table("lead_occurrences")
    op.drop_index(op.f("ix_feedback_lead_id"), table_name="feedback")
    op.drop_table("feedback")
    op.drop_index(op.f("ix_leads_status"), table_name="leads")
    op.drop_index(op.f("ix_leads_last_seen_at"), table_name="leads")
    op.drop_table("leads")
    op.drop_index(op.f("ix_raw_items_published_at"), table_name="raw_items")
    op.drop_index(op.f("ix_raw_items_processing_status"), table_name="raw_items")
    op.drop_index(op.f("ix_raw_items_exact_fingerprint"), table_name="raw_items")
    op.drop_table("raw_items")
    op.drop_table("collector_states")
    op.drop_index(op.f("ix_collection_runs_source_id"), table_name="collection_runs")
    op.drop_table("collection_runs")
    op.drop_table("sources")
