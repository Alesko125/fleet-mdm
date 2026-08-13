"""Джерело координати, ознака mock-локації та стан звітування

Revision ID: 0003_location
Revises: 0002_apk_module
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.base import TZDateTime, enum_column
from app.db.models import LOCATION_SOURCE_VALUES

revision: str = "0003_location"
down_revision: str | None = "0002_apk_module"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("location_fixes") as batch:
        batch.add_column(
            sa.Column(
                "source",
                enum_column(LOCATION_SOURCE_VALUES, "location_source_enum"),
                nullable=False,
                server_default="periodic",
            )
        )
        batch.add_column(
            sa.Column("is_mock", sa.Boolean(), nullable=False, server_default=sa.false())
        )

    with op.batch_alter_table("location_settings") as batch:
        batch.add_column(sa.Column("permission_reported_at", TZDateTime))
        batch.add_column(sa.Column("last_fix_at", TZDateTime))

    # Ретеншн видаляє за received_at — окремий індекс пришвидшує прибирання.
    op.create_index("ix_location_fixes_received_at", "location_fixes", ["received_at"])


def downgrade() -> None:
    op.drop_index("ix_location_fixes_received_at", table_name="location_fixes")

    with op.batch_alter_table("location_settings") as batch:
        batch.drop_column("last_fix_at")
        batch.drop_column("permission_reported_at")

    with op.batch_alter_table("location_fixes") as batch:
        batch.drop_column("is_mock")
        batch.drop_column("source")
