"""Flask app exposing command parsing and catalogue endpoints."""  # FIX: document expanded API surface
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Deque, Dict, Tuple

from flask import Flask, request

from . import moduler  # FIX: import parser module locally
from .db_utils import (  # FIX: source DB helpers locally to decouple from client package
    DB_PATH,  # FIX: shared database path constant
    ensure_schema,  # FIX: schema management helper
    load_apps,  # FIX: catalogue loader utility
    scan_and_update_db,  # FIX: scanner bridge for rescan endpoint
)

LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "server.log"
_LOGGER = logging.getLogger(__name__)

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
    _configure_logging()
    ensure_schema(DB_PATH)  # FIX: prepare database before handling requests

    @app.after_request  # FIX: inject CORS headers on every response
    def add_cors_headers(response):  # type: ignore[override]
        origin = request.headers.get("Origin")
        allowed = _resolve_cors_origin(origin)
        if allowed:
            response.headers.setdefault("Access-Control-Allow-Origin", allowed)
        response.headers.setdefault(
            "Access-Control-Allow-Headers", "Content-Type,X-RAI-API-Key"
        )
        response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")  # FIX: allow catalogue routes
        return response

    @app.route("/parse", methods=["POST"])  # FIX: parsing endpoint configuration
    def parse_endpoint() -> Tuple[Dict[str, object], int]:
        auth_error = _authenticate_request()
        if auth_error:
            return auth_error

        rate_error = _enforce_rate_limit()
        if rate_error:
            return rate_error

        if not request.is_json:
            return _json_error("Esperaba JSON", "content-type"), 400

        payload: Dict[str, object] = request.get_json(force=True)
        text = str(payload.get("text", "")).strip()
        apps = payload.get("apps")
        if not text:
            return moduler.build_error("No recibí texto", ""), 400

        start = time.time()
        try:
            result = moduler.parse(text, apps_catalogue=apps if isinstance(apps, list) else None)
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.exception("Error en parser: %s", exc)
            return _json_error("Algo salió mal parseando la orden", str(exc)), 500

        latency = (time.time() - start) * 1000
        if not moduler.validate_contract(result):
            _LOGGER.error("Respuesta fuera de contrato: %s", result)
            result = moduler.build_error("Solo controlo apps, por ahora.", text)

        _LOGGER.info(
            "Parse completado en %.0f ms :: action=%s app=%s",
            latency,
            result.get("action"),
            result.get("app_name"),
        )
        return result, 200

    @app.route("/apps", methods=["GET"])  # FIX: expose catalogue listing endpoint
    def list_apps() -> Tuple[Dict[str, object], int]:  # FIX: serve GET /apps responses
        auth_error = _authenticate_request()
        if auth_error:
            return auth_error

        rate_error = _enforce_rate_limit()
        if rate_error:
            return rate_error

        catalogue = load_apps(DB_PATH)  # FIX: serve latest catalogue snapshot
        return {"apps": catalogue}, 200  # FIX: respond with catalogue payload

    @app.route("/apps/scan", methods=["POST"])  # FIX: expose manual rescan endpoint
    def scan_apps() -> Tuple[Dict[str, object], int]:  # FIX: trigger catalogue refresh
        auth_error = _authenticate_request()
        if auth_error:
            return auth_error

        rate_error = _enforce_rate_limit()
        if rate_error:
            return rate_error

        start = time.time()  # FIX: measure scan latency
        try:
            scan_and_update_db(DB_PATH)  # FIX: trigger rescan using shared database
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.exception("Error escaneando apps: %s", exc)  # FIX: log scan failure
            return _json_error("No pude actualizar el catálogo", str(exc)), 500  # FIX: propagate scan error to caller
        latency = (time.time() - start) * 1000  # FIX: compute scan duration in ms
        _LOGGER.info("Escaneo completado en %.0f ms", latency)  # FIX: record scan completion
        return {"apps": load_apps(DB_PATH)}, 200  # FIX: return refreshed catalogue after scan

    @app.route("/health", methods=["GET"])  # FIX: expose health-check endpoint
    def health() -> Tuple[Dict[str, object], int]:  # FIX: serve health responses
        return {"status": "ok"}, 200  # FIX: health-check endpoint for clients

    return app


def _json_error(message: str, notes: str) -> Dict[str, object]:
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


def _resolve_cors_origin(request_origin: str | None) -> str | None:
    if "*" in _ALLOWED_ORIGINS:
        return "*"
    if request_origin and request_origin in _ALLOWED_ORIGINS:
        return request_origin
    return _ALLOWED_ORIGINS[0] if _ALLOWED_ORIGINS else None


def _authenticate_request() -> Tuple[Dict[str, object], int] | None:
    if not _API_KEY:
        return None
    provided = request.headers.get("X-RAI-API-Key") or request.args.get("api_key")
    if provided != _API_KEY:
        return _json_error("Solicitud no autorizada", "api_key"), 401
    return None


def _enforce_rate_limit() -> Tuple[Dict[str, object], int] | None:
    if _RATE_LIMIT_CAP <= 0:
        return None

    identifier = request.remote_addr or "unknown"
    bucket = _RATE_LIMIT_BUCKETS[identifier]
    now = time.time()
    while bucket and now - bucket[0] > _RATE_LIMIT_WINDOW:
        bucket.popleft()

    if len(bucket) >= _RATE_LIMIT_CAP:
        return (
            _json_error(
                "Demasiadas solicitudes; esperá unos segundos e intentá nuevamente.",
                "rate_limit",
            ),
            429,
        )

    bucket.append(now)
    return None


def _configure_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=3)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    else:
        root.addHandler(handler)


app = create_app()


if __name__ == "__main__":
    host = os.environ.get("RAI_SERVER_HOST", "127.0.0.1")
    port = _env_int("RAI_SERVER_PORT", 5050)
    app.run(host=host, port=port)
