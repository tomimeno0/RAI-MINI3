"""Flask app exposing the /parse endpoint."""
from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Tuple

from flask import Flask, request

from . import moduler

LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "server.log"
_LOGGER = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(__name__)
    _configure_logging()

    @app.after_request
    def add_cors_headers(response):  # type: ignore[override]
        response.headers.setdefault("Access-Control-Allow-Origin", "*")
        response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type")
        response.headers.setdefault("Access-Control-Allow-Methods", "POST, OPTIONS")
        return response

    @app.route("/parse", methods=["POST"])
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
