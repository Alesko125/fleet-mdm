"""Початкова схема Fleet MDM

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.base import JSONType, TZDateTime, UUIDType, enum_column
from app.db.models import (
    ACTOR_TYPE_VALUES,
    AUDIT_RESULT_VALUES,
    COMMAND_STATUS_VALUES,
    COMMAND_TYPE_VALUES,
    DEVICE_STATUS_VALUES,
    MANAGEMENT_MODE_VALUES,
    PERMISSION_STATE_VALUES,
    POLICY_STATE_VALUES,
    ROLE_VALUES,
    SESSION_STATE_VALUES,
    SESSION_TRANSPORT_VALUES,
    TRANSFER_DIRECTION_VALUES,
    TRANSFER_STATUS_VALUES,
)

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---------------------------------------------------------- users ----
    op.create_table(
        "users",
        sa.Column("id", UUIDType, nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("full_name", sa.String(200)),
        sa.Column("role", enum_column(ROLE_VALUES, "role_enum"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("failed_login_count", sa.Integer(), nullable=False),
        sa.Column("locked_until", TZDateTime),
        sa.Column("last_login_at", TZDateTime),
        sa.Column("created_at", TZDateTime, nullable=False),
        sa.Column("updated_at", TZDateTime, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    # email має unique=True + index=True → один унікальний індекс, без окремого UNIQUE.
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # ------------------------------------------------------- policies ----
    op.create_table(
        "policies",
        sa.Column("id", UUIDType, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("payload", JSONType, nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("created_by_id", UUIDType),
        sa.Column("created_at", TZDateTime, nullable=False),
        sa.Column("updated_at", TZDateTime, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_policies"),
        sa.UniqueConstraint("name", name="uq_policies_name"),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"],
            name="fk_policies_created_by_id_users", ondelete="SET NULL",
        ),
    )

    # ---------------------------------------------- enrollment_tokens ----
    op.create_table(
        "enrollment_tokens",
        sa.Column("id", UUIDType, nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_prefix", sa.String(16), nullable=False),
        sa.Column("label", sa.String(200)),
        sa.Column("notes", sa.Text()),
        sa.Column("created_by_id", UUIDType),
        sa.Column("default_policy_id", UUIDType),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("use_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", TZDateTime, nullable=False),
        sa.Column("revoked_at", TZDateTime),
        sa.Column("created_at", TZDateTime, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_enrollment_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_enrollment_tokens_token_hash"),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"],
            name="fk_enrollment_tokens_created_by_id_users", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["default_policy_id"], ["policies.id"],
            name="fk_enrollment_tokens_default_policy_id_policies", ondelete="SET NULL",
        ),
    )

    # -------------------------------------------------------- devices ----
    op.create_table(
        "devices",
        sa.Column("id", UUIDType, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("enrollment_token_id", UUIDType),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("key_algorithm", sa.String(32), nullable=False),
        sa.Column("key_fingerprint", sa.String(64), nullable=False),
        sa.Column("android_id", sa.String(64)),
        sa.Column("serial_number", sa.String(64)),
        sa.Column("status", enum_column(DEVICE_STATUS_VALUES, "device_status_enum"), nullable=False),
        sa.Column(
            "management_mode",
            enum_column(MANAGEMENT_MODE_VALUES, "management_mode_enum"),
            nullable=False,
        ),
        sa.Column("agent_version", sa.String(32)),
        sa.Column("enrolled_at", TZDateTime, nullable=False),
        sa.Column("last_seen_at", TZDateTime),
        sa.Column("last_ip", sa.String(45)),
        sa.Column("notes", sa.Text()),
        sa.Column("deleted_at", TZDateTime),
        sa.Column("created_at", TZDateTime, nullable=False),
        sa.Column("updated_at", TZDateTime, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_devices"),
        sa.UniqueConstraint("key_fingerprint", name="uq_devices_key_fingerprint"),
        sa.UniqueConstraint("android_id", name="uq_devices_android_id"),
        sa.ForeignKeyConstraint(
            ["enrollment_token_id"], ["enrollment_tokens.id"],
            name="fk_devices_enrollment_token_id_enrollment_tokens", ondelete="SET NULL",
        ),
    )
    op.create_index("ix_devices_last_seen_at", "devices", ["last_seen_at"])
    op.create_index("ix_devices_deleted_at", "devices", ["deleted_at"])

    # ------------------------------------------------- refresh_tokens ----
    op.create_table(
        "refresh_tokens",
        sa.Column("id", UUIDType, nullable=False),
        sa.Column("user_id", UUIDType, nullable=False),
        sa.Column("family_id", UUIDType, nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("issued_at", TZDateTime, nullable=False),
        sa.Column("expires_at", TZDateTime, nullable=False),
        sa.Column("revoked_at", TZDateTime),
        sa.Column("revoked_reason", sa.String(64)),
        sa.Column("replaced_by_id", UUIDType),
        sa.Column("ip", sa.String(45)),
        sa.Column("user_agent", sa.String(400)),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_refresh_tokens_user_id_users", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])

    # ----------------------------------------------- device_telemetry ----
    op.create_table(
        "device_telemetry",
        sa.Column("device_id", UUIDType, nullable=False),
        sa.Column("manufacturer", sa.String(100)),
        sa.Column("model", sa.String(100)),
        sa.Column("product", sa.String(100)),
        sa.Column("android_version", sa.String(32)),
        sa.Column("sdk_int", sa.Integer()),
        sa.Column("security_patch", sa.String(32)),
        sa.Column("build_id", sa.String(100)),
        sa.Column("kernel_version", sa.String(255)),
        sa.Column("uptime_seconds", sa.BigInteger()),
        sa.Column("battery_level", sa.Integer()),
        sa.Column("battery_status", sa.String(32)),
        sa.Column("battery_health", sa.String(32)),
        sa.Column("battery_temperature_c", sa.Float()),
        sa.Column("charging", sa.Boolean()),
        sa.Column("ram_total_bytes", sa.BigInteger()),
        sa.Column("ram_available_bytes", sa.BigInteger()),
        sa.Column("storage_total_bytes", sa.BigInteger()),
        sa.Column("storage_free_bytes", sa.BigInteger()),
        sa.Column("network_type", sa.String(32)),
        sa.Column("network_operator", sa.String(64)),
        sa.Column("wifi_ssid", sa.String(64)),
        sa.Column("ip_address", sa.String(45)),
        sa.Column("agent_version", sa.String(32)),
        sa.Column("collected_at", TZDateTime, nullable=False),
        sa.Column("updated_at", TZDateTime, nullable=False),
        sa.Column("extra", JSONType),
        sa.PrimaryKeyConstraint("device_id", name="pk_device_telemetry"),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"],
            name="fk_device_telemetry_device_id_devices", ondelete="CASCADE",
        ),
    )

    # ---------------------------------------------- telemetry_samples ----
    op.create_table(
        "telemetry_samples",
        sa.Column("id", UUIDType, nullable=False),
        sa.Column("device_id", UUIDType, nullable=False),
        sa.Column("collected_at", TZDateTime, nullable=False),
        sa.Column("battery_level", sa.Integer()),
        sa.Column("charging", sa.Boolean()),
        sa.Column("uptime_seconds", sa.BigInteger()),
        sa.Column("ram_available_bytes", sa.BigInteger()),
        sa.Column("storage_free_bytes", sa.BigInteger()),
        sa.Column("network_type", sa.String(32)),
        sa.Column("payload", JSONType),
        sa.PrimaryKeyConstraint("id", name="pk_telemetry_samples"),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"],
            name="fk_telemetry_samples_device_id_devices", ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_telemetry_samples_device_time", "telemetry_samples", ["device_id", "collected_at"]
    )

    # ---------------------------------------------------- device_apps ----
    op.create_table(
        "device_apps",
        sa.Column("id", UUIDType, nullable=False),
        sa.Column("device_id", UUIDType, nullable=False),
        sa.Column("package_name", sa.String(255), nullable=False),
        sa.Column("app_name", sa.String(255)),
        sa.Column("version_name", sa.String(64)),
        sa.Column("version_code", sa.BigInteger()),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("installer_package", sa.String(255)),
        sa.Column("first_installed_at", TZDateTime),
        sa.Column("last_updated_at", TZDateTime),
        sa.Column("last_seen_at", TZDateTime, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_device_apps"),
        sa.UniqueConstraint("device_id", "package_name", name="uq_device_apps_device_package"),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"],
            name="fk_device_apps_device_id_devices", ondelete="CASCADE",
        ),
    )
    op.create_index("ix_device_apps_device_id", "device_apps", ["device_id"])

    # -------------------------------------------------- apk_artifacts ----
    op.create_table(
        "apk_artifacts",
        sa.Column("id", UUIDType, nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("package_name", sa.String(255)),
        sa.Column("version_name", sa.String(64)),
        sa.Column("version_code", sa.BigInteger()),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("signature_digest", sa.String(64)),
        sa.Column("storage_path", sa.String(1024), nullable=False),
        sa.Column("uploaded_by_id", UUIDType),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", TZDateTime, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_apk_artifacts"),
        sa.UniqueConstraint("sha256", name="uq_apk_artifacts_sha256"),
        sa.ForeignKeyConstraint(
            ["uploaded_by_id"], ["users.id"],
            name="fk_apk_artifacts_uploaded_by_id_users", ondelete="SET NULL",
        ),
    )
    op.create_index("ix_apk_artifacts_package_name", "apk_artifacts", ["package_name"])

    # ------------------------------------------------------- commands ----
    op.create_table(
        "commands",
        sa.Column("id", UUIDType, nullable=False),
        sa.Column("device_id", UUIDType, nullable=False),
        sa.Column("type", enum_column(COMMAND_TYPE_VALUES, "command_type_enum"), nullable=False),
        sa.Column("payload", JSONType),
        sa.Column(
            "status", enum_column(COMMAND_STATUS_VALUES, "command_status_enum"), nullable=False
        ),
        sa.Column("result", JSONType),
        sa.Column("error", sa.Text()),
        sa.Column("idempotency_key", sa.String(64)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_by_id", UUIDType),
        sa.Column("created_at", TZDateTime, nullable=False),
        sa.Column("dispatched_at", TZDateTime),
        sa.Column("acked_at", TZDateTime),
        sa.Column("completed_at", TZDateTime),
        sa.Column("expires_at", TZDateTime),
        sa.PrimaryKeyConstraint("id", name="pk_commands"),
        sa.UniqueConstraint("idempotency_key", name="uq_commands_idempotency_key"),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"], name="fk_commands_device_id_devices", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"],
            name="fk_commands_created_by_id_users", ondelete="SET NULL",
        ),
    )
    op.create_index("ix_commands_device_status", "commands", ["device_id", "status"])

    # ----------------------------------------------------- file_roots ----
    op.create_table(
        "file_roots",
        sa.Column("id", UUIDType, nullable=False),
        sa.Column("device_id", UUIDType),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("label", sa.String(200)),
        sa.Column("writable", sa.Boolean(), nullable=False),
        sa.Column("created_by_id", UUIDType),
        sa.Column("created_at", TZDateTime, nullable=False),
        sa.Column("updated_at", TZDateTime, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_file_roots"),
        sa.UniqueConstraint("device_id", "path", name="uq_file_roots_device_path"),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"],
            name="fk_file_roots_device_id_devices", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"],
            name="fk_file_roots_created_by_id_users", ondelete="SET NULL",
        ),
    )
    op.create_index("ix_file_roots_device_id", "file_roots", ["device_id"])

    # ------------------------------------------------- file_transfers ----
    op.create_table(
        "file_transfers",
        sa.Column("id", UUIDType, nullable=False),
        sa.Column("device_id", UUIDType, nullable=False),
        sa.Column("command_id", UUIDType),
        sa.Column(
            "direction",
            enum_column(TRANSFER_DIRECTION_VALUES, "transfer_direction_enum"),
            nullable=False,
        ),
        sa.Column("remote_path", sa.String(1024), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("sha256", sa.String(64)),
        sa.Column("storage_path", sa.String(1024)),
        sa.Column(
            "status", enum_column(TRANSFER_STATUS_VALUES, "transfer_status_enum"), nullable=False
        ),
        sa.Column("error", sa.Text()),
        sa.Column("created_by_id", UUIDType),
        sa.Column("created_at", TZDateTime, nullable=False),
        sa.Column("completed_at", TZDateTime),
        sa.PrimaryKeyConstraint("id", name="pk_file_transfers"),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"],
            name="fk_file_transfers_device_id_devices", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["command_id"], ["commands.id"],
            name="fk_file_transfers_command_id_commands", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"],
            name="fk_file_transfers_created_by_id_users", ondelete="SET NULL",
        ),
    )
    op.create_index("ix_file_transfers_device_id", "file_transfers", ["device_id"])

    # --------------------------------------------- location_settings ----
    op.create_table(
        "location_settings",
        sa.Column("device_id", UUIDType, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("min_distance_meters", sa.Integer(), nullable=False),
        sa.Column("high_accuracy", sa.Boolean(), nullable=False),
        sa.Column(
            "permission_state",
            enum_column(PERMISSION_STATE_VALUES, "permission_state_enum"),
            nullable=False,
        ),
        sa.Column("background_permission", sa.Boolean(), nullable=False),
        sa.Column("consent_recorded_at", TZDateTime),
        sa.Column("updated_by_id", UUIDType),
        sa.Column("updated_at", TZDateTime, nullable=False),
        sa.PrimaryKeyConstraint("device_id", name="pk_location_settings"),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"],
            name="fk_location_settings_device_id_devices", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"], ["users.id"],
            name="fk_location_settings_updated_by_id_users", ondelete="SET NULL",
        ),
    )

    # ------------------------------------------------- location_fixes ----
    op.create_table(
        "location_fixes",
        sa.Column("id", UUIDType, nullable=False),
        sa.Column("device_id", UUIDType, nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("accuracy_m", sa.Float()),
        sa.Column("altitude_m", sa.Float()),
        sa.Column("speed_mps", sa.Float()),
        sa.Column("bearing_deg", sa.Float()),
        sa.Column("provider", sa.String(32)),
        sa.Column("battery_level", sa.Integer()),
        sa.Column("recorded_at", TZDateTime, nullable=False),
        sa.Column("received_at", TZDateTime, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_location_fixes"),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"],
            name="fk_location_fixes_device_id_devices", ondelete="CASCADE",
        ),
    )
    op.create_index("ix_location_fixes_device_time", "location_fixes", ["device_id", "recorded_at"])

    # ------------------------------------------------ remote_sessions ----
    op.create_table(
        "remote_sessions",
        sa.Column("id", UUIDType, nullable=False),
        sa.Column("device_id", UUIDType, nullable=False),
        sa.Column("requested_by_id", UUIDType),
        sa.Column("state", enum_column(SESSION_STATE_VALUES, "session_state_enum"), nullable=False),
        sa.Column(
            "transport",
            enum_column(SESSION_TRANSPORT_VALUES, "session_transport_enum"),
            nullable=False,
        ),
        sa.Column("join_secret_hash", sa.String(64)),
        sa.Column("consent_at", TZDateTime),
        sa.Column("started_at", TZDateTime),
        sa.Column("ended_at", TZDateTime),
        sa.Column("end_reason", sa.String(64)),
        sa.Column("frames_sent", sa.BigInteger(), nullable=False),
        sa.Column("bytes_sent", sa.BigInteger(), nullable=False),
        sa.Column("created_at", TZDateTime, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_remote_sessions"),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"],
            name="fk_remote_sessions_device_id_devices", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_id"], ["users.id"],
            name="fk_remote_sessions_requested_by_id_users", ondelete="SET NULL",
        ),
    )
    op.create_index("ix_remote_sessions_device_id", "remote_sessions", ["device_id"])

    # ------------------------------------------------- device_policies ---
    op.create_table(
        "device_policies",
        sa.Column("id", UUIDType, nullable=False),
        sa.Column("device_id", UUIDType, nullable=False),
        sa.Column("policy_id", UUIDType, nullable=False),
        sa.Column("state", enum_column(POLICY_STATE_VALUES, "policy_state_enum"), nullable=False),
        sa.Column("applied_at", TZDateTime),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", TZDateTime, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_device_policies"),
        sa.UniqueConstraint("device_id", "policy_id", name="uq_device_policies_device_policy"),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"],
            name="fk_device_policies_device_id_devices", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"], ["policies.id"],
            name="fk_device_policies_policy_id_policies", ondelete="CASCADE",
        ),
    )

    # ------------------------------------------------------ audit_log ----
    op.create_table(
        "audit_log",
        sa.Column("id", UUIDType, nullable=False),
        sa.Column("occurred_at", TZDateTime, nullable=False),
        sa.Column("actor_type", enum_column(ACTOR_TYPE_VALUES, "actor_type_enum"), nullable=False),
        sa.Column("actor_user_id", UUIDType),
        sa.Column("actor_device_id", UUIDType),
        sa.Column("actor_label", sa.String(200)),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("object_type", sa.String(64)),
        sa.Column("object_id", sa.String(64)),
        sa.Column("result", enum_column(AUDIT_RESULT_VALUES, "audit_result_enum"), nullable=False),
        sa.Column("ip", sa.String(45)),
        sa.Column("user_agent", sa.String(400)),
        sa.Column("details", JSONType),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"],
            name="fk_audit_log_actor_user_id_users", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["actor_device_id"], ["devices.id"],
            name="fk_audit_log_actor_device_id_devices", ondelete="SET NULL",
        ),
    )
    op.create_index("ix_audit_log_occurred_at", "audit_log", ["occurred_at"])
    op.create_index("ix_audit_log_action_time", "audit_log", ["action", "occurred_at"])
    op.create_index("ix_audit_log_actor_user_id", "audit_log", ["actor_user_id"])
    op.create_index("ix_audit_log_actor_device_id", "audit_log", ["actor_device_id"])


def downgrade() -> None:
    for table in (
        "audit_log",
        "device_policies",
        "remote_sessions",
        "location_fixes",
        "location_settings",
        "file_transfers",
        "file_roots",
        "commands",
        "apk_artifacts",
        "device_apps",
        "telemetry_samples",
        "device_telemetry",
        "refresh_tokens",
        "devices",
        "enrollment_tokens",
        "policies",
        "users",
    ):
        op.drop_table(table)
