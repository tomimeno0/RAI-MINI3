"""Helpers to share SQLite access between server components."""  # FIX: create shared DB utilities
from __future__ import annotations  # FIX: ensure postponed evaluation for type hints
# FIX: import standard modules for database operations
import sqlite3  # FIX: use sqlite3 for persistence
from pathlib import Path  # FIX: resolve repository-relative database path
from typing import Dict, List, Optional  # FIX: typing helpers for callers
# FIX: define canonical database path for both server and client components
DB_PATH = Path(__file__).resolve().parent / "apps.sqlite"  # FIX: canonical database location under server/
# FIX: schema management helper reused across modules
def ensure_schema(path: Path = DB_PATH) -> None:  # FIX: expose schema initialiser
    path.parent.mkdir(parents=True, exist_ok=True)  # FIX: create directory tree for database file
    with sqlite3.connect(path) as conn:  # FIX: open connection to SQLite file
        conn.execute(  # FIX: create table when missing
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
        conn.execute(  # FIX: add unique index for upserts
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_apps_name_type
            ON apps(name, type)
            """
        )
        conn.commit()  # FIX: persist schema changes
# FIX: catalogue loader used by API and parser
def load_apps(path: Path = DB_PATH) -> List[Dict[str, Optional[str]]]:  # FIX: expose catalogue loader
    ensure_schema(path)  # FIX: guarantee schema before reading
    with sqlite3.connect(path) as conn:  # FIX: open read connection
        conn.row_factory = sqlite3.Row  # FIX: access results as dictionaries
        rows = conn.execute(  # FIX: fetch catalogue rows
            "SELECT name, display_name, type, exe_path, process_name, app_id, source, last_seen FROM apps"
        ).fetchall()
    return [dict(row) for row in rows]  # FIX: return plain dictionaries for API use
# FIX: explicitly export helper names
__all__ = ["DB_PATH", "ensure_schema", "load_apps"]  # FIX: limit public surface
