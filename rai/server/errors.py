"""Error helpers with structured logging support."""

from __future__ import annotations

from typing import Dict

from .logging_utils import get_logger

_LOGGER = get_logger(__name__)


def json_error(message: str, notes: str) -> Dict[str, object]:
    """Build the canonical error payload returned by the server."""

    _LOGGER.info(
        "Generando respuesta de error",
        extra={"message": message, "notes": notes},
    )
    return {
        "action": "error",
        "app_name": None,
        "app_type": None,
        "exe_path": None,
        "process_name": None,
        "app_id": None,
        "args": [],
        "confidence": 0.0,
        "speak": message,
        "notes": notes,
    }


__all__ = ["json_error"]

