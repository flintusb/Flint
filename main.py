import ctypes
import faulthandler
import logging
import os
import platform
import subprocess
import sys
import time
import traceback
from contextlib import ExitStack
from types import TracebackType
from typing import Any

logger = logging.getLogger("flint")

if platform.system() != "Windows":
    print(
        "error: Flint is a Windows-only tool.\n"
        "\n"
        "Flint uses Windows APIs (raw disk access, WMI, SetupAPI) that have "
        "no cross-platform equivalent.\n"
        "\n"
        "If you installed via pip, you can uninstall with:\n"
        "  pip uninstall flint-usb\n"
        "\n"
        "For details see: https://github.com/flintusb/Flint",
        file=sys.stderr,
    )
    sys.exit(1)
_crash_file_stack = ExitStack()
_SINGLE_INSTANCE_NAME = "FlintFlashingApp_v1"
_CRASH_PATH = os.path.join(
    os.environ.get("LOCALAPPDATA", os.environ.get("TEMP", ".")),
    "Flint",
    "crash.log",
)
# U16: the most this ever reads off the GUI thread, whatever the log's size.
_CRASH_TAIL_BYTES = 64 * 1024


def _log(message: str) -> None:
    logger.info(message)


def _install_crash_logging() -> None:
    try:
        os.makedirs(os.path.dirname(_CRASH_PATH), exist_ok=True)
        # The file must stay open for the process lifetime (faulthandler and
        # the excepthook write to it), so it is kept in a module-level
        # ExitStack rather than closed at the end of this function.
        crash = _crash_file_stack.enter_context(
            open(_CRASH_PATH, "a", encoding="utf-8")  # noqa: SIM115
        )
        faulthandler.enable(crash)

        def _hook(
            exc_type: type[BaseException],
            exc_value: BaseException,
            exc_tb: TracebackType | None,
        ) -> None:
            try:
                crash.write(
                    "".join(
                        traceback.format_exception(
                            exc_type, exc_value, exc_tb
                        )
                    )
                )
                crash.flush()
            except OSError:
                pass
            logger.exception("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))
            sys.__excepthook__(exc_type, exc_value, exc_tb)

        sys.excepthook = _hook
    except OSError:
        logger.exception("Failed to install crash logging")


def _windowless_python() -> str:
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        alt = exe[: -len("python.exe")] + "pythonw.exe"
        if os.path.isfile(alt):
            return alt
    return exe


def _is_launcher_exe(path: str) -> bool:
    """Return True if path is a real .exe file (pip launcher or frozen)."""
    if os.path.isfile(path):
        return path.lower().endswith(".exe")
    # Python 3.12+ pip scripts may omit the .exe extension in sys.argv[0].
    return bool(os.path.isfile(path + ".exe"))


def _ensure_admin(settings: Any) -> None:
    if ctypes.windll.shell32.IsUserAnAdmin():
        _log("already elevated")
        return
    # Respect user preference: ask before auto-elevating.
    try:
        ask = bool(settings.get("ask_before_elevation"))
    except Exception:
        ask = True
    if ask:
        # Use a native MessageBox to ask before elevation since QApplication
        # may not exist yet.
        try:
            MB_YESNO = 0x04
            MB_TOPMOST = 0x40000
            IDYES = 6
            res = ctypes.windll.user32.MessageBoxW(
                None,
                "Flint requires administrator privileges to access raw disks.\n\nElevate now?",
                "Flint — elevation required",
                MB_YESNO | MB_TOPMOST,
            )
            if res != IDYES:
                _log("user declined elevation; continuing without admin")
                return
        except Exception:
            # If the native dialog fails, fall back to auto-elevate.
            pass

    # If we're running from a real .exe (pip console/gui-script launcher or
    # PyInstaller frozen), relaunch that exe directly with the remaining args.
    # Otherwise fall back to pythonw + script (dev mode).
    if _is_launcher_exe(sys.argv[0]):
        exe = sys.argv[0]
        if not exe.lower().endswith(".exe") and os.path.isfile(exe + ".exe"):
            exe = exe + ".exe"
        args = sys.argv[1:]
    else:
        exe = _windowless_python()
        args = sys.argv
    _log(f"not elevated; spawning runas: {exe} {args}")
    params = subprocess.list2cmdline(args)
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", exe, params, None, 10  # SW_SHOWDEFAULT
    )
    _log(f"ShellExecuteW returned {result}")
    if result <= 32:
        _log(f"elevation failed (error {result}); continuing without admin")
        return
    sys.exit(0)


