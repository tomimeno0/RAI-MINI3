"""Flask application exposing the RAI server endpoints."""

from __future__ import annotations

import hmac
import os
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from typing import Deque, Dict, Optional

from flask import Flask, Response, jsonify, request, g

from . import moduler
from .db_utils import DB_PATH, ensure_schema, ingest_scan, load_apps, scan_and_update_db
from .errors import json_error
from .logging_utils import TRACE_HEADER, get_logger, with_trace_id


_LOGGER = get_logger(__name__)

_ALLOWED_ORIGINS = tuple(
    origin.strip()
    for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
) or ("*",)
_API_KEY = os.environ.get("RAI_SERVER_API_KEY")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        _LOGGER.warning("Valor inválido para %s=%s; usando %s", name, raw, default)
        return default


_RATE_LIMIT_PER_MINUTE = max(_env_int("RATE_LIMIT_REQUESTS_PER_MINUTE", 0), 0)
_RATE_LIMIT_BURST = max(_env_int("RATE_LIMIT_BURST", 0), 0)
_RATE_LIMIT_WINDOW = 60.0
_RATE_LIMIT_CAP = (
    _RATE_LIMIT_PER_MINUTE + _RATE_LIMIT_BURST if _RATE_LIMIT_PER_MINUTE else 0
)
_RATE_LIMIT_BUCKETS: Dict[str, Deque[float]] = defaultdict(deque)


def create_app() -> Flask:
    app = Flask(__name__)
    ensure_schema(DB_PATH)

    @app.before_request
    def _before_request() -> Optional[Response]:  # type: ignore[override]
        trace_id = _extract_trace_id()
        g.trace_id = trace_id

        if request.method == "OPTIONS":
            return _build_response({}, 204, trace_id)

        limited = _check_rate_limit(trace_id)
        if limited is not None:
            return limited

        if request.endpoint == "health":
            return None

        auth = _require_api_key(trace_id)
        if auth is not None:
            return auth
        return None

    @app.after_request
    def _add_cors_headers(response: Response) -> Response:  # type: ignore[override]
        origin = request.headers.get("Origin")
        allowed = _resolve_cors_origin(origin)
        if allowed:
            response.headers.setdefault("Access-Control-Allow-Origin", allowed)
        response.headers.setdefault(
            "Access-Control-Allow-Headers",
            f"Content-Type,X-RAI-Key,{TRACE_HEADER}",
        )
        response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        return response

    @app.route("/health", methods=["GET"])
    def health() -> Response:
        trace_id = _current_trace_id()
        with with_trace_id(_LOGGER, trace_id):
            return _build_response({"status": "ok"}, 200, trace_id)

    @app.route("/parse", methods=["POST"])
    def parse() -> Response:
        trace_id = _current_trace_id()
        with with_trace_id(_LOGGER, trace_id) as log:
            payload = request.get_json(silent=True) or {}
            if not isinstance(payload, dict):
                error = json_error("Solicitud inválida", "cuerpo JSON requerido")
                return _build_response(error, 400, trace_id)

            text = payload.get("text")
            host = payload.get("host")
            if not isinstance(text, str) or not text.strip():
                error = json_error("Texto requerido", "campo 'text' vacío o ausente")
                return _build_response(error, 400, trace_id)

            result = moduler.interpret(text, host=str(host) if isinstance(host, str) else None)
            result["trace_id"] = trace_id
            log.info("interpret ejecutado", extra={"host": host})
            return _build_response(result, 200, trace_id)

    @app.route("/apps/scan", methods=["GET"])
    def list_apps() -> Response:
        trace_id = _current_trace_id()
        with with_trace_id(_LOGGER, trace_id):
            apps = load_apps(DB_PATH)
            return _build_response({"apps": apps}, 200, trace_id)

    @app.route("/apps/scan", methods=["POST"])
    def receive_scan() -> Response:
        trace_id = _current_trace_id()
        with with_trace_id(_LOGGER, trace_id) as log:
            payload = request.get_json(silent=True) or {}
            if not isinstance(payload, dict):
                error = json_error("Solicitud inválida", "cuerpo JSON requerido")
                return _build_response(error, 400, trace_id)

            mode = payload.get("mode")
            if isinstance(mode, str) and mode.lower() == "local":
                try:
                    count = scan_and_update_db(DB_PATH)
                except Exception as exc:  # pragma: no cover - scanner es específico de Windows
                    log.error("scan local falló", extra={"error": str(exc)})
                    error = json_error("Escaneo local falló", str(exc))
                    return _build_response(error, 500, trace_id)
                return _build_response({"status": "ok", "items": count, "mode": "local"}, 200, trace_id)

            host = payload.get("host")
            apps = payload.get("apps")
            if not isinstance(host, str) or not host.strip():
                error = json_error("Hostname inválido", "campo 'host' requerido")
                return _build_response(error, 400, trace_id)
            if not isinstance(apps, list):
                error = json_error("Apps inválidas", "campo 'apps' debe ser una lista")
                return _build_response(error, 400, trace_id)

            filtered_apps = [item for item in apps if isinstance(item, dict)]
            try:
                count = ingest_scan(host, filtered_apps, db_path=DB_PATH, trace_id=trace_id, logger=_LOGGER)
            except ValueError as exc:
                error = json_error("Payload inválido", str(exc))
                return _build_response(error, 400, trace_id)
            except sqlite3.Error as exc:
                log.error("Error escribiendo en SQLite", extra={"error": str(exc)})
                error = json_error("Error de base de datos", "no se pudo persistir el escaneo")
                return _build_response(error, 500, trace_id)

            log.info("scan recibido", extra={"host": host, "items": count})
            return _build_response({"status": "ok", "items": count}, 200, trace_id)

    return app


