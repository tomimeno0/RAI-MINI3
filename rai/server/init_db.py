"""Compatibility wrapper around the deployment migration runner."""
from __future__ import annotations

from pathlib import Path

from .db_utils import DB_PATH, ensure_schema, scan_and_update_db


def init(db_path: Path | None = None, run_scan: bool = True) -> None:
    """Initialise the database and optionally trigger a scan."""

    target = db_path or DB_PATH
    ensure_schema(target)
    if run_scan:
        scan_and_update_db(target)


if __name__ == "__main__":
    init()