_server_global: Any | None = None


def release_single_instance() -> None:
    """Drop the single-instance lock before spawning an elevated relaunch.

    The relaunched process must be able to acquire the pipe; without this,
    the old instance (still alive while it shuts down) holds it and the new
    instance exits thinking another copy is running.
    """
    from PyQt6.QtNetwork import QLocalServer

    global _server_global
    if _server_global is None:
        return
    server = _server_global
    _server_global = None
    try:
        server.close()
        QLocalServer.removeServer(_SINGLE_INSTANCE_NAME)
    except Exception:
        logger.exception("failed to release single-instance lock")


def _acquire_single_instance(
    qlocal_server: Any,
    qlocal_socket: Any,
    name: str = _SINGLE_INSTANCE_NAME,
) -> Any | None:
    # If another instance is alive, poke it to come to the foreground and
    # exit. A listen failure right after that means the other instance is
    # mid-exit (e.g. an elevation relaunch): retry briefly instead of
    # exiting immediately.
    for _ in range(6):
        probe = qlocal_socket()
        probe.connectToServer(name)
        if probe.waitForConnected(500):
            probe.write(b"show")
            probe.flush()
            probe.waitForBytesWritten(500)
            probe.disconnectFromServer()
            return None
        probe.abort()
        qlocal_server.removeServer(name)
        server = qlocal_server()
        if server.listen(name):
            return server
        time.sleep(0.3)
    return None


def _maybe_show_crash_report(
    window: Any,
    settings: Any,
    dialogs: Any,
    application: Any,
) -> None:
    """If the crash log grew since the last run, offer to copy the report.

    U16/H5: this runs from a 400 ms timer on the GUI thread, so only the
    tail of the (unbounded) log is ever read — the recent traceback is
    the useful part, and a multi-megabyte log must not stall startup.
    """
    try:
        size = os.path.getsize(_CRASH_PATH)
    except OSError:
        return
    seen = int(settings.get("crash_report_seen_bytes") or 0)
    if size <= seen:
        return
    start = max(seen, size - _CRASH_TAIL_BYTES)
    try:
        with open(_CRASH_PATH, "rb") as f:
            f.seek(start)
            chunk = f.read(size - start)
    except OSError:
        return
    content = chunk.decode("utf-8", errors="replace")
    settings.set_many(crash_report_seen_bytes=size)
    if not content.strip():
        return
    dlg = dialogs.FlintDialog(
        window,
        kind="warning",
        title="Flint \u2014 crashed last time",
        message=(
            "Flint crashed on a previous run.\n\n"
            "The crash report has been saved to disk."
        ),
        buttons=[
            ("Copy report", "ghost", "copy"),
            ("Ignore", "primary", "no"),
        ],
    )
    if dlg.run() == "copy":
        clipboard = application.clipboard()
        if clipboard is not None:
            clipboard.setText(content.strip())


def _console_available() -> bool:
    """Return True when a console this process can print to is attached.

    Decides what a bare ``flint`` does: with a console (cmd, PowerShell, a
    pip launcher typed in a terminal) the CLI command overview is shown;
    without one (double-clicked flint.exe, pythonw, flintw) the GUI starts,
    since CLI output there would be invisible.
    """
    if sys.stdout is None or sys.stderr is None:
        # pythonw / flintw / flint.pyw: windowless interpreter, no stdio.
        return False
    try:
        import msvcrt

        handle = msvcrt.get_osfhandle(sys.stdout.fileno())
        # FILE_TYPE_DISK=1, FILE_TYPE_CHAR=2 (console), FILE_TYPE_PIPE=3.
        if handle != -1 and ctypes.windll.kernel32.GetFileType(handle) in (
            1,
            2,
            3,
        ):
            return True
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        # Windowed PyInstaller build launched from cmd/PowerShell: the std
        # handles are not real, but the parent console can be attached to
        # (the same trick as cli._ensure_cli_stdio). From Explorer there is
        # no parent console, AttachConsole fails, and the GUI is the answer.
        try:
            ATTACH_PARENT_PROCESS = 0xFFFFFFFF
            if ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
                sys.stdout = open(  # noqa: SIM115
                    "CONOUT$", "w", encoding="utf-8", errors="replace"
                )
                sys.stderr = open(  # noqa: SIM115
                    "CONOUT$", "w", encoding="utf-8", errors="replace"
                )
                sys.stdin = open(  # noqa: SIM115
                    "CONIN$", "r", encoding="utf-8", errors="replace"
                )
                return True
        except Exception:
            pass
    return False


