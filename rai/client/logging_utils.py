"""Structured logging helpers for the RAI client."""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import socket
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Iterator

TRACE_HEADER = "X-RAI-Trace"

_TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "rai_client_trace_id", default=None
)

_HOSTNAME = socket.gethostname()

_LOG_TARGET = Path(
    os.environ.get(
        "RAI_CLIENT_LOG_PATH",
        Path(__file__).resolve().parents[2] / "logs" / "client.log",
    )
)

_RESERVED_ATTRS = set(logging.makeLogRecord({}).__dict__.keys()) | {
    "asctime",
    "message",
    "created",
    "msecs",
    "relativeCreated",
    "trace_id",
}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        payload: Dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "name": record.name,
            "trace_id": self._resolve_trace_id(record),
            "msg": record.getMessage(),
            "extra": self._extract_extra(record),
            "host": _HOSTNAME,
        }
        if record.exc_info:
            payload["extra"].setdefault(
                "exception", self.formatException(record.exc_info)
            )
        if record.stack_info:
            payload["extra"].setdefault("stack", record.stack_info)
        return json.dumps(payload, ensure_ascii=False)

    def _resolve_trace_id(self, record: logging.LogRecord) -> str:
        trace_id = getattr(record, "trace_id", None)
        if not trace_id:
            trace_id = _TRACE_ID.get() or "-"
        return str(trace_id)

    def _extract_extra(self, record: logging.LogRecord) -> Dict[str, object]:
        extras: Dict[str, object] = {}
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key == "trace_id":
                continue
            if key == "extra" and isinstance(value, dict):
                for extra_key, extra_value in value.items():
                    extras[extra_key] = _serialise(extra_value)
                continue
            extras[key] = _serialise(value)
        return extras


def _serialise(value: object) -> object:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _serialise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialise(item) for item in value]
    return repr(value)


_CONFIGURED = False


def _ensure_configured() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    if _LOG_TARGET.suffix:
        log_file = _LOG_TARGET
    else:
        log_file = _LOG_TARGET / "client.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    handler: logging.Handler
    try:
        handler = RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
    except OSError:
        handler = logging.StreamHandler()

    handler.setFormatter(_JsonFormatter())

    root = logging.getLogger("rai.client")
    root.setLevel(logging.DEBUG)
    root.propagate = False
    if root.handlers:
        root.handlers.clear()
    root.addHandler(handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    _ensure_configured()
    if name.startswith("rai.client"):
        return logging.getLogger(name)
    return logging.getLogger(f"rai.client.{name}")


class _TraceLoggerAdapter(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger, trace_id: str):
        super().__init__(logger, {})
        self._trace_id = trace_id

    def process(self, msg: object, kwargs: Dict[str, object]) -> tuple[object, Dict[str, object]]:
        extra = kwargs.setdefault("extra", {})
        if isinstance(extra, dict):
            extra.setdefault("trace_id", self._trace_id)
        else:  # pragma: no cover - defensive
            extra = {"value": _serialise(extra), "trace_id": self._trace_id}
            kwargs["extra"] = extra
        return msg, kwargs


@contextlib.contextmanager
def with_trace_id(logger: logging.Logger, trace_id: str) -> Iterator[logging.LoggerAdapter]:
    token = _TRACE_ID.set(trace_id)
    adapter = _TraceLoggerAdapter(logger, trace_id)
    try:
        yield adapter
    finally:
        _TRACE_ID.reset(token)

