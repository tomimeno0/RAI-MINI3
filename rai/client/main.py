"""CLI entry-point for the RAI mini assistant client."""
from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Optional
from urllib import request

from . import audio, executor, scanner

LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "client.log"
SERVER_URL = "http://127.0.0.1:5050/parse"


def configure_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=3)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()])


def send_to_server(text: str, apps_catalogue: Optional[list]) -> Dict[str, object]:
    payload = {"text": text}
    if apps_catalogue:
        payload["apps"] = apps_catalogue
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(SERVER_URL, data=data, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=10) as response:
            charset = response.headers.get_content_charset("utf-8")
            body = response.read().decode(charset)
            return json.loads(body)
    except Exception as exc:
        logging.getLogger(__name__).error("Error llamando al parser: %s", exc)
        return {
            "action": "error",
            "speak": "No pude comunicarme con el parser. ¿Está el servidor encendido?",
            "notes": str(exc),
        }


def main() -> None:
    configure_logging()
    log = logging.getLogger(__name__)
    log.info("Iniciando cliente RAI")

    apps = scanner.scan_and_update_db()
    log.info("Catálogo inicial: %s entradas", len(apps))

    listener = audio.AudioInputManager()
    print("RAI listo. Decí 'hola rai' o presioná Enter para hablar.")

    try:
        while True:
            hotword = listener.wait_for_hotword()
            if not hotword.triggered:
                if hotword.transcript == "salir":
                    print("¡Hasta luego!")
                    break
                continue

            if hotword.transcript:
                command_text = hotword.transcript
            else:
                command_text = listener.capture_command() or ""

            command_text = command_text.strip()
            if not command_text:
                print("No escuché ningún comando.")
                continue
            if command_text.lower() in {"salir", "exit", "quit"}:
                print("¡Hasta luego!")
                break

            log.info("Comando capturado: %s", command_text)
            response = send_to_server(command_text, apps)
            speak = response.get("speak") or "Listo"
            print(speak)
            log.info("Respuesta parser: %s", response)

            if response.get("action") == "listar_apps":
                _print_available_apps(apps)
                continue

            exec_result = executor.execute({k: response.get(k) for k in response.keys()})
            log.info("Resultado ejecución: %s", exec_result)
    finally:
        listener.close()


def _print_available_apps(apps: list) -> None:
    if not apps:
        print("No tengo apps registradas.")
        return
    top = sorted(apps, key=lambda x: x.get("last_seen", ""), reverse=True)[:5]
    friendly = ", ".join(entry.get("display_name", entry.get("name", "")) for entry in top)
    print(f"Tenés {friendly}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario")
        sys.exit(0)
