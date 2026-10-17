"""Regression tests for the ISO drop-zone worker lifecycle.

W1: ``_retire_decompress_worker`` must never drop the last Python
reference to a still-running ``QThread`` (Qt aborts the process) and must
only remove the temp dir once the worker really stopped.
W2: the zone needs a public ``busy()`` / ``live_workers()`` accessor so
``MainWindow`` can see its workers.
W3: ``load_iso`` on a path that no longer resolves shows the inline error
instead of doing nothing.

No real ``QThread`` is ever started here (repo pitfall: a started worker
races the assertions and a live thread destroyed at teardown aborts the
process); the fakes below implement only ``isRunning`` / ``wait`` /
``requestInterruption``.
"""

from typing import ClassVar

import pytest


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path):
    import core.settings as s

    original = s.SETTINGS_PATH
    s.SETTINGS_PATH = tmp_path / "s.json"
    s._CACHE = None
    yield
    s._CACHE = None
    s.SETTINGS_PATH = original


class _Signal:
    """Minimal stand-in for a bound ``pyqtSignal``."""

    def __init__(self) -> None:
        self.slots: list = []

    def connect(self, slot) -> None:
        self.slots.append(slot)

    def emit(self, *args) -> None:
        for slot in list(self.slots):
            slot(*args)


class _FakeWorker:
    """A worker that reports whatever running state the test asks for."""

    made: ClassVar[list] = []

    def __init__(
        self,
        running: bool = False,
        tmp_dir: str = "",
        wait_stops: bool = False,
    ) -> None:
        self.running = running
        self._tmp_dir = tmp_dir
        self.wait_stops = wait_stops
        self.interruptions = 0
        self.waits: list[int] = []
        self.finished = _Signal()
        _FakeWorker.made.append(self)

    def isRunning(self) -> bool:
        return self.running

    def isInterruptionRequested(self) -> bool:
        return bool(self.interruptions)

    def requestInterruption(self) -> None:
        self.interruptions += 1

    def wait(self, ms: int = 0) -> bool:
        self.waits.append(ms)
        if self.wait_stops:
            self.running = False
        return not self.running

    def cancel(self) -> None:
        self.requestInterruption()

    def deleteLater(self) -> None:
        pass

    def stop(self) -> None:
        """Finish the fake thread and deliver ``finished``."""
        self.running = False
        self.finished.emit()


_zones: list = []


@pytest.fixture(autouse=True)
def _stop_zone_drop_timers():
    """Stop any drop timer a test armed but never cancelled.

    ``test_load_iso_on_vanished_path_shows_inline_error`` deliberately
    asserts the timer is active; without this guard the 5 s single-shot
    could fire inside a later test's pytest-qt event pump (the flaky CI
    access violation we traced to ``ui/widgets.py``).
    """
    yield
    for zone in _zones:
        zone._drop_timer.stop()
    _zones.clear()



# --- W1 -----------------------------------------------------------------


def test_retire_decompress_worker_keeps_a_running_thread_alive(
    qapp, tmp_path
):
    """W1: after the 3 s grace period a still-running worker must be kept
    referenced (and its temp dir untouched) instead of being dropped."""
    zone = _zone()
    tmp_dir = _tmp_dir(tmp_path)
    worker = _FakeWorker(running=True, tmp_dir=str(tmp_dir))
    zone._decompress_worker = worker  # type: ignore[assignment]

    zone._retire_decompress_worker()

    assert worker.interruptions == 1, "the retire path asks it to stop"
    assert worker.waits == [3000]
    assert zone._decompress_worker is None
    assert worker in zone._retired_workers, "reference must survive"
    assert tmp_dir.exists(), "never delete below a running extractor"
    # The retained worker is still visible to the shutdown accessors.
    assert worker in zone.live_workers()


def test_retired_worker_releases_reference_and_tmp_dir_when_finished(
    qapp, tmp_path
):
    """W1: the retained worker drops out (and cleans up) on ``finished``."""
    zone = _zone()
    tmp_dir = _tmp_dir(tmp_path)
    worker = _FakeWorker(running=True, tmp_dir=str(tmp_dir))
    zone._decompress_worker = worker  # type: ignore[assignment]
    zone._retire_decompress_worker()

    worker.stop()

    assert zone._retired_workers == []
    assert not tmp_dir.exists(), "now that it stopped, the dir goes away"


def test_retire_decompress_worker_cleans_up_when_it_stopped_in_time(
    qapp, tmp_path
):
    """W1 control: a worker that stops within the grace period keeps the
    old (safe) behaviour - temp dir removed, nothing retired."""
    zone = _zone()
    tmp_dir = _tmp_dir(tmp_path)
    worker = _FakeWorker(running=True, tmp_dir=str(tmp_dir), wait_stops=True)
    zone._decompress_worker = worker  # type: ignore[assignment]

    zone._retire_decompress_worker()

    assert zone._decompress_worker is None
    assert zone._retired_workers == []
    assert not tmp_dir.exists()


