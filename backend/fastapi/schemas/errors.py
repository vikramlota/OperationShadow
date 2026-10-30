"""Standardized API error response contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import Field, field_validator
from schemas.entities import SchemaBase, ensure_utc_datetime


class ErrorDetail(SchemaBase):
    """Granular field or constraint error item."""

    loc: list[str | int] = Field(default_factory=list)
    message: str
    error_type: str | None = None


class ErrorResponse(SchemaBase):
    """Consistent structured error envelope returned by FastAPI error handlers."""

    error_code: str
    message: str
    details: list[ErrorDetail] | dict[str, Any] | None = None
    request_path: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("timestamp", mode="after")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        normalized = ensure_utc_datetime(value)
        assert normalized is not None
        return normalized
