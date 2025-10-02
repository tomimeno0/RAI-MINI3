"""Natural language parser orchestrating Cohere with a rule-based fallback."""
from __future__ import annotations

import json
import logging
import os
import time
import unicodedata
import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .db_utils import DB_PATH, ensure_schema, load_apps  # FIX: source DB helpers from shared server utilities

_LOGGER = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
SYSTEM_PROMPT_FILE = PROMPTS_DIR / "system.txt"
FEWSHOT_FILE = PROMPTS_DIR / "fewshot.jsonl"

ALLOWED_ACTIONS = {
    "abrir_app",
    "minimizar",
    "maximizar",
    "enfocar",
    "cerrar",
    "listar_apps",
    "error",
}
ALLOWED_APP_TYPES = {"EXE", "UWP"}

ALIASES = {
    "whats app": "whatsapp",
    "whatsapp": "whatsapp",
    "wasap": "whatsapp",
    "guasap": "whatsapp",
    "google chrome": "chrome",
    "chrome": "chrome",
    "discord": "discord",
    "administrador tareas": "administrador de tareas",
    "task manager": "administrador de tareas",
}

VERB_ACTIONS = {
    "abrir": "abrir_app",
    "abrime": "abrir_app",
    "abre": "abrir_app",
    "abrí": "abrir_app",
    "cerrar": "cerrar",
    "cerrame": "cerrar",
    "cerrá": "cerrar",
    "cerra": "cerrar",
    "minimizar": "minimizar",
    "minimizame": "minimizar",
    "minimizá": "minimizar",
    "maximizar": "maximizar",
    "maximizame": "maximizar",
    "maximizá": "maximizar",
    "enfocar": "enfocar",
    "enfocame": "enfocar",
    "enfocá": "enfocar",
    "focus": "enfocar",
    "listar": "listar_apps",
    "lista": "listar_apps",
}

LISTAR_PATTERNS = (
    "que apps tengo",
    "qué apps tengo",
    "cuales apps",
    "qué aplicaciones",
)


@dataclass
class ParseResult:
    """Structured payload returned to the client."""

    action: str
    app_name: Optional[str]
    app_type: Optional[str]
    exe_path: Optional[str]
    process_name: Optional[str]
    app_id: Optional[str]
    confidence: float
    speak: str
    notes: str
    args: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "action": self.action,
            "app_name": self.app_name,
            "app_type": self.app_type,
            "exe_path": self.exe_path,
            "process_name": self.process_name,
            "app_id": self.app_id,
            "confidence": float(self.confidence),
            "speak": self.speak,
            "notes": self.notes,
            "args": list(self.args),
        }


def validate_contract(payload: Dict[str, object]) -> bool:
    """Ensure the payload follows the agreed JSON contract."""

    if not isinstance(payload, dict):
        return False

    required = {
        "action",
        "app_name",
        "app_type",
        "exe_path",
        "process_name",
        "app_id",
        "args",
        "confidence",
        "speak",
        "notes",
    }
    if required - payload.keys():
        return False

    if payload["action"] not in ALLOWED_ACTIONS:
        return False

    app_type = payload.get("app_type")
    if app_type is not None and app_type not in ALLOWED_APP_TYPES:
        return False

    if not isinstance(payload.get("args"), list):
        return False

    try:
        float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        return False

    return True


def parse(text: str, apps_catalogue: Optional[Iterable[Dict[str, object]]] = None) -> Dict[str, object]:
    """Parse natural-language commands using Cohere with a rules fallback."""

    ensure_schema(DB_PATH)
    apps = list(apps_catalogue) if apps_catalogue else load_apps(DB_PATH)
    if not apps:
        _LOGGER.warning("Catálogo vacío al parsear")

    orchestrator = _get_cohere_orchestrator()
    normalized = _normalise_text(text)
    if orchestrator:
        result = orchestrator.parse(text, normalized, apps)
        if result and validate_contract(result):
            return result
        if result:
            _LOGGER.warning("Respuesta Cohere fuera de contrato. Activando fallback.")

    return _rule_based_parse(text, normalized, apps)


