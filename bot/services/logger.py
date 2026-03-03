"""Structured logging.

Goals:
- One global logging configuration (root logger) suitable for Render.
- JSON logs by default (easy to search/parse).
- Best-effort redaction of secrets/tokens in both message and extra context.

No third-party deps.
"""

from __future__ import annotations

import json
import logging
import os
import re
import traceback
from datetime import datetime, timezone
from typing import Any, Optional


class SensitiveDataRedactor:
    """Redacts sensitive data patterns from strings and dict-like payloads."""

    PATTERNS = [
        # Bearer tokens
        (re.compile(r"bearer\s+([a-zA-Z0-9_\-\.]+)", re.IGNORECASE), "bearer_token"),
        (re.compile(r"authorization[\"']?\s*[:=]\s*bearer\s+([a-zA-Z0-9_\-\.]+)", re.IGNORECASE), "authorization"),
        # Key/value-ish secrets
        (re.compile(r"password[\"']?\s*[:=]\s*[\"']?([^\s,\"']+)", re.IGNORECASE), "password"),
        (re.compile(r"token[\"']?\s*[:=]\s*[\"']?([^\s,\"']+)", re.IGNORECASE), "token"),
        (re.compile(r"api[_-]?key[\"']?\s*[:=]\s*[\"']?([^\s,\"']+)", re.IGNORECASE), "api_key"),
        (re.compile(r"secret[\"']?\s*[:=]\s*[\"']?([^\s,\"']+)", re.IGNORECASE), "secret"),
        (re.compile(r"credential[s]?[\"']?\s*[:=]\s*[\"']?([^\s,\"']+)", re.IGNORECASE), "credential"),
        # Generic authorization
        (re.compile(r"authorization[\"']?\s*[:=]\s*[\"']?([^\s,\"']+)", re.IGNORECASE), "authorization"),
    ]

    SENSITIVE_FIELDS = {
        "password",
        "token",
        "api_key",
        "secret",
        "credential",
        "credentials",
        "authorization",
        "auth",
        "bearer",
        "access_token",
        "refresh_token",
        "client_secret",
        "private_key",
        "aws_secret_access_key",
        "dropbox_access_token",
        "google_refresh_token",
        "bot_token",
        "yandex_password",
        "icloud_app_password",
    }

    @classmethod
    def redact_string(cls, text: Any) -> Any:
        if not isinstance(text, str):
            return text
        out = text
        for pattern, name in cls.PATTERNS:
            out = pattern.sub(f"{name}=[REDACTED]", out)
        return out

    @classmethod
    def redact_value(cls, value: Any) -> Any:
        if isinstance(value, str):
            return cls.redact_string(value)
        if isinstance(value, dict):
            return cls.redact_dict(value)
        if isinstance(value, (list, tuple)):
            return [cls.redact_value(v) for v in value]
        return value

    @classmethod
    def redact_dict(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return cls.redact_value(data)
        out: dict[str, Any] = {}
        for k, v in data.items():
            if str(k).lower() in cls.SENSITIVE_FIELDS:
                out[k] = "[REDACTED]"
            else:
                out[k] = cls.redact_value(v)
        return out


_RESERVED_LOGRECORD_KEYS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
}


class JsonFormatter(logging.Formatter):
    """JSON formatter with redaction and small payload guard."""

    MAX_PAYLOAD_SIZE = 4096  # 4KB per line

    def format(self, record: logging.LogRecord) -> str:
        base: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": SensitiveDataRedactor.redact_string(record.getMessage()),
        }

        # Extras (structured context)
        for k, v in record.__dict__.items():
            if k in _RESERVED_LOGRECORD_KEYS:
                continue
            if k in base:
                continue
            if str(k).lower() in SensitiveDataRedactor.SENSITIVE_FIELDS:
                base[k] = "[REDACTED]"
            else:
                base[k] = SensitiveDataRedactor.redact_value(v)

        # Exceptions
        if record.exc_info:
            exc_type = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            base["exc_type"] = exc_type
            base["traceback"] = "".join(traceback.format_exception(*record.exc_info))[-2000:]

        raw = json.dumps(base, ensure_ascii=False, separators=(",", ":"))
        if len(raw) <= self.MAX_PAYLOAD_SIZE:
            return raw

        # truncate message first
        base["message"] = (base.get("message") or "")[:800] + "… [truncated]"
        raw2 = json.dumps(base, ensure_ascii=False, separators=(",", ":"))
        return raw2[: self.MAX_PAYLOAD_SIZE - 30] + "… [truncated]"


class PlainFormatter(logging.Formatter):
    """Human-friendly formatter with redaction."""

    def format(self, record: logging.LogRecord) -> str:
        msg = SensitiveDataRedactor.redact_string(record.getMessage())
        ts = datetime.now(timezone.utc).isoformat()
        line = f"{ts} {record.levelname:<7} {record.name}: {msg}"
        if record.exc_info:
            line += "\n" + "".join(traceback.format_exception(*record.exc_info))
        return line


_CONFIGURED = False
_CONFIGURED_FMT: str | None = None


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure root logging.

    Idempotent. Intended to be called early from runtime.
    """

    global _CONFIGURED, _CONFIGURED_FMT

    fmt = (fmt or os.getenv("LOG_FORMAT") or "json").lower()
    level = (level or os.getenv("LOG_LEVEL") or "INFO").upper()

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))

    if _CONFIGURED:
        # allow adjusting format if requested
        if _CONFIGURED_FMT != fmt:
            for h in root.handlers:
                if fmt in ("plain", "text"):
                    h.setFormatter(PlainFormatter())
                else:
                    h.setFormatter(JsonFormatter())
            _CONFIGURED_FMT = fmt
        return

    handler = logging.StreamHandler()
    if fmt in ("plain", "text"):
        handler.setFormatter(PlainFormatter())
    else:
        handler.setFormatter(JsonFormatter())

    # Replace existing handlers (Render best practice)
    root.handlers.clear()
    root.addHandler(handler)

    # Make common noisy loggers sane
    logging.getLogger("aiohttp.access").setLevel(logging.INFO)
    logging.getLogger("aiohttp.server").setLevel(logging.INFO)

    _CONFIGURED = True
    _CONFIGURED_FMT = fmt


class StructuredLogger:
    """Tiny wrapper around stdlib logging for `logger.info(msg, **ctx)` style."""

    def __init__(self, name: str):
        self._logger = logging.getLogger(name)

    def debug(self, message: str, **context: Any) -> None:
        self._logger.debug(message, extra=context)

    def info(self, message: str, **context: Any) -> None:
        self._logger.info(message, extra=context)

    def warning(self, message: str, **context: Any) -> None:
        self._logger.warning(message, extra=context)

    def error(self, message: str, error: Optional[Exception] = None, **context: Any) -> None:
        if error is not None:
            ctx = dict(context)
            ctx.setdefault("error_type", type(error).__name__)
            ctx.setdefault("error_message", str(error))
            self._logger.error(
                message,
                extra=ctx,
                exc_info=(type(error), error, getattr(error, "__traceback__", None)),
            )
        else:
            self._logger.error(message, extra=context)

    def exception(self, message: str, **context: Any) -> None:
        self._logger.exception(message, extra=context)


def get_logger(name: str) -> StructuredLogger:
    return StructuredLogger(name)
