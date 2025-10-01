"""Audio and keyboard input utilities for the RAI mini client.

This module tries to use SpeechRecognition+pyaudio when available to detect the
"hola rai" hotword and capture subsequent commands. When those dependencies are
missing – which is common in sandboxed or CI environments – the module falls
back to a keyboard-driven workflow. The goal is to guarantee that the client
never crashes because audio input is unavailable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

try:  # Optional dependency, keep imports guarded.
    import speech_recognition as sr  # type: ignore
except Exception:  # pragma: no cover - executed when the dependency is missing.
    sr = None  # type: ignore


_LOGGER = logging.getLogger(__name__)


@dataclass
class HotwordResult:
    """Represents the outcome of waiting for the "hola rai" hotword."""

    triggered: bool
    transcript: Optional[str] = None


class AudioInputManager:
    """Manage hotword detection and command acquisition.

    The class is intentionally conservative: it keeps track of whether audio
    capture is supported and gracefully falls back to keyboard input whenever
    something fails.
    """

    HOTWORD = "hola rai"

    def __init__(self) -> None:
        self._recognizer: Optional["sr.Recognizer"] = None
        self._microphone: Optional["sr.Microphone"] = None
        self._audio_enabled = False
        self._keyboard_only = False
        self._init_audio_stack()

    def _init_audio_stack(self) -> None:
        """Attempt to initialise the SpeechRecognition stack."""

        if sr is None:
            _LOGGER.info("SpeechRecognition no disponible; usando modo teclado")
            self._keyboard_only = True
            return

        try:
            self._recognizer = sr.Recognizer()
            self._microphone = sr.Microphone()
            self._audio_enabled = True
            _LOGGER.info("Audio habilitado con SpeechRecognition")
        except Exception as exc:  # pragma: no cover - hardware/driver specific
            _LOGGER.warning("No se pudo inicializar el micrófono: %s", exc)
            self._keyboard_only = True

    @property
    def using_audio(self) -> bool:
        return self._audio_enabled and not self._keyboard_only

    def wait_for_hotword(self) -> HotwordResult:
        """Wait for the hotword. Returns a :class:`HotwordResult`."""

        if self._keyboard_only or not self.using_audio:
            transcript = input(
                "\nPresioná Enter para hablar o escribí tu comando (salir para terminar):\n> "
            ).strip()
            if transcript.lower() == "salir":
                return HotwordResult(triggered=False, transcript="salir")
            if transcript:
                # Interpret typed text as a full command bypassing hotword.
                return HotwordResult(triggered=True, transcript=transcript)
            return HotwordResult(triggered=True, transcript=None)

        assert self._recognizer is not None and self._microphone is not None
        with self._microphone as source:  # pragma: no cover - needs audio device
            self._recognizer.adjust_for_ambient_noise(source, duration=0.5)
            _LOGGER.info("Esperando hotword '%s'", self.HOTWORD)
            try:
                audio = self._recognizer.listen(source, timeout=5, phrase_time_limit=3)
                transcript = self._recognizer.recognize_google(
                    audio, language="es-ES"
                )
                normalized = transcript.lower().strip()
                if self.HOTWORD in normalized:
                    return HotwordResult(triggered=True)
                _LOGGER.debug("Hotword no detectada en '%s'", normalized)
            except sr.WaitTimeoutError as exc:  # pragma: no cover
                _LOGGER.debug("Timeout esperando hotword: %s", exc)
            except Exception as exc:  # pragma: no cover
                _LOGGER.warning("Fallo reconocimiento de hotword: %s", exc)

        return HotwordResult(triggered=False)

    def capture_command(self) -> Optional[str]:
        """Capture the command once the hotword triggered."""

        if self._keyboard_only or not self.using_audio:
            command = input("hola, ¿qué querés?\n> ").strip()
            return command or None

        assert self._recognizer is not None and self._microphone is not None
        with self._microphone as source:  # pragma: no cover
            print("hola, ¿qué querés?")
            _LOGGER.info("Escuchando comando tras hotword")
            audio = self._recognizer.listen(source, timeout=6, phrase_time_limit=5)
            try:
                transcript = self._recognizer.recognize_google(
                    audio, language="es-ES"
                )
                return transcript.strip()
            except Exception as exc:
                _LOGGER.warning("Fallo al reconocer el comando: %s", exc)
                return None

    def close(self) -> None:
        """Release audio resources, if any."""

        if self._microphone is not None:
            try:
                self._microphone.__exit__(None, None, None)
            except AttributeError:
                # Microphone does not define __exit__; ignore.
                pass
        self._audio_enabled = False


__all__ = ["AudioInputManager", "HotwordResult"]
