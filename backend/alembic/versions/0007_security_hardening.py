"""Ревокація токенів і ротація облікових даних пристроїв

Revision ID: 0007_security
Revises: 0006_support
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.base import TZDateTime, UUIDType

revision: str = "0007_security"
down_revision: str | None = "0006_support"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    now = sa.func.now()

    # --- користувачі: миттєве відкликання виданих access-токенів ----------
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column(
                "sessions_valid_from", TZDateTime, nullable=False, server_default=now
            )
        )
        batch.add_column(
            sa.Column(
                "password_changed_at", TZDateTime, nullable=False, server_default=now
            )
        )

    # --- пристрої: ротація ключа ------------------------------------------
    with op.batch_alter_table("devices") as batch:
        batch.add_column(
            sa.Column("key_created_at", TZDateTime, nullable=False, server_default=now)
        )
        batch.add_column(sa.Column("key_rotated_at", TZDateTime))
        batch.add_column(
            sa.Column("key_generation", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(
            sa.Column(
                "tokens_valid_from", TZDateTime, nullable=False, server_default=now
            )
        )

    # Для наявних пристроїв вік ключа рахуємо від дати реєстрації.
    op.execute(
        "UPDATE devices SET key_created_at = enrolled_at WHERE enrolled_at IS NOT NULL"
    )

    op.create_table(
        "device_key_history",
        sa.Column("id", UUIDType, primary_key=True),
        sa.Column(
            "device_id",
            UUIDType,
            sa.ForeignKey("devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("key_fingerprint", sa.String(64), nullable=False),
        sa.Column("key_algorithm", sa.String(32), nullable=False, server_default="EC_P256"),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("reason", sa.String(64), nullable=False, server_default="rotation"),
        sa.Column(
            "rotated_by_user_id", UUIDType, sa.ForeignKey("users.id", ondelete="SET NULL")
        ),
        sa.Column("created_at", TZDateTime, nullable=False, server_default=now),
        sa.Column("retired_at", TZDateTime, nullable=False, server_default=now),
    )
    op.create_index(
        "ix_device_key_history_device_id", "device_key_history", ["device_id"]
    )
    op.create_index(
        "ix_device_key_history_key_fingerprint", "device_key_history", ["key_fingerprint"]
    )


def downgrade() -> None:
    op.drop_index("ix_device_key_history_key_fingerprint", table_name="device_key_history")
    op.drop_index("ix_device_key_history_device_id", table_name="device_key_history")
    op.drop_table("device_key_history")

    with op.batch_alter_table("devices") as batch:
        batch.drop_column("tokens_valid_from")
        batch.drop_column("key_generation")
        batch.drop_column("key_rotated_at")
        batch.drop_column("key_created_at")

    with op.batch_alter_table("users") as batch:
        batch.drop_column("password_changed_at")
        batch.drop_column("sessions_valid_from")
