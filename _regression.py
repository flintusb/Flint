# mypy: ignore-errors
import os
import sys
import tempfile
import time
import hashlib
from pathlib import Path

import faulthandler

faulthandler.dump_traceback_later(180, exit=True)

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

d = tempfile.mkdtemp()
BLOB = os.urandom(2 * 1024 * 1024 + 5)
IMG = os.path.join(d, "img.bin")
open(IMG, "wb").write(BLOB)
ISO = os.path.join(d, "test.iso")
open(ISO, "wb").write(BLOB)
TOTAL = 0

# Capture themed completion popups instead of letting them block (they
# are validated at the end of the run).
from ui import dialogs as _dialogs_mod

_ORIG_COMPLETION = _dialogs_mod.completion
_ORIG_INFORM = _dialogs_mod.inform
_POPUPS = []


def _capture_completion(parent, *, kind, title, message, buttons=None):
    _POPUPS.append((kind, title, message))
    return None


def _capture_inform(parent, *, kind="info", title="", message="", check=None):
    _POPUPS.append((kind, title, message))
    return None


_dialogs_mod.completion = _capture_completion
_dialogs_mod.inform = _capture_inform


def check(name):
    global TOTAL
    TOTAL += 1
    print(f"OK  {name}")
    sys.stdout.flush()


def _hush_poller(win):
    try:
        win._poller.drives_ready.disconnect()
    except Exception as exc:
        print("_hush_poller: disconnect failed:", exc)
        sys.stdout.flush()
    win._poller.requestInterruption()


# ---------- core: history v2 + export/import/clear ----------
import core.history as h

h.HISTORY_PATH = Path(d) / "h.json"
h.clear_history()
h.append_history({"success": True, "schema_version": 2, "iso": "x.iso",
                  "bootable": True, "avg_mbps": 12.5})
entry = h.load_history()[0]
assert entry["schema_version"] == 2 and entry["bootable"] is True
exp = os.path.join(d, "exp.json")
h.export_history(exp)
h.clear_history()
assert h.load_history() == []
h.import_history(exp)
assert len(h.load_history()) == 1 and h.load_history()[0]["iso"] == "x.iso"
check("history v2 + export/import/clear")

# ---------- core: settings ----------
import core.settings as s

s.SETTINGS_PATH = Path(d) / "s.json"
# Isolation: the history section's lazy `settings.get` may already have
# cached the *real* settings file (theme etc.) — drop the cache so this
# block exercises only the temp file above.
s._CACHE = None
s.set_many(onboarding_seen=True)
assert s.get("theme") == "dark" and s.get("verify_after_write") is True
s.set_many(theme="light", window_geometry="W1000H700")
assert s.get("theme") == "light" and s.get("window_geometry") == "W1000H700"
check("settings defaults + round-trip")

# ---------- core: verify ----------
import core.verify as v

sha = hashlib.sha256(BLOB).hexdigest()
ok, digest = v.hash_drive(IMG, size=len(BLOB))
assert ok and digest == sha, digest
ok2, err2 = v.hash_drive(IMG, size=len(BLOB) + 4096)
assert not ok2 and "read-back ended" in err2, err2
check("verify ok + short-read")

# ---------- core: bootcheck ----------
import core.bootcheck as bc

open(os.path.join(d, "mbr.bin"), "wb").write(b"\x00" * 510 + b"\x55\xaa")
mb = bc.probe_bootability(os.path.join(d, "mbr.bin"))
assert mb["mbr_signature"] is True and mb["error"] is None, mb
plain = bc.probe_bootability(IMG)
assert plain["mbr_signature"] is False and plain["gpt"] is False, plain
open(os.path.join(d, "zero.bin"), "wb").write(b"\x00" * 512)
zb = bc.probe_bootability(os.path.join(d, "zero.bin"))
assert zb["mbr_signature"] is False and zb["error"] is None, zb
open(os.path.join(d, "tiny.bin"), "wb").write(b"\x00" * 100)
assert bc.probe_bootability(os.path.join(d, "tiny.bin"))["error"]
check("bootcheck MBR / plain / zero")

# ---------- core: eject regex acceptance (headless-safe) ----------
from core.eject import eject_drive

