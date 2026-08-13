"""Метадані APK та прогрес виконання команд

Revision ID: 0002_apk_module
Revises: 0001_initial
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.base import JSONType

revision: str = "0002_apk_module"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("apk_artifacts") as batch:
        batch.add_column(sa.Column("label", sa.String(255)))
        batch.add_column(sa.Column("signature_schemes", JSONType))
        batch.add_column(sa.Column("min_sdk", sa.Integer()))
        batch.add_column(sa.Column("target_sdk", sa.Integer()))
        batch.add_column(sa.Column("compile_sdk", sa.Integer()))
        batch.add_column(sa.Column("abis", JSONType))
        batch.add_column(sa.Column("permissions", JSONType))
        batch.add_column(
            sa.Column("is_split", sa.Boolean(), nullable=False, server_default=sa.false())
        )

    with op.batch_alter_table("commands") as batch:
        batch.add_column(sa.Column("progress_percent", sa.Integer()))
        batch.add_column(sa.Column("status_message", sa.String(255)))

    # Пошук останньої версії пакета в репозиторії.
    op.create_index(
        "ix_apk_artifacts_package_version",
        "apk_artifacts",
        ["package_name", "version_code"],
    )


def downgrade() -> None:
    op.drop_index("ix_apk_artifacts_package_version", table_name="apk_artifacts")

    with op.batch_alter_table("commands") as batch:
        batch.drop_column("status_message")
        batch.drop_column("progress_percent")

    with op.batch_alter_table("apk_artifacts") as batch:
        for column in (
            "is_split",
            "permissions",
            "abis",
            "compile_sdk",
            "target_sdk",
            "min_sdk",
            "signature_schemes",
            "label",
        ):
            batch.drop_column(column)
