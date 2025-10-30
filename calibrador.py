"""Manejo seguro del micrófono y reconocimiento de audio para RAI-MINI."""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

import speech_recognition as sr

logger = logging.getLogger("rai.audio")

recognizer = sr.Recognizer()

_calibration_lock = threading.Lock()
_capture_lock = threading.Lock()
_background_guard = threading.Lock()
_calibrated = False


def calibrate_once(duration: float = 1.0) -> bool:
    """Calibra el micrófono solo una vez para ajustar el umbral de ruido."""
    global _calibrated
    with _calibration_lock:
        if _calibrated:
            return True
        try:
            with sr.Microphone() as source:
                logger.info("Calibrando micrófono (duración %.2fs)...", duration)
                recognizer.adjust_for_ambient_noise(source, duration=duration)
            _calibrated = True
            logger.info("Calibración del micrófono completada.")
            return True
        except PermissionError as exc:
            logger.error("Permiso denegado para usar el micrófono: %s", exc)
        except (OSError, AttributeError, ValueError, AssertionError) as exc:
            logger.error("Micrófono no disponible para calibración: %s", exc)
        except Exception as exc:  # pragma: no cover - defensivo
            logger.exception("Error inesperado calibrando el micrófono: %s", exc)
        return False


def capture_phrase(
    timeout: Optional[float] = None,
    phrase_time_limit: Optional[float] = None,
) -> Optional["sr.AudioData"]:
    """
    Captura una frase utilizando un contexto nuevo del micrófono.

    Devuelve AudioData o None si no se consigue audio útil (timeout, permisos, etc.).
    """
    if not _calibrated:
        calibrate_once(duration=1.0)

    with _capture_lock:
        try:
            with sr.Microphone() as source:
                logger.debug(
                    "Escuchando audio (timeout=%s, phrase_limit=%s)...",
                    timeout,
                    phrase_time_limit,
                )
                try:
                    audio = recognizer.listen(
                        source,
                        timeout=timeout,
                        phrase_time_limit=phrase_time_limit,
                    )
                except sr.WaitTimeoutError:
                    logger.warning("No se detectó voz antes del timeout configurado.")
                    return None
                return audio
        except PermissionError as exc:
            logger.error("Permiso denegado para usar el micrófono: %s", exc)
        except (OSError, AttributeError, ValueError, AssertionError) as exc:
            logger.error("No pude acceder al micrófono: %s", exc)
        except Exception as exc:  # pragma: no cover - defensivo
            logger.exception("Error inesperado capturando audio: %s", exc)
        return None


def start_background_listener(
    callback: Callable[[sr.Recognizer, "sr.AudioData"], None],
    *,
    phrase_time_limit: Optional[float] = None,
    timeout: Optional[float] = None,
) -> Callable[[bool], None]:
    """
    Inicia un listener en segundo plano usando hilos propios sin mantener
    fuentes abiertas fuera de un contexto.

    Devuelve una función stop(wait=False) para detener el hilo.
    """
    stop_event = threading.Event()

    def _worker() -> None:
        logger.info("Hilo de escucha en segundo plano iniciado.")
        calibrate_once(duration=1.0)
        while not stop_event.is_set():
            audio = capture_phrase(timeout=timeout, phrase_time_limit=phrase_time_limit)
            if audio is None:
                continue
            try:
                callback(recognizer, audio)
            except Exception as exc:  # pragma: no cover - dependerá del callback
                logger.exception("Callback de fondo falló: %s", exc)
        logger.info("Hilo de escucha en segundo plano detenido.")

    with _background_guard:
        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

    def stop(wait: bool = False) -> None:
        stop_event.set()
        if wait:
            thread.join(timeout=2.0)

    return stop


__all__ = [
    "recognizer",
    "calibrate_once",
    "capture_phrase",
    "start_background_listener",
]
