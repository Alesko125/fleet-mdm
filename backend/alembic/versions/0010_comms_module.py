"""Комунікаційний модуль: налаштування, SMS та журнал дзвінків (шифровані)

Revision ID: 0010_comms
Revises: 0009_commands
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.base import TZDateTime, UUIDType

revision: str = "0010_comms"
down_revision: str | None = "0009_commands"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERM = ("unknown", "granted", "denied", "not_requested", "unavailable")
DIRECTION = ("inbox", "sent", "draft", "outbox", "incoming", "outgoing", "missed", "other")


def upgrade() -> None:
    op.create_table(
        "comms_settings",
        sa.Column(
            "device_id", UUIDType,
            sa.ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sms_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("call_log_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("metadata_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("enabled_at", TZDateTime),
        sa.Column("enabled_by_id", UUIDType, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("disabled_at", TZDateTime),
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column(
            "sms_permission",
            sa.Enum(*PERM, name="comms_permission_enum", native_enum=False),
            nullable=False, server_default="unknown",
        ),
        sa.Column(
            "call_log_permission",
            sa.Enum(*PERM, name="comms_permission_enum", native_enum=False),
            nullable=False, server_default="unknown",
        ),
        sa.Column("permission_reported_at", TZDateTime),
        sa.Column("last_sync_at", TZDateTime),
        sa.Column("updated_by_id", UUIDType, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("updated_at", TZDateTime, nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "comms_sms",
        sa.Column("id", UUIDType, primary_key=True),
        sa.Column(
            "device_id", UUIDType,
            sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("provider_id", sa.String(64)),
        sa.Column(
            "direction",
            sa.Enum(*DIRECTION, name="comms_direction_enum", native_enum=False),
            nullable=False,
        ),
        sa.Column("address_encrypted", sa.Text()),
        sa.Column("body_encrypted", sa.Text()),
        sa.Column("body_length", sa.Integer()),
        sa.Column("sent_at", TZDateTime),
        sa.Column("collected_at", TZDateTime, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("device_id", "provider_id", name="uq_comms_sms_provider"),
    )
    op.create_index("ix_comms_sms_device_time", "comms_sms", ["device_id", "sent_at"])

    op.create_table(
        "comms_calls",
        sa.Column("id", UUIDType, primary_key=True),
        sa.Column(
            "device_id", UUIDType,
            sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("provider_id", sa.String(64)),
        sa.Column(
            "direction",
            sa.Enum(*DIRECTION, name="comms_direction_enum", native_enum=False),
            nullable=False,
        ),
        sa.Column("number_encrypted", sa.Text()),
        sa.Column("contact_encrypted", sa.Text()),
        sa.Column("duration_seconds", sa.Integer()),
        sa.Column("occurred_at", TZDateTime),
        sa.Column("collected_at", TZDateTime, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("device_id", "provider_id", name="uq_comms_calls_provider"),
    )
    op.create_index("ix_comms_calls_device_time", "comms_calls", ["device_id", "occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_comms_calls_device_time", table_name="comms_calls")
    op.drop_table("comms_calls")
    op.drop_index("ix_comms_sms_device_time", table_name="comms_sms")
    op.drop_table("comms_sms")
    op.drop_table("comms_settings")
