"""Settings page: navigation, theme radios, expert sync, tray-on-close
toggle and close-event behavior."""

import pytest
from PyQt6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path):
    import core.settings as s

    original = s.SETTINGS_PATH
    s.SETTINGS_PATH = tmp_path / "s.json"
    s._CACHE = None
    yield
    s._CACHE = None
    s.SETTINGS_PATH = original


def _make_window(qapp, tmp_path, seed: dict | None = None):
    import core.settings as s

    values = {"onboarding_seen": True, "theme": "dark"}
    values.update(seed or {})
    s.set_many(**values)
    from ui.window import MainWindow

    w = MainWindow()
    if w._poller.receivers(w._poller.drives_ready):
        w._poller.drives_ready.disconnect()
    w._poller.requestInterruption()
    return w


class _FakeTray:
    def __init__(self) -> None:
        self.messages: list[tuple] = []

    def setToolTip(self, text) -> None:
        pass

    def showMessage(self, *args) -> None:
        self.messages.append(args)


def test_close_to_tray_default_off(qapp, tmp_path):
    import core.settings as s

    assert s.get("close_to_tray") is False


def test_settings_page_reachable_from_nav(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        assert len(w._nav_items) == 4
        w._on_nav_clicked(3)
        assert w._pages.currentIndex() == 3
        assert w._bottombar.isHidden()
        w._on_nav_clicked(0)
        assert w._pages.currentIndex() == 0
        assert not w._bottombar.isHidden()
    finally:
        w._shutdown()


def test_dots_menu_has_only_settings(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        texts = [a.text() for a in w._build_dots_menu().actions()]
        assert texts == ["Settings", "", "Check for updates\u2026"]
    finally:
        w._shutdown()


def test_close_tray_toggle_persists(qapp, tmp_path):
    import core.settings as s

    w = _make_window(qapp, tmp_path)
    try:
        assert not w._close_to_tray_toggle.isChecked()
        w._close_to_tray_toggle.setChecked(True)
        assert s.get("close_to_tray") is True
        w._close_to_tray_toggle.setChecked(False)
        assert s.get("close_to_tray") is False
    finally:
        w._shutdown()


def test_elevation_confirm_toggle_persists(qapp, tmp_path):
    import core.settings as s

    w = _make_window(
        qapp, tmp_path, seed={"ask_before_elevation": False}
    )
    try:
        # U06: ask_before_elevation used to live only in settings.json.
        assert hasattr(w, "_elevate_confirm_toggle")
        assert not w._elevate_confirm_toggle.isChecked()
        w._elevate_confirm_toggle.setChecked(True)
        assert s.get("ask_before_elevation") is True
        w._elevate_confirm_toggle.setChecked(False)
        assert s.get("ask_before_elevation") is False
    finally:
        w._shutdown()


def test_settings_expert_toggle_syncs_with_write_page(qapp, tmp_path):
    w = _make_window(qapp, tmp_path)
    try:
        w._settings_expert_toggle.setChecked(False)
        assert not w._expert_toggle.isChecked()
        assert w._expert_options_body.isHidden()
        w._expert_toggle.setChecked(True)
        assert w._settings_expert_toggle.isChecked()
        assert not w._expert_options_body.isHidden()
    finally:
        w._shutdown()


def test_theme_radio_applies_theme(qapp, tmp_path):
    import core.settings as s

    w = _make_window(qapp, tmp_path)
    try:
        assert w._theme_radios["dark"].isChecked()
        w._theme_radios["light"].setChecked(True)
        assert s.get("theme") == "light"
        assert w._theme_radios["light"].isChecked()
        w._theme_radios["dark"].setChecked(True)
        assert s.get("theme") == "dark"
    finally:
        w._shutdown()


def test_close_blocked_while_busy(qapp, tmp_path, monkeypatch):
    from ui import dialogs as d

    warned = []
    monkeypatch.setattr(
        d, "inform", lambda parent, **kw: warned.append(kw)
    )
    w = _make_window(qapp, tmp_path)
    # Deterministic: force the no-tray branch regardless of whether the
    # test session has a real system tray available.
    w._tray = None
    w.show()
    try:
        w._writing = True
        w.close()
        assert w.isVisible()
        assert not w.isHidden()
        assert warned, "busy close must explain itself without a tray"
        w._writing = False
    finally:
        w.hide()
        w._shutdown()


def test_close_hides_to_tray_when_enabled(qapp, tmp_path):
    w = _make_window(qapp, tmp_path, seed={"close_to_tray": True})
    w._tray = _FakeTray()
    w.show()
    try:
        w.close()
        assert w.isHidden()
    finally:
        w._shutdown()


def test_close_quits_when_disabled(qapp, tmp_path, monkeypatch):
    w = _make_window(qapp, tmp_path)
    calls = []

    class _FakeApp:
        def quit(self):
            calls.append(1)

        def processEvents(self):
            pass

    monkeypatch.setattr(QApplication, "instance", lambda: _FakeApp())
    w.show()
    try:
        w.close()
        assert calls
    finally:
        w._shutdown()


def test_auto_eject_toggle_persists(qapp, tmp_path):
    import core.settings as s

    w = _make_window(qapp, tmp_path)
    try:
        assert not w._auto_eject_toggle.isChecked()
        w._auto_eject_toggle.setChecked(True)
        assert s.get("auto_eject") is True
        w._auto_eject_toggle.setChecked(False)
        assert s.get("auto_eject") is False
    finally:
        w._shutdown()


def test_auto_eject_seeded_from_settings(qapp, tmp_path):
    w = _make_window(qapp, tmp_path, seed={"auto_eject": True})
    try:
        assert w._auto_eject_toggle.isChecked()
    finally:
        w._shutdown()


def test_auto_eject_defers_eject_on_success(qapp, tmp_path, monkeypatch):
    """Auto-eject runs after a delay, never in the finish turn.

    The write rewrites the partition table, so Windows is still
    re-enumerating the drive when _finish_flash returns; ejecting
    immediately races PnP and fails with "drive not found in device
    list". Drives the real timer -> _run_eject_attempt chain.
    """
    from PyQt6.QtTest import QTest

    import ui.window as window_mod

    w = _make_window(qapp, tmp_path, seed={"auto_eject": True})
    drive = {"physical_path": r"\\.\PHYSICALDRIVE7", "serial": "AAA"}
    w._current_drive = drive
    shown = _prime_eject(monkeypatch, w, drive, lambda p: (True, "ejected"))
    monkeypatch.setattr(window_mod, "_AUTO_EJECT_DELAY_MS", 0)
    monkeypatch.setattr(w, "_show_toast", lambda *a, **k: None)
    try:
        w._finish_flash(True, "", None)
        assert not shown, "auto-eject must not race Windows re-enumeration"
        assert w._eject_timer.isActive(), "auto-eject must be scheduled"
        QTest.qWait(50)
        assert len(shown) == 1, "auto-eject must run once the delay elapses"
        assert shown[0]["kind"] == "success"
    finally:
        w._eject_timer.stop()
        w._shutdown()


def test_auto_eject_targets_the_written_drive(qapp, tmp_path, monkeypatch):
    """The eject must use the drive that was written, not the selection.

    _finish_flash clears _active_write_drive before ejecting, so the
    target has to be captured before the clear and passed explicitly.
    """
    w = _make_window(qapp, tmp_path, seed={"auto_eject": True})
    written = {"physical_path": r"\\.\PHYSICALDRIVE7", "serial": "AAA"}
    other = {"physical_path": r"\\.\PHYSICALDRIVE8", "serial": "BBB"}
    w._active_write_drive = written
    w._current_drive = other
    scheduled: list[dict] = []
    monkeypatch.setattr(w, "_schedule_eject", lambda t, d: scheduled.append(t))
    monkeypatch.setattr(w, "_show_toast", lambda *a, **k: None)
    try:
        w._finish_flash(True, "", None)
        assert scheduled == [written]
        assert w._active_write_drive is None
    finally:
        w._eject_timer.stop()
        w._shutdown()


def test_completion_dialog_eject_uses_written_drive(
    qapp, tmp_path, monkeypatch
):
    """The Flash-complete dialog's Eject button ejects the written drive."""
    from ui import dialogs as d

    w = _make_window(qapp, tmp_path, seed={"auto_eject": False})
    written = {"physical_path": r"\\.\PHYSICALDRIVE7", "serial": "AAA"}
    other = {"physical_path": r"\\.\PHYSICALDRIVE8", "serial": "BBB"}
    w._active_write_drive = written
    w._current_drive = other
    monkeypatch.setattr(d, "completion", lambda *a, **kw: "eject")
    seen: list[dict] = []
    monkeypatch.setattr(w, "_on_eject_clicked", lambda target=None: seen.append(target))
    monkeypatch.setattr(w, "_show_toast", lambda *a, **k: None)
    try:
        w._finish_flash(True, "", None)
        assert seen == [written]
    finally:
        w._eject_timer.stop()
        w._shutdown()


def _prime_eject(monkeypatch, w, drive, eject_result):
    """Wire a fake drive + eject_drive result into a window for eject tests."""
    import ui.window as window_mod
    from ui import dialogs as d

    shown: list[dict] = []
    monkeypatch.setattr(window_mod, "eject_drive", eject_result)
    monkeypatch.setattr(window_mod, "_EJECT_RETRY_DELAY_MS", 0)
    monkeypatch.setattr(w, "_recheck_drive", lambda t: drive)
    monkeypatch.setattr(w, "_drive_path_for", lambda d_: drive["physical_path"])
    monkeypatch.setattr(d, "inform", lambda *a, **kw: shown.append(kw))
    return shown


def test_eject_retries_transient_failure_then_succeeds(
    qapp, tmp_path, monkeypatch
):
    """A SetupAPI miss right after the write is retried, not reported.

    This is the regression test for the post-flash popup: ejecting while
    Windows is still re-enumerating the drive used to surface the raw
    "drive not found in device list" error to the user.
    """
    from PyQt6.QtTest import QTest

    w = _make_window(qapp, tmp_path)
    drive = {"physical_path": r"\\.\PHYSICALDRIVE7", "serial": "AAA"}
    calls = {"n": 0}

    def fake_eject(path):
        calls["n"] += 1
        if calls["n"] == 1:
            return False, "drive not found in device list"
        return True, "ejected"

    shown = _prime_eject(monkeypatch, w, drive, fake_eject)
    try:
        w._on_eject_clicked(drive)
        # First attempt failed transiently: retried silently, not shown.
        assert calls["n"] == 1
        assert not shown, "a transient eject failure must not popup yet"
        assert w._eject_timer.isActive()
        QTest.qWait(50)
        assert calls["n"] == 2
        assert len(shown) == 1
        assert shown[0]["kind"] == "success"
    finally:
        w._eject_timer.stop()
        w._shutdown()


def test_eject_retries_when_drive_missing_from_list(
    qapp, tmp_path, monkeypatch
):
    """A transient miss in the polled list is retried before giving up."""
    from PyQt6.QtTest import QTest

    w = _make_window(qapp, tmp_path)
    drive = {"physical_path": r"\\.\PHYSICALDRIVE7", "serial": "AAA"}
    checks = {"n": 0}

    def fake_recheck(t):
        checks["n"] += 1
        return None if checks["n"] == 1 else drive

    shown = _prime_eject(monkeypatch, w, drive, lambda p: (True, "ejected"))
    monkeypatch.setattr(w, "_recheck_drive", fake_recheck)
    try:
        w._on_eject_clicked(drive)
        assert checks["n"] == 1
        assert not shown, "a transient list miss must not popup yet"
        QTest.qWait(50)
        assert checks["n"] == 2
        assert len(shown) == 1
        assert shown[0]["kind"] == "success"
    finally:
        w._eject_timer.stop()
        w._shutdown()


def test_eject_transient_failure_finally_shows_friendly_message(
    qapp, tmp_path, monkeypatch
):
    """When the retry is spent, the raw SetupAPI text is never shown."""
    w = _make_window(qapp, tmp_path)
    drive = {"physical_path": r"\\.\PHYSICALDRIVE7", "serial": "AAA"}
    shown = _prime_eject(
        monkeypatch,
        w,
        drive,
        lambda p: (False, "drive not found in device list"),
    )
    try:
        w._eject_target = drive
        w._eject_retries = 0  # retry budget already spent
        w._run_eject_attempt()
        assert len(shown) == 1
        assert shown[0]["kind"] == "error"
        assert "re-enumerating" in shown[0]["message"]
        assert "device list" not in shown[0]["message"]
    finally:
        w._shutdown()


def test_eject_failure_without_retry_keeps_original_message(
    qapp, tmp_path, monkeypatch
):
    """Non-transient failures (device in use, access denied) are kept."""
    w = _make_window(qapp, tmp_path)
    drive = {"physical_path": r"\\.\PHYSICALDRIVE7", "serial": "AAA"}
    shown = _prime_eject(
        monkeypatch,
        w,
        drive,
        lambda p: (False, "device in use \u2014 close programs accessing the drive"),
    )
    try:
        w._eject_target = drive
        w._eject_retries = 0
        w._run_eject_attempt()
        assert len(shown) == 1
        assert shown[0]["kind"] == "error"
        assert "device in use" in shown[0]["message"]
    finally:
        w._shutdown()


def test_auto_eject_off_shows_completion_dialog(qapp, tmp_path, monkeypatch):
    from ui import dialogs as d

    w = _make_window(qapp, tmp_path, seed={"auto_eject": False})
    shown: list[dict] = []
    monkeypatch.setattr(d, "completion", lambda *a, **kw: shown.append(kw) or "close")
    monkeypatch.setattr(w, "_show_toast", lambda *a, **k: None)
    try:
        w._finish_flash(True, "", None)
        assert shown, "completion dialog must appear when auto-eject is off"
    finally:
        w._shutdown()


def test_fleet_skip_flashed_persists_across_instances(qapp, tmp_path):
    import core.settings as s

    w = _make_window(qapp, tmp_path)
    try:
        assert not w._fleet_skip_flashed.isChecked()
        w._fleet_skip_flashed.setChecked(True)
        assert s.get("fleet_skip_flashed") is True
    finally:
        w._shutdown()

    s._CACHE = None  # force a disk read: this is a round-trip, not a cache hit
    w2 = _make_window(qapp, tmp_path)
    try:
        assert w2._fleet_skip_flashed.isChecked()
        w2._fleet_skip_flashed.setChecked(False)
        assert s.get("fleet_skip_flashed") is False
    finally:
        w2._shutdown()


def test_log_level_applied_at_startup(qapp, tmp_path, monkeypatch):
    import ui.window as window_mod

    applied: list[str] = []
    monkeypatch.setattr(
        window_mod,
        "apply_log_level",
        lambda level: applied.append(level) or level,
    )
    w = _make_window(qapp, tmp_path, seed={"log_level": "DEBUG"})
    try:
        assert applied == ["DEBUG"]
    finally:
        w._shutdown()
