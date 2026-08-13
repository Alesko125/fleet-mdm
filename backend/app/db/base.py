"""Базовий клас моделей: іменування обмежень, UUID-PK, часові мітки."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.security import utcnow

# Іменовані обмеження потрібні, щоб Alembic міг їх коректно змінювати.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# JSONB у PostgreSQL, звичайний JSON в інших діалектах (тести на SQLite).
JSONType = sa.JSON().with_variant(JSONB(), "postgresql")

# `sa.Uuid` рендериться як UUID у PostgreSQL і як CHAR(32) деінде.
UUIDType = sa.Uuid(as_uuid=True)

TZDateTime = sa.DateTime(timezone=True)


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        uuid.UUID: UUIDType,
        datetime: TZDateTime,
        dict: JSONType,
    }


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


def enum_column(values: tuple[str, ...], name: str, **kwargs) -> sa.Enum:
    """VARCHAR + CHECK замість нативного enum — переносимо між діалектами."""
    return sa.Enum(*values, name=name, native_enum=False, validate_strings=True, **kwargs)
