"""Shim to run the Flask server with ``python server/app.py`` on Windows."""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rai.server.app import app as flask_app  # noqa: E402


if __name__ == "__main__":
    host = os.environ.get("RAI_SERVER_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("RAI_SERVER_PORT", "5050"))
    except ValueError:
        port = 5050
    flask_app.run(host=host, port=port)
