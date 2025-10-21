# Dependencias opcionales: pip install pywin32 pygetwindow
"""Acciones concretas para controlar aplicaciones y ventanas en Windows."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any, Dict, Iterable, List, Optional, Tuple, TypedDict, cast

try:
    import pygetwindow as gw  # type: ignore
except Exception:  # pragma: no cover - dependencia opcional
    gw = None


CATALOG_PATH = Path(__file__).with_name("apps.json")


class ActionResponse(TypedDict):
    """Respuesta estandarizada para operaciones sobre aplicaciones."""

    ok: bool
    msg: str


def _unique_strings(values: Iterable[object]) -> List[str]:
    seen = set()
    cleaned: List[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        if not stripped or stripped.lower() in seen:
            continue
        seen.add(stripped.lower())
        cleaned.append(stripped)
    return cleaned


def _normalize_entry(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    app_id = str(data.get("id", "")).strip()
    if not app_id:
        return None

    raw_aliases = data.get("aliases") or []
    if isinstance(raw_aliases, str):
        raw_aliases = [raw_aliases]
    aliases = _unique_strings([app_id, *raw_aliases])

    app_type = str(data.get("type", "exe")).strip().lower() or "exe"

    window_hints_raw = data.get("window_hints") or []
    if isinstance(window_hints_raw, str):
        window_hints_raw = [window_hints_raw]
    window_hints = _unique_strings(window_hints_raw) or [app_id]

    raw_paths: Iterable[object]
    if isinstance(data.get("paths"), list):
        raw_paths = data.get("paths", [])  # type: ignore[assignment]
    else:
        raw_paths = []

    launch = data.get("launch")
    paths: List[str] = []
    for candidate in list(raw_paths):
        if isinstance(candidate, str) and candidate.strip():
            paths.append(candidate.strip())
    if isinstance(launch, str) and launch.strip() and launch.strip() not in paths:
        paths.insert(0, launch.strip())

    exe_name: Optional[str] = None
    if app_type != "uwp":
        for path in paths:
            expanded = os.path.expandvars(path)
            candidate = os.path.basename(expanded)
            if candidate:
                exe_name = candidate
                break

    raw_actions = data.get("actions") or data.get("acciones") or {}
    actions: Dict[str, List[str]] = {}
    if isinstance(raw_actions, dict):
        for raw_key, raw_value in raw_actions.items():
            if not isinstance(raw_key, str):
                continue
            key = raw_key.strip().lower()
            if not key:
                continue
            commands: List[str] = []
            if isinstance(raw_value, str):
                value = raw_value.strip()
                if value:
                    commands.append(value)
            elif isinstance(raw_value, (list, tuple)):
                for item in raw_value:
                    if isinstance(item, str) and item.strip():
                        commands.append(item.strip())
            if commands:
                actions[key] = commands

    normalized = {
        "id": app_id,
        "aliases": aliases,
        "type": app_type,
        "launch": launch if isinstance(launch, str) and launch.strip() else (paths[0] if paths else None),
        "paths": paths,
        "exe_name": exe_name,
        "window_hints": window_hints,
    }
    if actions:
        normalized["actions"] = actions
    return normalized


def _load_catalog_from_json() -> List[Dict[str, Any]]:
    if not CATALOG_PATH.exists():
        return []
    try:
        with CATALOG_PATH.open("r", encoding="utf-8") as fh:
            raw_data = json.load(fh)
    except Exception:  # pragma: no cover - lectura defensiva
        return []

    if isinstance(raw_data, dict):
        raw_entries = [raw_data]
    elif isinstance(raw_data, list):
        raw_entries = raw_data
    else:
        return []

    catalog: List[Dict[str, Any]] = []
    seen_ids = set()
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        normalized = _normalize_entry(entry)
        if not normalized:
            continue
        if normalized["id"] in seen_ids:
            continue
        seen_ids.add(normalized["id"])
        catalog.append(normalized)
    return catalog


def _default_catalog() -> List[Dict[str, Any]]:
    defaults = [
        {
            "id": "whatsapp",
            "aliases": ["whatsapp", "wa", "whats"],
            "type": "exe",
            "paths": [
                r"%LOCALAPPDATA%\\WhatsApp\\WhatsApp.exe",
                r"%PROGRAMFILES%\\WindowsApps\\5319275A.WhatsAppDesktop_8wekyb3d8bbwe\\WhatsApp.exe",
            ],
            "window_hints": ["WhatsApp"],
        },
        {
            "id": "discord",
            "aliases": ["discord"],
            "type": "exe",
            "paths": [
                r"%LOCALAPPDATA%\\Discord\\app-1.0.9013\\Discord.exe",
                r"%LOCALAPPDATA%\\Discord\\Update.exe",
            ],
            "window_hints": ["Discord"],
        },
        {
            "id": "chrome",
            "aliases": ["chrome", "google chrome", "navegador"],
            "type": "exe",
            "paths": [
                r"%PROGRAMFILES%\\Google\\Chrome\\Application\\chrome.exe",
                r"%PROGRAMFILES(X86)%\\Google\\Chrome\\Application\\chrome.exe",
            ],
            "window_hints": ["Chrome", "Google Chrome"],
        },
    ]
    catalog = []
    seen_ids = set()
    for entry in defaults:
        normalized = _normalize_entry(entry)
        if not normalized:
            continue
        if normalized["id"] in seen_ids:
            continue
        seen_ids.add(normalized["id"])
        catalog.append(normalized)
    return catalog


ACTIONS_CATALOG: List[Dict[str, Any]] = _load_catalog_from_json() or _default_catalog()


def find_app_by_alias(alias: str) -> Optional[Dict[str, object]]:
    alias_lower = alias.lower()
    for app in ACTIONS_CATALOG:
        identifiers = {app["id"].lower()} | {a.lower() for a in app["aliases"]}
        if alias_lower in identifiers:
            return app
    return None


def _actions_map(app: Dict[str, Any]) -> Dict[str, List[str]]:
    actions = app.get("actions")
    if isinstance(actions, dict):
        return actions
    return {}


def _select_action_commands(app: Dict[str, Any], keys: Iterable[str]) -> List[str]:
    actions = _actions_map(app)
    for key in keys:
        normalized = str(key).strip().lower()
        if not normalized:
            continue
        commands = actions.get(normalized)
        if isinstance(commands, list) and commands:
            return commands
    return []


_CONTROL_ACTION_ALIASES: Dict[str, Tuple[str, ...]] = {
    "minimize": ("minimize", "minimizar", "ocultar"),
    "maximize": ("maximize", "maximizar"),
    "focus": ("focus", "enfocar", "activar", "mostrar"),
}


def _run_command(command: str, *, wait: bool) -> Tuple[bool, str]:
    normalized = command.strip()
    if not normalized:
        return False, "Comando vacio"
    try:
        if wait:
            result = subprocess.run(normalized, shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                salida = (result.stdout or "").strip()
                return True, salida
            error = (result.stderr or "").strip() or (result.stdout or "").strip() or f"Fallo ({result.returncode})"
            return False, error
        try:
            parts = shlex.split(normalized, posix=False)
        except ValueError:
            parts = []
        if parts:
            executable = os.path.expandvars(parts[0].strip('"'))
            if os.path.isfile(executable):
                parts[0] = executable
                subprocess.Popen(parts)
                return True, ""
        path_candidate = os.path.expandvars(normalized.strip('"'))
        if os.path.isfile(path_candidate):
            subprocess.Popen([path_candidate])
            return True, ""
        subprocess.Popen(normalized, shell=True)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _run_commands(commands: Iterable[str], *, wait: bool) -> Tuple[bool, str]:
    last_message = ""
    for command in commands:
        ok, message = _run_command(command, wait=wait)
        if not ok:
            return False, message
        if message:
            last_message = message
    return True, last_message


def _control_success_message(action: str, app_id: str) -> str:
    if action == "minimize":
        return f"Minimicé {app_id}"
    if action == "maximize":
        return f"Maximicé {app_id}"
    if action == "focus":
        return f"Puse en foco {app_id}"
    return f"{action} {app_id}"


def do_action(action: str, target: Optional[str], args: Dict[str, object]) -> Tuple[bool, str]:
    if os.name != "nt":
        return False, "Solo disponible en Windows"

    if action == "open_taskmgr":
        return _open_task_manager()

    if action == "open_app":
        return _open_app(target)
    if action == "close_app":
        return _close_app(target)
    if action == "minimize":
        return _control_window(target, "minimize")
    if action == "maximize":
        return _control_window(target, "maximize")
    if action == "focus":
        return _control_window(target, "focus")

    return False, "Acción no implementada"


def _open_app(target: Optional[str]) -> Tuple[bool, str]:
    if not target:
        return False, "Necesito saber qué aplicación abrir"
    app = find_app_by_alias(target)
    if not app:
        return False, "Aplicación no reconocida"

    action_error: Optional[str] = None
    action_commands = _select_action_commands(app, ("abrir", "open", "launch", "iniciar", "start"))
    if action_commands:
        ok, message = _run_commands(action_commands, wait=False)
        if ok:
            return True, message or f"Abriendo {app['id']}"
        action_error = message or "No pude ejecutar el comando registrado"

    app_type = app.get("type", "exe")
    launch = app.get("launch")

    if app_type == "uwp":
        if not launch:
            return False, action_error or "No tengo el comando para abrir la aplicación"
        try:
            subprocess.Popen(str(launch), shell=True)
            return True, f"Abriendo {app['id']}"
        except Exception as exc:
            return False, f"No pude abrir {app['id']}: {exc}"

    candidate_paths: List[str] = []
    for raw_path in app.get("paths", []):  # type: ignore[index]
        if isinstance(raw_path, str) and raw_path:
            candidate_paths.append(os.path.expandvars(raw_path))
    if isinstance(launch, str) and launch:
        expanded = os.path.expandvars(launch)
        if expanded not in candidate_paths:
            candidate_paths.insert(0, expanded)

    for path in candidate_paths:
        if not path:
            continue
        if not os.path.exists(path):
            continue
        try:
            subprocess.Popen([path])
            return True, f"Abriendo {app['id']}"
        except Exception as exc:
            return False, f"No pude abrir {app['id']}: {exc}"

    if candidate_paths:
        try:
            subprocess.Popen([candidate_paths[0]])
            return True, f"Abriendo {app['id']}"
        except Exception:
            pass
    return False, action_error or "No encontré la aplicación instalada"


def _close_app(target: Optional[str]) -> Tuple[bool, str]:
    if not target:
        return False, "Necesito saber qué cerrar"
    app = find_app_by_alias(target)
    if not app:
        return False, "Aplicación no reconocida"

    action_error: Optional[str] = None
    action_commands = _select_action_commands(app, ("cerrar", "close", "terminate", "detener", "stop", "salir"))
    if action_commands:
        ok, message = _run_commands(action_commands, wait=True)
        if ok:
            return True, message or f"Cerré {app['id']}"
        action_error = message or "No pude ejecutar el comando registrado"

    exe = app.get("exe_name")
    taskkill_message: Optional[str] = None
    if exe:
        try:
            result = subprocess.run(
                ["taskkill", "/IM", exe, "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                return True, f"Cerré {app['id']}"
            stdout = (result.stdout or "").lower()
            if "no se encuentra" in stdout or "not found" in stdout:
                taskkill_message = "La aplicación no está en ejecución"
            else:
                taskkill_message = (
                    result.stdout.strip()
                    or result.stderr.strip()
                    or "No se pudo cerrar"
                )
        except Exception as exc:
            taskkill_message = f"Error al cerrar: {exc}"

    window_closed, window_message = _close_by_window(app)
    if window_closed:
        return True, window_message

    if exe:
        return False, taskkill_message or action_error or window_message or "No se pudo cerrar"
    return False, action_error or window_message or "Necesito pygetwindow para cerrar esta aplicación"


def _close_by_window(app: Dict[str, Any]) -> Tuple[bool, str]:
    if gw is None:
        return False, "Instala pygetwindow para controlar ventanas"

    hints = app.get("window_hints") or []
    for hint in hints:
        try:
            windows = gw.getWindowsWithTitle(hint)
        except Exception as exc:  # pragma: no cover
            return False, f"No pude acceder a ventanas: {exc}"
        closed_any = False
        for window in windows:
            if not window:
                continue
            try:
                window.close()
                closed_any = True
            except Exception as exc:
                return False, f"No pude cerrar la ventana: {exc}"
        if closed_any:
            return True, f"Cerré {app['id']}"
    return False, "No encontré la ventana abierta"


def _open_task_manager() -> Tuple[bool, str]:
    try:
        subprocess.Popen(["taskmgr"])
        return True, "Abriendo Administrador de tareas"
    except Exception as exc:
        return False, f"No pude abrir Task Manager: {exc}"


def _control_window(target: Optional[str], action: str) -> Tuple[bool, str]:
    if not target:
        return False, "¿Qué ventana debo manipular?"

    app_obj = find_app_by_alias(target)
    if not app_obj:
        return False, "Aplicación desconocida"
    app = cast(Dict[str, Any], app_obj)
    app_id = str(app.get("id") or target)

    alias_keys = _CONTROL_ACTION_ALIASES.get(action, (action,))
    custom_error: Optional[str] = None
    custom_commands = _select_action_commands(app, alias_keys)
    if custom_commands:
        ok, message = _run_commands(custom_commands, wait=False)
        if ok:
            return True, message or _control_success_message(action, app_id)
        custom_error = message or "No pude ejecutar el comando registrado"

    if gw is None:
        return False, custom_error or "Instala pygetwindow para controlar ventanas"

    hints = app.get("window_hints") or []
    for hint in hints:
        try:
            windows = gw.getWindowsWithTitle(hint)
        except Exception as exc:  # pragma: no cover
            return False, f"No pude acceder a ventanas: {exc}"
        for window in windows:
            if not window:
                continue
            try:
                if action == "minimize":
                    window.minimize()
                    return True, _control_success_message(action, app_id)
                if action == "maximize":
                    window.maximize()
                    return True, _control_success_message(action, app_id)
                if action == "focus":
                    window.activate()
                    return True, _control_success_message(action, app_id)
            except Exception as exc:
                return False, f"No pude controlar la ventana: {exc}"
    return False, custom_error or "No encontré la ventana"


def _resolve_path_from_app(app: Dict[str, Any]) -> str:
    launch = app.get("launch")
    if isinstance(launch, str) and launch.strip():
        return os.path.expandvars(launch.strip())
    for candidate in app.get("paths", []):  # type: ignore[index]
        if isinstance(candidate, str) and candidate.strip():
            return os.path.expandvars(candidate.strip())
    return ""


def _select_process_hint(app: Dict[str, Any]) -> str:
    exe_name = app.get("exe_name")
    if isinstance(exe_name, str) and exe_name.strip():
        return exe_name.strip()
    for hint in app.get("window_hints") or []:
        if isinstance(hint, str) and hint.strip():
            return hint.strip()
    path_value = _resolve_path_from_app(app)
    if path_value:
        return Path(path_value).stem
    identifier = app.get("id")
    if isinstance(identifier, str):
        return identifier
    return ""


def open_app(app_key: str) -> ActionResponse:
    """Abre una aplicacion conocida y estandariza la respuesta."""
    try:
        ok, msg = _open_app(app_key)
    except Exception as exc:  # noqa: BLE001
        ok = False
        msg = f"Error inesperado al abrir {app_key}: {exc}"
    return {"ok": ok, "msg": msg}


def minimize_app(app_key: str) -> ActionResponse:
    """Minimiza una aplicacion identificada por su clave o alias."""
    try:
        ok, msg = _control_window(app_key, "minimize")
    except Exception as exc:  # noqa: BLE001
        ok = False
        msg = f"Error inesperado al minimizar {app_key}: {exc}"
    return {"ok": ok, "msg": msg}


def close_app(app_key: str) -> ActionResponse:
    """Cierra una aplicacion activa y devuelve el estado de la operacion."""
    try:
        ok, msg = _close_app(app_key)
    except Exception as exc:  # noqa: BLE001
        ok = False
        msg = f"Error inesperado al cerrar {app_key}: {exc}"
    return {"ok": ok, "msg": msg}


def list_known_apps() -> Dict[str, Dict[str, Any]]:
    """Devuelve el indice de aplicaciones conocidas listo para consulta."""
    index: Dict[str, Dict[str, Any]] = {}
    for app in ACTIONS_CATALOG:
        key = str(app.get("id", "")).strip()
        if not key:
            continue
        raw_aliases = app.get("aliases") or []
        if isinstance(raw_aliases, str):
            raw_aliases = [raw_aliases]
        friendly = _unique_strings(raw_aliases)
        index[key] = {
            "key": key,
            "friendly": friendly,
            "process": _select_process_hint(app),
            "path": _resolve_path_from_app(app),
        }
    return index
