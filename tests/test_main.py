"""Tests for main.py — helper functions and CLI/GUI dispatch."""

import sys
from unittest.mock import patch

import main
from core.cli import EXIT_USAGE
from main import _is_launcher_exe, _windowless_python


def test_windowless_python_returns_string():
    result = _windowless_python()
    assert isinstance(result, str)
    assert "python" in result.lower() or "pythonw" in result.lower()


def test_windowless_python_prefers_pythonw(monkeypatch):
    fake_python = "C:\\Python312\\python.exe"
    monkeypatch.setattr(sys, "executable", fake_python)

    with patch("os.path.isfile", side_effect=lambda p: "pythonw" in p):
        result = _windowless_python()
        assert "pythonw" in result


def test_is_launcher_exe_false_for_nonexistent(tmp_path):
    fake = str(tmp_path / "nonexistent.exe")
    assert _is_launcher_exe(fake) is False


def test_is_launcher_exe_true_when_exe_exists(tmp_path):
    exe = tmp_path / "flint.exe"
    exe.write_bytes(b"fake")
    assert _is_launcher_exe(str(exe)) is True


def test_is_launcher_exe_false_for_non_exe(tmp_path):
    txt = tmp_path / "flint.txt"
    txt.write_bytes(b"fake")
    assert _is_launcher_exe(str(txt)) is False


# --- console detection ----------------------------------------------------


def test_console_available_false_without_stdio(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    assert main._console_available() is False


def test_console_available_false_when_fileno_fails(monkeypatch):
    class _NoFileno:
        def fileno(self) -> int:
            raise OSError("no fd")

    monkeypatch.setattr(sys, "stdout", _NoFileno())
    # dev process: not frozen, so the fallback branch stays closed too.
    assert main._console_available() is False


# --- dispatch -------------------------------------------------------------


def _stub_gui(monkeypatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(main, "_run_gui", lambda: calls.append(1) or 7)
    return calls


def test_bare_with_console_shows_overview(monkeypatch, capsys):
    monkeypatch.setattr(main, "_console_available", lambda: True)
    calls = _stub_gui(monkeypatch)
    assert main.main([]) == 0
    out = capsys.readouterr().out
    assert "Usage: flint <command>" in out
    assert "flint launch" in out
    assert calls == []


def test_bare_without_console_starts_gui(monkeypatch):
    monkeypatch.setattr(main, "_console_available", lambda: False)
    calls = _stub_gui(monkeypatch)
    assert main.main([]) == 7
    assert calls == [1]


def test_launch_starts_gui(monkeypatch):
    calls = _stub_gui(monkeypatch)
    assert main.main(["launch"]) == 7
    assert calls == [1]


def test_launch_with_global_flag_still_starts_gui(monkeypatch):
    calls = _stub_gui(monkeypatch)
    assert main.main(["--json", "launch"]) == 7
    assert calls == [1]


def test_launch_with_extra_args_is_usage_error(monkeypatch):
    calls = _stub_gui(monkeypatch)
    assert main.main(["launch", "--yes"]) == EXIT_USAGE
    assert calls == []


def test_cli_command_never_starts_gui(monkeypatch, capsys):
    calls = _stub_gui(monkeypatch)
    assert main.main(["frobnicate"]) == EXIT_USAGE
    capsys.readouterr()
    assert calls == []


def test_gui_main_always_starts_gui(monkeypatch):
    calls = _stub_gui(monkeypatch)
    assert main.gui_main() == 7
    assert calls == [1]


# --- elevation --------------------------------------------------------------


def test_elevation_command_dev_appends_launch():
    from ui.window import MainWindow

    _cmd, args = MainWindow._elevation_command()
    assert args.split('"')[-1].strip() == "launch"


def test_elevation_command_frozen_appends_launch(monkeypatch):
    from ui.window import MainWindow

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _cmd, args = MainWindow._elevation_command()
    assert args == "launch"
