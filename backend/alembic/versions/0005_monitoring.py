"""Моніторинг: показники CPU, правила алертів та історія сповіщень

Revision ID: 0005_monitoring
Revises: 0004_files
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.base import JSONType, TZDateTime, UUIDType, enum_column
from app.db.models import (
    ALERT_RULE_TYPE_VALUES,
    ALERT_SEVERITY_VALUES,
    ALERT_STATE_VALUES,
)

revision: str = "0005_monitoring"
down_revision: str | None = "0004_files"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- показники CPU у телеметрії --------------------------------------
    with op.batch_alter_table("device_telemetry") as batch:
        batch.add_column(sa.Column("cpu_cores", sa.Integer()))
        batch.add_column(sa.Column("cpu_load_percent", sa.Float()))
        batch.add_column(sa.Column("cpu_abi", sa.String(64)))

    with op.batch_alter_table("telemetry_samples") as batch:
        batch.add_column(sa.Column("cpu_load_percent", sa.Float()))

    # --- правила -----------------------------------------------------------
    op.create_table(
        "alert_rules",
        sa.Column("id", UUIDType, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column(
            "type", enum_column(ALERT_RULE_TYPE_VALUES, "alert_rule_type_enum"), nullable=False
        ),
        sa.Column(
            "severity", enum_column(ALERT_SEVERITY_VALUES, "alert_severity_enum"), nullable=False
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("thresholds", JSONType, nullable=False),
        sa.Column("device_id", UUIDType),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("created_by_id", UUIDType),
        sa.Column("created_at", TZDateTime, nullable=False),
        sa.Column("updated_at", TZDateTime, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_alert_rules"),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"],
            name="fk_alert_rules_device_id_devices", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"],
            name="fk_alert_rules_created_by_id_users", ondelete="SET NULL",
        ),
    )
    op.create_index("ix_alert_rules_device_id", "alert_rules", ["device_id"])

    # --- історія сповіщень -------------------------------------------------
    op.create_table(
        "alerts",
        sa.Column("id", UUIDType, nullable=False),
        sa.Column("rule_id", UUIDType),
        sa.Column("device_id", UUIDType, nullable=False),
        sa.Column(
            "type", enum_column(ALERT_RULE_TYPE_VALUES, "alert_rule_type_enum"), nullable=False
        ),
        sa.Column(
            "severity", enum_column(ALERT_SEVERITY_VALUES, "alert_severity_enum"), nullable=False
        ),
        sa.Column("state", enum_column(ALERT_STATE_VALUES, "alert_state_enum"), nullable=False),
        sa.Column("dedup_key", sa.String(255), nullable=False),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("details", JSONType),
        sa.Column("first_seen_at", TZDateTime, nullable=False),
        sa.Column("last_seen_at", TZDateTime, nullable=False),
        sa.Column("acknowledged_at", TZDateTime),
        sa.Column("acknowledged_by_id", UUIDType),
        sa.Column("resolved_at", TZDateTime),
        sa.PrimaryKeyConstraint("id", name="pk_alerts"),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["alert_rules.id"],
            name="fk_alerts_rule_id_alert_rules", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"], name="fk_alerts_device_id_devices", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["acknowledged_by_id"], ["users.id"],
            name="fk_alerts_acknowledged_by_id_users", ondelete="SET NULL",
        ),
    )
    op.create_index("ix_alerts_device_state", "alerts", ["device_id", "state"])
    op.create_index("ix_alerts_first_seen", "alerts", ["first_seen_at"])


def downgrade() -> None:
    op.drop_index("ix_alerts_first_seen", table_name="alerts")
    op.drop_index("ix_alerts_device_state", table_name="alerts")
    op.drop_table("alerts")

    op.drop_index("ix_alert_rules_device_id", table_name="alert_rules")
    op.drop_table("alert_rules")

    with op.batch_alter_table("telemetry_samples") as batch:
        batch.drop_column("cpu_load_percent")

    with op.batch_alter_table("device_telemetry") as batch:
        batch.drop_column("cpu_abi")
        batch.drop_column("cpu_load_percent")
        batch.drop_column("cpu_cores")