res = eject_drive(r"\\.\PHYSICALDRIVE7")
assert res[0] is False
assert res[1] != "eject is only supported for physical drives", res
res2 = eject_drive(r"C:\some\file.bin")
assert res2[1] == "eject is only supported for physical drives"
check("eject path parsing")

# ---------- core: writer/wipe import ----------
import core.writer
import core.wipe
import core.drives

assert core.writer.UsbWriter is not None
assert core.wipe.WipeWorker and core.drives.DrivePoller
check("writer/wipe/drives import")

# ---------- UI battery ----------
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import QApplication

app = QApplication([])
from ui.window import MainWindow

FAKE = {"UUID": "SD", "letter": "E", "letters": ["E"], "size_gb": 4,
        "bus_type": "USB", "model": "Sdisk", "serial": "S9",
        "physical_path": r"\\.\PHYSICALDRIVE7"}

w = MainWindow()
_hush_poller(w)
w._detector.list_removable_drives = lambda: [FAKE]
w._select_drive(FAKE)
assert w._wipe_btn.isEnabled() and not w._cancel_btn.isEnabled()
assert w._done_bar.isHidden()
check("ui init + drive states")

w._iso_zone.load_iso(ISO)
w._iso_zone._worker.wait(10000)
app.processEvents()
w._iso_zone._digest = "d" * 64
w._write_duration = 2.5
w._current_drive_path = lambda: None
w._finish_flash(True, "", "e" * 64)
assert not w._done_bar.isHidden()
e = h.load_history()[-1]
assert e["verified"] and e["written_sha256"] == "e" * 64
assert e["drive_serial"] == "S9" and e["avg_mbps"] is not None
assert "Flint flash report" in w._build_report_text(w._last_report)
check("_finish_flash report + boot + history")

w._current_drive = FAKE
w._writing = False
w._on_wipe_finished(True, "")
assert w._done_label.text() == "Drive wiped"
assert h.load_history()[-1]["iso"] == "\u2014 wipe \u2014"
check("wipe finished history entry")

