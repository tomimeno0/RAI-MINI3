"""Flask app exposing command parsing and catalogue endpoints."""  # FIX: document expanded API surface
from __future__ import annotations


from flask import Flask, Response, jsonify, request

from . import moduler  # FIX: import parser module locally
from .db_utils import (  # FIX: source DB helpers locally to decouple from client package
    DB_PATH,  # FIX: shared database path constant
    ensure_schema,  # FIX: schema management helper
    load_apps,  # FIX: catalogue loader utility
    scan_and_update_db,  # FIX: scanner bridge for rescan endpoint
)
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


    @app.route("/health", methods=["GET"])  # FIX: expose health-check endpoint
    def health() -> Response:  # FIX: serve health responses
        trace_id = _extract_trace_id()
        with with_trace_id(_LOGGER, trace_id):
            return _build_response({"status": "ok"}, 200, trace_id)  # FIX: health-check endpoint for clients

    return app



app = create_app()


if __name__ == "__main__":
    host = os.environ.get("RAI_SERVER_HOST", "127.0.0.1")
    port = _env_int("RAI_SERVER_PORT", 5050)
    app.run(host=host, port=port)
