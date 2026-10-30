"""Structured application logging for Operation Shadow."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import sys
from typing import Any

from core.config import settings


class StructuredJsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects with UTC timestamps."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for extra_key in ("mission_id", "vehicle_id", "event_id", "error_code", "request_path"):
            val = getattr(record, extra_key, None)
            if val is not None:
                payload[extra_key] = val
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(
    *,
    level: str | None = None,
    json_output: bool | None = None,
) -> logging.Logger:
    """Configure the `operation_shadow` root logger idempotently."""
    resolved_level = getattr(logging, (level or settings.log_level).upper(), logging.INFO)
    use_json = settings.log_json if json_output is None else json_output

    logger = logging.getLogger("operation_shadow")
    logger.setLevel(resolved_level)
    logger.propagate = False

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(resolved_level)
    if use_json:
        handler.setFormatter(StructuredJsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )

    logger.handlers.clear()
    logger.addHandler(handler)
    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the `operation_shadow` namespace."""
    return logging.getLogger(f"operation_shadow.{name}")
