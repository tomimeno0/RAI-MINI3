"""Flask app exposing command parsing and catalogue endpoints."""  # FIX: document expanded API surface
from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Tuple

from flask import Flask, request

from ..client.scanner import scan_and_update_db  # FIX: reuse scanner to refresh catalogue
from . import moduler
from .db_utils import DB_PATH, ensure_schema, load_apps  # FIX: share DB helpers with server modules

LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "server.log"
_LOGGER = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(__name__)
    _configure_logging()
    ensure_schema(DB_PATH)  # FIX: prepare database before handling requests

    @app.after_request  # FIX: inject CORS headers on every response
    def add_cors_headers(response):  # type: ignore[override]
        response.headers.setdefault("Access-Control-Allow-Origin", "*")
        response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type")
        response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")  # FIX: allow catalogue routes
        return response

    @app.route("/parse", methods=["POST"])  # FIX: parsing endpoint configuration
    def parse_endpoint() -> Tuple[Dict[str, object], int]:
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
        catalogue = load_apps(DB_PATH)  # FIX: serve latest catalogue snapshot
        return {"apps": catalogue}, 200  # FIX: respond with catalogue payload

    @app.route("/apps/scan", methods=["POST"])  # FIX: expose manual rescan endpoint
    def scan_apps() -> Tuple[Dict[str, object], int]:  # FIX: trigger catalogue refresh
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
    app.run(host="127.0.0.1", port=5050)