class CohereOrchestrator:
    """Manage Cohere prompts, retries and graceful degradation."""

    def __init__(self) -> None:
        self._client = None
        self._system_prompt = _load_system_prompt()
        self._fewshots = _load_fewshots()
        self._api_key = os.environ.get("COHERE_API_KEY")
        self._model = os.environ.get("COHERE_MODEL", "command-r")
        self._cohere = _resolve_cohere_module()

        if not self._api_key:
            _LOGGER.info("COHERE_API_KEY no configurada; parser por reglas")
            return

        if not self._cohere:
            _LOGGER.info("Paquete cohere no encontrado; parser por reglas")
            return

        client_cls = getattr(self._cohere, "Client", None)
        if client_cls is None:
            _LOGGER.error("Cliente Cohere no disponible en paquete instalado")
            return

        try:
            self._client = client_cls(self._api_key)
        except Exception as exc:  # pragma: no cover - defensivo
            _LOGGER.exception("No se pudo inicializar Cohere: %%s", exc)
            self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def parse(
        self,
        text: str,
        normalized: str,
        apps: Sequence[Dict[str, object]],
    ) -> Optional[Dict[str, object]]:
        if not self.available:
            return None

        messages = self._build_messages(text, normalized, apps, force_json=False)
        attempt = self._invoke(messages)
        payload = _safe_json_loads(attempt) if attempt else None
        if payload and validate_contract(payload):
            payload.setdefault("notes", "")
            payload["notes"] = f"cohere:{payload['notes']}" if payload.get("notes") else "cohere"
            return payload

        _LOGGER.warning("Respuesta Cohere inválida o fuera de contrato. Reintentando...")
        messages = self._build_messages(text, normalized, apps, force_json=True)
        attempt = self._invoke(messages)
        payload = _safe_json_loads(attempt) if attempt else None
        if payload and validate_contract(payload):
            payload.setdefault("notes", "")
            payload["notes"] = f"cohere_retry:{payload['notes']}" if payload.get("notes") else "cohere_retry"
            return payload

        return None

    def _invoke(self, messages: List[Dict[str, str]]) -> Optional[str]:
        if not self.available:
            return None
        try:
            start = time.time()
            response = self._client.chat(
                model=self._model,
                messages=messages,
                temperature=0.0,
            )
            latency = (time.time() - start) * 1000
            _LOGGER.info("Cohere chat completado en %.0f ms", latency)
        except Exception as exc:  # pragma: no cover - defensivo
            _LOGGER.exception("Error consultando Cohere: %%s", exc)
            return None

        return _extract_cohere_text(response)

    def _build_messages(
        self,
        text: str,
        normalized: str,
        apps: Sequence[Dict[str, object]],
        *,
        force_json: bool,
    ) -> List[Dict[str, str]]:
        catalogue = _format_catalogue(apps)
        user_prompt = (
            f"Orden original: {text}\n"
            f"Texto normalizado: {normalized}\n"
            f"Catálogo disponible (JSON): {catalogue}"
        )
        if force_json:
            user_prompt += "\nDEVUELVE SOLO JSON VÁLIDO."

        messages: List[Dict[str, str]] = []
        if self._system_prompt:
            messages.append({"role": "system", "content": self._system_prompt})
        for example in self._fewshots:
            messages.append({"role": "user", "content": example["input"]})
            messages.append({"role": "assistant", "content": example["output"]})
        messages.append({"role": "user", "content": user_prompt})
        return messages


def _safe_json_loads(raw: Optional[str]) -> Optional[Dict[str, object]]:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        _LOGGER.warning("No se pudo parsear JSON de Cohere: %s", raw)
        return None


_cohere_instance: Optional[CohereOrchestrator] = None


def _get_cohere_orchestrator() -> Optional[CohereOrchestrator]:
    global _cohere_instance
    if _cohere_instance is None:
        _cohere_instance = CohereOrchestrator()
    if _cohere_instance.available:
        return _cohere_instance
    return None


def _rule_based_parse(
    text: str,
    normalized: str,
    apps: Sequence[Dict[str, object]],
) -> Dict[str, object]:
    _LOGGER.info("Usando parser por reglas para: %s", text)

    if _matches_list_request(normalized):
        return ParseResult(
            action="listar_apps",
            app_name=None,
            app_type=None,
            exe_path=None,
            process_name=None,
            app_id=None,
            confidence=0.6,
            speak="Estas son las apps registradas",
            notes="fallback_rules:list",
        ).to_dict()

    action = _detect_action(normalized)
    if not action:
        return build_error("No entendí la acción", text)

    app_entry = _resolve_app(normalized, apps)
    if not app_entry:
        return build_error("No encontré esa app. Probá otro nombre o pedime 'listar'.", text)

    speak_map = {
        "abrir_app": f"Abriendo {app_entry.get('display_name', app_entry['name'])}",
        "cerrar": f"Cerrando {app_entry.get('display_name', app_entry['name'])}",
        "minimizar": f"Minimizando {app_entry.get('display_name', app_entry['name'])}",
        "maximizar": f"Maximizando {app_entry.get('display_name', app_entry['name'])}",
        "enfocar": f"Poniendo {app_entry.get('display_name', app_entry['name'])} en foco",
    }

    result = ParseResult(
        action=action,
        app_name=app_entry.get("name"),
        app_type=app_entry.get("type"),
        exe_path=app_entry.get("exe_path"),
        process_name=app_entry.get("process_name"),
        app_id=app_entry.get("app_id"),
        confidence=0.65,
        speak=speak_map.get(action, "Listo"),
        notes=f"fallback_rules:{action}",
    )
    return result.to_dict()


