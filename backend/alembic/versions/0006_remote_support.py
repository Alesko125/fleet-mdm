"""Віддалена підтримка: причина, згода, таймаути й статистика сеансу

Revision ID: 0006_support
Revises: 0005_monitoring
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.base import JSONType, TZDateTime

revision: str = "0006_support"
down_revision: str | None = "0005_monitoring"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("remote_sessions") as batch:
        batch.add_column(sa.Column("reason", sa.String(500)))
        batch.add_column(
            sa.Column("input_enabled", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column("input_available", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(sa.Column("expires_at", TZDateTime))
        batch.add_column(sa.Column("last_frame_at", TZDateTime))
        batch.add_column(sa.Column("stats", JSONType))

    op.create_index("ix_remote_sessions_state", "remote_sessions", ["state"])


def downgrade() -> None:
    op.drop_index("ix_remote_sessions_state", table_name="remote_sessions")

    with op.batch_alter_table("remote_sessions") as batch:
        for column in (
            "stats",
            "last_frame_at",
            "expires_at",
            "input_available",
            "input_enabled",
            "reason",
        ):
            batch.drop_column(column)