def test_retire_decompress_worker_is_a_noop_without_a_worker(qapp):
    zone = _zone()
    zone._retire_decompress_worker()
    assert zone._retired_workers == []


def test_retired_worker_list_is_bounded_but_never_drops_running_threads(
    qapp,
):
    """W1: pruning keeps the list bounded; running threads are kept."""
    zone = _zone()
    running = _FakeWorker(running=True)
    stopped = [_FakeWorker(running=False) for _ in range(20)]
    zone._retired_workers.extend(stopped)  # type: ignore[arg-type]
    zone._retired_workers.append(running)  # type: ignore[arg-type]

    zone._prune_retired_workers()

    assert len(zone._retired_workers) <= 16
    assert running in zone._retired_workers


def test_prune_is_a_noop_for_a_short_list(qapp):
    zone = _zone()
    kept = [_FakeWorker(running=False) for _ in range(3)]
    zone._retired_workers.extend(kept)  # type: ignore[arg-type]
    zone._prune_retired_workers()
    assert zone._retired_workers == kept


def _zone():
    from ui.widgets import IsoDropZone

    zone = IsoDropZone()
    _zones.append(zone)
    return zone


def _tmp_dir(tmp_path, name: str = "flint-decompress-xyz"):
    path = tmp_path / name
    path.mkdir(exist_ok=True)
    return path


# --- W2 -----------------------------------------------------------------


def test_busy_is_false_for_an_idle_zone(qapp):
    zone = _zone()
    assert zone.busy() is False
    assert zone.live_workers() == []


@pytest.mark.parametrize(
    "attribute", ["_worker", "_analyzer", "_decompress_worker"]
)
def test_busy_tracks_each_zone_worker(qapp, attribute):
    """W2: hashing, detection and decompression are all visible."""
    zone = _zone()
    worker = _FakeWorker(running=True)
    setattr(zone, attribute, worker)

    assert zone.busy() is True
    assert zone.live_workers() == [worker]

    worker.stop()

    assert zone.busy() is False, "an attribute is not the same as running"
    assert zone.live_workers() == []


def test_busy_is_true_while_an_archive_is_pending(qapp):
    zone = _zone()
    zone._archive_pending = True
    assert zone.busy() is True


def test_live_workers_lists_running_workers_only(qapp):
    """W2: shutdown gets real worker objects, never None, never stopped."""
    zone = _zone()
    hashing = _FakeWorker(running=True)
    stopped = _FakeWorker(running=False)
    zone._worker = hashing  # type: ignore[assignment]
    zone._analyzer = stopped  # type: ignore[assignment]

    assert zone.live_workers() == [hashing]
    assert None not in zone.live_workers()


def test_zone_workers_expose_cancel_for_shutdown(qapp, tmp_path):
    """W2: every worker ``live_workers()`` can hand out is cancellable.

    Qt ignores ``requestInterruption()`` until the thread is actually
    running, so this only pins the API (the flag itself is covered by the
    fake-worker tests above); nothing here is started.
    """
    from ui.widgets import DecompressWorker, IsoDetectWorker, IsoWorker

    archive = tmp_path / "image.iso.zip"
    archive.write_bytes(b"PK\x03\x04")
    workers = [
        IsoWorker(str(archive)),
        IsoDetectWorker(str(archive)),
        DecompressWorker(str(archive), str(tmp_path), ".zip"),
    ]
    try:
        for worker in workers:
            assert not worker.isRunning()
            assert callable(getattr(worker, "cancel", None)), type(worker)
            worker.cancel()  # a stopped thread ignores it; must not raise
    finally:
        assert not any(worker.isRunning() for worker in workers)


# --- W3 -----------------------------------------------------------------


def test_load_iso_on_vanished_path_shows_inline_error(qapp, tmp_path):
    """W3: a recents entry that no longer resolves is not a silent no-op."""
    zone = _zone()
    missing = tmp_path / "deleted-image.iso"

    zone.load_iso(str(missing))

    assert (
        zone._drop_error.text() == "deleted-image.iso is no longer available"
    )
    assert not zone._drop_error.isHidden()
    assert zone._drop_timer.isActive()
    assert zone.path is None
    assert zone.source_path is None
    assert zone.busy() is False


def test_load_iso_with_an_empty_path_stays_a_noop(qapp):
    zone = _zone()
    zone.load_iso("")
    assert zone._drop_error.isHidden()
    assert not zone._drop_timer.isActive()

