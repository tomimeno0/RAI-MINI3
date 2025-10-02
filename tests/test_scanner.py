import json
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rai.client import scanner


def test_parse_appxpackage_output_handles_list():
    raw = json.dumps([
        {
            "PackageFullName": "Contoso.App_1.0.0.0_x64__123",
            "Name": "Contoso.App",
            "Publisher": "CN=Contoso",
            "Version": "1.0.0.0",
        }
    ])
    entries = scanner._parse_appxpackage_output(raw)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["PackageFullName"] == "Contoso.App_1.0.0.0_x64__123"
    assert entry["Name"] == "Contoso.App"


@pytest.fixture
def fake_pywin32(monkeypatch):
    pythoncom = types.SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None)

    class DummyShortcut:
        Targetpath = r"C:\\Program Files\\App\\App.exe"

    class DummyShell:
        def CreateShortcut(self, _):
            return DummyShortcut()

    client = types.SimpleNamespace(Dispatch=lambda prog: DummyShell())
    win32com = types.SimpleNamespace(client=client)
    modules = {
        "pythoncom": pythoncom,
        "win32com": win32com,
        "win32com.client": client,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    yield
    for name in modules:
        sys.modules.pop(name, None)


def test_resolve_shortcut_uses_pywin32(fake_pywin32):
    target = scanner._resolve_shortcut_with_pywin32(Path("dummy.lnk"))
    assert target == r"C:\\Program Files\\App\\App.exe"