def _run_gui() -> int:
    """Start the desktop app: elevation, single-instance, tray, main window."""
    global logger
    global _server_global

    try:
        from PyQt6.QtCore import QTimer
        from PyQt6.QtNetwork import QLocalServer, QLocalSocket
        from PyQt6.QtWidgets import QApplication

        from core import settings, writeback
        from core.log import setup_logging
        from ui import dialogs
        from ui.style import build_style
        from ui.window import MainWindow

        logger = setup_logging()
        _log(f"start: pid={os.getpid()} argv={sys.argv}")
        _install_crash_logging()
        _ensure_admin(settings)
        # U15: settings/history fsyncs move off the GUI thread; the CLI
        # (and the tests) keep the synchronous default.
        writeback.enable()
        app = QApplication(sys.argv)
        startup_theme = settings.get("theme")
        if startup_theme == "auto":
            from ui.widgets import _windows_uses_dark_mode

            startup_theme = "dark" if _windows_uses_dark_mode() else "light"
        app.setStyleSheet(build_style(startup_theme))
        app.setQuitOnLastWindowClosed(False)

        server = _acquire_single_instance(QLocalServer, QLocalSocket)
        if server is None:
            _log("another instance is running; exiting")
            return 0
        _server_global = server

        window = MainWindow()

        def _on_new_instance() -> None:
            while server.hasPendingConnections():
                conn = server.nextPendingConnection()
                if conn is None:
                    continue

                def _on_data(c: QLocalSocket = conn) -> None:
                    c.readAll()
                    window._force_show()

                conn.readyRead.connect(_on_data)

        server.newConnection.connect(_on_new_instance)

        window.show()
        _log(
            f"shown: visible={window.isVisible()} "
            f"minimized={window.isMinimized()} "
            f"native_visible={window._native_is_visible()}"
        )

        def _ensure_visible() -> None:
            window._force_show()
            if window.isMinimized():
                window.showNormal()
            _log(
                f"retry show: visible={window.isVisible()} "
                f"minimized={window.isMinimized()} "
                f"native_visible={window._native_is_visible()}"
            )

        def _on_quit() -> None:
            window._shutdown()
            _log(
                f"aboutToQuit: visible={window.isVisible()} "
                f"writer={window._writer is not None} "
                f"verifier={window._page_verifier is not None} "
                f"tray={window._tray is not None}"
            )

        app.aboutToQuit.connect(_on_quit)
        QTimer.singleShot(
            400,
            lambda: _maybe_show_crash_report(
                window, settings, dialogs, QApplication
            ),
        )
        QTimer.singleShot(300, _ensure_visible)
        QTimer.singleShot(1200, _ensure_visible)
        QTimer.singleShot(3000, _ensure_visible)
        # Quiet 7-day update check; never blocks and only surfaces a new release.
        try:
            QTimer.singleShot(6000, window._maybe_auto_check_updates)
        except Exception:
            logger.exception("failed to schedule update check")

        rc = app.exec()
        _log(f"app exited rc={rc}")
        return rc
    except Exception:
        logger.exception("GUI failed to start")
        try:
            print(
                "GUI failed to start — try 'flint doctor' for diagnostics "
                "or use CLI commands like 'flint list'",
                file=sys.stderr,
                flush=True,
            )
        except OSError:
            pass
        return 1


def gui_main() -> int:
    """Entry point for windowless launchers (flintw, flint.pyw): always the GUI."""
    return _run_gui()


def main(argv: list[str] | None = None) -> int:
    """Dispatch between the CLI and the desktop app.

    - ``flint launch`` (no other arguments) opens the GUI. A second
      invocation focuses the already-running window (single instance).
    - A bare ``flint`` shows the CLI command overview when a console is
      attached, and starts the GUI when there is none (double-clicked
      flint.exe, pythonw) so the portable no-install flow keeps working.
    - Anything else is a CLI command; unknown commands are explained by
      the CLI and can never silently open a window.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    # Route on the non-global arguments only: '--json launch' etc. still
    # mean "open the app" (the GUI ignores global output flags).
    routed = [a for a in args if a not in ("--cli", "--json", "--quiet")]
    if routed == ["launch"]:
        return _run_gui()
    if not args:
        if _console_available():
            from core import cli

            return cli.main([])
        return _run_gui()
    from core import cli

    return cli.main(args)


if __name__ == "__main__":
    sys.exit(main())
