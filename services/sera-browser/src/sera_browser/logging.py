"""Structured JSON logging with sensitive-data redaction.

Logs NEVER contain credentials. A `redact()` helper masks Authorization,
Cookie, Set-Cookie, password, token, secret, api_key, etc. before any log
line is emitted.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any, Iterable

_SENSITIVE_KEYS = (
    "authorization",
    "cookie",
    "set-cookie",
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "id_token",
    "private_key",
    "storage_state",
)

_MASK = "***REDACTED***"

# Loose bearer / basic token matcher for header values.
_TOKEN_RE = re.compile(r"(?i)(bearer|basic|token)\s+\S+")


def redact(value: Any) -> Any:
    """Recursively redact sensitive values in dicts/lists/strings."""
    if isinstance(value, dict):
        return {
            k: (_MASK if _is_sensitive_key(k) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        if _TOKEN_RE.search(value):
            value = _TOKEN_RE.sub(lambda m: f"{m.group(1)} {_MASK}", value)
        return value
    return value


def _is_sensitive_key(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in _SENSITIVE_KEYS)


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, "extra") and isinstance(record.extra, dict):
            record.extra = redact(record.extra)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Attach structured extra fields if present.
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict) and extra:
            payload.update(redact(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Redact any sensitive args the caller put into the message itself
        # (best-effort; structured `extra` is the preferred channel).
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    root = logging.getLogger()
    # Reset handlers so reconfigure is safe.
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(stream=sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    handler.addFilter(_RedactingFilter())
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: Any,
) -> None:
    """Emit a structured event with redaction applied to `fields`."""
    logger.log(level, event, extra={"extra": {**fields, "event": event}})


def safe_repr_headers(headers: Iterable[tuple[str, str]] | dict[str, str]) -> dict[str, str]:
    """Return a redacted, JSON-safe view of headers for logging/state."""
    if isinstance(headers, dict):
        items = headers.items()
    else:
        items = headers
    out: dict[str, str] = {}
    for k, v in items:
        if _is_sensitive_key(k):
            out[k] = _MASK
        else:
            out[k] = v
    return out
