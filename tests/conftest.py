"""Shared test fixtures for the Flint test suite."""

import sys
import time

import pytest


@pytest.fixture(autouse=True)
def _isolated_settings_store(tmp_path, monkeypatch):
    """Redirect every settings path to a temp directory.

    D5: this fixture used to be called ``_isolated_settings``, which 13 test
    modules redefine as their own weaker autouse fixture (``SETTINGS_PATH``
    only).  A module-level fixture of the same name *shadows* the conftest
    one, so in those modules ``settings._LOCK_PATH`` still pointed at the
    real ``%APPDATA%\\Flint\\settings.lock`` and a deferred ``set_many()``
    flush recreated/touched the user's file.  The name is now unique so no
    module can shadow it; ``_isolated_settings`` survives below purely as a
    value alias for the tests that ask for the directory handle.
    """
    from core import paths, settings

    app_dir = tmp_path / "app"
    app_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(paths, "APP_DIR", app_dir)
    # settings.py imported APP_DIR by value and calls APP_DIR.mkdir() when
    # persisting, so patching paths.APP_DIR alone still touched the real
    # profile directory.
    monkeypatch.setattr(settings, "APP_DIR", app_dir)
    monkeypatch.setattr(settings, "SETTINGS_PATH", app_dir / "settings.json")
    monkeypatch.setattr(settings, "_LOCK_PATH", app_dir / "settings.lock")
    settings._CACHE = None
    return app_dir


@pytest.fixture()
def _isolated_settings(_isolated_settings_store):
    """Value alias for tests that use the fixture as a directory handle."""
    return _isolated_settings_store


@pytest.fixture(autouse=True)
def _isolated_history(tmp_path, monkeypatch):
    """Keep every test off the real %APPDATA%\\Flint\\history.json.

    core/history.py imports APP_DIR by value and derives HISTORY_PATH and
    _HISTORY_LOCK_PATH from it at module import time, so patching
    paths.APP_DIR (or settings' copies) never reached it.  Every UI flow
    that finishes a flash or a wipe calls append_audited_history(), which
    was appending test records to the user's real audit log.

    The paths are restored only after this fixture has drained the
    writeback queue, so a job submitted by a test that turned background
    persistence on cannot land on the real file once monkeypatch has
    restored it.
    """
    from core import history, writeback

    app_dir = tmp_path / "app"
    app_dir.mkdir(exist_ok=True)
    hist = app_dir / "history.json"
    monkeypatch.setattr(history, "APP_DIR", app_dir)
    monkeypatch.setattr(history, "HISTORY_PATH", hist)
    monkeypatch.setattr(history, "_HISTORY_LOCK_PATH", hist.with_suffix(".lock"))
    yield hist
    # Run while HISTORY_PATH is still the temp copy: a deferred save has
    # to land in the sandbox, never in the profile.
    writeback.flush(5.0)
    writeback.disable()
    history._pending_entries = None
    history._invalidate_history_cache()


@pytest.fixture(autouse=True)
def _isolated_app_dir(tmp_path, monkeypatch):
    """Redirect core.paths.APP_DIR so queue persistence never touches the
    real %APPDATA%\\Flint. Separate fixture name so test modules that
    shadow ``_isolated_settings`` cannot accidentally undo this."""
    from core import paths

    app_dir = tmp_path / "app"
    app_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(paths, "APP_DIR", app_dir)
    return app_dir


@pytest.fixture(autouse=True)
def _join_drive_pollers(monkeypatch):
    """Stop and join every real drive poller started by a test.

    Window helpers request interruption but do not wait for the QThread.
    Without joining, COM-backed WMI polling can outlive its test and overlap
    unrelated worker threads in later tests.
    """
    from core.drives import DrivePoller

    started = []
    original_start = DrivePoller.start

    def track_start(poller, *args, **kwargs):
        started.append(poller)
        return original_start(poller, *args, **kwargs)

    monkeypatch.setattr(DrivePoller, "start", track_start)
    yield

    unjoined = []
    for poller in reversed(started):
        deadline = time.monotonic() + 1.0
        while (
            not poller.isRunning()
            and not poller.isFinished()
            and time.monotonic() < deadline
        ):
            time.sleep(0.001)
        if poller.isRunning():
            poller.requestInterruption()
        if not poller.wait(6000):
            unjoined.append(poller)
    if unjoined:
        pytest.fail(f"{len(unjoined)} DrivePoller thread(s) did not stop")


@pytest.fixture(autouse=True)
def _isolated_system_disk_cache(monkeypatch):
    """Reset drives._OTHER_SYSTEM_CACHE before every test.

    It is a process-lifetime cache in production; without this, one test
    that resolves real registry/WMI data (e.g. cli flash -> _is_system_disk)
    leaks the host's physical paths into later tests' exclusion sets."""
    from core import drives

    monkeypatch.setattr(drives, "_OTHER_SYSTEM_CACHE", None)


@pytest.fixture(autouse=True)
def _stop_stale_drop_timers():
    """Stop any IsoDropZone drop timer that outlives its test.

    The error-hiding timer is a 3-5 s single-shot armed by drop/load paths.
    A test can arm it and end before it fires; the timer is a child of the
    zone, and (since the callback is now a bound method) the zone is
    collectable, but a surviving window/zone wrapper would let the timer
    fire inside a later, unrelated test's pytest-qt event pump - the flaky
    CI access violation in ui/widgets.py. Stopping every surviving timer at
    each test boundary removes that whole failure class.
    """
    yield
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        return
    from ui.widgets import IsoDropZone

    for widget in app.allWidgets():
        if isinstance(widget, IsoDropZone):
            try:
                widget._drop_timer.stop()
            except RuntimeError:
                pass  # C++ object already deleted; nothing to stop


@pytest.fixture()
def _make_window(monkeypatch):
    """Create a MainWindow without showing it (skips elevation checks)."""

    def _make():
        from unittest.mock import patch

        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            app = QApplication(sys.argv)

        with patch("core.drives.DrivePoller"), patch(
            "core.drives.DriveDetector"
        ):
            from ui.window import MainWindow

            w = MainWindow()
            w.hide()
            return w

    return _make