def _resolve_cors_origin(origin: Optional[str]) -> Optional[str]:
    if "*" in _ALLOWED_ORIGINS:
        return "*"
    if origin and origin in _ALLOWED_ORIGINS:
        return origin
    return None


def _extract_trace_id() -> str:
    header = request.headers.get(TRACE_HEADER)
    return header or uuid.uuid4().hex


def _current_trace_id() -> str:
    return getattr(g, "trace_id", _extract_trace_id())


def _build_response(payload: Dict[str, object], status: int, trace_id: str) -> Response:
    response = jsonify(payload)
    response.status_code = status
    response.headers[TRACE_HEADER] = trace_id
    return response


def _rate_limit_identifier() -> str:
    api_key = request.headers.get("X-RAI-Key")
    if api_key:
        return f"key:{api_key}"
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return f"ip:{forwarded.split(',')[0].strip()}"
    return f"ip:{request.remote_addr or 'unknown'}"


def _check_rate_limit(trace_id: str) -> Optional[Response]:
    if not _RATE_LIMIT_CAP:
        return None

    identifier = _rate_limit_identifier()
    bucket = _RATE_LIMIT_BUCKETS[identifier]
    now = time.monotonic()
    cutoff = now - _RATE_LIMIT_WINDOW
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= _RATE_LIMIT_CAP:
        _LOGGER.warning(
            "Límite de rate alcanzado",
            extra={"identifier": identifier, "trace_id": trace_id},
        )
        error = json_error("Demasiadas solicitudes", "rate_limit_exceeded")
        return _build_response(error, 429, trace_id)
    bucket.append(now)
    return None


def _require_api_key(trace_id: str) -> Optional[Response]:
    if not _API_KEY:
        return None
    provided = request.headers.get("X-RAI-Key")
    if not provided or not hmac.compare_digest(str(provided), _API_KEY):
        _LOGGER.warning("API key inválida", extra={"trace_id": trace_id})
        error = json_error("Acceso denegado", "api_key_requerida")
        return _build_response(error, 401, trace_id)
    return None


app = create_app()


if __name__ == "__main__":  # pragma: no cover - CLI helper
    host = os.environ.get("RAI_SERVER_HOST", "127.0.0.1")
    port = _env_int("RAI_SERVER_PORT", 5050)
    app.run(host=host, port=port)

