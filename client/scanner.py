"""Shim to invoke the Windows scanner with ``python client/scanner.py``."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rai.client.scanner import main as scanner_main  # noqa: E402


if __name__ == "__main__":
    sys.exit(scanner_main())
