"""Flask app exposing command parsing and catalogue endpoints."""  # FIX: document expanded API surface
from __future__ import annotations

import time
import uuid
from typing import Dict

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


def create_app() -> Flask:
    app = Flask(__name__)
    ensure_schema(DB_PATH)  # FIX: prepare database before handling requests

    @app.after_request  # FIX: inject CORS headers on every response
    def add_cors_headers(response):  # type: ignore[override]
        response.headers.setdefault("Access-Control-Allow-Origin", "*")
        response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type")
        response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")  # FIX: allow catalogue routes
        return response

    @app.route("/parse", methods=["POST"])  # FIX: parsing endpoint configuration
    def parse_endpoint() -> Response:
        trace_id = _extract_trace_id()
        with with_trace_id(_LOGGER, trace_id) as log:
            if not request.is_json:
                log.warning("Solicitud sin JSON")
                return _build_response(json_error("Esperaba JSON", "content-type"), 400, trace_id)

            payload: Dict[str, object] = request.get_json(force=True)
            text = str(payload.get("text", "")).strip()
            apps = payload.get("apps")
            if not text:
                log.warning("Solicitud sin texto")
                return _build_response(moduler.build_error("No recibí texto", ""), 400, trace_id)

            start = time.time()
            try:
                result = moduler.parse(
                    text,
                    apps_catalogue=apps if isinstance(apps, list) else None,
                )
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("Error en parser")
                return _build_response(
                    json_error("Algo salió mal parseando la orden", str(exc)),
                    500,
                    trace_id,
                )

            latency = (time.time() - start) * 1000
            if not moduler.validate_contract(result):
                log.error("Respuesta fuera de contrato", extra={"result": result})
                result = moduler.build_error("Solo controlo apps, por ahora.", text)

            log.info(
                "Parse completado",
                extra={
                    "latency_ms": round(latency, 2),
                    "action": result.get("action"),
                    "app_name": result.get("app_name"),
                },
            )
            return _build_response(result, 200, trace_id)

    @app.route("/apps", methods=["GET"])  # FIX: expose catalogue listing endpoint
    def list_apps() -> Response:  # FIX: serve GET /apps responses
        trace_id = _extract_trace_id()
        with with_trace_id(_LOGGER, trace_id) as log:
            catalogue = load_apps(DB_PATH)  # FIX: serve latest catalogue snapshot
            log.info("Catálogo solicitado", extra={"entries": len(catalogue)})
            return _build_response({"apps": catalogue}, 200, trace_id)  # FIX: respond with catalogue payload

    @app.route("/apps/scan", methods=["POST"])  # FIX: expose manual rescan endpoint
    def scan_apps() -> Response:  # FIX: trigger catalogue refresh
        trace_id = _extract_trace_id()
        with with_trace_id(_LOGGER, trace_id) as log:
            start = time.time()  # FIX: measure scan latency
            try:
                scan_and_update_db(DB_PATH)  # FIX: trigger rescan using shared database
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("Error escaneando apps")  # FIX: log scan failure
                return _build_response(
                    json_error("No pude actualizar el catálogo", str(exc)),
                    500,
                    trace_id,
                )
            latency = (time.time() - start) * 1000  # FIX: compute scan duration in ms
            log.info("Escaneo completado", extra={"latency_ms": round(latency, 2)})
            return _build_response({"apps": load_apps(DB_PATH)}, 200, trace_id)

    @app.route("/health", methods=["GET"])  # FIX: expose health-check endpoint
    def health() -> Response:  # FIX: serve health responses
        trace_id = _extract_trace_id()
        with with_trace_id(_LOGGER, trace_id):
            return _build_response({"status": "ok"}, 200, trace_id)  # FIX: health-check endpoint for clients

    return app


def _extract_trace_id() -> str:
    incoming = request.headers.get(TRACE_HEADER)
    return incoming or uuid.uuid4().hex


def _build_response(payload: Dict[str, object], status: int, trace_id: str) -> Response:
    response = jsonify(payload)
    response.status_code = status
    response.headers[TRACE_HEADER] = trace_id
    return response


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050)
