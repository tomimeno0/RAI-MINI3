"""Initialise the SQLite database and optionally trigger a scan."""
from __future__ import annotations

import logging
from pathlib import Path

from .db_utils import DB_PATH, ensure_schema, scan_and_update_db  # FIX: use shared server utilities

logging.basicConfig(level=logging.INFO)


def init(db_path: Path | None = None, run_scan: bool = True) -> None:
    path = db_path or DB_PATH  # FIX: resolve database path using shared constant
    logging.info("Inicializando base de datos en %s", path)
    ensure_schema(path)  # FIX: prepare schema via shared helper
    if run_scan:
        scan_and_update_db(path)  # FIX: trigger scanner via shared wrapper


if __name__ == "__main__":
    init()
