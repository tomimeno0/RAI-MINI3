"""Windows application executor utilities.

The original executor exposed a parser-focused command dispatcher.  The new API
offers dedicated helpers for controlling desktop and UWP applications.  Each
helper returns a structured dictionary that callers can forward to the HUD.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

try:  # pywin32 is optional when running on Windows hosts.
    import win32con  # type: ignore
    import win32gui  # type: ignore
    import win32process  # type: ignore
except Exception:  # pragma: no cover - executed when pywin32 is absent.
    win32con = win32gui = win32process = None  # type: ignore

if sys.platform.startswith("win"):
    try:
        _USER32 = ctypes.WinDLL("user32", use_last_error=True)
        _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:  # pragma: no cover - safety net for exotic interpreters.
        _USER32 = _KERNEL32 = None
else:  # pragma: no cover - we do not exercise win32 paths on non Windows.
    _USER32 = _KERNEL32 = None

_LOGGER = logging.getLogger(__name__)


ResultDict = Dict[str, Optional[object]]


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _make_result(ok: bool, *, message: str, pid: Optional[int] = None, window: Optional[int] = None) -> ResultDict:
    return {
        "ok": ok,
        "pid": pid,
        "window": f"0x{window:X}" if window else None,
        "message": message,
    }


def _trace_id(target: Dict[str, object]) -> str:
    raw = target.get("trace_id") if isinstance(target, dict) else None
    return str(raw) if raw else f"exec-{uuid.uuid4()}"


def _log(level: int, trace_id: str, message: str, **fields: object) -> None:
    payload = {"trace_id": trace_id, **fields}
    _LOGGER.log(level, "%s | %s", message, payload)


def _validate_windows(trace_id: str) -> Optional[ResultDict]:
    if not _is_windows():
        _log(logging.WARNING, trace_id, "Windows only action", platform=sys.platform)
        return _make_result(False, message="unsupported_platform")
    return None


def open_app(target: Dict[str, object]) -> ResultDict:
    """Launch an executable or UWP application.

    Parameters
    ----------
    target:
        Dictionary describing the application.  Expected keys are:
        - ``exe_path`` (str): path to a desktop executable.
        - ``uwp_package`` or ``aumid`` (str): identifier for UWP apps.
        - ``window_class`` / ``window_title`` / ``process_name`` for window lookup.
    """

    trace_id = _trace_id(target)
    invalid = _validate_windows(trace_id)
    if invalid:
        return invalid

    exe_path = target.get("exe_path")
    aumid = target.get("aumid") or target.get("uwp_package")
    pid: Optional[int] = None

    if exe_path:
        try:
            executable = Path(str(exe_path)).expanduser()
        except Exception:  # pragma: no cover - defensive conversion.
            _log(logging.ERROR, trace_id, "Invalid exe path", exe_path=exe_path)
            return _make_result(False, message="invalid_exe_path")

        if not executable.exists():
            _log(logging.ERROR, trace_id, "Executable not found", path=str(executable))
            return _make_result(False, message="executable_not_found")

        if executable.is_dir():
            _log(logging.ERROR, trace_id, "Executable is directory", path=str(executable))
            return _make_result(False, message="invalid_executable")

        cmd: List[str]
        if executable.suffix.lower() == ".lnk":
            cmd = ["cmd", "/c", "start", "", str(executable)]
        else:
            cmd = [str(executable)]

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(executable.parent),
                shell=False,
            )
            pid = proc.pid
            _log(logging.INFO, trace_id, "Launched executable", pid=pid, command=cmd)
        except FileNotFoundError:
            _log(logging.ERROR, trace_id, "Executable launcher missing", command=cmd)
            return _make_result(False, message="launcher_not_found")
        except Exception as exc:  # pragma: no cover - runtime specific.
            _log(logging.ERROR, trace_id, "Failed to spawn executable", error=str(exc))
            return _make_result(False, message=str(exc))
    elif aumid:
        cmd = ["explorer.exe", f"shell:AppsFolder\\{aumid}"]
        try:
            subprocess.Popen(cmd, shell=False)
            _log(logging.INFO, trace_id, "Launched UWP app", aumid=aumid)
        except Exception as exc:  # pragma: no cover - runtime specific.
            _log(logging.ERROR, trace_id, "Failed to start UWP", error=str(exc), aumid=aumid)
            return _make_result(False, message=str(exc))
    else:
        _log(logging.ERROR, trace_id, "Target missing identifiers")
        return _make_result(False, message="missing_target")

    hwnd = _wait_for_window(target, pid, trace_id)
    if hwnd:
        return _make_result(True, pid=pid, window=hwnd, message="started")
    return _make_result(True, pid=pid, window=None, message="started_no_window")


def close_app(target: Dict[str, object]) -> ResultDict:
    trace_id = _trace_id(target)
    invalid = _validate_windows(trace_id)
    if invalid:
        return invalid

    hwnds = _find_windows(target, trace_id)
    pid = target.get("pid") if isinstance(target.get("pid"), int) else None
    if hwnds:
        hwnd = hwnds[0]
        resolved_pid = _window_pid(hwnd)
        if resolved_pid:
            pid = resolved_pid
        if _send_wm_close(hwnd):
            _log(logging.INFO, trace_id, "Sent WM_CLOSE", hwnd=hwnd, pid=pid)
            return _make_result(True, pid=pid, window=hwnd, message="closed_window")
        _log(logging.WARNING, trace_id, "WM_CLOSE failed", hwnd=hwnd)

    process_name = target.get("process_name")
    if process_name:
        force = bool(target.get("force"))
        ok, stderr = _taskkill(process_name, force)
        if ok:
            _log(logging.INFO, trace_id, "taskkill executed", process_name=process_name, force=force)
            return _make_result(True, pid=pid, window=None, message="terminated")
        _log(logging.ERROR, trace_id, "taskkill failed", error=stderr, process_name=process_name)
        return _make_result(False, pid=pid, window=None, message=stderr or "taskkill_failed")

    return _make_result(False, pid=pid, window=None, message="not_found")


def minimize_app(target: Dict[str, object]) -> ResultDict:
    return _apply_show_window(target, win32con.SW_MINIMIZE if win32con else 6, "minimized")


def maximize_app(target: Dict[str, object]) -> ResultDict:
    return _apply_show_window(target, win32con.SW_MAXIMIZE if win32con else 3, "maximized")


def focus_app(target: Dict[str, object]) -> ResultDict:
    trace_id = _trace_id(target)
    invalid = _validate_windows(trace_id)
    if invalid:
        return invalid

    hwnds = _find_windows(target, trace_id)
    if not hwnds:
        _log(logging.WARNING, trace_id, "Window not found for focus")
        return _make_result(False, message="window_not_found")

    hwnd = hwnds[0]

    if win32gui:
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNORMAL if win32con else 1)
            win32gui.SetForegroundWindow(hwnd)
            _log(logging.INFO, trace_id, "Focused window using pywin32", hwnd=hwnd)
            return _make_result(True, pid=_window_pid(hwnd), window=hwnd, message="focused")
        except Exception as exc:  # pragma: no cover - runtime specific.
            _log(logging.WARNING, trace_id, "pywin32 focus failed", error=str(exc))

    if _USER32:
        try:
            _USER32.ShowWindow(hwnd, 9)  # SW_RESTORE
            _USER32.SetForegroundWindow(hwnd)
            _log(logging.INFO, trace_id, "Focused window using ctypes", hwnd=hwnd)
            return _make_result(True, pid=_window_pid(hwnd), window=hwnd, message="focused")
        except Exception as exc:  # pragma: no cover - runtime specific.
            _log(logging.WARNING, trace_id, "ctypes focus failed", error=str(exc))

    return _make_result(False, window=hwnd, pid=_window_pid(hwnd), message="focus_failed")


def _apply_show_window(target: Dict[str, object], show_flag: int, message: str) -> ResultDict:
    trace_id = _trace_id(target)
    invalid = _validate_windows(trace_id)
    if invalid:
        return invalid

    hwnds = _find_windows(target, trace_id)
    if not hwnds:
        _log(logging.WARNING, trace_id, "Window not found for show")
        return _make_result(False, message="window_not_found")

    hwnd = hwnds[0]
    success = False
    if win32gui:
        try:
            win32gui.ShowWindow(hwnd, show_flag)
            success = True
            _log(logging.INFO, trace_id, "ShowWindow via pywin32", hwnd=hwnd, flag=show_flag)
        except Exception as exc:  # pragma: no cover
            _log(logging.WARNING, trace_id, "pywin32 ShowWindow failed", error=str(exc))

    if not success and _USER32:
        try:
            _USER32.ShowWindow(hwnd, show_flag)
            success = True
            _log(logging.INFO, trace_id, "ShowWindow via ctypes", hwnd=hwnd, flag=show_flag)
        except Exception as exc:  # pragma: no cover
            _log(logging.WARNING, trace_id, "ctypes ShowWindow failed", error=str(exc))

    if not success:
        return _make_result(False, pid=_window_pid(hwnd), window=hwnd, message="show_failed")

    return _make_result(True, pid=_window_pid(hwnd), window=hwnd, message=message)


def _find_windows(target: Dict[str, object], trace_id: str) -> List[int]:
    if not _is_windows() or not target:
        return []

    class_filter = str(target.get("window_class") or "").lower()
    title_filter = str(target.get("window_title") or "").lower()
    process_filter = str(target.get("process_name") or "").lower()
    pid_filter = target.get("pid") if isinstance(target.get("pid"), int) else None

    hwnds: List[int] = []

    def matcher(hwnd: int) -> None:
        if not _is_window_visible(hwnd):
            return

        if class_filter:
            class_name = _window_class(hwnd) or ""
            if class_filter not in class_name.lower():
                return

        if title_filter:
            title = _window_text(hwnd) or ""
            if title_filter not in title.lower():
                return

        pid = _window_pid(hwnd)
        if pid_filter and pid != pid_filter:
            return

        if process_filter:
            process_name = _process_name_from_pid(pid) if pid else ""
            if not process_name or process_name.lower() != process_filter:
                return

        hwnds.append(hwnd)

    _enum_windows(matcher)
    _log(logging.DEBUG, trace_id, "Window search", count=len(hwnds))
    return hwnds


def _enum_windows(callback: Callable[[int], None]) -> None:
    if win32gui:
        win32gui.EnumWindows(lambda hwnd, _: callback(hwnd), 0)
        return

    if not _USER32:
        return

    EnumWindows = _USER32.EnumWindows
    EnumWindows.restype = ctypes.c_bool
    EnumWindows.argtypes = [ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, ctypes.c_void_p), ctypes.c_void_p]

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, ctypes.c_void_p)
    def _callback(hwnd: int, _: int) -> bool:  # pragma: no cover - only on Windows without pywin32
        callback(hwnd)
        return True

    EnumWindows(_callback, 0)


def _window_text(hwnd: int) -> Optional[str]:
    if win32gui:
        try:
            return win32gui.GetWindowText(hwnd)
        except Exception:  # pragma: no cover
            return None

    if not _USER32:
        return None

    GetWindowTextLengthW = _USER32.GetWindowTextLengthW
    GetWindowTextW = _USER32.GetWindowTextW
    length = GetWindowTextLengthW(hwnd)
    if length == 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _window_class(hwnd: int) -> Optional[str]:
    if win32gui:
        try:
            return win32gui.GetClassName(hwnd)
        except Exception:  # pragma: no cover
            return None

    if not _USER32:
        return None

    GetClassNameW = _USER32.GetClassNameW
    buffer = ctypes.create_unicode_buffer(256)
    if GetClassNameW(hwnd, buffer, 256) == 0:  # pragma: no cover
        return None
    return buffer.value


def _window_pid(hwnd: int) -> Optional[int]:
    if win32process:
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return int(pid)
        except Exception:  # pragma: no cover
            return None

    if not _USER32:
        return None

    pid = wintypes.DWORD()
    _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value) if pid.value else None


def _is_window_visible(hwnd: int) -> bool:
    if win32gui:
        return bool(win32gui.IsWindowVisible(hwnd))
    if not _USER32:
        return False
    return bool(_USER32.IsWindowVisible(hwnd))


def _send_wm_close(hwnd: int) -> bool:
    if win32gui and win32con:
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            return True
        except Exception:  # pragma: no cover
            return False

    if not _USER32:
        return False

    WM_CLOSE = 0x0010
    PostMessageW = _USER32.PostMessageW
    return bool(PostMessageW(hwnd, WM_CLOSE, 0, 0))


def _taskkill(process_name: str, force: bool) -> tuple[bool, Optional[str]]:
    cmd = ["taskkill", "/IM", process_name, "/T"]
    if force:
        cmd.insert(1, "/F")
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode == 0, proc.stderr.strip() or proc.stdout.strip()


def _wait_for_window(target: Dict[str, object], pid: Optional[int], trace_id: str, timeout: float = 5.0) -> Optional[int]:
    end_time = time.monotonic() + timeout
    delay = 0.2
    while time.monotonic() < end_time:
        hwnds = _find_windows({**target, "pid": pid} if pid else target, trace_id)
        if hwnds:
            return hwnds[0]
        time.sleep(delay)
        delay = min(delay * 1.5, 0.8)
    _log(logging.DEBUG, trace_id, "Window wait timed out")
    return None


def _process_name_from_pid(pid: Optional[int]) -> Optional[str]:
    if not pid or not _is_windows():
        return None

    query = [
        "wmic",
        "process",
        "where",
        f"ProcessId={pid}",
        "get",
        "Name",
        "/value",
    ]
    proc = subprocess.run(query, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        if line.startswith("Name="):
            return line.split("=", 1)[1].strip()
    return None


__all__ = [
    "open_app",
    "close_app",
    "minimize_app",
    "maximize_app",
    "focus_app",
]