good = os.path.join(d, "good.bin")
open(good, "wb").write(BLOB)
bad = os.path.join(d, "bad.bin")
open(bad, "wb").write(BLOB[: len(BLOB) // 2] + b"x" * (len(BLOB) - len(BLOB) // 2))
w._verify_zone.load_iso(ISO)
w._verify_zone._worker.wait(10000)
app.processEvents()

w._detector.list_removable_drives = lambda: [{**FAKE, "physical_path": good}]
w._select_drive({**FAKE, "physical_path": good})
w._current_drive_path = lambda: good
w._on_page_verify_start()
while w._page_verifier is not None and w._page_verifier.isRunning():
    app.processEvents()
    w._page_verifier.wait(20)
app.processEvents()
assert w._verify_progress._title.text() == "Verified"
check("verify page matching -> Verified")

w._detector.list_removable_drives = lambda: [{**FAKE, "physical_path": bad}]
w._select_drive({**FAKE, "physical_path": bad})
w._current_drive_path = lambda: bad
w._on_page_verify_start()
while w._page_verifier is not None and w._page_verifier.isRunning():
    app.processEvents()
    w._page_verifier.wait(20)
app.processEvents()
assert "hash mismatch" in w._verify_progress._error.text()
check("verify page mismatch detected")

w._set_theme("light")
assert "#f2f2f2" in app.styleSheet()
w._set_theme("dark")
check("theme switch")

w._poller.requestInterruption()
w.close()
w._poller.wait(3000)

# ---------- batch 1: guard rails ----------
w2 = MainWindow()
_hush_poller(w2)
w2._detector.list_removable_drives = lambda: [FAKE]
w2._select_drive(FAKE)

# stale ISO digest: late emission from an old path must be ignored
w2._verify_zone.load_iso(ISO)
w2._verify_zone._worker.wait(10000)
app.processEvents()
w2._verify_zone._on_hash_done("/old/disk.iso", True, "f" * 64)
assert w2._verify_zone.digest != "f" * 64
w2._verify_zone._on_hash_done(ISO, True, "g" * 64)
assert w2._verify_zone.digest == "g" * 64
check("stale ISO digest ignored")

# drive swap + nav blocked while busy
w2._writing = True
w2._select_drive({**FAKE, "physical_path": r"\\.\PHYSICALDRIVE9"})
assert w2._current_drive["physical_path"] == FAKE["physical_path"]
before = w2._pages.currentIndex()
w2._on_nav_clicked(1)
assert w2._pages.currentIndex() == before
w2._writing = False
check("drive swap + nav blocked while busy")

# page verify refuses while busy
w2._writing = True
w2._on_page_verify_start()
assert "Wait for the current operation" in w2._verify_progress._error.text()
w2._writing = False
check("page verify busy guard")

# verify-after-write surfaces skipped verification instead of pretending
w2._iso_zone.load_iso(ISO)
w2._iso_zone._worker.wait(10000)
app.processEvents()
w2._verify_toggle.setChecked(True)
w2._iso_zone._digest = None
w2._active_write_drive = FAKE
w2._current_drive = None
w2._write_started = time.perf_counter() - 1.0
w2._on_write_finished(True, "", None)
assert w2._done_label.text() == "Flash complete \u2014 not verified"
assert not w2._done_bar.isHidden() and "Verification skipped" in w2._progress._error.text()
e = h.load_history()[-1]
assert e["success"] is True and e["verified"] is False
check("skipped verification surfaced")

# report drive identity comes from the captured write target
w2._current_drive = FAKE
w2._active_write_drive = FAKE
w2._iso_zone._digest = "d" * 64
w2._current_drive_path = lambda: None
w2._write_duration = 2.0
w2._finish_flash(True, "", "e" * 64)
e = h.load_history()[-1]
assert e["drive"] == FAKE["model"] and e["written_sha256"] == "e" * 64
check("captured write drive in report")

w2._shutdown()
check("shutdown waits + retires workers")

# ---------- batch 3: audit fixes ----------
w5 = MainWindow()
_hush_poller(w5)
w5._detector.list_removable_drives = lambda: [FAKE]
w5._select_drive(FAKE)

# ISO load (click / Ctrl+O / drop) is blocked while busy
w5._iso_zone._browse_guard = lambda: True
w5._iso_zone.load_iso(ISO)
assert w5._iso_zone.path is None
w5._iso_zone._browse_guard = None
w5._iso_zone.load_iso(ISO)
w5._iso_zone._worker.wait(10000)
app.processEvents()
assert w5._iso_zone.path == ISO
check("ISO load blocked while busy")

# verify mismatch snapshots are bounded, sample_offset anchored at the
# first differing byte
big_iso = os.path.join(d, "big.iso")
big_payload = os.urandom(3 * 1024 * 1024)
open(big_iso, "wb").write(big_payload)
big_dev = os.path.join(d, "big.dev")
corrupted = bytearray(big_payload)
corrupted[2 * 1024 * 1024 + 7] ^= 0xFF
open(big_dev, "wb").write(bytes(corrupted))
res = v.verify_device(big_dev, source_iso=big_iso, chunk_size=1024 * 1024)
assert len(res["mismatches"]) == 1, res["mismatches"]
off, length, sample_off, expected, actual = res["mismatches"][0]
assert off == 2 * 1024 * 1024 and length == 1024 * 1024
assert sample_off == 7
assert len(expected) == len(actual) == v.MISMATCH_SAMPLE_SIZE
assert expected == big_payload[off + 7 : off + 7 + len(expected)]
assert actual == bytes(corrupted)[off + 7 : off + 7 + len(actual)]
check("verify mismatch bounded snapshots")

# wipe cancel surfaces the partial-wipe warning and a wipe report
w5._current_drive = FAKE
w5._active_write_drive = FAKE
w5._write_started = time.perf_counter()
w5._on_wipe_finished(False, "cancelled")
assert any(p[1] == "Wipe cancelled" for p in _POPUPS)
assert w5._last_report is not None and w5._last_report["iso"] == "\u2014 wipe \u2014"
assert w5._last_report["success"] is False
check("wipe cancel popup + wipe report")

# corrupted history entries (junk duration) must not crash the page
h.append_history({"success": True, "iso": "x.iso", "drive": "d",
                  "duration": "not-a-number"})
w5._reload_history()
assert w5._history_list.count() >= 1
check("corrupted history entry tolerated")

# eject guards: no drive, then drive vanished between selection and eject
w5._current_drive = None
w5._active_write_drive = None
w5._on_eject_clicked()
assert any(p[1] == "Eject" and p[2] == "No drive selected." for p in _POPUPS)
w5._current_drive = FAKE
w5._drives = []  # drive removed meanwhile
seen = len(_POPUPS)
w5._on_eject_clicked()
# A transient miss is retried silently instead of popping up at once.
assert w5._eject_timer.isActive()
assert len(_POPUPS) == seen
w5._eject_timer.stop()
w5._eject_retries = 0  # retry budget spent: the final attempt must warn
w5._run_eject_attempt()
assert any("no longer present" in p[2] for p in _POPUPS)
w5._drives = [FAKE]
seen = len(_POPUPS)
w5._on_eject_clicked()
# FAKE matches again, so this reaches eject_drive; it must either popup
# or defer a retry - and never leave a pending timer behind for the suite.
assert len(_POPUPS) > seen or w5._eject_timer.isActive()
w5._eject_timer.stop()
check("eject guards (no drive / drive vanished / unmatched)")

# taskbar progress retries after a cooldown instead of latching forever
calls = {"n": 0}
w5._init_taskbar = lambda: calls.__setitem__("n", calls["n"] + 1)
w5._tb = None
w5._tb_last_try = time.monotonic()
w5._set_taskbar_progress(10.0)
assert calls == {"n": 0}
w5._tb_last_try = time.monotonic() - 10
w5._set_taskbar_progress(10.0)
assert calls == {"n": 1}
check("taskbar retry cooldown")

# failure / cancel set an honest chip title
w5._finish_flash(False, "boom", None)
assert w5._progress._title.text() == "Failed"
w5._finish_flash(False, "cancelled", None)
assert w5._progress._title.text() == "Cancelled"
check("failure chip titles")

# in-writer verify switches the chip to verify presentation and freezes
# the measured write duration at that point
w5._write_started = time.perf_counter() - 3.0
w5._write_duration = 0.0
w5._on_write_phase("Verifying")
assert w5._write_duration > 2.0
assert w5._progress._title.text() == "Verifying\u2026"
w5._on_write_phase("Writing (native)")
assert w5._progress._title.text() == "Writing (native)\u2026"
check("in-writer verify phase presentation")

# letterless drives render with a placeholder instead of a bare colon
w5._current_drive = {**FAKE, "letter": "", "letters": []}
w5._update_drive_ui()
assert "no drive letter" in w5._target_detail.text()
w5._current_drive = FAKE
w5._update_drive_ui()
assert "E:" in w5._target_detail.text()
check("letterless drive label")

# a stale write-finished signal retires (and never resurrects) its worker
import core.writer
dummy = core.writer.UsbWriter(ISO, r"\\.\PHYSICALDRIVE7")
w5._on_write_finished(True, "", dummy)
assert dummy in w5._retired_workers
check("stale write signal retired")

# poller suspend/resume + busy-guarded manual scan
w5._poller.suspend()
w5._poller.resume()
w5._writing = True
w5._request_scan()
w5._writing = False
w5._poller.request_scan()
check("poller suspend + busy scan guard")

w5._shutdown()
w5._shutdown()
check("shutdown idempotent")

# ---------- batch 4: tools & sidecar ----------
w6 = MainWindow()
_hush_poller(w6)
w6._detector.list_removable_drives = lambda: [FAKE]
w6._select_drive(FAKE)

methods = [a.text() for a in w6._wipe_menu.actions()]
assert methods == [
    "Zero fill (fast)",
    "Random data (NIST)",
    "DoD 5220.22-M (3 passes)",
], methods
# The dropdown must be a QPushButton#iconBtn so the theme applies
# (a QToolButton here matched no stylesheet rule and rendered unstyled).
from PyQt6.QtWidgets import QPushButton

assert isinstance(w6._wipe_menu_btn, QPushButton)
assert w6._wipe_menu_btn.objectName() == "iconBtn"
assert w6._wipe_menu_btn.toolTip() == "Wipe method"
check("wipe method menu")

image3 = os.path.join(d, "sidecar.iso")
open(image3, "wb").write(b"payload")
side = image3 + ".sha256"
open(side, "w").write("b" * 64 + "  sidecar.iso\n")
w6._iso_zone._path = image3
w6._iso_zone._digest = "a" * 64
w6._on_iso_hash_ready(image3, True, "a" * 64)
assert w6._sidecar_status == "mismatch"
assert not w6._sidecar_label.isHidden()
assert "MISMATCH" in w6._sidecar_label.text()
w6._on_flash_clicked()
assert w6._writer is None
check("sidecar mismatch blocks flash")

open(side, "w").write("a" * 64 + "  sidecar.iso\n")
w6._on_iso_hash_ready(image3, True, "a" * 64)
assert w6._sidecar_status == "ok"
assert not w6._sidecar_label.isHidden()
check("sidecar match unblocks")

w6._shutdown()

# ---------- batch 2: flows ----------
w3 = MainWindow()
_hush_poller(w3)
w3._detector.list_removable_drives = lambda: [FAKE]
assert w3._progress._title.text() == "Ready"
w3._select_drive(FAKE)
assert "Sdisk" in w3._target_detail.text() and "S/N \u2026S9" in w3._target_detail.text()
check("idle Ready + target drive card")

w3._detector.last_error = "boom"
w3._drives = []
w3._current_drive = None
w3._update_drive_ui()
assert w3._drive_name.text() == "Drive detection failed"
w3._detector.last_error = None
check("detection failure surfaced in chip")

w3._verify_zone.load_iso(ISO)
w3._verify_zone._worker.wait(10000)
app.processEvents()
w3._select_drive(FAKE)
w3._current_drive_path = lambda: r"\\.\PHYSICALDRIVE7"
w3._on_page_verify_start()
assert w3._verify_mode.text().startswith("Comparing against:")
while w3._page_verifier is not None and w3._page_verifier.isRunning():
    app.processEvents()
    w3._page_verifier.wait(20)
app.processEvents()
assert w3._verify_mode.text() == ""
check("verify mode label set/cleared")

good2 = os.path.join(d, "good2.bin")
open(good2, "wb").write(BLOB)
w3._detector.list_removable_drives = lambda: [{**FAKE, "physical_path": good2}]
w3._select_drive({**FAKE, "physical_path": good2})
w3._current_drive_path = lambda: good2
w3._on_page_verify_start()
while w3._page_verifier is not None and w3._page_verifier.isRunning():
    app.processEvents()
    w3._page_verifier.wait(20)
app.processEvents()
assert w3._verify_progress._stat_values["Written"].text().endswith("GB"), \
    w3._verify_progress._stat_values["Written"].text()
check("verify page live byte stats")

mbr = os.path.join(d, "bootable-mbr.bin")
open(mbr, "wb").write(b"\x00" * 510 + b"\x55\xaa")
import ui.window as uw

uw.probe_bootability = lambda path: {
    "gpt": False, "mbr_signature": True,
    "efi_partition": False, "error": None,
}
w3._iso_zone.load_iso(ISO)
w3._iso_zone._worker.wait(10000)
app.processEvents()
w3._iso_zone._digest = "d" * 64
w3._current_drive = {**FAKE, "physical_path": mbr}
w3._active_write_drive = {**FAKE, "physical_path": mbr}
w3._write_duration = 1.5
w3._finish_flash(True, "", "e" * 64)
assert w3._done_summary.text() == "Boot: MBR (legacy)", w3._done_summary.text()
check("boot result in done bar")

w3._finish_flash(False, "cancelled", None)
assert w3._done_label.text() == "Write cancelled" and not w3._done_bar.isHidden()
assert "partially" in w3._progress._error.text()
w3._finish_flash(False, "write failed: 87", None)
assert "Re-insert it" in w3._progress._error.text()
check("cancel recovery copy + friendly errors")

from PyQt6.QtGui import QKeyEvent
w3._verify_toggle.setChecked(False)
ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier)
w3._verify_toggle.keyPressEvent(ev)
assert w3._verify_toggle.isChecked()
check("toggle keyboard activation")

# clear-iso removes the image and drops a stale digest
w3._iso_zone.load_iso(ISO)
w3._iso_zone._worker.wait(10000)
app.processEvents()
w3._iso_zone._digest = "c" * 64
w3._iso_zone._clear_guard = lambda: False
w3._iso_zone.clear_iso()
assert w3._iso_zone.path is None and w3._iso_zone.digest is None
assert w3._iso_zone._empty.isHidden() is False
assert w3._iso_zone._loaded.isHidden()
w3._iso_zone._on_hash_done(ISO, True, "z" * 64)
assert w3._iso_zone.digest is None
check("clear-iso unloads image")

# busy guard keeps a clear from unloading the flash image
w3._iso_zone.load_iso(ISO)
w3._iso_zone._worker.wait(10000)
app.processEvents()
w3._iso_zone._clear_guard = lambda: True
w3._iso_zone.clear_iso()
assert w3._iso_zone.path == ISO
w3._iso_zone._clear_guard = None
check("clear-iso blocked while busy")

# SHA-256 dropped as a local file lands in the verify field
from PyQt6.QtCore import QMimeData, QPointF, QUrl
from PyQt6.QtGui import QDropEvent
sha_file = os.path.join(d, "checksum.sha256")
with open(sha_file, "w") as f:
    f.write("ab" * 32 + "\n")
mime = QMimeData()
mime.setUrls([QUrl.fromLocalFile(sha_file)])
ev = QDropEvent(
    QPointF(0, 0), Qt.DropAction.CopyAction, mime,
    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
)
w3._verify_sha_input.setText("")
w3._verify_sha_input.dropEvent(ev)
assert w3._verify_sha_input.text() == "ab" * 32
junk = os.path.join(d, "junk.txt")
with open(junk, "w") as f:
    f.write("not a checksum")
mime2 = QMimeData()
mime2.setUrls([QUrl.fromLocalFile(junk)])
ev2 = QDropEvent(
    QPointF(0, 0), Qt.DropAction.CopyAction, mime2,
    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
)
w3._verify_sha_input.setText("abc")
w3._verify_sha_input.dropEvent(ev2)
assert w3._verify_sha_input.text() == "abc"
check("SHA-256 file drop on verify input")

# F5 refresh + Ctrl+O browse shortcuts are wired on the window
from PyQt6.QtGui import QShortcut
scs = w3.findChildren(QShortcut)
f5 = next(s for s in scs if s.key().toString() == "F5")
ctrl_o = next(s for s in scs if s.key().toString() == "Ctrl+O")
assert f5 is not None and ctrl_o is not None
assert f5.context() == Qt.ShortcutContext.ApplicationShortcut and (
    ctrl_o.context() == Qt.ShortcutContext.ApplicationShortcut
)
hits = {"f5": 0}
w3._poller.request_scan = lambda: hits.__setitem__("f5", hits["f5"] + 1)
f5.activated.emit()
assert hits == {"f5": 1}
check("F5 refresh + Ctrl+O shortcuts")

w3._shutdown()

# ---------- turn-off fixes ----------
w4 = MainWindow()
_hush_poller(w4)

# ISO hashing reports progress and finishes at 100
pct_log = []
w4._iso_zone.load_iso(ISO)
w4._iso_zone._worker.progress.connect(pct_log.append)
w4._iso_zone._worker.wait(10000)
app.processEvents()
assert pct_log and pct_log[-1] == 100, pct_log
assert pct_log == sorted(pct_log)
assert "SHA256 verified" in w4._iso_zone._iso_meta.text()
check("ISO hash progress signal")

# invalid file drops surface a transient error, valid drops clear it
from PyQt6.QtCore import QMimeData, QPointF, QUrl
from PyQt6.QtGui import QDropEvent
zip_path = os.path.join(d, "x.zip")
with open(zip_path, "wb") as f:
    f.write(b"PK\x03\x04")
mime = QMimeData()
mime.setUrls([QUrl.fromLocalFile(zip_path)])
ev = QDropEvent(
    QPointF(0, 0), Qt.DropAction.CopyAction, mime,
    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
)
w4._iso_zone.dragEnterEvent(ev)
assert ev.isAccepted()
w4._iso_zone.dropEvent(ev)
dc = w4._iso_zone._decompress_worker
if dc is not None and dc.isRunning():
    dc.wait(5000)
QApplication.processEvents()
assert w4._iso_zone._drop_error.isHidden() is False
assert w4._iso_zone._drop_timer.isActive()
mime2 = QMimeData()
mime2.setUrls([QUrl.fromLocalFile(ISO)])
ev2 = QDropEvent(
    QPointF(0, 0), Qt.DropAction.CopyAction, mime2,
    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
)
w4._iso_zone.dropEvent(ev2)
assert w4._iso_zone._drop_error.isHidden()
check("drop rejection feedback")

# administrator affordance appears only when detection fails, not elevated
w4._detector.last_error = "boom"
w4._drives = []
w4._current_drive = None
w4._is_elevated = lambda: False
w4._update_drive_ui()
assert w4._target_admin_btn.isHidden() is False
assert w4._target_change.isHidden()
w4._is_elevated = lambda: True
w4._update_drive_ui()
assert w4._target_admin_btn.isHidden()
assert w4._target_change.isHidden() is False
w4._relaunch_elevated()
cmd, args = MainWindow._elevation_command()
assert args.startswith('"')
if cmd.lower().endswith("python.exe"):
    assert not os.path.isfile(
        cmd[: -len("python.exe")] + "pythonw.exe"
    )
else:
    assert cmd.lower().endswith("pythonw.exe") and os.path.isfile(cmd)
w4._elevation_command = lambda: ("", "")
w4._relaunch_elevated()
w4._detector.last_error = None
check("admin relaunch affordance")

# close blocked while busy; hides to tray only when close_to_tray is on
class _Tray:
    def setToolTip(self, t):
        pass

    def showMessage(self, *args):
        pass

w4._tray = _Tray()
s.set_many(close_to_tray=True)
w4._close_to_tray_toggle.setChecked(True)
w4.show()
w4._writing = True
w4.close()
assert not w4.isHidden()
w4._writing = False
w4.close()
assert w4.isHidden()
s.set_many(close_to_tray=False)
w4._close_to_tray_toggle.setChecked(False)
check("close blocked while busy; tray close opt-in")

# confirm text includes the data-erasure warning when content is known
ok_du = uw.shutil.disk_usage
uw.shutil.disk_usage = lambda p: type(
    "U", (), {"used": 5_000_000_000}
)() if p == "E:\\" else (_ for _ in ()).throw(OSError())
txt = w4._confirm_text(FAKE, ISO)
assert "It holds about" in txt and "all of it will be erased" in txt
txt2 = w4._confirm_text(FAKE)
assert "partitions and files" in txt2
uw.shutil.disk_usage = ok_du
check("erasure warning in confirm text")

# verification expectation hint appears during read-back and clears
class _NoSignal:
    def connect(self, slot):
        pass

class _FakeVerifier:
    progress = _NoSignal()
    stats = _NoSignal()
    done = _NoSignal()

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

real_vw = uw.VerifyWorker
uw.VerifyWorker = _FakeVerifier
assert w4._verify_hint.isHidden()
w4._iso_zone._digest = "d" * 64
w4._current_drive = FAKE
w4._current_drive_path = lambda: r"\\.\PHYSICALDRIVE7"
w4._start_verify()
assert w4._verify_hint.isHidden() is False
w4._verifier = None
w4._write_duration = 2.0
w4._finish_flash(True, "", "e" * 64)
assert w4._verify_hint.isHidden()
uw.VerifyWorker = real_vw
check("verification expectation hint")

w4._shutdown()

# ---------- single-instance ----------
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
import importlib
import main as mn

pipe_name = f"FlintRegression_{os.getpid()}"
QLocalServer.removeServer(pipe_name)
srv = mn._acquire_single_instance(QLocalServer, QLocalSocket, pipe_name)
assert srv is not None and srv.isListening()
assert mn._acquire_single_instance(QLocalServer, QLocalSocket, pipe_name) is None
probe = QLocalSocket()
probe.connectToServer(pipe_name)
assert probe.waitForConnected(1000)
probe.write(b"show")
probe.flush()
probe.disconnectFromServer()
srv.close()
check("single-instance guard")

faulthandler.cancel_dump_traceback_later()
_dialogs_mod.completion = _ORIG_COMPLETION
_dialogs_mod.inform = _ORIG_INFORM
kinds = [p[0] for p in _POPUPS]
assert "success" in kinds and "warning" in kinds and "error" in kinds, kinds
assert any(p[1] == "Verification passed" for p in _POPUPS)
assert any(p[1] == "Drive wiped" for p in _POPUPS)
check("completion popups fired (success/warning/error)")
print(f"\nALL REGRESSION PASSED ({TOTAL} groups)")