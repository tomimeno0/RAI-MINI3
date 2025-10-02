"""Shared SQLite helpers for the server components."""  # FIX: provide reusable database utilities
from __future__ import annotations  # FIX: ensure future annotations support

import sqlite3  # FIX: interact with SQLite database
from pathlib import Path  # FIX: handle filesystem paths
from typing import Dict, List  # FIX: provide precise typing aliases

DB_PATH = Path(__file__).resolve().parents[1] / "server" / "apps.sqlite"  # FIX: canonical database path


def ensure_schema(path: Path = DB_PATH) -> None:  # FIX: create schema if missing
    path.parent.mkdir(parents=True, exist_ok=True)  # FIX: ensure directory exists
    with sqlite3.connect(path) as conn:  # FIX: open database connection
        conn.execute(  # FIX: create apps table with expected columns
            """
            CREATE TABLE IF NOT EXISTS apps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                type TEXT NOT NULL,
                exe_path TEXT,
                process_name TEXT,
                app_id TEXT,
                source TEXT DEFAULT 'scan',
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(name, type)
            )
            """
        )
        conn.execute(  # FIX: add unique index for faster lookups
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_apps_name_type ON apps(name, type)"
        )
        conn.commit()  # FIX: persist schema changes


def load_apps(path: Path = DB_PATH) -> List[Dict[str, object]]:  # FIX: fetch catalogue entries
    ensure_schema(path)  # FIX: guard against missing schema
    with sqlite3.connect(path) as conn:  # FIX: open database connection for reads
        conn.row_factory = sqlite3.Row  # FIX: map rows to dict-like objects
        cursor = conn.execute(  # FIX: retrieve all apps ordered by last seen
            "SELECT name, display_name, type, exe_path, process_name, app_id, source, last_seen FROM apps ORDER BY last_seen DESC"
        )
        return [dict(row) for row in cursor.fetchall()]  # FIX: convert rows to dictionaries


def scan_and_update_db(path: Path = DB_PATH) -> None:  # FIX: delegate scanning to client scanner when available
    from ..client import scanner  # FIX: local import to avoid hard dependency at module load

    scanner.ensure_schema(path)  # FIX: reuse client schema logic for compatibility
    scanner.scan_and_update_db(path)  # FIX: trigger scanner update against shared DB