def build_error(message: str, original_text: str) -> Dict[str, object]:
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
        "notes": f"input: {original_text}",
    }


def _matches_list_request(text: str) -> bool:
    text = text.strip()
    if "lista" in text and "app" in text:
        return True
    return any(pattern in text for pattern in LISTAR_PATTERNS)


def _detect_action(text: str) -> Optional[str]:
    for word in text.split():
        if word in VERB_ACTIONS:
            return VERB_ACTIONS[word]
    if "poner" in text and "foco" in text:
        return "enfocar"
    return None


def _resolve_app(text: str, apps: Sequence[Dict[str, object]]) -> Optional[Dict[str, object]]:
    normalised_words = set(text.split())
    best_match: Optional[Dict[str, object]] = None
    best_score = 0

    for app in apps:
        canonical = (app.get("name") or "").lower()
        if not canonical:
            continue
        candidates = {canonical}
        candidates.update(_alias_variations(canonical))
        for key, value in ALIASES.items():
            if value == canonical:
                candidates.add(key)
        for candidate in candidates:
            candidate_words = candidate.split()
            if candidate in text or all(part in normalised_words for part in candidate_words):
                score = len(candidate)
                if score > best_score:
                    best_score = score
                    best_match = app
                    break
    return best_match


def _alias_variations(name: str) -> Iterable[str]:
    variations = {name.replace(" ", "")}
    if " el " in name:
        variations.add(name.replace(" el ", " "))
    if " la " in name:
        variations.add(name.replace(" la ", " "))
    return variations


def _normalise_text(text: str) -> str:
    text = text.lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.replace("¿", " ").replace("?", " ")
    text = text.replace(" el ", " ").replace(" la ", " ")
    text = text.replace(" los ", " ").replace(" las ", " ")
    text = text.replace(" al ", " ")
    text = " ".join(text.split())
    return text


def _format_catalogue(apps: Sequence[Dict[str, object]]) -> str:
    serialisable = []
    for app in apps:
        serialisable.append(
            {
                "name": app.get("name"),
                "display_name": app.get("display_name"),
                "type": app.get("type"),
                "exe_path": app.get("exe_path"),
                "process_name": app.get("process_name"),
                "app_id": app.get("app_id"),
            }
        )
    return json.dumps(serialisable, ensure_ascii=False)


def _load_system_prompt() -> str:
    try:
        return SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        _LOGGER.error("No se encontró system.txt para Cohere en %s", SYSTEM_PROMPT_FILE)  # FIX: log missing system prompt path
        return ""


def _load_fewshots() -> List[Dict[str, str]]:
    if not FEWSHOT_FILE.exists():
        _LOGGER.error("No se encontró fewshot.jsonl para Cohere en %s", FEWSHOT_FILE)  # FIX: log missing few-shot path
        return []
    examples: List[Dict[str, str]] = []
    for line in FEWSHOT_FILE.read_text(encoding="utf-8").splitlines():
        try:
            obj = json.loads(line)
            if not ("input" in obj and "output" in obj):
                raise ValueError("faltan campos input/output")
            examples.append({"input": str(obj["input"]), "output": str(obj["output"])})
        except Exception as exc:  # pragma: no cover - defensivo
            _LOGGER.error("Ejemplo few-shot inválido: %s", exc)
    return examples


def _extract_cohere_text(response: object) -> Optional[str]:
    if response is None:
        return None
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text
    message = getattr(response, "message", None)
    if message is not None:
        content = getattr(message, "content", None)
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    candidate = item.get("text") or item.get("content")
                    if isinstance(candidate, str) and candidate.strip():
                        return candidate
                else:
                    candidate = getattr(item, "text", None)
                    if isinstance(candidate, str) and candidate.strip():
                        return candidate
        elif isinstance(content, str):
            if content.strip():
                return content
    return None


def _resolve_cohere_module():
    spec = importlib.util.find_spec("cohere")
    if spec is None:
        return None
    module = importlib.import_module("cohere")
    return module


__all__ = ["parse", "build_error", "validate_contract"]
