"""persist delivery chunks recipient pause and callback receipts

Revision ID: 7d8905a1ece6
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7d8905a1ece6"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recipient_states",
        sa.Column("recipient_key", sa.String(length=100), nullable=False),
        sa.Column("paused", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
        sa.PrimaryKeyConstraint("recipient_key", name=op.f("pk_recipient_states")),
    )
    op.create_table(
        "callback_receipts",
        sa.Column("callback_id", sa.String(length=128), nullable=False),
        sa.Column("feedback_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["feedback_id"], ["feedback.id"], name=op.f("fk_callback_receipts_feedback_id_feedback")
        ),
        sa.PrimaryKeyConstraint("callback_id", name=op.f("pk_callback_receipts")),
    )
    op.create_table(
        "notification_chunks",
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "buttons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=30), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','sending','sent','failed','ambiguous')",
            name=op.f("ck_notification_chunks_status"),
        ),
        sa.CheckConstraint(
            "position >= 0 AND attempt_count >= 0", name=op.f("ck_notification_chunks_counts")
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name=op.f("fk_notification_chunks_notification_id_notifications"),
        ),
        sa.PrimaryKeyConstraint("notification_id", "position", name=op.f("pk_notification_chunks")),
    )


def downgrade() -> None:
    op.drop_table("notification_chunks")
    op.drop_table("callback_receipts")
    op.drop_table("recipient_states")
