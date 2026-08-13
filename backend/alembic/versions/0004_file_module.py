"""Файловий модуль: ліміти каталогів і відновлювані передачі

Revision ID: 0004_files
Revises: 0003_location
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.base import TZDateTime, UUIDType

revision: str = "0004_files"
down_revision: str | None = "0003_location"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
# Перелік типів команд розширено значенням FILE_MKDIR: у нас enum реалізовано
# як VARCHAR + CHECK, тож обмеження треба перестворити.
COMMAND_TYPES = (
    "REFRESH_TELEMETRY", "SYNC_CONFIG", "REBOOT",
    "APP_LIST", "APP_INSTALL", "APP_UPDATE", "APP_UNINSTALL", "APP_LAUNCH",
    "FILE_LIST", "FILE_UPLOAD", "FILE_DOWNLOAD", "FILE_RENAME", "FILE_DELETE",
    "FILE_MKDIR",
    "LOCATION_CONFIG", "LOCATION_REQUEST",
    "POLICY_APPLY", "KIOSK_SET",
    "REMOTE_SUPPORT_REQUEST", "REMOTE_SUPPORT_END",
)



CHECK_NAME = "ck_commands_command_type_enum"


def _recreate_command_type_check(values: tuple[str, ...]) -> None:
    """Оновлює CHECK-обмеження переліку типів команд.

    SQLite не рефлектує CHECK-обмеження, тож там перестворення пропускаємо:
    тестова база будується напряму з моделей і вже містить актуальний перелік.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    quoted = ", ".join(f"'{value}'" for value in values)
    op.drop_constraint(CHECK_NAME, "commands", type_="check")
    op.create_check_constraint(CHECK_NAME, "commands", f"type IN ({quoted})")


def upgrade() -> None:
    with op.batch_alter_table("file_roots") as batch:
        batch.add_column(sa.Column("max_file_size_mb", sa.Integer()))
        batch.add_column(
            sa.Column(
                "allow_privileged", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )

    with op.batch_alter_table("file_transfers") as batch:
        batch.add_column(
            sa.Column("received_bytes", sa.BigInteger(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("chunk_size", sa.Integer(), nullable=False, server_default="1048576")
        )
        batch.add_column(
            sa.Column(
                "checksum_verified", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch.add_column(sa.Column("expires_at", TZDateTime))
        batch.add_column(sa.Column("root_id", UUIDType))
        batch.create_foreign_key(
            "fk_file_transfers_root_id_file_roots",
            "file_roots",
            ["root_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_index("ix_file_transfers_status", "file_transfers", ["status"])
    _recreate_command_type_check(COMMAND_TYPES)


def downgrade() -> None:
    _recreate_command_type_check(tuple(v for v in COMMAND_TYPES if v != "FILE_MKDIR"))
    op.drop_index("ix_file_transfers_status", table_name="file_transfers")

    with op.batch_alter_table("file_transfers") as batch:
        batch.drop_constraint("fk_file_transfers_root_id_file_roots", type_="foreignkey")
        for column in (
            "root_id",
            "expires_at",
            "checksum_verified",
            "chunk_size",
            "received_bytes",
        ):
            batch.drop_column(column)

    with op.batch_alter_table("file_roots") as batch:
        batch.drop_column("allow_privileged")
        batch.drop_column("max_file_size_mb")

