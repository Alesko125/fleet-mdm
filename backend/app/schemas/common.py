"""Спільні DTO."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class PaginationParams(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class MessageResponse(BaseModel):
    message: str


class HealthResponse(BaseModel):
    """Стан сервісу.

    Усі поля, крім `status`, необовʼязкові: у production публічний
    `/health` віддає лише статус, щоб не розкривати версію збірки й
    розклад залежностей стороннім.
    """

    status: str
    version: str | None = None
    environment: str | None = None
    database: bool | None = None
    redis: bool | None = None
