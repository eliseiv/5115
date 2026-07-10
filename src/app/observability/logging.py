"""Structured JSON logging with correlation ids and secret redaction (05-security.md)."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.observability.context import get_request_id, request_id_var, session_id_var, user_id_var
from app.observability.redaction import redact


class JsonFormatter(logging.Formatter):
    """Renders log records as single-line JSON with correlation ids; redacts secrets."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "requestId": request_id_var.get(),
            "sessionId": session_id_var.get(),
            "userId": user_id_var.get(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(redact(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps({k: v for k, v in payload.items() if v is not None})


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Log a structured event; fields are redacted by the formatter."""
    logger.log(level, message, extra={"extra_fields": {**fields, "requestId": get_request_id()}})
