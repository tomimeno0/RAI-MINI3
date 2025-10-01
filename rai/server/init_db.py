"""Initialise the SQLite database and optionally trigger a scan."""
from __future__ import annotations

import logging
from pathlib import Path

from ..client import scanner

logging.basicConfig(level=logging.INFO)


def init(db_path: Path | None = None, run_scan: bool = True) -> None:
    path = db_path or scanner.DB_PATH
    logging.info("Inicializando base de datos en %s", path)
    scanner.ensure_schema(path)
    if run_scan:
        scanner.scan_and_update_db(path)


if __name__ == "__main__":
    init()
