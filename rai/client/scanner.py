"""Escáner de aplicaciones para el cliente RAI.

Este módulo detecta aplicaciones instaladas en Windows (UWP, ejecutables y
accesos directos) y genera una salida JSON compatible con ``POST /apps/scan``.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import socket
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from .logging_utils import TRACE_HEADER, get_logger, with_trace_id

__all__ = ["scan_apps"]

# ---------------------------------------------------------------------------
# Configuración global
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
CACHE_PATH = BASE_DIR / "scanner_cache.json"

SCAN_DIRECTORIES: Tuple[Path, ...] = (
    Path(os.environ.get("ProgramFiles", "C:/Program Files")),
    Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")),
)
USER_DIR = Path(os.environ.get("USERPROFILE", ""))
if USER_DIR:
    SCAN_DIRECTORIES = SCAN_DIRECTORIES + (
        USER_DIR / "AppData" / "Local" / "Programs",
    )

START_MENU_SHORTCUT_DIRECTORIES: Tuple[Path, ...] = (
    Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
)

SUSPICIOUS_PATTERNS: Tuple[str, ...] = (
    "\\Temp\\",
    "\\Downloads\\",
    "\\AppData\\Local\\Temp",
    "\\Users\\Public\\",
)

LOGGER = get_logger(__name__)


# ---------------------------------------------------------------------------
# Modelos de datos
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class AppInfo:
    """Representa una aplicación detectada en el sistema."""

    name: str
    exe_path: Optional[str]
    uwp_package_fullname: Optional[str]
    source: str
    version: Optional[str]
    publisher: Optional[str]
    icon_path: Optional[str]
    hash: Optional[str]
    scanned_at: str
    security_flag: bool = False
    security_reason: Optional[str] = None

    def identity(self) -> str:
        """Identificador único para deduplicación."""
        if self.uwp_package_fullname:
            return f"uwp::{self.uwp_package_fullname.lower()}"
        if self.exe_path:
            return f"exe::{self.exe_path.lower()}"
        return f"misc::{self.name.lower()}::{self.source}"

    def to_dict(self) -> Dict[str, Optional[str]]:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Funciones utilitarias
# ---------------------------------------------------------------------------
def _utcnow() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _calculate_hash(exe_path: Path) -> Optional[str]:
    try:
        with exe_path.open("rb") as file:
            digest = hashlib.sha256()
            for chunk in iter(lambda: file.read(8192), b""):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"
    except (OSError, PermissionError) as exc:
        LOGGER.debug("No se pudo calcular hash de %s: %s", exe_path, exc)
        return None


def _extract_version_info(exe_path: Path) -> Tuple[Optional[str], Optional[str]]:
    """Obtiene versión y publisher desde los metadatos del ejecutable."""

    try:
        import win32api  # type: ignore
    except ImportError:
        win32api = None

    if win32api is not None:
        try:
            info = win32api.GetFileVersionInfo(str(exe_path), "\\")
            ms = info.get("FileVersionMS")
            ls = info.get("FileVersionLS")
            if ms and ls:
                version = f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
            else:
                version = None

            translations = win32api.GetFileVersionInfo(str(exe_path), "\\VarFileInfo\\Translation")
            publisher = None
            if translations:
                for lang, codepage in translations:
                    str_info_path = f"\\StringFileInfo\\{lang:04X}{codepage:04X}\\CompanyName"
                    try:
                        publisher = win32api.GetFileVersionInfo(str(exe_path), str_info_path)
                    except Exception:  # pragma: no cover - dependiente de SO
                        continue
                    if publisher:
                        break
            return version, publisher
        except Exception as exc:  # pragma: no cover - dependiente de SO
            LOGGER.debug("Fallo win32api para %s: %s", exe_path, exc)

    try:
        import pefile  # type: ignore
    except ImportError:
        pefile = None

    if pefile is not None:
        try:
            pe = pefile.PE(str(exe_path))
            file_info = getattr(pe, "FileInfo", None)
            version = None
            publisher = None
            if file_info:
                for file_info_entry in file_info:
                    if getattr(file_info_entry, "Key", b"") == b"StringFileInfo":
                        for st in file_info_entry.StringTable:
                            if not version:
                                version = st.entries.get(b"FileVersion", b"").decode("utf-8", errors="ignore") or None
                            if not publisher:
                                publisher = st.entries.get(b"CompanyName", b"").decode("utf-8", errors="ignore") or None
            if not version and getattr(pe, "VS_FIXEDFILEINFO", None):
                fixed = pe.VS_FIXEDFILEINFO[0]
                version = f"{fixed.FileVersionMS >> 16}.{fixed.FileVersionMS & 0xFFFF}.{fixed.FileVersionLS >> 16}.{fixed.FileVersionLS & 0xFFFF}"
            pe.close()
            return version, publisher
        except Exception as exc:  # pragma: no cover - dependiente de biblioteca
            LOGGER.debug("Fallo pefile para %s: %s", exe_path, exc)

    return None, None


def _assess_security_risk(exe_path: Optional[Path]) -> Tuple[bool, Optional[str]]:
    if not exe_path:
        return False, None
    exe_str = str(exe_path)
    for pattern in SUSPICIOUS_PATTERNS:
        if pattern.lower() in exe_str.lower():
            reason = f"Ruta sospechosa que contiene '{pattern.strip('\\')}'"
            return True, reason
    return False, None


# ---------------------------------------------------------------------------
# Resolución de accesos directos
# ---------------------------------------------------------------------------
def _resolve_shortcut_with_pywin32(path: Path) -> Optional[str]:  # pragma: no cover - solo Windows
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError:
        return None

    try:
        pythoncom.CoInitialize()
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(str(path))
        target = shortcut.Targetpath or None
    except Exception as exc:
        LOGGER.debug("pywin32 no pudo resolver %s: %s", path, exc)
        target = None
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass
    return target


def _resolve_shortcut_with_ctypes(path: Path) -> Optional[str]:  # pragma: no cover - solo Windows
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None

    CLSID_ShellLink = ctypes.c_byte * 16
    IID_IShellLinkW = ctypes.c_byte * 16
    IID_IPersistFile = ctypes.c_byte * 16

    shell_link = ctypes.c_void_p()
    persist_file = ctypes.c_void_p()

    ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]

    ole32.CoInitialize(None)
    try:
        clsid_shell_link = CLSID_ShellLink(*b"\x00\x21\xC0\x00\x00\x00\x00\x00\xC0\x00\x00\x00\x00\x00\x00\x46")
        iid_shell_link = IID_IShellLinkW(*b"\x00\x00\x00\x00\x00\x00\x00\x00\xC0\x00\x00\x00\x00\x00\x00\x46")
        iid_persist_file = IID_IPersistFile(*b"\x00\x00\x01\x0b\x00\x00\x00\x00\xC0\x00\x00\x00\x00\x00\x00\x46")
        hres = ole32.CoCreateInstance(
            ctypes.byref(clsid_shell_link),
            None,
            1,
            ctypes.byref(iid_shell_link),
            ctypes.byref(shell_link),
        )
        if hres != 0:
            return None
        hres = shell_link.value
        if not hres:
            return None
        class IShellLink(ctypes.Structure):
            pass
        class IPersistFileStruct(ctypes.Structure):
            pass
        IShellLink._fields_ = [("vtable", ctypes.POINTER(ctypes.c_void_p))]
        IPersistFileStruct._fields_ = [("vtable", ctypes.POINTER(ctypes.c_void_p))]
        shell_link_ptr = ctypes.cast(shell_link.value, ctypes.POINTER(IShellLink))
        vtable = shell_link_ptr.contents.vtable
        QueryInterface = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(vtable[0])
        GetPath = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)(vtable[20])
        persist_file_ptr = ctypes.c_void_p()
        if QueryInterface(shell_link_ptr, ctypes.byref(iid_persist_file), ctypes.byref(persist_file_ptr)) != 0:
            return None
        persist_file_struct = ctypes.cast(persist_file_ptr.value, ctypes.POINTER(IPersistFileStruct))
        persist_vtable = persist_file_struct.contents.vtable
        Load = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_uint)(persist_vtable[5])
        if Load(persist_file_struct, str(path), 0) != 0:
            return None
        buffer = ctypes.create_unicode_buffer(260)
        if GetPath(shell_link_ptr, buffer, len(buffer), None, 0) != 0:
            return None
        return buffer.value or None
    except Exception as exc:
        LOGGER.debug("ctypes no pudo resolver %s: %s", path, exc)
        return None
    finally:
        ole32.CoUninitialize()


def _resolve_shortcut(path: Path) -> Optional[str]:
    target = _resolve_shortcut_with_pywin32(path)
    if target:
        return target
    target = _resolve_shortcut_with_ctypes(path)
    if target:
        return target

    powershell_cmd = (
        "powershell",
        "-NoProfile",
        "-Command",
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{str(path)}');$s.TargetPath",
    )
    try:
        proc = subprocess.run(powershell_cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        LOGGER.debug("PowerShell no disponible para resolver %s", path)
        return None
    if proc.returncode == 0:
        target = proc.stdout.strip() or None
        return target
    LOGGER.debug("PowerShell retornó %s al resolver %s", proc.returncode, path)
    return None


# ---------------------------------------------------------------------------
# UWP Apps
# ---------------------------------------------------------------------------
def _run_powershell(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )


def _parse_appxpackage_output(raw: str) -> List[Dict[str, str]]:
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        LOGGER.error("Salida JSON inválida de Get-AppxPackage: %s", exc)
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _collect_uwp_apps() -> List[AppInfo]:  # pragma: no cover - Windows
    command = (
        "Get-AppxPackage | Select-Object PackageFullName, Name, Publisher, Version | "
        "ConvertTo-Json -Depth 2"
    )
    try:
        result = _run_powershell(command)
    except FileNotFoundError:
        LOGGER.warning("PowerShell no disponible; se omite escaneo UWP")
        return []

    if result.returncode != 0:
        LOGGER.warning("Get-AppxPackage falló (%s): %s", result.returncode, result.stderr.strip())
        return []

    entries = _parse_appxpackage_output(result.stdout)
    apps: List[AppInfo] = []
    for entry in entries:
        package = entry.get("PackageFullName")
        name = entry.get("Name") or package
        if not package or not name:
            continue
        apps.append(
            AppInfo(
                name=name,
                exe_path=None,
                uwp_package_fullname=package,
                source="uwp",
                version=entry.get("Version"),
                publisher=entry.get("Publisher"),
                icon_path=None,
                hash=None,
                scanned_at=_utcnow(),
            )
        )
    return apps


# ---------------------------------------------------------------------------
# Ejecutables tradicionales
# ---------------------------------------------------------------------------
def _iter_files_with_extension(directories: Iterable[Path], extension: str) -> Iterator[Path]:
    for directory in directories:
        if not directory or not directory.exists():
            continue
        try:
            for file in directory.rglob(f"*{extension}"):
                if file.is_file():
                    yield file
        except (OSError, PermissionError) as exc:
            LOGGER.debug("No se pudo recorrer %s: %s", directory, exc)


def _build_executable_entry(exe_path: Path, source: str) -> AppInfo:
    version, publisher = _extract_version_info(exe_path)
    sha_hash = _calculate_hash(exe_path)
    suspicious, reason = _assess_security_risk(exe_path)
    return AppInfo(
        name=exe_path.stem,
        exe_path=str(exe_path),
        uwp_package_fullname=None,
        source=source,
        version=version,
        publisher=publisher,
        icon_path=None,
        hash=sha_hash,
        scanned_at=_utcnow(),
        security_flag=suspicious,
        security_reason=reason,
    )


def _collect_executables() -> List[AppInfo]:  # pragma: no cover - Windows
    apps: List[AppInfo] = []
    for exe_file in _iter_files_with_extension(SCAN_DIRECTORIES, ".exe"):
        apps.append(_build_executable_entry(exe_file, source="exe"))
    for shortcut in _iter_files_with_extension(START_MENU_SHORTCUT_DIRECTORIES, ".lnk"):
        target = _resolve_shortcut(shortcut)
        if not target:
            LOGGER.debug("No se pudo resolver shortcut %s", shortcut)
            continue
        target_path = Path(target)
        apps.append(_build_executable_entry(target_path, source="shortcut"))
    return apps


# ---------------------------------------------------------------------------
# Deduplicación y cache
# ---------------------------------------------------------------------------
def _deduplicate(apps: Iterable[AppInfo]) -> List[AppInfo]:
    deduped: Dict[str, AppInfo] = {}
    for app in apps:
        key = app.identity()
        if key not in deduped:
            deduped[key] = app
        else:
            existing = deduped[key]
            if existing.source == "shortcut" and app.source == "exe":
                continue
            deduped[key] = app
    return list(deduped.values())


def _load_cache(cache_path: Path) -> Dict[str, Dict[str, object]]:
    if not cache_path.exists():
        return {}
    try:
        with cache_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict):
                return data
    except json.JSONDecodeError as exc:
        LOGGER.warning("Cache inválida (%s), se ignora", exc)
    except OSError as exc:
        LOGGER.debug("No se pudo leer cache %s: %s", cache_path, exc)
    return {}


def _save_cache(cache_path: Path, apps: Iterable[AppInfo]) -> None:
    cache_payload = {app.identity(): app.to_dict() for app in apps}
    try:
        with cache_path.open("w", encoding="utf-8") as fh:
            json.dump(cache_payload, fh, indent=2)
    except OSError as exc:
        LOGGER.error("No se pudo guardar cache %s: %s", cache_path, exc)


def _filter_incremental(apps: List[AppInfo], cache_path: Path) -> List[AppInfo]:
    cache = _load_cache(cache_path)
    changed: List[AppInfo] = []
    for app in apps:
        key = app.identity()
        cached = cache.get(key)
        if cached != app.to_dict():
            changed.append(app)
    _save_cache(cache_path, apps)
    return changed


# ---------------------------------------------------------------------------
# Función pública
# ---------------------------------------------------------------------------
def scan_apps(full: bool = True, cache_path: Path = CACHE_PATH) -> List[Dict[str, object]]:
    """Realiza un escaneo de aplicaciones instaladas.

    El escaneo identifica aplicaciones UWP y ejecutables tradicionales en
    Windows. En sistemas no Windows se devuelve una lista vacía. Por defecto se
    ejecuta un escaneo completo; si ``full`` es ``False`` se emplea un modo
    incremental que sólo devuelve las aplicaciones nuevas o modificadas
    respecto al contenido de ``cache_path``. En ambos casos el cache se
    actualiza con los resultados más recientes.

    Limitaciones
    ------------
    * El cálculo de hashes y metadatos depende de los permisos de acceso a los
      archivos; si no están disponibles se registran como ``null``.
    * Resolver accesos directos requiere soporte COM, por lo que algunas
      instalaciones pueden no reportar todos los accesos directos.
    * El tiempo típico de ejecución en equipos con catálogos medianos es de
      3-6 segundos, aunque puede extenderse si existen muchos ejecutables.
    """

    trace_id = uuid.uuid4().hex
    with with_trace_id(LOGGER, trace_id) as log:
        if not sys.platform.startswith("win32"):
            log.warning("Escaneo completo disponible únicamente en Windows")
            return []

        log.info("Inicio de escaneo", extra={"mode": "full" if full else "incremental"})
        apps: List[AppInfo] = []
        apps.extend(_collect_executables())
        apps.extend(_collect_uwp_apps())

        deduped = _deduplicate(apps)

        if full:
            _save_cache(cache_path, deduped)
            return [app.to_dict() for app in deduped]

        filtered = _filter_incremental(deduped, cache_path)
        return [app.to_dict() for app in filtered]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_payload(apps: List[Dict[str, object]]) -> Dict[str, object]:
    return {"host": socket.gethostname(), "apps": apps}


def _send_payload(url: str, payload: Dict[str, object]) -> str:
    import urllib.error
    import urllib.request

    trace_id = uuid.uuid4().hex
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", TRACE_HEADER: trace_id},
        method="POST",
    )
    with with_trace_id(LOGGER, trace_id) as log:
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                log.info("Payload enviado", extra={"status": response.status})
        except urllib.error.URLError as exc:
            log.error("Error enviando payload", extra={"url": url, "error": str(exc)})
            raise
    return trace_id


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Escáner de aplicaciones del cliente RAI")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--full", action="store_true", help="Forzar escaneo completo")
    group.add_argument("--incremental", action="store_true", help="Realizar escaneo incremental")
    parser.add_argument("--send", action="store_true", help="Enviar el resultado al endpoint remoto")
    parser.add_argument("--url", help="URL del endpoint POST /apps/scan")
    args = parser.parse_args(argv)

    full_scan = True
    if args.incremental:
        full_scan = False
    if args.full:
        full_scan = True

    apps = scan_apps(full=full_scan)
    payload = _build_payload(apps)
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    if args.send:
        if not args.url:
            parser.error("--send requiere --url")
        _send_payload(args.url, payload)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
