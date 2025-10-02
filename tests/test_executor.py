"""Unit tests for the Windows executor helpers."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rai.client import executor


@pytest.fixture(autouse=True)
def _force_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend we run on Windows to exercise code paths."""

    monkeypatch.setattr(executor, "_is_windows", lambda: True)


def test_open_app_missing_executable(tmp_path: Path) -> None:
    target = {"exe_path": str(tmp_path / "Missing.exe"), "trace_id": "test-open"}
    result = executor.open_app(target)
    assert result["ok"] is False
    assert result["message"] == "executable_not_found"


def test_focus_app_without_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "_find_windows", lambda target, trace_id: [])
    result = executor.focus_app({"trace_id": "test-focus"})
    assert result["ok"] is False
    assert result["message"] == "window_not_found"


def test_close_app_uses_first_window(monkeypatch: pytest.MonkeyPatch) -> None:
    handles = [0x401, 0x402]
    monkeypatch.setattr(executor, "_find_windows", lambda target, trace_id: handles)
    monkeypatch.setattr(executor, "_window_pid", lambda hwnd: 111 if hwnd == handles[0] else 222)
    monkeypatch.setattr(executor, "_send_wm_close", lambda hwnd: hwnd == handles[0])
    result = executor.close_app({"trace_id": "test-close"})

    assert result["ok"] is True
    assert result["message"] == "closed_window"
    assert result["pid"] == 111
    assert result["window"] == "0x401"
