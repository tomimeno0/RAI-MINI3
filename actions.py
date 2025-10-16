# Dependencias opcionales: pip install pywin32 pygetwindow
"""Acciones concretas para controlar aplicaciones y ventanas en Windows."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import pygetwindow as gw  # type: ignore
except Exception:  # pragma: no cover - dependencia opcional
    gw = None


CATALOG_PATH = Path(__file__).with_name("apps.json")


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

    normalized = {
        "id": app_id,
        "aliases": aliases,
        "type": app_type,
        "launch": launch if isinstance(launch, str) and launch.strip() else (paths[0] if paths else None),
        "paths": paths,
        "exe_name": exe_name,
        "window_hints": window_hints,
    }
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

    app_type = app.get("type", "exe")
    launch = app.get("launch")

    if app_type == "uwp":
        if not launch:
            return False, "No tengo el comando para abrir la aplicación"
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
    return False, "No encontré la aplicación instalada"


def _close_app(target: Optional[str]) -> Tuple[bool, str]:
    if not target:
        return False, "Necesito saber qué cerrar"
    app = find_app_by_alias(target)
    if not app:
        return False, "Aplicación no reconocida"

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
        return False, taskkill_message or window_message or "No se pudo cerrar"
    return False, window_message or "Necesito pygetwindow para cerrar esta aplicación"


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
    if gw is None:
        return False, "Instala pygetwindow para controlar ventanas"
    if not target:
        return False, "¿Qué ventana debo manipular?"

    app = find_app_by_alias(target)
    if not app:
        return False, "Aplicación desconocida"

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
                    return True, f"Minimicé {app['id']}"
                if action == "maximize":
                    window.maximize()
                    return True, f"Maximicé {app['id']}"
                if action == "focus":
                    window.activate()
                    return True, f"Puse en foco {app['id']}"
            except Exception as exc:
                return False, f"No pude controlar la ventana: {exc}"
    return False, "No encontré la ventana"

