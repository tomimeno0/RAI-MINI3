"""CLI entry-point for the RAI mini assistant client."""
from __future__ import annotations

import json
import sys
import uuid
from typing import Dict, Optional, Tuple
from urllib import request

from . import audio, executor, scanner
from .logging_utils import TRACE_HEADER, get_logger, with_trace_id


LOGGER = get_logger(__name__)


def configure_logging() -> None:
    """Ensure logging handlers are configured."""

    get_logger(__name__)


def send_to_server(text: str, apps_catalogue: Optional[list], trace_id: str) -> Tuple[Dict[str, object], str]:
    payload = {"text": text}
    if apps_catalogue:
        payload["apps"] = apps_catalogue
    data = json.dumps(payload).encode("utf-8")



def main() -> None:
    configure_logging()
    LOGGER.info("Iniciando cliente RAI")

    apps = scanner.scan_apps()
    LOGGER.info("Catálogo inicial", extra={"entries": len(apps)})

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

            trace_id = uuid.uuid4().hex
            with with_trace_id(LOGGER, trace_id) as log:
                log.info("Comando capturado", extra={"command": command_text})
                response, response_trace = send_to_server(command_text, apps, trace_id)
            speak = response.get("speak") or "Listo"
            print(speak)
            with with_trace_id(LOGGER, response_trace) as log:
                log.info("Respuesta parser", extra={"response": response})

                if response.get("action") == "listar_apps":
                    _print_available_apps(apps)
                    continue

                exec_payload = {k: response.get(k) for k in response.keys()}
                exec_result = executor.execute(exec_payload)
                log.info("Resultado ejecución", extra={"result": exec_result})
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
