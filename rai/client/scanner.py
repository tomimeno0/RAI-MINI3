"""Application scanner for the RAI mini client."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

_LOGGER = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parents[1] / "server_db" / "apps.sqlite"


@dataclass
class AppRecord:
    name: str
    display_name: str
    type: str
    exe_path: Optional[str]
    process_name: Optional[str]
    app_id: Optional[str]
    source: str = "scan"

    def to_row(self) -> Dict[str, Optional[str]]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "type": self.type,
            "exe_path": self.exe_path,
            "process_name": self.process_name,
            "app_id": self.app_id,
            "source": self.source,
            "last_seen": datetime.utcnow().isoformat(timespec="seconds"),
        }


def normalise(text: str) -> str:
    text = text.lower().strip()
    text = text.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    for article in (" el ", " la ", " los ", " las "):
        text = text.replace(article, " ")
    return " ".join(text.split())


def ensure_schema(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS apps (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                type TEXT NOT NULL,
                exe_path TEXT,
                process_name TEXT,
                app_id TEXT,
                source TEXT,
                last_seen TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_apps_name_type
            ON apps(name, type)
            """
        )
        conn.commit()


def scan_and_update_db(db_path: Path = DB_PATH) -> List[Dict[str, Optional[str]]]:
    ensure_schema(db_path)
    apps: List[AppRecord] = []

    if sys.platform.startswith("win32"):
        apps.extend(_scan_executables())
        apps.extend(_scan_uwp_apps())
    else:
        _LOGGER.warning("Escaneo real solo disponible en Windows; usando catálogo base")

    apps.extend(_baseline_entries())

    _upsert_records(apps, db_path)
    return [record.to_row() for record in apps]


def _baseline_entries() -> Iterable[AppRecord]:
    base = {
        "whatsapp": AppRecord(
            name="whatsapp",
            display_name="WhatsApp",
            type="UWP",
            exe_path=None,
            process_name="WhatsApp.exe",
            app_id=None,
            source="baseline",
        ),
        "discord": AppRecord(
            name="discord",
            display_name="Discord",
            type="EXE",
            exe_path=None,
            process_name="Discord.exe",
            app_id=None,
            source="baseline",
        ),
        "chrome": AppRecord(
            name="chrome",
            display_name="Google Chrome",
            type="EXE",
            exe_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
            process_name="chrome.exe",
            app_id=None,
            source="baseline",
        ),
        "administrador de tareas": AppRecord(
            name="administrador de tareas",
            display_name="Administrador de tareas",
            type="EXE",
            exe_path="C:\\Windows\\System32\\taskmgr.exe",
            process_name="Taskmgr.exe",
            app_id=None,
            source="baseline",
        ),
    }
    return base.values()


def _scan_executables() -> List[AppRecord]:  # pragma: no cover - Windows specific
    paths = []
    env_keys = [
        "ProgramFiles",
        "ProgramFiles(x86)",
        "AppData",
        "ProgramData",
    ]
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        paths.append(Path(userprofile) / "Desktop")
    for key in env_keys:
        base = os.environ.get(key)
        if not base:
            continue
        if key == "AppData":
            paths.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
        elif key == "ProgramData":
            paths.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
        else:
            paths.append(Path(base))

    results: List[AppRecord] = []
    for root in paths:
        if not root.exists():
            continue
        for ext in (".lnk", ".exe"):
            for file in root.rglob(f"*{ext}"):
                if ext == ".lnk":
                    target = _resolve_shortcut(file)
                    process_name = Path(target).name if target else file.name
                    exe_path = target or str(file)
                else:
                    exe_path = str(file)
                    process_name = file.name
                name = normalise(file.stem)
                results.append(
                    AppRecord(
                        name=name,
                        display_name=file.stem,
                        type="EXE",
                        exe_path=exe_path,
                        process_name=process_name,
                        app_id=None,
                    )
                )
    return results


def _scan_uwp_apps() -> List[AppRecord]:  # pragma: no cover - Windows specific
    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        "Get-StartApps | ConvertTo-Json -Depth 2",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        _LOGGER.warning("PowerShell no disponible para escanear apps UWP")
        return []

    if proc.returncode != 0 or not proc.stdout.strip():
        _LOGGER.warning("Get-StartApps falló: %s", proc.stderr.strip())
        return []

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        _LOGGER.error("No se pudo parsear salida de Get-StartApps: %s", exc)
        return []

    if isinstance(data, dict):
        data = [data]

    results: List[AppRecord] = []
    for entry in data:
        display_name = entry.get("Name") or entry.get("AppName")
        app_id = entry.get("AppId") or entry.get("Aumid")
        if not display_name or not app_id:
            continue
        name = normalise(display_name)
        results.append(
            AppRecord(
                name=name,
                display_name=display_name,
                type="UWP",
                exe_path=None,
                process_name=None,
                app_id=app_id,
            )
        )
    return results


def _resolve_shortcut(path: Path) -> Optional[str]:  # pragma: no cover
    script = (
        "powershell",
        "-NoProfile",
        "-Command",
        f"(New-Object -ComObject WScript.Shell).CreateShortcut('{path}').TargetPath",
    )
    try:
        proc = subprocess.run(script, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        _LOGGER.debug("PowerShell no encontrado al resolver shortcut")
        return None
    if proc.returncode == 0:
        target = proc.stdout.strip()
        return target or None
    return None


def _upsert_records(apps: Iterable[AppRecord], db_path: Path) -> None:
    rows = [app.to_row() for app in apps]
    with sqlite3.connect(db_path) as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO apps(name, display_name, type, exe_path, process_name, app_id, source, last_seen)
                VALUES(:name, :display_name, :type, :exe_path, :process_name, :app_id, :source, :last_seen)
                ON CONFLICT(name, type) DO UPDATE SET
                    display_name=excluded.display_name,
                    exe_path=excluded.exe_path,
                    process_name=excluded.process_name,
                    app_id=excluded.app_id,
                    source=excluded.source,
                    last_seen=excluded.last_seen
                """,
                row,
            )
        conn.commit()


def load_apps(db_path: Path = DB_PATH) -> List[Dict[str, Optional[str]]]:
    ensure_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT name, display_name, type, exe_path, process_name, app_id, source, last_seen FROM apps"
        ).fetchall()
    return [dict(row) for row in rows]


__all__ = ["scan_and_update_db", "load_apps", "DB_PATH"]
