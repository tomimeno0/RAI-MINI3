import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rai.server.moduler import interpret


@pytest.fixture()
def sample_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "apps.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE apps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                source TEXT NOT NULL,
                exe_path TEXT,
                uwp_package TEXT,
                aumid TEXT,
                host TEXT,
                last_seen TEXT
            )
            """
        )
        rows = [
            (
                "whatsapp",
                "WhatsApp",
                "exe",
                r"C:\\Program Files\\WhatsApp\\WhatsApp.exe",
                None,
                None,
                "PC-JUAN",
                "2024-05-01T10:00:00",
            ),
            (
                "calculadora",
                "Calculadora",
                "uwp",
                None,
                "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
                "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
                "PC-JUAN",
                "2024-05-01T10:10:00",
            ),
            (
                "discord",
                "Discord",
                "exe",
                r"C:\\Users\\Juan\\AppData\\Local\\Discord\\app.exe",
                None,
                None,
                "PC-JUAN",
                "2024-05-02T08:00:00",
            ),
            (
                "foto editor",
                "Foto Editor Pro",
                "exe",
                r"C:\\Tools\\foto_editor.exe",
                None,
                None,
                "PC-JUAN",
                "2024-05-02T09:00:00",
            ),
            (
                "video editor",
                "Video Editor Pro",
                "exe",
                r"C:\\Tools\\video_editor.exe",
                None,
                None,
                "PC-JUAN",
                "2024-05-02T09:05:00",
            ),
        ]
        conn.executemany(
            "INSERT INTO apps (name, display_name, source, exe_path, uwp_package, aumid, host, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    return db_path


def test_alias_resolves_whatsapp(sample_db: Path) -> None:
    result = interpret("abrime wpp", db_path=sample_db)
    assert result["action"] == "abrir"
    assert result["target"]["source"] == "exe"
    assert result["target"]["exe_path"].endswith("WhatsApp.exe")
    assert result["confidence"] >= 0.6


def test_uwp_and_exe_resolution(sample_db: Path) -> None:
    uwp_result = interpret("abrime la calculadora", db_path=sample_db)
    assert uwp_result["action"] == "abrir"
    assert uwp_result["target"]["source"] == "uwp"
    assert "Calculator" in uwp_result["target"]["uwp_package"]

    exe_result = interpret("cerrame el discord", db_path=sample_db)
    assert exe_result["action"] == "cerrar"
    assert exe_result["target"]["source"] == "exe"
    assert exe_result["target"]["exe_path"].endswith("app.exe")


def test_ambiguous_editor_returns_candidates(sample_db: Path) -> None:
    result = interpret("abrime el editor", db_path=sample_db)
    assert result["action"] == "abrir"
    assert "candidatos" in result["notes"].lower()
    assert result["confidence"] <= 0.45
    # Should list both editor apps in notes
    assert "Foto Editor" in result["notes"]
    assert "Video Editor" in result["notes"]
