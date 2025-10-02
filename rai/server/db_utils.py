"""Shared SQLite helpers for the server components."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, List

from server.init_db import DEFAULT_DB_PATH as DEFAULT_SERVER_DB_PATH, apply_migrations, get_logger

DB_PATH = Path(DEFAULT_SERVER_DB_PATH)
_LOGGER = get_logger()


def ensure_schema(path: Path = DB_PATH) -> None:
    """Ensure the SQLite schema is present by applying migrations."""

    target = Path(path) if path else DB_PATH
    apply_migrations(db_path=target, logger=_LOGGER)


def load_apps(path: Path = DB_PATH) -> List[Dict[str, object]]:
    """Return the list of active installs for the parser catalogue."""

    ensure_schema(path)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """
            SELECT
                lower(a.display_name) AS normalized_name,
                a.display_name,
                CASE WHEN i.package_id IS NOT NULL THEN 'UWP' ELSE 'EXE' END AS app_type,
                b.exe_path,
                NULL AS process_name,
                p.package_fullname AS app_id,
                i.source,
                i.last_seen_at,
                h.hostname
            FROM installs i
            JOIN hosts h ON h.id = i.host_id
            JOIN apps_catalog a ON a.id = i.app_catalog_id
            LEFT JOIN packages p ON p.id = i.package_id
            LEFT JOIN binaries b ON b.id = i.binary_id
            WHERE i.is_active = 1 AND i.removed_at IS NULL
            ORDER BY i.last_seen_at DESC
            """
        )
        apps: List[Dict[str, object]] = []
        for row in cursor.fetchall():
            apps.append(
                {
                    "name": row["normalized_name"],
                    "display_name": row["display_name"],
                    "type": row["app_type"],
                    "exe_path": row["exe_path"],
                    "process_name": row["process_name"],
                    "app_id": row["app_id"],
                    "source": row["source"],
                    "last_seen": row["last_seen_at"],
                    "hostname": row["hostname"],
                }
            )
        return apps


def scan_and_update_db(path: Path = DB_PATH) -> None:
    """Run the client scanner and persist results into the shared database."""

    ensure_schema(path)
    from ..client import scanner  # Lazy import to avoid Windows-only deps at import time

    if hasattr(scanner, "scan_and_update_db"):
        scanner.scan_and_update_db(path)
    else:  # pragma: no cover - compatibility shim until scanner grows DB integration
        _LOGGER.warning("Scanner module does not expose scan_and_update_db")

