# Dependencias opcionales: pip install requests openai cohere speechrecognition keyboard pywin32 pygetwindow
# Cómo ejecutar: python client.py
"""
Cliente principal de RAI-MINI para Windows.
Gestiona la escucha, el parser (modo offline y API) y la orquestación con el HUD.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional

try:
    import speech_recognition as sr  # type: ignore
except Exception:  # pragma: no cover - dependencia opcional
    sr = None

try:
    import keyboard  # type: ignore
except Exception:  # pragma: no cover - dependencia opcional
    keyboard = None

from actions import ACTIONS_CATALOG, do_action, find_app_by_alias
from hud import RAIHUD


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
)


CONTRACT_KEYS = {"action", "target", "args", "confidence", "reason"}
VALID_ACTIONS = {
    "open_app",
    "close_app",
    "minimize",
    "maximize",
    "focus",
    "open_taskmgr",
    "none",
}


@dataclass
class ParseResult:
    action: str
    target: Optional[str]
    args: Dict[str, str]
    confidence: float
    reason: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "action": self.action,
            "target": self.target,
            "args": self.args,
            "confidence": float(max(0.0, min(1.0, self.confidence))),
            "reason": self.reason,
        }


def strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


ACTION_SYNONYMS = {
    "open_app": [
        "abrir",
        "abre",
        "abrime",
        "lanza",
        "inicia",
        "ejecuta",
        "arranca",
    ],
    "close_app": ["cierra", "cerrar", "termina", "finaliza", "apaga"],
    "minimize": ["minimiza", "minimizar", "oculta", "esconde"],
    "maximize": ["maximiza", "maximizar", "pantalla completa"],
    "focus": ["enfoca", "enfocar", "trae", "pon en foco", "poneme"],
}

TASK_MGR_ALIASES = [
    "administrador de tareas",
    "task manager",
    "administrador tareas",
    "taskmgr",
]


def parse_text_offline(raw_text: str) -> ParseResult:
    logging.debug("Parseando en modo offline")
    text = strip_accents(raw_text.lower().strip())
    if not text:
        return ParseResult("none", None, {}, 0.1, "No llegó texto")

    # Task manager detección directa
    for alias in TASK_MGR_ALIASES:
        if alias in text:
            return ParseResult(
                action="open_taskmgr",
                target=None,
                args={},
                confidence=0.9,
                reason="Coincidencia con administrador de tareas",
            )

    detected_action: Optional[str] = None
    detected_target: Optional[str] = None

    for action, synonyms in ACTION_SYNONYMS.items():
        if any(word in text for word in synonyms):
            detected_action = action
            break

    if detected_action is None:
        logging.debug("No se detectó acción, devolviendo none")
        return ParseResult("none", None, {}, 0.2, "No identifiqué la acción")

    # Intentar encontrar target en catálogo
    for app in ACTIONS_CATALOG:
        identifiers = {app["id"].lower()} | {alias.lower() for alias in app["aliases"]}
        if any(identifier in text for identifier in identifiers):
            detected_target = app["id"]
            break

    if detected_action == "open_app" and not detected_target:
        return ParseResult("none", None, {}, 0.3, "No reconocí qué aplicación abrir")

    if detected_action in {"close_app", "minimize", "maximize", "focus"} and not detected_target:
        return ParseResult(detected_action, None, {}, 0.4, "No encontré la app objetivo")

    confidence = 0.6
    if detected_target:
        confidence = 0.85

    return ParseResult(detected_action, detected_target, {}, confidence, "Regla offline aplicada")


def parse_text(text: str) -> ParseResult:
    logging.info("Interpretando orden")
    api_result = interpret_with_api(text)
    if api_result:
        logging.info("Interpretación por API exitosa")
        return api_result
    logging.info("Usando parser offline")
    return parse_text_offline(text)


def interpret_with_api(text: str) -> Optional[ParseResult]:
    payload_text = text.strip()
    if not payload_text:
        return None

    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        try:
            logging.debug("Consultando OpenAI")
            return _interpret_with_openai(payload_text, openai_key)
        except Exception as exc:  # pragma: no cover - ruta de red opcional
            logging.warning("Fallo OpenAI: %s", exc)

    cohere_key = os.getenv("COHERE_API_KEY")
    if cohere_key:
        try:
            logging.debug("Consultando Cohere")
            return _interpret_with_cohere(payload_text, cohere_key)
        except Exception as exc:  # pragma: no cover - ruta de red opcional
            logging.warning("Fallo Cohere: %s", exc)

    return None


def _interpret_with_openai(text: str, api_key: str) -> Optional[ParseResult]:
    url = "https://api.openai.com/v1/chat/completions"
    prompt = (
        "Eres un asistente que interpreta órdenes para controlar ventanas en Windows. "
        "Responde únicamente con un JSON válido que cumpla con el contrato: "
        "{\"action\":...,\"target\":...,\"args\":{},\"confidence\":float,\"reason\":str}. "
        "Las acciones válidas son open_app, close_app, minimize, maximize, focus, open_taskmgr o none. "
        "El target debe ser el id exacto del catálogo interno (whatsapp, discord, chrome) o null. "
        "Si dudas responde action none con baja confianza. Orden: "
        f"{text}"
    )
    body = {
        "model": "gpt-3.5-turbo",
        "messages": [
            {"role": "system", "content": "Responde solo JSON"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise RuntimeError(exc) from exc

    try:
        parsed = json.loads(payload)
        content = parsed["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # pragma: no cover - depende respuesta externa
        raise RuntimeError(f"Respuesta inesperada de OpenAI: {payload}") from exc

    return _coerce_contract(content)


def _interpret_with_cohere(text: str, api_key: str) -> Optional[ParseResult]:
    url = "https://api.cohere.ai/v1/chat"
    body = {
        "model": "command-r",
        "message": (
            "Interpreta la orden y responde solo con JSON del contrato "
            "{\\\"action\\\":...,\\\"target\\\":...,\\\"args\\\":{},\\\"confidence\\\":float,\\\"reason\\\":str}. "
            "Acciones válidas: open_app, close_app, minimize, maximize, focus, open_taskmgr, none. "
            "Orden: " + text
        ),
        "temperature": 0,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise RuntimeError(exc) from exc

    try:
        parsed = json.loads(payload)
        content = parsed["text"][0]["text"].strip()
    except Exception as exc:  # pragma: no cover - depende respuesta externa
        raise RuntimeError(f"Respuesta inesperada Cohere: {payload}") from exc

    return _coerce_contract(content)


def _coerce_contract(content: str) -> Optional[ParseResult]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        logging.warning("La API no devolvió JSON válido: %s", content)
        return None

    if not CONTRACT_KEYS.issubset(data.keys()):
        logging.warning("JSON sin llaves requeridas: %s", data)
        return None

    action = str(data.get("action", "none"))
    if action not in VALID_ACTIONS:
        action = "none"

    target = data.get("target")
    if target is not None:
        target = str(target)
        if not find_app_by_alias(target):
            logging.debug("Target %s fuera de catálogo, anulando", target)
            target = None

    args = data.get("args") or {}
    if not isinstance(args, dict):
        args = {}

    confidence = float(data.get("confidence", 0.5))
    reason = str(data.get("reason", "Interpretación remota"))

    return ParseResult(action, target, args, confidence, reason)


class CommandListener:
    """Escucha eventos de activación por micrófono o teclado."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._preloaded: Optional[str] = None

    def wait_for_activation(self) -> bool:
        logging.info("Esperando activación. Decí 'hola rai', pulsa F8 o escribe en consola.")
        activation_detected = False

        # Intento con teclado F8 (opcional)
        if keyboard is not None:
            logging.debug("Escucha de teclado F8 activa")
            activation_detected = self._wait_keyboard()
        if activation_detected:
            return True

        # Intento con hotword (opcional)
        if sr is not None:
            try:
                activation_detected = self._listen_hotword()
            except Exception as exc:  # pragma: no cover - requiere micro
                logging.warning("No se pudo usar el micrófono: %s", exc)
                activation_detected = False
        if activation_detected:
            return True

        # Fallback manual por consola
        user = input("Escribe 'hola rai' o directamente la orden: ")
        trimmed = user.strip()
        if not trimmed:
            return False
        normalized = strip_accents(trimmed.lower())
        if "hola rai" in normalized:
            return True
        self._preloaded = trimmed
        return True

    def _wait_keyboard(self) -> bool:
        logging.info("Pulsa F8 para activar (ESC para cancelar)")
        start = time.time()
        timeout = 8
        while not self._stop.is_set():
            if keyboard.is_pressed("esc"):
                return False
            if keyboard.is_pressed("f8"):
                time.sleep(0.3)
                return True
            time.sleep(0.1)
            if time.time() - start > timeout:
                return False
        return False

    def _listen_hotword(self) -> bool:
        if sr is None:
            return False
        recognizer = sr.Recognizer()
        with sr.Microphone() as source:
            logging.info("Di 'hola rai'...")
            audio = recognizer.listen(source, timeout=4, phrase_time_limit=3)
        try:
            text = recognizer.recognize_google(audio, language="es-ES")
        except Exception:
            return False
        normalized = strip_accents(text.lower())
        return "hola rai" in normalized

    def stop(self) -> None:
        self._stop.set()

    def consume_preloaded(self) -> Optional[str]:
        value = self._preloaded
        self._preloaded = None
        return value


