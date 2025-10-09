# Dependencias opcionales: pip install pywin32 pygetwindow
"""
Acciones concretas para controlar aplicaciones y ventanas en Windows.
"""
from __future__ import annotations

import os
import subprocess
from typing import Dict, List, Optional, Tuple

try:
    import pygetwindow as gw  # type: ignore
except Exception:  # pragma: no cover - dependencia opcional
    gw = None


ACTIONS_CATALOG: List[Dict[str, object]] = [
    {
        "id": "whatsapp",
        "aliases": ["whatsapp", "wa", "whats"],
        "exe_name": "WhatsApp.exe",
        "paths": [
            os.path.expandvars(r"%LOCALAPPDATA%\\WhatsApp\\WhatsApp.exe"),
            os.path.expandvars(r"%PROGRAMFILES%\\WindowsApps\\5319275A.WhatsAppDesktop_8wekyb3d8bbwe\\WhatsApp.exe"),
        ],
        "window_hints": ["WhatsApp"],
    },
    {
        "id": "discord",
        "aliases": ["discord"],
        "exe_name": "Discord.exe",
        "paths": [
            os.path.expandvars(r"%LOCALAPPDATA%\\Discord\\app-1.0.9013\\Discord.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\\Discord\\Update.exe"),
        ],
        "window_hints": ["Discord"],
    },
    {
        "id": "chrome",
        "aliases": ["chrome", "google chrome", "navegador"],
        "exe_name": "chrome.exe",
        "paths": [
            os.path.expandvars(r"%PROGRAMFILES%\\Google\\Chrome\\Application\\chrome.exe"),
            os.path.expandvars(r"%PROGRAMFILES(X86)%\\Google\\Chrome\\Application\\chrome.exe"),
        ],
        "window_hints": ["Chrome", "Google Chrome"],
    },
]


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

    for path in app["paths"]:  # type: ignore[index]
        if path and os.path.exists(path):
            try:
                subprocess.Popen([path])
                return True, f"Abriendo {app['id']}"
            except Exception as exc:
                return False, f"No pude abrir {app['id']}: {exc}"
    return False, "No encontré la aplicación instalada"


def _close_app(target: Optional[str]) -> Tuple[bool, str]:
    if not target:
        return False, "Necesito saber qué cerrar"
    app = find_app_by_alias(target)
    if not app:
        return False, "Aplicación no reconocida"

    exe = app["exe_name"]  # type: ignore[index]
    try:
        result = subprocess.run(
            ["taskkill", "/IM", exe, "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return True, f"Cerré {app['id']}"
        if "no se encuentra" in result.stdout.lower() or "not found" in result.stdout.lower():
            return False, "La aplicación no está en ejecución"
        return False, result.stdout.strip() or result.stderr.strip() or "No se pudo cerrar"
    except Exception as exc:
        return False, f"Error al cerrar: {exc}"


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

    hints = app["window_hints"]  # type: ignore[index]
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

