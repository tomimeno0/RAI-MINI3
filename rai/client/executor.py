"""Execute actions returned by the RAI parser on Windows systems."""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, Optional

try:  # pywin32 is optional.
    import win32con  # type: ignore
    import win32gui  # type: ignore
    import win32process  # type: ignore
except Exception:  # pragma: no cover - executed when pywin32 is absent.
    win32con = win32gui = win32process = None  # type: ignore

# Type alias for payloads received from the parser.
ExecutionPayload = Dict[str, Optional[str]]

# Type alias for the metadata returned to the caller.
ExecutionResult = Dict[str, str]

ActionHandler = Callable[[ExecutionPayload], ExecutionResult]

_LOGGER = logging.getLogger(__name__)


def _is_windows() -> bool:
    """Return whether the current interpreter runs on Windows."""

    return sys.platform.startswith("win32")


def execute(action_payload: ExecutionPayload) -> ExecutionResult:
    """Execute an action returned by the parser.

    Parameters
    ----------
    action_payload:
        JSON payload returned by the server. Only the "action" key is mandatory.

    Returns
    -------
    dict
        Dictionary with execution metadata. Useful for logging/tests.
    """

    action = action_payload.get("action")
    if not action:
        return {"status": "ignored", "reason": "no_action"}

    if action == "listar_apps":
        return {"status": "skipped", "reason": "list_handled_client"}

    if not _is_windows():
        _LOGGER.warning("Acciones de ventana solo disponibles en Windows")
        return {"status": "skipped", "reason": "non_windows"}

    # Dispatch table keeps branching predictable as we add simple actions.
    handlers: Dict[str, ActionHandler] = {
        "abrir_app": _open_app,
        "cerrar": _close_app,
    }

    handler = handlers.get(action)
    if handler:
        return handler(action_payload)

    if action in {"minimizar", "maximizar", "enfocar"}:
        return _control_window(action, action_payload)

    return {"status": "ignored", "reason": f"unsupported_action:{action}"}


def _open_app(payload: ExecutionPayload) -> ExecutionResult:
    """Launch desktop or UWP applications based on payload information."""

    app_type = payload.get("app_type")
    exe_path = payload.get("exe_path")
    app_id = payload.get("app_id")

    try:
        if app_type == "EXE" and exe_path:
            executable = Path(exe_path).expanduser()
            if executable.suffix.lower() == ".lnk":
                # Use ``start`` so Windows resolves the shortcut target.
                cmd = ["cmd", "/c", "start", "", str(executable)]
                subprocess.Popen(cmd, shell=False)
            else:
                subprocess.Popen([str(executable)], shell=False)
            return {"status": "ok", "action": "abrir_app"}

        if app_type == "UWP" and app_id:
            cmd = ["cmd", "/c", "start", f"shell:AppsFolder\\{app_id}"]
            subprocess.Popen(cmd, shell=False)
            return {"status": "ok", "action": "abrir_app"}
    except Exception as exc:  # pragma: no cover - depends on OS
        _LOGGER.error("No se pudo abrir la app: %s", exc)
        return {"status": "error", "error": str(exc)}

    return {"status": "error", "error": "datos_incompletos"}


def _close_app(payload: ExecutionPayload) -> ExecutionResult:
    """Terminate a process using ``taskkill`` (soft then forced)."""

    process_name = payload.get("process_name")
    if not process_name:
        return {"status": "error", "error": "process_name_missing"}

    cmd = ["taskkill", "/IM", process_name, "/T"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            _LOGGER.warning("taskkill devolvió %s: %s", proc.returncode, proc.stderr)
            # Retry with /F if graceful terminate failed.
            force_cmd = ["taskkill", "/F", "/IM", process_name, "/T"]
            proc = subprocess.run(force_cmd, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                return {"status": "error", "error": proc.stderr.strip()}
            return {"status": "ok", "mode": "forced"}
        return {"status": "ok", "mode": "soft"}
    except FileNotFoundError:
        return {"status": "error", "error": "taskkill_not_found"}


def _control_window(action: str, payload: ExecutionPayload) -> ExecutionResult:
    """Handle window minimisation/maximisation/focus actions."""

    if win32gui is None or win32con is None:
        _LOGGER.warning("pywin32 requerido para %s", action)
        return {"status": "skipped", "reason": "pywin32_missing"}

    process_name = payload.get("process_name")
    if not process_name:
        return {"status": "error", "error": "process_name_missing"}

    target_hwnd = _find_window_by_process(process_name)
    if not target_hwnd:
        return {"status": "error", "error": "window_not_found"}

    if action == "minimizar":
        win32gui.ShowWindow(target_hwnd, win32con.SW_MINIMIZE)
    elif action == "maximizar":
        win32gui.ShowWindow(target_hwnd, win32con.SW_MAXIMIZE)
    elif action == "enfocar":
        win32gui.ShowWindow(target_hwnd, win32con.SW_SHOWNORMAL)
        win32gui.SetForegroundWindow(target_hwnd)
    else:
        return {"status": "ignored", "reason": "unknown_window_action"}

    return {"status": "ok", "action": action}


def _find_window_by_process(process_name: str) -> Optional[int]:
    """Return the first window handle associated with *process_name*."""

    if win32gui is None or win32process is None:
        return None

    hwnds: list[int] = []
    target_process = process_name.lower()  # Cache lower case to avoid repeats.

    def callback(hwnd: int, _: int) -> None:
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            executable = Path(_process_exe_from_pid(pid)).name
            if executable.lower() == target_process:
                hwnds.append(hwnd)
        except Exception:  # pragma: no cover - defensive
            return

    win32gui.EnumWindows(callback, 0)
    return hwnds[0] if hwnds else None


def _process_exe_from_pid(pid: int) -> str:
    """Return the executable path for a process id using WMIC."""

    query = (
        "wmic", "process", "where", f"ProcessId={pid}", "get", "ExecutablePath", "/value"
    )
    proc = subprocess.run(query, capture_output=True, text=True, check=False)
    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            if line.startswith("ExecutablePath="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(f"No se pudo obtener la ruta para PID {pid}")


__all__ = ["execute"]