def capture_command(preloaded: Optional[str] = None) -> Optional[str]:
    if preloaded:
        logging.info("Usando orden ingresada manualmente durante la activación")
        return preloaded
    logging.info("Capturando orden por texto (Enter vacío para cancelar)")
    try:
        command = input("Indica tu orden: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return command or None


def summarize(result: ParseResult) -> str:
    action_map = {
        "open_app": "Abrir",
        "close_app": "Cerrar",
        "minimize": "Minimizar",
        "maximize": "Maximizar",
        "focus": "Enfocar",
        "open_taskmgr": "Task Manager",
        "none": "Sin acción",
    }
    label = action_map.get(result.action, result.action)
    if result.target:
        return f"{label}: {result.target}"
    return label


def main() -> None:
    if os.name != "nt":
        logging.warning("RAI-MINI está diseñado para Windows. Algunas funciones pueden fallar.")

    listener = CommandListener()
    hud = RAIHUD()

    try:
        while True:
            if not listener.wait_for_activation():
                logging.info("Activación cancelada o no detectada")
                continue

            hud.set_state("escuchando")
            hud.show_message("Hola, soy RAI. ¿En qué te puedo ayudar?", typing=True)

            command = capture_command(listener.consume_preloaded())
            if not command:
                hud.set_state("error")
                hud.show_message("No recibí ninguna orden", typing=False)
                hud.schedule_close(2.5)
                continue

            if strip_accents(command.lower()) in {"salir", "exit"}:
                logging.info("Comando de salida recibido")
                hud.set_state("ejecutando")
                hud.show_message("Cerrando RAI", typing=False)
                hud.schedule_close(1.5)
                break

            result = parse_text(command)
            hud.set_state("ejecutando")
            hud.show_message(summarize(result), typing=False)

            if result.action == "none":
                hud.set_state("error")
                hud.show_message(result.reason or "No puedo ejecutar esa acción", typing=False)
                hud.schedule_close(2.5)
                continue

            success, feedback = do_action(result.action, result.target, result.args)
            if success:
                hud.set_state("exito")
                hud.show_message(feedback or "Acción completada", typing=False)
            else:
                hud.set_state("error")
                hud.show_message(feedback or "Ocurrió un error", typing=False)

            hud.schedule_close(2.8)
    except KeyboardInterrupt:
        logging.info("Saliendo por interrupción del usuario")
    finally:
        listener.stop()
        hud.destroy()


if __name__ == "__main__":
    main()
