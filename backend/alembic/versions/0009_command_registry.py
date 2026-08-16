"""Реєстр команд: тривалість виконання та конфігуровані дозволи

Revision ID: 0009_commands
Revises: 0008_support_ext
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.base import TZDateTime, UUIDType

revision: str = "0009_commands"
down_revision: str | None = "0008_support_ext"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Нові типи команд. Перелік зберігається як VARCHAR + CHECK (native_enum=False),
# тож розширення зводиться до заміни обмеження.
NEW_TYPES = (
    "REFRESH_TELEMETRY", "SYNC_CONFIG", "REBOOT",
    "AGENT_RESTART", "COLLECT_DIAGNOSTICS",
    "APP_LIST", "APP_INSTALL", "APP_UPDATE", "APP_UNINSTALL", "APP_LAUNCH",
    "APP_STOP",
    "FILE_LIST", "FILE_UPLOAD", "FILE_DOWNLOAD", "FILE_RENAME", "FILE_DELETE",
    "FILE_MKDIR",
    "LOCATION_CONFIG", "LOCATION_REQUEST",
    "POLICY_APPLY", "KIOSK_SET",
    "REMOTE_SUPPORT_REQUEST", "REMOTE_SUPPORT_END",
)

OLD_TYPES = tuple(
    t for t in NEW_TYPES
    if t not in ("AGENT_RESTART", "COLLECT_DIAGNOSTICS", "APP_STOP")
)

# Ім'я з NAMING_CONVENTION: ck_%(table_name)s_%(constraint_name)s
CHECK_NAME = "ck_commands_command_type_enum"


def _replace_type_check(values: tuple[str, ...]) -> None:
    """Оновлює CHECK-обмеження переліку типів команд, якщо воно існує.

    SQLite не рефлектує CHECK-обмеження, тож там гілка пропускається:
    тестова база будується напряму з моделей і вже містить актуальний
    перелік значень.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # SQLAlchemy від 1.4 НЕ створює CHECK для Enum(native_enum=False):
    # `create_constraint` за замовчуванням False, і перелік перевіряється
    # на боці Python. Тому обмеження може взагалі не існувати — і саме на
    # цьому міграція падала на PostgreSQL, тоді як на SQLite (де гілка
    # пропускається) усе проходило. Перестворюємо лише те, що є.
    exists = bind.execute(
        sa.text(
            "SELECT 1 FROM pg_constraint "
            "WHERE conname = :name AND conrelid = 'commands'::regclass"
        ),
        {"name": CHECK_NAME},
    ).scalar()
    if not exists:
        return

    quoted = ", ".join(f"'{value}'" for value in values)
    op.execute(f"ALTER TABLE commands DROP CONSTRAINT {CHECK_NAME}")
    op.execute(
        f"ALTER TABLE commands ADD CONSTRAINT {CHECK_NAME} CHECK (type IN ({quoted}))"
    )


def upgrade() -> None:
    _replace_type_check(NEW_TYPES)

    with op.batch_alter_table("commands") as batch:
        # Тривалість рахується на сервері: годинник пристрою може розходитись.
        batch.add_column(sa.Column("duration_ms", sa.Integer()))
        batch.add_column(sa.Column("queue_ms", sa.Integer()))

    op.create_table(
        "command_permissions",
        sa.Column("id", UUIDType, primary_key=True),
        sa.Column(
            "command_type",
            sa.Enum(*NEW_TYPES, name="command_type_enum", native_enum=False),
            nullable=False,
        ),
        sa.Column("required_permission", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "updated_by_id", UUIDType, sa.ForeignKey("users.id", ondelete="SET NULL")
        ),
        sa.Column("updated_at", TZDateTime, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("command_type", name="uq_command_permissions_type"),
    )


def downgrade() -> None:
    op.drop_table("command_permissions")

    with op.batch_alter_table("commands") as batch:
        batch.drop_column("queue_ms")
        batch.drop_column("duration_ms")

    _replace_type_check(OLD_TYPES)
