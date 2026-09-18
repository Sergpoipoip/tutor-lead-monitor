"""add payload free notification expiry markers

Revision ID: b35778628004
Revises: 7d8905a1ece6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b35778628004"
down_revision: str | None = "7d8905a1ece6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_markers",
        sa.Column("recipient_hash", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("scope_key", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "(kind = 'digest_period' AND scope_key ~ '^[0-9a-f]{64}$') OR "
            "(kind IN ('immediate','digest') AND scope_key ~ "
            "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')",
            name=op.f("ck_notification_markers_scope_key"),
        ),
        sa.CheckConstraint(
            "kind IN ('immediate','digest','digest_period')",
            name=op.f("ck_notification_markers_kind"),
        ),
        sa.CheckConstraint(
            "recipient_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_notification_markers_recipient_hash")
        ),
        sa.PrimaryKeyConstraint(
            "recipient_hash", "kind", "scope_key", name=op.f("pk_notification_markers")
        ),
    )


def downgrade() -> None:
    op.drop_table("notification_markers")
