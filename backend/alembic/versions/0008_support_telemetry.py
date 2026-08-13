"""Віддалена підтримка: роздільність кадру, лічильники керування й перегляду

Revision ID: 0008_support_ext
Revises: 0007_security
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.base import TZDateTime

revision: str = "0008_support_ext"
down_revision: str | None = "0007_security"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("remote_sessions") as batch:
        # Роздільність кадру — щоб панель могла перерахувати клік у пікселі.
        batch.add_column(sa.Column("frame_width", sa.Integer()))
        batch.add_column(sa.Column("frame_height", sa.Integer()))
        # Лічильники для звіту й аудиту.
        batch.add_column(
            sa.Column("input_events", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("viewer_joins", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("last_viewer_at", TZDateTime))


def downgrade() -> None:
    with op.batch_alter_table("remote_sessions") as batch:
        batch.drop_column("last_viewer_at")
        batch.drop_column("viewer_joins")
        batch.drop_column("input_events")
        batch.drop_column("frame_height")
        batch.drop_column("frame_width")
