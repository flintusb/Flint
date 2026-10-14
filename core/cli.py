"""Headless / scriptable mode.

Modern invocation (no ``--cli`` prefix needed — it is accepted as a
compat alias)::

    flint list
    flint flash  --image <file> --drive <serial|letter|path> --confirm <serial> [--verify] [--resume]
    flint verify --drive <serial|letter|path> [--sha256 <hex> --image <file>]
    flint wipe   --drive <serial|letter|path> --confirm <serial> [--method zero|random|nist|dod]
    flint backup --drive <serial|letter|path> --out <file> [--confirm <serial>]
    flint clone  --from <serial|letter|path> --to <serial|letter|path> --confirm <serial of --to>
    flint queue  --file <list.txt> --drive <serial|letter|path> --confirm <serial>
    flint flash-all --image <file> [--image <file> ...] --confirm ARM [--timeout <seconds>]
    flint doctor
    flint completions
    flint launch
    flint help [<command>]

A bare ``flint`` (no arguments) prints a command overview. The desktop
app is reached with ``flint launch``, with ``flintw``, or by
double-clicking ``flint.exe`` — never by accident from a terminal.

Add ``--json`` anywhere for NDJSON output (:envvar:`FLINT_PROGRESS=json`
is equivalent); add ``--quiet`` to suppress progress and informational
messages. :envvar:`FLINT_VERIFY=1` makes ``flash`` verify by default.

Streams: data and the final ``RESULT`` line go to **stdout**; progress
``FLINT <pct> <speed>MB/s ETA <s>s`` lines and informational notes go
to **stderr**, so scripts can capture stdout as pure data without
``2>&1`` noise. ``completions`` is the exception: its whole stdout is a
shell script, so its ``RESULT`` line goes to **stderr**.

Every command needing elevation is relaunched via UAC automatically when
run unelevated. The privilege-free bucket is ``list``, ``doctor``,
``report``, ``completions``, ``help`` and the read-only ``deploy
--status | --cancel | --retry`` job sub-commands — ``deploy --run``
performs a raw disk write, so it elevates exactly like ``flash``.

Compressed images (``.zip`` / ``.gz`` / ``.xz``, plus ``.zst`` when the
``zstandard`` package is installed) are extracted to a temporary file
before writing: an archive is never written raw. When a
``<image>.sha256`` sidecar sits next to the image it must parse and
match the image digest, otherwise the flash fails before the drive is
touched.

Destructive commands validate against the live drive list and require
typing the full serial of the destroyed drive (``--confirm``); when run
interactively and no ``--confirm`` is passed, the serial is prompted for
instead. Pass ``--yes`` to bypass confirmation for non-interactive
scripts.

Exit codes: 0 ok, 1 failure, 2 cancelled, 3 usage/validation, 4
elevation denied.
"""

import ctypes
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from datetime import datetime
from typing import Any

from PyQt6.QtCore import QCoreApplication, QEventLoop

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_CANCELLED = 2
EXIT_USAGE = 3
EXIT_NO_ADMIN = 4

_VALUE_OPTS = {
    "image",
    "drive",
    "out",
    "file",
    "method",
    "confirm",
    "from",
    "to",
    "sha256",
    "timeout",
    "retries",
    "shell",
    "partition-scheme",
    "filesystem",
    "write-mode",
    "parallel",
    "job",
    "format",
}
_FLAG_OPTS = {
    "verify",
    "json",
    "help",
    "skip-flashed",
    "yes",
    "quiet",
    "dry-run",
    "copy-report",
    "verbose",
    "resume",
    "bypass-tpm",
    "check-fake",
    "integrity",
    "status",
    "cancel",
    "retry",
    "run",
}

_METHODS = ("zero", "random", "nist", "dod")

# D10: the completion scripts are generated from these two sets so a flag
# added here can never be missing from the PowerShell/bash/zsh lists.
_OPTION_HELP: dict[str, str] = {
    "image": "ISO/IMG/DD image file",
    "drive": "target drive serial, letter, or path",
    "out": "output file",
    "file": "queue file listing images",
    "method": "erasure pattern (zero|random|nist|dod)",
    "confirm": "serial of the target drive",
    "from": "source drive serial or letter",
    "to": "target drive serial or letter",
    "sha256": "expected SHA-256 digest",
    "timeout": "timeout in seconds",
    "retries": "retries per failed read",
    "shell": "completion shell (powershell|bash|zsh)",
    "partition-scheme": "partition table type (gpt|mbr)",
    "filesystem": "filesystem (fat32|ntfs|exfat)",
    "write-mode": "write mode (auto|dd|filecopy)",
    "parallel": "concurrent targets (1-8)",
    "job": "deployment job id",
    "format": "export format (json|csv|markdown)",
    "verify": "read back and compare digests",
    "json": "NDJSON machine-readable output",
    "help": "show help",
    "skip-flashed": "skip drives already flashed",
    "yes": "bypass confirmation",
    "quiet": "suppress progress messages",
    "dry-run": "preview without performing",
    "copy-report": "copy flash report to clipboard",
    "verbose": "extra detail",
    "resume": "resume an interrupted write",
    "bypass-tpm": "patch boot.wim to skip Windows 11 TPM",
    "check-fake": "probe for counterfeit capacity",
    "integrity": "verify audited history records",
    "status": "list persisted deployment jobs",
    "cancel": "cancel a queued deployment job",
    "retry": "return a failed job to queued state",
    "run": "execute a persisted deployment job",
    "version": "show version",
    "cli": "legacy compat prefix",
}
_ALL_OPTIONS: list[str] = sorted(
    f"--{name}"
    for name in _VALUE_OPTS | _FLAG_OPTS | {"version", "cli"}
)


def _zsh_option_lines() -> str:
    """One zsh ``_arguments`` entry per option, value-taking ones first-class."""
    lines = []
    for name in (n[2:] for n in _ALL_OPTIONS):
        desc = _OPTION_HELP.get(name, name)
        if name in ("image", "out", "file"):
            lines.append(f"        '--{name}[{desc}]':file:_files'")
        elif name in _VALUE_OPTS:
            lines.append(f"        '--{name}[{desc}]:'")
        else:
            lines.append(f"        '--{name}[{desc}]'")
    return "\n".join(lines)


_JSON = False
_QUIET = False
logger = logging.getLogger("flint")


def _colorize(code: str, text: str) -> str:
    """Wrap text in an ANSI colour when stdout is an interactive terminal."""
    if _JSON:
        return text
    try:
        if sys.stdout is None or not sys.stdout.isatty():
            return text
    except (AttributeError, OSError):
        return text
    return f"\033[{code}m{text}\033[0m"


def _encode_fallback(stream: Any, line: str) -> str:
    """Re-encode *line* for a stream that rejected it (B17).

    Redirected consoles are ANSI-codepage pipes (cp1252 on most Windows
    boxes), where the Unicode RESULT icons/box drawing cannot encode.
    Round-tripping through the stream's own codec with ``errors="replace"``
    guarantees a printable line for any encoding.
    """
    enc = getattr(stream, "encoding", None) or "ascii"
    try:
        return line.encode(enc, errors="replace").decode(enc)
    except Exception:
        return line.encode("ascii", errors="replace").decode("ascii")


def _print(line: str) -> None:
    """Data line: stdout (RESULT, DRIVE, help, JSON)."""
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # B17: a RESULT line must never crash the CLI just because the
        # consumer's pipe uses a legacy codepage.
        try:
            print(_encode_fallback(sys.stdout, line), flush=True)
        except OSError:
            pass
    except OSError:
        pass


def _eprint(line: str) -> None:
    """Informational line: stderr (progress, notes, usage errors)."""
    if _QUIET:
        return
    try:
        print(line, file=sys.stderr, flush=True)
    except UnicodeEncodeError:
        try:
            print(_encode_fallback(sys.stderr, line), file=sys.stderr, flush=True)
        except OSError:
            pass
    except OSError:
        pass


def _emit_json(**fields: object) -> None:
    _print(json.dumps(fields, separators=(",", ":")))


_WINERROR_HINTS: dict[int, str] = {
    5: "try running as administrator",
    21: "drive not responding \u2014 check cable and port",
    31: "device hardware failure \u2014 try another USB port",
    1167: "drive was disconnected during operation",
}


def _winerror_hint(exc: BaseException) -> str:
    """Map common Windows error codes to actionable hints."""
    code = getattr(exc, "winerror", None)
    if code is None:
        return ""
    return _WINERROR_HINTS.get(code, "")


def _result(
    status: str, message: str, exit_code: int, *, to_stderr: bool = False
) -> int:
    """Emit the final machine-readable line and return the exit code.

    ``to_stderr`` routes the text-mode line away from stdout, for the one
    command whose whole stdout payload is a document (``completions``)."""
    if _JSON:
        _emit_json(type="result", status=status, message=message, exit=exit_code)
    else:
        icons = {
            "ok": ("✓", "32"),
            "fail": ("✗", "31"),
            "canceled": ("⚠", "33"),
            "cancelled": ("⚠", "33"),
        }
        icon, colour = icons.get(status, ("", "33"))
        prefix = f"{icon} " if icon else ""
        line = _colorize(colour, f"{prefix}RESULT {status}: {message}")
        if to_stderr:
            try:
                print(line, file=sys.stderr, flush=True)
            except OSError:
                pass
        else:
            _print(line)
    return exit_code


def _env_flag(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return value in ("1", "true", "yes", "on")


def _missing(opts: dict[str, object], name: str, syntax: str) -> str | None:
    """Return a missing-required-option message when *name* was not supplied.

    Distinguishes a required flag being absent from the flag being given a
    value that does not exist (which the caller reports separately)."""
    if str(opts.get(name, "")).strip():
        return None
    return f"missing required option --{name} <{syntax}>"


def _block_bar(pct: float, width: int = 16) -> str:
    """Render a block-character progress bar: ▓▓▓▓▓░░░░░."""
    fill = int(pct / 100 * width)
    return "\u2593" * fill + "\u2591" * (width - fill)


def _signoff() -> None:
    """Print a short branded sign-off after successful destructive commands."""
    if _JSON:
        return
    _eprint("\n  \u2b21 Flint out.")


def _ensure_cli_stdio() -> None:
    """Give the packaged (windowed) build real stdio for CLI mode.

    PyInstaller builds flint with ``console=False``, so the GUI never
    flashes a console. CLI invocations launched from PowerShell or
    Explorer then have no stdout handle at all and ``print`` raises
    OSError under Python 3.14's stricter stdio handling. When the std
    handles are not real files or pipes (cmd-style redirection), attach
    to the parent console instead so CLI output is visible. In dev there
    is a real console and nothing happens.
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        import msvcrt

        for stream in (sys.stdout, sys.stderr):
            fd = stream.fileno()
            handle = msvcrt.get_osfhandle(fd)
            if handle != -1 and ctypes.windll.kernel32.GetFileType(handle) in (
                1,
                3,
            ):
                return
    except Exception:
        pass
    try:
        # ATTACH_PARENT_PROCESS: reuse the console of the process that
        # launched us, then reopen the standard streams onto it.
        if ctypes.windll.kernel32.AttachConsole(0xFFFFFFFF):
            # These streams must outlive the function; they become
            # sys.stdout/stderr/stdin for the rest of the process.
            sys.stdout = open(  # noqa: SIM115
                "CONOUT$", "w", encoding="utf-8", errors="replace"
            )
            sys.stderr = open(  # noqa: SIM115
                "CONOUT$", "w", encoding="utf-8", errors="replace"
            )
            sys.stdin = open(  # noqa: SIM115
                "CONIN$", "r", encoding="utf-8", errors="replace"
            )
    except Exception:
        pass


def _interactive() -> bool:
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except Exception:
        return False


def _prompt(prompt: str) -> str | None:
    """Read one line from the interactive terminal; None on failure."""
    if not _interactive():
        return None
    try:
        _eprint(prompt)
        line = input()
    except EOFError:
        return None
    except OSError:
        return None
    return line


# ---------------------------------------------------------------------------
# Option parsing and help
# ---------------------------------------------------------------------------


def _opts(argv: list[str]) -> tuple[dict[str, object], str | None]:
    """Parse ``--name value`` pairs; return ``(opts, error)``."""
    opts: dict[str, object] = {}
    i = 0
    missing_value_errors = {
        "image": "requires --image <file>",
        "drive": "requires --drive <serial|letter|path>",
        "out": "requires --out <file>",
        "file": "requires --file <file>",
        "method": "requires --method <zero|random|nist|dod>",
        "confirm": "requires --confirm <serial>",
        "from": "requires --from <serial|letter|path>",
        "to": "requires --to <serial|letter|path>",
        "sha256": "requires --sha256 <hex>",
        "timeout": "requires --timeout <seconds>",
        "retries": "requires --retries <1-10>",
    }
    while i < len(argv):
        arg = argv[i]
        if not arg.startswith("--"):
            return {}, f"unexpected argument: {arg}"
        name = arg[2:]
        if name in _VALUE_OPTS:
            if i + 1 >= len(argv):
                return {}, missing_value_errors.get(
                    name, f"missing value for --{name}"
                )
            opts[name] = argv[i + 1]
            i += 2
        elif name in _FLAG_OPTS or name == "cli":
            opts[name] = True
            i += 1
        else:
            return {}, f"unknown option: --{name}"
    return opts, None


_COMMAND_HELP: dict[str, str] = {
    "list": (
        "flint list\n"
        "  Print every detected drive with the serial that --confirm expects.\n"
        "  Needs no privileges; drive discovery can be the very first step of\n"
        "  a script (serials are printed exactly as they must be confirmed).\n"
        "  Options: --json\n"
        "  Example: flint list\n"
    ),
    "flash": (
        "flint flash --image <file> --drive <serial|letter|path>\n"
        "           [--confirm <serial> | --yes] [--verify] [--bypass-tpm]\n"
        "           [--partition-scheme gpt|mbr] [--filesystem fat32|ntfs|exfat]\n"
        "           [--write-mode auto|dd|filecopy] [--quiet]\n"
        "  Write an image to a drive, erasing everything on it. The image\n"
        "  is written raw (no filesystem) and verified by default when\n"
        "  FLINT_VERIFY=1 or --verify is given.\n"
        "  --image  <file>        the ISO/IMG/DD image to write\n"
        "  --drive  <serial|letter|path>\n"
        "                         target drive (see 'flint list')\n"
        "  --confirm <serial>     full serial of the target drive; when run\n"
        "                         interactively it can be omitted and is\n"
        "                         prompted for instead\n"
        "  --yes                  bypass drive confirmation without prompting\n"
        "  --verify               read back the drive and compare digests\n"
        "  --bypass-tpm           patch boot.wim to skip Windows 11 TPM,\n"
        "                         Secure Boot and RAM checks (file-copy mode)\n"
        "  --partition-scheme gpt|mbr\n"
        "                         partition table type (default: auto)\n"
        "  --filesystem fat32|ntfs|exfat\n"
        "                         filesystem for file-copy mode (default: fat32)\n"
        "  --write-mode auto|dd|filecopy\n"
        "                         auto picks per image; dd writes raw byte-for-byte\n"
        "                         and filecopy repartitions and copies files. The\n"
        "                         aliases 'raw' (for dd) and 'file-copy' (for\n"
        "                         filecopy) are accepted too (default: auto)\n"
        "  --check-fake           probe the drive for counterfeit capacity\n"
        "                         before flashing (non-destructive, ~1 s)\n"
        "  --resume               continue an interrupted write from the last\n"
        "                         saved offset (reads '<image>.flint_job.json');\n"
        "                         only meaningful for raw writes to the same drive\n"
        "  --quiet                suppress progress and informational messages\n"
        "  Example: flint flash --image C:\\img\\ubuntu.iso --drive E: --confirm 4C530001270509112345\n"
    ),
    "verify": (
        "flint verify --drive <serial|letter|path>\n"
        "             [--sha256 <hex> --image <file>]\n"
        "  Verify a drive. Without --sha256 it is a read-only bad-block scan\n"
        "  of the whole drive. With --sha256 the digest covers only the image\n"
        "  bytes, so --image is required to know how many bytes to compare.\n"
        "  Example: flint verify --drive E: --sha256 0a4b8c… --image C:\\img\\ubuntu.iso\n"
    ),
    "wipe": (
        "flint wipe --drive <serial|letter|path>\n"
        "          [--confirm <serial> | --yes] [--method zero|random|nist|dod]\n"
        "          [--quiet]\n"
        "  Erase a drive, destroy all data on it.\n"
        "  --method zero|random|nist|dod   erasure pattern (default zero)\n"
        "  --yes                          bypass drive confirmation without prompting\n"
        "  --quiet                        suppress progress and informational messages\n"
        "  Example: flint wipe --drive E: --confirm 4C530001270509112345 --method nist\n"
    ),
    "backup": (
        "flint backup --drive <serial|letter|path> --out <file>\n"
        "             [--confirm <serial>] [--yes] [--quiet]\n"
        "  Copy a drive byte-for-byte into an image file. Read-only, so\n"
        "  confirmation is optional.\n"
        "  --yes                  continue without optional serial validation\n"
        "  --quiet                suppress progress and informational messages\n"
        "  Example: flint backup --drive E: --out C:\\img\\backup.img\n"
    ),
    "clone": (
        "flint clone --from <serial|letter|path> --to <serial|letter|path>\n"
        "            [--confirm <serial of --to> | --yes] [--quiet]\n"
        "  Copy one drive to another byte-for-byte. Only the target needs\n"
        "  confirmation; it must be at least as large as the source.\n"
        "  --yes                  bypass target confirmation without prompting\n"
        "  --quiet                suppress progress and informational messages\n"
        "  Example: flint clone --from E: --to F: --confirm 4C530001270509112346\n"
    ),
    "queue": (
        "flint queue --file <list.txt> --drive <serial|letter|path>\n"
        "            [--confirm <serial> | --yes] [--verify] [--bypass-tpm]\n"
        "            [--write-mode auto|dd|filecopy] [--quiet]\n"
        "  Flash every image listed in a file (one per line; # comments\n"
        "  allowed) to the same drive, stopping on the first failure.\n"
        "  --yes                  bypass drive confirmation without prompting\n"
        "  --verify               read back and compare digests\n"
        "  --write-mode auto|dd|filecopy\n"
        "                         write mode for every image (default: auto)\n"
        "  --bypass-tpm           patch boot.wim (needs file-copy mode)\n"
        "  --quiet                suppress progress and informational messages\n"
        "  Example: flint queue --file queue.txt --drive E: --confirm 4C530001270509112345\n"
    ),
    "flash-all": (
        "flint flash-all --image <file> [--image <file> ...]\n"
        "                [--confirm ARM | --yes] [--timeout <seconds>]\n"
        "                [--skip-flashed] [--parallel <1-8>] [--quiet]\n"
        "  Fleet mode for scripts: flash every queued image to every drive\n"
        "  that is (or becomes) plugged in, one drive after another, until\n"
        "  the budget expires. A drive is skipped if any image does not fit;\n"
        "  a drive is flashed only once per run; a failed flash aborts the\n"
        "  fleet immediately.\n"
        "  --confirm ARM          arm the fleet; must be the literal word ARM\n"
        "                         (prompted for when run interactively)\n"
        "  --yes                  arm fleet mode without prompting\n"
        "  --timeout <seconds>    total budget; stop watching after this\n"
        "                         (default 3600); interrupt earlier with Ctrl+C\n"
        "  --skip-flashed         skip drives already flashed with the same image\n"
        "  --parallel <1-8>      concurrent target drives (default 1);\n"
        "                         every queued image is flashed per drive\n"
        "  --write-mode auto|dd|filecopy\n"
        "                         write mode for every image (default: auto)\n"
        "  --bypass-tpm           patch boot.wim (needs file-copy mode)\n"
        "  --quiet                suppress progress and informational messages\n"
        "  Example: flint flash-all --image C:\\img\\agent.iso --confirm ARM\n"
    ),
    "deploy": (
        "flint deploy --image <file> --drive <serial|letter|path> [--drive ...]\n"
        "             [--confirm ARM | --yes] [--parallel <1-8>]\n"
        "             [--write-mode auto|dd|filecopy] [--bypass-tpm]\n"
        "  Deploy one image to explicit target drives concurrently.\n"
        "  Each target is tracked independently and failures do not erase\n"
        "  the result of other targets. Default concurrency is 2.\n"
        "  --status               list persisted deployment jobs\n"
        "  --cancel --job <id>    cancel a queued/resumable job\n"
        "  --retry --job <id>     return a failed job to queued state\n"
        "  --run --job <id>       execute or resume a persisted job (writes,\n"
        "                         so it elevates like 'flint flash'; the other\n"
        "                         job sub-commands need no privileges)\n"
    ),
    "doctor": (
        "flint doctor\n"
        "  Print a diagnostic report: version, runtime, Python, OS,\n"
        "  architecture, elevation, native-writer availability and the live\n"
        "  drive list.\n"
        "  Options: --json\n"
        "  Example: flint doctor\n"
    ),
    "report": (
        "flint report [--out <file>] [--format json|csv|markdown] [--integrity]\n"
        "  Export local operation history or verify its hash-chain integrity.\n"
        "  --out <file>  copy the history JSON to a destination path\n"
        "  --integrity   fail if an audited record was changed or reordered\n"
    ),
    "completions": (
        "flint completions [--shell powershell|bash|zsh]\n"
        "  Print a shell completion script. Default is PowerShell.\n"
        "  --shell bash     generate bash completion script\n"
        "  --shell zsh      generate zsh completion script\n"
        "  --shell powershell  generate PowerShell completion script (default)\n"
        "  PowerShell: flint completions | Out-File -Append $PROFILE\n"
        "  Bash: flint completions --shell bash | sudo tee /etc/bash_completion.d/flint\n"
        "  Zsh: flint completions --shell zsh > /usr/local/share/zsh/site-functions/_flint\n"
    ),
    "scan": (
        "flint scan --drive <serial|letter|path> [--retries <1-10>] [--quiet]\n"
        "  Read every sector on a drive to find unreadable media.\n"
        "  Read-only; does not modify the drive. No image needed.\n"
        "  --retries <1-10>   retries per failed read (default 3)\n"
        "  --quiet            suppress progress and informational messages\n"
    ),
    "launch": (
        "flint launch\n"
        "  Open the Flint desktop app. Takes no options; if the app is\n"
        "  already running its window is brought to the front. Elevation\n"
        "  is requested by the app itself when needed. 'flintw' and\n"
        "  double-clicking flint.exe open the same app.\n"
    ),
}

# One-line descriptions for the overview screen and the zsh completion
# script — a single source so the two can never drift apart.
_COMMAND_SUMMARY: dict[str, str] = {
    "list": "List removable drives with the serial --confirm expects",
    "flash": "Write an image to a drive, erasing everything on it",
    "verify": "Verify a drive against a SHA-256 checksum or scan for bad blocks",
    "wipe": "Erase a drive (zero, random, nist, or DoD passes)",
    "backup": "Copy a drive byte-for-byte into an image file",
    "clone": "Copy one drive to another byte-for-byte",
    "queue": "Flash every image listed in a file, one after another",
    "flash-all": "Fleet mode \u2014 flash every queued image to every drive",
    "deploy": "Deploy one image to explicit target drives concurrently",
    "doctor": "Print a diagnostic report of this machine",
    "report": "Export or verify the local operation history",
    "completions": "Print a shell completion script",
    "scan": "Read every sector of a drive to find unreadable media",
    "launch": "Open the Flint desktop app",
    "help": "Show help for a command",
}

# Grouping for the overview screen only (the dispatcher stays flat).
_OVERVIEW_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Write images", ("flash", "queue", "flash-all", "deploy")),
    ("Inspect drives & media", ("list", "scan", "verify", "doctor")),
    ("Maintain drives", ("wipe", "backup", "clone", "report")),
    ("App & shell", ("launch", "completions", "help")),
)


def _usage() -> str:
    """Render the command overview shown for a bare ``flint`` and for help."""
    from core.version import APP_VERSION

    width = max(len(name) for name in _COMMAND_SUMMARY) + 2
    lines = [
        f"  \u2b21 Flint v{APP_VERSION}",
        "  Write. Verify. Trust.",
        "",
        "Usage: flint <command> [options]",
        "",
    ]
    for title, names in _OVERVIEW_GROUPS:
        lines.append(_colorize("90", title))
        for name in names:
            lines.append(f"  {name:<{width}}  {_COMMAND_SUMMARY[name]}")
        lines.append("")
    hints = (
        ("flint list", "Show the drives attached to this machine"),
        ("flint <command> --help", "Detailed help for any command"),
        ("flint launch", "Open the desktop app"),
    )
    hint_width = max(len(cmd) for cmd, _ in hints) + 2
    lines.append(_colorize("90", "Get started"))
    for cmd, hint in hints:
        lines.append(f"  {cmd:<{hint_width}}  {hint}")
    lines.extend(
        [
            "",
            "Docs: https://flintweb.vercel.app/cli",
            "",
            "Run 'flint <command> --help' or 'flint help <command>' for details.",
            "Run 'flint --version' for the version.",
        ]
    )
    return "\n".join(lines)


def _command_help(name: str) -> str:
    return _COMMAND_HELP.get(name, _usage())


# ---------------------------------------------------------------------------
# Elevation
# ---------------------------------------------------------------------------


_ELEVATION_TIMEOUT_SECONDS = 60.0


def _run_elevated_relaunch(
    ps: str,
    out_file: str,
    err_file: str,
) -> subprocess.CompletedProcess[str]:
    """Launch the PowerShell UAC relay with a hard timeout."""
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True,
        text=True,
        check=False,
        timeout=_ELEVATION_TIMEOUT_SECONDS,
    )


def ensure_elevated(argv: list[str]) -> int | None:
    """Return None when already elevated (or elevation is unavailable),
    otherwise relaunch elevated and return the child's exit code.

    The child's own exit codes pass through unchanged (1 fail, 2 cancelled,
    3 usage) and its stdout/stderr are relayed verbatim, so scripts keep
    the documented contract and always receive a RESULT line. Only a
    declined or denied UAC prompt itself maps to EXIT_NO_ADMIN.
    """
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return None
    except Exception:
        return None
    _eprint("administrator privileges required - relaunching elevated...")
    # When argv[0] is a real .exe (pip launcher or frozen binary), launch
    # it directly; otherwise wrap in python.exe (dev mode).
    if argv[0].lower().endswith(".exe") and os.path.isfile(argv[0]):
        exe = argv[0].replace("'", "''")
        args_list = argv[1:]
    else:
        exe = sys.executable.replace("'", "''")
        args_list = argv
    # Build PowerShell-compatible argument list: quote each arg individually
    # instead of using cmd-style list2cmdline (which uses double-quotes
    # incompatible with PowerShell -ArgumentList).
    ps_args = " ".join(
        "'" + a.replace("'", "''") + "'" for a in args_list
    )
    tmp = tempfile.mkdtemp(prefix="flint-elev-")
    out_file = os.path.join(tmp, "stdout.txt")
    err_file = os.path.join(tmp, "stderr.txt")
    try:
        ps = (
            "$ErrorActionPreference = 'Stop'; "
            "try { "
            f"$p = Start-Process -FilePath '{exe}' "
            f"-ArgumentList '{ps_args.replace(chr(39), chr(39) * 2)}' "
            f"-Verb RunAs -Wait -PassThru "
            f"-RedirectStandardOutput '{out_file}' "
            f"-RedirectStandardError '{err_file}'; "
            "exit $p.ExitCode "
            "} catch { exit 255 }"
        )
        proc = _run_elevated_relaunch(ps, out_file, err_file)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _eprint(f"could not relaunch elevated: {exc}")
        return EXIT_NO_ADMIN
    stdout_text = ""
    stderr_text = ""
    try:
        with open(out_file, encoding="utf-8", errors="replace") as fh:
            stdout_text = fh.read()
    except OSError:
        pass
    try:
        with open(err_file, encoding="utf-8", errors="replace") as fh:
            stderr_text = fh.read()
    except OSError:
        pass
    try:
        shutil.rmtree(tmp, ignore_errors=True)
    except OSError:
        pass
    if stdout_text:
        _print(stdout_text.rstrip("\r\n"))
    if stderr_text:
        _eprint(stderr_text.rstrip("\r\n"))
    if proc.returncode == 255:
        _eprint("elevation denied (UAC prompt not accepted)")
        return EXIT_NO_ADMIN
    if proc.returncode in (EXIT_OK, EXIT_FAIL, EXIT_CANCELLED, EXIT_USAGE):
        return proc.returncode
    _eprint(f"elevation relaunch failed (exit {proc.returncode})")
    return EXIT_NO_ADMIN


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------


def _detect_drives(detector: Any = None) -> list[dict[str, Any]]:
    if detector is None:
        from core.drives import DriveDetector

        detector = DriveDetector()
    return detector.list_removable_drives()  # type: ignore[no-any-return]


def _resolve_drive(
    spec: str, drives: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Match a drive by physical path, serial, or volume letter."""
    if not drives:
        return None
    lowered = spec.casefold()
    for drive in drives:
        if spec == drive.get("physical_path"):
            return drive
        serial = (drive.get("serial") or "").casefold()
        if serial and serial == lowered:
            return drive
        letters = drive.get("letters") or (
            [drive["letter"]] if drive.get("letter") else []
        )
        if any(letter.casefold() == lowered.strip(":") for letter in letters):
            return drive
    return None


def _is_system_disk(drive: dict[str, Any]) -> bool:
    """Return True if *drive* hosts the running OS partition."""
    from core.drives import DriveDetector

    detector = DriveDetector()
    system_paths = detector._system_disk_paths()
    if system_paths is None:
        # Identification failed upstream. This is a guard for DESTRUCTIVE
        # operations: unknown must fail closed (treat as system disk and
        # refuse), exactly like DriveDetector's own B01 contract. A drive
        # that got here with an unresolvable system disk comes from
        # somewhere unusual — never raw-write it on a guess.
        logger.warning(
            "system disk unknown while checking %s; refusing destructive "
            "operation",
            drive.get("physical_path"),
        )
        return True
    return drive.get("physical_path", "") in system_paths


def _drive_capacity(drive: dict[str, Any]) -> int:
    """Exact drive capacity in bytes.

    Prefers the detector's raw ``size_bytes`` (no rounding); falls back to
    the rounded ``size_gb * 1e9`` for drive records that predate it."""
    size = drive.get("size_bytes")
    if isinstance(size, (int, float)) and size > 0:
        return int(size)
    return int(drive.get("size_gb", 0) * 1_000_000_000)


def _check_fake_drive(drive: dict[str, Any], assume_yes: bool) -> int | None:
    """Non-destructively probe *drive* for counterfeit capacity.

    Returns an exit code when the drive appears fake and the user has not
    consented with ``--yes``; returns ``None`` to allow the operation.
    """
    from core.fake_detect import probe_capacity

    reported = _drive_capacity(drive)
    if reported <= 0:
        return None
    _eprint("probing drive for counterfeit capacity...")
    suspicious, msg = probe_capacity(drive["physical_path"], reported)
    if not suspicious:
        _eprint(f"  {msg}")
        return None
    _eprint(f"WARNING: {msg}")
    if assume_yes:
        _eprint("proceeding anyway (--yes was given)")
        return None
    return _result("fail", f"fake drive detected — {msg}", EXIT_USAGE)


def _serial_of(drive: dict[str, Any]) -> str:
    return drive.get("serial") or drive.get("name") or ""


def _drive_letters(drive: dict[str, Any]) -> list[str]:
    letters = drive.get("letters") or (
        [drive["letter"]] if drive.get("letter") else []
    )
    return [str(letter) for letter in letters]


def _display_letters(drive: dict[str, Any]) -> str:
    letters = []
    for letter in _drive_letters(drive):
        value = letter.rstrip("\\")
        if len(value) == 1 and value.isalpha():
            value += ":"
        letters.append(value)
    return ",".join(letters) or "-"


def _record_flash_history(
    drive: dict[str, Any],
    image: str,
    *,
    started: float,
    ok: bool,
    verify: bool,
) -> None:
    """L07: append every CLI flash to audited history.

    Without this, `--skip-flashed` / the GUI's skip toggle could never see
    a stick flashed from the CLI (serial *and* physical-path matching both
    read history).  History is best-effort: a write that already happened
    must not fail because a log write did.
    """
    try:
        from core.history import append_audited_history, flash_report

        append_audited_history(
            flash_report(
                iso_name=os.path.basename(image),
                drive_model=str(
                    drive.get("model") or drive.get("name") or "unknown"
                ),
                duration_seconds=time.monotonic() - started,
                verified=bool(ok and verify),
                success=bool(ok),
                drive_serial=str(drive.get("serial") or "") or None,
                operation="flash",
                drive_path=str(drive.get("physical_path") or "") or None,
            )
        )
    except Exception:
        logger.debug("failed to record flash history", exc_info=True)


def _require_confirm(
    drive: dict[str, Any], confirmed: str | None
) -> str | None:
    if not confirmed:
        return "this command destroys a drive: pass --confirm <full serial>"
    serial = _serial_of(drive).casefold()
    if not serial:
        return "drive has no serial to confirm against"
    if confirmed.casefold() != serial:
        return (
            f"--confirm {confirmed!r} does not match the drive serial "
            f"{_serial_of(drive)!r}"
        )
    return None


def _confirm_drive(
    drive: dict[str, Any],
    confirmed: str | None,
    *,
    assume_yes: bool = False,
) -> str | None:
    """Resolve destruction confirmation.

    ``--yes`` bypasses confirmation entirely. Otherwise ``--confirm`` wins;
    when missing and the terminal is interactive, prompt for the full serial.
    ``--quiet`` never prompts: it falls back to the non-interactive error so
    scripts cannot hang on an invisible ``input()`` read.
    """
    if confirmed and assume_yes:
        return "pass either --confirm <serial> or --yes, not both"
    if assume_yes:
        return None
    if confirmed:
        return _require_confirm(drive, confirmed)
    if _interactive() and not _QUIET:
        serial = _serial_of(drive)
        name = drive.get("model") or drive.get("name") or "unknown device"
        size = drive.get("size_gb", 0)
        letters = _display_letters(drive)
        _eprint(
            f"Drive: {name} "
            f"(serial={serial}, {size}GB, letters={letters})"
        )
        _eprint("This will ERASE this drive and every existing file on it.")
        typed = _prompt(f"Type the full serial '{serial}' to continue:")
        if typed is None:
            return "cancelled (no input)"
        return _require_confirm(drive, typed.strip())
    return "this command destroys a drive: pass --confirm <full serial>"


def _arm_fleet_confirmation(
    confirmed: str | None,
    *,
    image_count: int,
    assume_yes: bool = False,
) -> str | None:
    """Fleet arming: --yes bypasses confirmation; otherwise ARM is required."""
    if confirmed and assume_yes:
        return "pass either --confirm ARM or --yes, not both"
    if assume_yes:
        return None
    if confirmed:
        if confirmed.strip().casefold() != "arm":
            return "--confirm must be the literal word ARM"
        return None
    if _interactive() and not _QUIET:
        _eprint(
            f"Fleet: {image_count} image(s); all fitting drives will be erased."
        )
        _eprint(
            "This will ERASE every fitting drive and every existing file on it."
        )
        typed = _prompt("Type ARM to arm fleet mode:")
        if typed is None:
            return "cancelled (no input)"
        if typed.strip().casefold() != "arm":
            return "arming cancelled: input was not ARM"
        return None
    return "this destroys every fitting drive: pass --confirm ARM to arm"


# ---------------------------------------------------------------------------
# Worker running
# ---------------------------------------------------------------------------

_CANCEL_GRACE_SECONDS = 30.0
_APP: QCoreApplication | None = None


def _ensure_app() -> QCoreApplication:
    """Return the process-wide ``QCoreApplication``, creating it on demand.

    Every ``QEventLoop`` needs an application object; commands dispatched
    through ``main()`` get one there, but helpers are also called directly
    (tests, embedded use), so the guarantee lives at the point of use."""
    global _APP
    app = QCoreApplication.instance()
    if app is None:
        _APP = QCoreApplication([])
        app = _APP
    return app


def _drain_after_cancel(
    worker: Any, loop: QEventLoop, outcome: dict[str, object]
) -> None:
    """Ctrl+C path: cancel the worker, then keep servicing its signals.

    The worker thread is asked to stop and its ``done`` emission is
    waited for (bounded by ``_CANCEL_GRACE_SECONDS``) so the caller can
    report a normal cancellation instead of abandoning a live thread.
    """
    cancel = getattr(worker, "cancel", None)
    if callable(cancel):
        try:
            cancel()
        except Exception:  # cancellation must not mask Ctrl+C
            pass
    _eprint("interrupted - cancelling, waiting for the worker to stop...")
    deadline = time.monotonic() + _CANCEL_GRACE_SECONDS
    while "ok" not in outcome and time.monotonic() < deadline:
        try:
            QCoreApplication.processEvents()
        except Exception:  # keep draining until the deadline
            pass
        time.sleep(0.05)
    if "ok" not in outcome:
        outcome["ok"] = False
        outcome["message"] = "cancelled"


def _run_worker(worker: Any) -> tuple[bool, str]:
    """Run a QThread worker to completion, streaming progress.

    Emits ``FLINT <pct> <speed>MB/s ETA <s>s`` on every worker signal and
    at 100% (or one NDJSON progress object per update with ``--json``);
    speed/ETA come from the worker's own signals when it provides them
    (writer/wipe/backup/clone), or are derived from reported bytes, or
    are reported as 0 / remaining-seconds when unavailable (verify).

    Ctrl+C during ``loop.exec()`` cancels the worker and waits (bounded)
    for it to report ``done`` instead of abandoning a running thread.
    """
    _ensure_app()
    loop = QEventLoop()
    outcome: dict[str, object] = {}
    state: dict[str, float] = {
        "pct": 0.0,
        "speed": 0.0,
        "written": 0.0,
        "total": 0.0,
    }
    stated_at = time.monotonic()
    last_print = {"t": 0.0}

    def _emit() -> None:
        elapsed = max(time.monotonic() - stated_at, 1e-6)
        speed = state["speed"]
        eta = 0
        if speed <= 0 and state["total"] > 0 and state["written"] > 0:
            speed = state["written"] / 1_000_000 / elapsed
            remaining = state["total"] - state["written"]
            eta = int(remaining / 1_000_000 / speed) if speed > 0 else 0
        if _JSON:
            _emit_json(
                type="progress",
                pct=round(state["pct"], 1),
                speed=round(speed, 1),
                eta=eta,
                written=int(state["written"]),
                total=int(state["total"]),
            )
        else:
            bar = _block_bar(state["pct"])
            _eprint(f"FLINT {bar} {state['pct']:.1f}% {speed:.1f}MB/s ETA {eta}s")
        last_print["t"] = time.monotonic()

    def on_progress(pct: float) -> None:
        if pct >= 100.0 or (
            pct > state["pct"]
            and time.monotonic() - last_print["t"] >= 0.25
        ):
            state["pct"] = pct
            _emit()

    def on_speed(mbps: float) -> None:
        state["speed"] = mbps

    def on_written(n: int) -> None:
        state["written"] = float(n)

    def on_total(n: int) -> None:
        state["total"] = float(n)

    def on_done(ok: bool, message: str) -> None:
        outcome["ok"] = ok
        outcome["message"] = message
        loop.quit()

    worker.progress.connect(on_progress)
    speed_signal = getattr(worker, "speed_mbps", None)
    if speed_signal is not None:
        speed_signal.connect(on_speed)
    written_signal = getattr(worker, "written_bytes", None)
    if written_signal is not None:
        written_signal.connect(on_written)
    total_signal = getattr(worker, "total_bytes", None)
    if total_signal is not None:
        total_signal.connect(on_total)
    worker.done.connect(on_done)
    worker.start()
    try:
        loop.exec()
    except KeyboardInterrupt:
        _drain_after_cancel(worker, loop, outcome)
    return bool(outcome.get("ok", False)), str(outcome.get("message", ""))


def _iso_digest(image: str) -> tuple[str | None, str | None]:
    from core.verify import compute_sha256

    _eprint(f"hashing {image}...")
    ok, result = compute_sha256(image)
    if not ok:
        return None, result
    # Informational only: stderr keeps stdout a pure data stream, which
    # --json mode depends on.
    _eprint(f"SHA256 {result}")
    return result, None


# ---------------------------------------------------------------------------
# Image preparation: sidecar trust, decompression, write mode
# ---------------------------------------------------------------------------


def _unsupported_compression(image: str) -> str | None:
    """Return a clear error when *image* is an archive we cannot extract.

    A compressed image must never be written raw, so an unsupported
    archive fails here — before any drive is selected, confirmed or
    touched — rather than being flashed byte-for-byte.
    """
    from core.decompress import COMPRESSED_EXTENSIONS, compressed_format

    fmt = compressed_format(image)
    if fmt is None:
        return None
    name = os.path.basename(image)
    if fmt not in COMPRESSED_EXTENSIONS:
        return (
            f"unsupported compression format {fmt} for {name} — "
            "supported archives are .zip, .gz, .xz and .zst"
        )
    if fmt == ".zst":
        try:
            import zstandard  # noqa: F401
        except ImportError:
            return (
                f"{name} is a .zst archive but the 'zstandard' package is "
                "not installed — run 'pip install zstandard' first "
                "(compressed images are never written raw)"
            )
    return None


def _is_backup_image(image: str) -> bool:
    """True when *image* is a Flint backup artifact (a raw disk image).

    ``core.backup`` produces a single ``.img`` file; the GUI names it
    ``flint-backup-%Y%m%d-%H%M%S.img``. Both markers select raw writing
    below, so a restore can never fall into the file-copy path.
    """
    name = os.path.basename(image).lower()
    if name.startswith("flint-backup-"):
        return True
    return os.path.splitext(name)[1] == ".img"


def _effective_write_mode(opts: dict[str, object], image: str) -> str:
    """Write mode actually used for *image*.

    ``auto`` (the default) normally falls through to
    ``diskpart.resolve_write_mode``'s detection inside the writer; a
    Flint backup is a raw disk image, so ``auto`` resolves to ``dd`` for
    it instead of letting auto-detection pick file-copy. An explicitly
    requested mode is always honoured.
    """
    mode = str(opts.get("write-mode", "auto") or "auto").strip()
    if mode.lower() in ("", "auto") and _is_backup_image(image):
        return "dd"
    return mode


def _bypass_tpm_error(opts: dict[str, object], images: list[str]) -> str | None:
    """Fail fast when ``--bypass-tpm`` cannot take effect.

    ``patch_boot_wim_on_usb`` is only reachable from the file-copy write
    path, so requesting the bypass for an image that would be written raw
    (hybrid image, ``--write-mode dd``, backup image, ...) is a usage
    error instead of a silent no-op.
    """
    if not opts.get("bypass-tpm"):
        return None
    from core.decompress import is_compressed
    from core.diskpart import resolve_write_mode

    for image in images:
        if is_compressed(image):
            # The container says nothing about the payload's format; the
            # check runs again on the extracted file before it is written.
            continue
        try:
            effective = resolve_write_mode(
                _effective_write_mode(opts, image), image
            )
        except Exception:
            logger.debug(
                "could not resolve write mode for %s", image, exc_info=True
            )
            continue
        if effective == "filecopy":
            continue
        return (
            "--bypass-tpm only takes effect in file-copy mode, but "
            f"{os.path.basename(image)} would be written as "
            f"'{effective}' — pass --write-mode filecopy (hybrid images "
            "and backup images always write raw)"
        )
    return None


def _check_sidecar(image: str) -> int | None:
    """Enforce a ``<image>.sha256`` sidecar before flashing.

    Absent sidecar → no check. Present sidecar → it must parse *and*
    match the image digest; unreadable/unparsable or mismatched sidecars
    abort with a RESULT line before the drive is touched.
    """
    from core.checksum import find_sidecar, sidecar_digest

    sidecar = find_sidecar(image)
    if sidecar is None:
        return None
    # E: pass the image name so a sidecar listing several files cannot
    # hand this image another file's digest.
    ok, expected = sidecar_digest(sidecar, os.path.basename(image))
    if not ok:
        return _result("fail", f"sidecar check failed: {expected}", EXIT_USAGE)
    digest, hash_error = _iso_digest(image)
    if digest is None:
        return _result(
            "fail", hash_error or "image hashing failed", EXIT_FAIL
        )
    if digest.lower() != expected.lower():
        return _result(
            "fail",
            f"{os.path.basename(image)} does not match {sidecar.name} "
            f"(sha256 {digest[:12]}\u2026 != {expected[:12]}\u2026)",
            EXIT_FAIL,
        )
    _eprint(f"sidecar checksum OK ({sidecar.name})")
    return None


def _prepare_images(images: list[str]) -> int | None:
    """Fail fast when any image in *images* cannot be written.

    Reports an unsupported archive (B04) or a failing ``<image>.sha256``
    sidecar (B11) for the first offender, before anything is touched.
    """
    for image in images:
        error = _unsupported_compression(image)
        if error:
            return _result("fail", error, EXIT_USAGE)
        rejected = _check_sidecar(image)
        if rejected is not None:
            return rejected
    return None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _drives_json(drives: list[dict[str, Any]]) -> list[dict[str, object]]:
    return [
        {
            "index": index,
            "name": drive.get("name") or drive.get("model") or "unknown device",
            "serial": _serial_of(drive),
            "size_gb": drive.get("size_gb", 0),
            "size_bytes": _drive_capacity(drive),
            "letters": drive.get("letters")
            or ([drive["letter"]] if drive.get("letter") else []),
            "path": drive.get("physical_path"),
        }
        for index, drive in enumerate(drives, 1)
    ]


def _cmd_list(opts: dict[str, object]) -> int:
    """Print every detected drive with the serial that --confirm expects.

    Needs no privileges, so scripts can discover serials first and then
    run destructive commands with the exact value."""
    drives = _detect_drives()
    if not drives:
        if _JSON:
            _emit_json(type="drives", drives=[])
        return _result(
            "ok",
            "no removable drives detected; run 'flint doctor' for diagnostics",
            EXIT_OK,
        )
    if _JSON:
        _emit_json(type="drives", drives=_drives_json(drives))
    else:
        rows: list[tuple[str, str, str, str, str, str]] = []
        for index, drive in enumerate(drives, 1):
            name = str(
                drive.get("name")
                or drive.get("model")
                or "unknown device"
            )[:20]
            serial = _serial_of(drive)[:20]
            size = f"{drive.get('size_gb', 0)}GB"
            letters = _display_letters(drive)
            path = str(drive.get("physical_path") or "")
            rows.append(
                (str(index), name, serial, size, letters, path)
            )

        headers = ("#", "NAME", "SERIAL", "SIZE", "LETTERS")
        widths = [
            max(len(headers[column]), *(len(row[column]) for row in rows))
            for column in range(len(headers))
        ]
        widths[1] = min(widths[1], 20)
        widths[2] = min(widths[2], 20)

        title = f"Drives ({len(drives)})"
        inner_width = (
            sum(widths) + 2 * (len(headers) - 1)
        )
        top_bar = "\u2500" * (inner_width - len(title) - 1)
        _eprint(f"\u250c\u2500 {title} {top_bar}\u2510")

        header_line = "  ".join(
            f"{headers[i]:<{widths[i]}}" for i in range(len(headers))
        )
        _eprint(f"\u2502 {header_line}\u2502")

        for row in rows:
            idx, name, serial, size, letters, _path = row
            name_column = _colorize("36", f"{name:<{widths[1]}}")
            line = (
                f"{idx:<{widths[0]}}  "
                f"{name_column}  "
                f"{serial:<{widths[2]}}  "
                f"{size:<{widths[3]}}  "
                f"{letters:<{widths[4]}}"
            )
            # Pad to inner_width accounting for ANSI escape codes in name
            visible_len = len(idx) + 2 + len(name) + 2 + len(serial) + 2 + len(size) + 2 + len(letters)
            pad = inner_width - visible_len
            _eprint(f"\u2502 {line}{' ' * max(pad, 0)}\u2502")

        bottom_bar = "\u2500" * inner_width
        _eprint(f"\u2514{bottom_bar}\u2518")
    return _result("ok", f"{len(drives)} drive(s) listed", EXIT_OK)


def _copy_to_clipboard(text: str) -> None:
    """Copy text to the Windows clipboard via PowerShell.

    Capped at 15 s: PowerShell startup plus ``Set-Clipboard``. Without the
    cap a wedged child hung `flint report` forever after the report itself
    had already been printed.
    """
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Set-Clipboard -Value '{text.replace(chr(39), chr(39)*2)}'"],
            capture_output=True,
            check=False,
            timeout=15,
        )
        _eprint("report copied to clipboard")
    except (OSError, subprocess.TimeoutExpired):
        pass


def _cmd_flash(opts: dict[str, object]) -> int:
    from core.decompress import compressed_format, decompress_image, is_compressed
    from core.writer import UsbWriter

    missing = _missing(opts, "image", "file")
    if missing:
        return _result("fail", missing, EXIT_USAGE)
    image = str(opts.get("image", ""))
    if not os.path.isfile(image):
        return _result("fail", f"--image file not found: {image}", EXIT_USAGE)
    missing = _missing(opts, "drive", "serial|letter|path")
    if missing:
        return _result("fail", missing, EXIT_USAGE)
    rejected = _prepare_images([image])
    if rejected is not None:
        return rejected
    compressed = is_compressed(image)
    # L13: a bypass that can never patch the image must fail here rather
    # than be accepted and silently ignored. Archives are skipped (the
    # payload is re-checked once it has been extracted).
    bypass_error = _bypass_tpm_error(opts, [image])
    if bypass_error:
        return _result("fail", bypass_error, EXIT_USAGE)
    drives = _detect_drives()
    drive = _resolve_drive(str(opts.get("drive", "")), drives)
    if drive is None:
        if _JSON:
            # Keep the NDJSON contract intact: the drive list is emitted as
            # one JSON object, never as raw text lines.
            _emit_json(type="drives", drives=_drives_json(drives))
        else:
            _eprint("drive not found; detected drives:")
            for d in drives:
                _eprint(
                    f"  serial={_serial_of(d)!r} "
                    f"path={d.get('physical_path')} "
                    f"letters={d.get('letters')}"
                )
        return _result(
            "fail",
            "drive not found — run 'flint list' to see available drives",
            EXIT_USAGE,
        )
    if _is_system_disk(drive):
        return _result(
            "fail",
            "refusing to flash the system disk (the OS is running from this drive)",
            EXIT_USAGE,
        )
    if opts.get("check-fake"):
        denied = _check_fake_drive(drive, bool(opts.get("yes")))
        if denied is not None:
            return denied
    issue = _confirm_drive(
        drive,
        str(opts.get("confirm", "")),
        assume_yes=bool(opts.get("yes")),
    )
    if issue:
        return _result("fail", issue, EXIT_USAGE)
    if opts.get("dry-run"):
        image_size = os.path.getsize(image)
        _eprint("DRY RUN — would flash:")
        _eprint(f"  image: {image} ({image_size:,} bytes)")
        if compressed:
            _eprint(
                f"  compressed: {compressed_format(image)} "
                "— will be decompressed to a temp file first"
            )
        _eprint(f"  drive: {drive.get('model') or drive.get('name')} ({drive['physical_path']})")
        _eprint(f"  serial: {_serial_of(drive)}")
        _eprint(f"  letters: {_display_letters(drive)}")
        _eprint(f"  verify: {bool(opts.get('verify')) or _env_flag('FLINT_VERIFY')}")
        _eprint(f"  bypass-tpm: {bool(opts.get('bypass-tpm'))}")
        _eprint(f"  partition-scheme: {opts.get('partition-scheme', 'auto')}")
        _eprint(f"  filesystem: {opts.get('filesystem', 'fat32')}")
        _eprint(f"  write-mode: {_effective_write_mode(opts, image)}")
        _eprint(f"  resume: {bool(opts.get('resume'))}")
        return _result("ok", "dry run — no changes made", EXIT_OK)
    verify = bool(opts.get("verify")) or _env_flag("FLINT_VERIFY")
    with ExitStack() as stack:
        # B04: an archive is never written raw — extract it to a temp file,
        # flash the extracted payload and let the context manager delete it.
        try:
            write_image = stack.enter_context(decompress_image(image))
        except Exception as exc:  # report, never raw-write it
            return _result(
                "fail",
                f"could not decompress {os.path.basename(image)}: {exc}",
                EXIT_FAIL,
            )
        if write_image != image:
            _eprint(
                f"decompressed {os.path.basename(image)} -> "
                f"{os.path.basename(write_image)}"
            )
            bypass_error = _bypass_tpm_error(opts, [write_image])
            if bypass_error:
                return _result("fail", bypass_error, EXIT_USAGE)
        write_mode = _effective_write_mode(opts, write_image)
        image_size = os.path.getsize(write_image)
        drive_capacity = _drive_capacity(drive)
        if image_size > drive_capacity:
            return _result(
                "fail",
                f"image is larger than the target drive "
                f"({image_size:,} bytes vs {drive_capacity:,} bytes)",
                EXIT_USAGE,
            )
        letters = drive.get("letters") or (
            [drive["letter"]] if drive.get("letter") else []
        )
        worker = UsbWriter(
            write_image,
            drive["physical_path"],
            letters=letters,
            partition_scheme=str(opts.get("partition-scheme", "auto")),
            filesystem=str(opts.get("filesystem", "fat32")),
            write_mode=write_mode,
            verify_after_write=verify,
            resume=bool(opts.get("resume")),
            bypass_tpm=bool(opts.get("bypass-tpm")),
            target_fingerprint=str(
                drive.get("serial") or drive.get("physical_path")
            ),
        )
        started = time.monotonic()
        ok, message = _run_worker(worker)
        _record_flash_history(
            drive, image, started=started, ok=ok, verify=verify
        )
        if not ok:
            return _result(
                "canceled" if message == "cancelled" else "fail",
                message,
                EXIT_CANCELLED if message == "cancelled" else EXIT_FAIL,
            )
    if opts.get("copy-report"):
        _copy_to_clipboard(f"Flint flash: {os.path.basename(image)} -> {_serial_of(drive)}")
    # L08: exactly one read-back pass. When verify was requested the writer
    # already read the drive back and reported a failure as a failed flash,
    # so a second VerifyWorker pass would only double the work.
    if not verify:
        return _result("ok", "flashed", EXIT_OK)
    return _result("ok", "flashed and verified", EXIT_OK)


def _cmd_verify_raw(path: str, size: int, expected: str) -> int:
    from core.verify import VerifyWorker

    worker = VerifyWorker(path, expected, size)
    ok, message = _run_worker(worker)
    if not ok:
        # Same mapping as flash/wipe/backup/clone: a user cancel is exit 2
        # ("canceled"), everything else is a failure (exit 1).
        return _result(
            "canceled" if message == "cancelled" else "fail",
            message,
            EXIT_CANCELLED if message == "cancelled" else EXIT_FAIL,
        )
    return _result("ok", "verification passed", EXIT_OK)


def _cmd_verify(opts: dict[str, object]) -> int:
    missing = _missing(opts, "drive", "serial|letter|path")
    if missing:
        return _result("fail", missing, EXIT_USAGE)
    drives = _detect_drives()
    drive = _resolve_drive(str(opts.get("drive", "")), drives)
    if drive is None:
        return _result(
            "fail",
            "drive not found — run 'flint list' to see available drives",
            EXIT_USAGE,
        )
    expected = str(opts.get("sha256", ""))
    if expected:
        if len(expected) != 64 or any(
            c not in "0123456789abcdefABCDEF" for c in expected
        ):
            return _result("fail", "--sha256 must be a 64-hex digest", EXIT_USAGE)
        # The digest covers only the image bytes, so the drive must be
        # sized to the image (not the whole disk, which is usually larger
        # and would never match). Derive the byte count from --image.
        image = str(opts.get("image", ""))
        if not image:
            return _result(
                "fail",
                "--sha256 requires --image <file> so the byte range to "
                "verify is known",
                EXIT_USAGE,
            )
        if not os.path.isfile(image):
            return _result("fail", "--image file not found", EXIT_USAGE)
        size = os.path.getsize(image)
    else:
        # No digest: read-only bad-block scan of the whole drive.
        from core.verify import verify_device

        _eprint("scanning drive...")
        result = verify_device(drive["physical_path"])
        if not result.get("ok"):
            return _result(
                "fail",
                f"bad sectors: {len(result['bad_sectors'])} ({result['error']})",
                EXIT_FAIL,
            )
        return _result("ok", "no bad sectors", EXIT_OK)
    return _cmd_verify_raw(
        drive["physical_path"], size, expected.lower()
    )


def _cmd_wipe(opts: dict[str, object]) -> int:
    from core.wipe import WIPE_METHODS, WipeWorker

    missing = _missing(opts, "drive", "serial|letter|path")
    if missing:
        return _result("fail", missing, EXIT_USAGE)
    drives = _detect_drives()
    drive = _resolve_drive(str(opts.get("drive", "")), drives)
    if drive is None:
        return _result(
            "fail",
            "drive not found — run 'flint list' to see available drives",
            EXIT_USAGE,
        )
    if _is_system_disk(drive):
        return _result(
            "fail",
            "refusing to wipe the system disk (the OS is running from this drive)",
            EXIT_USAGE,
        )
    issue = _confirm_drive(
        drive,
        str(opts.get("confirm", "")),
        assume_yes=bool(opts.get("yes")),
    )
    if issue:
        return _result("fail", issue, EXIT_USAGE)
    # Validation first, dry-run second: a dry run must not print (or accept)
    # a method the real run would reject.
    method = str(opts.get("method", "zero")).lower()
    if method not in WIPE_METHODS:
        return _result(
            "fail", f"--method must be one of {', '.join(WIPE_METHODS)}", EXIT_USAGE
        )
    if opts.get("dry-run"):
        _eprint("DRY RUN — would wipe:")
        _eprint(f"  drive: {drive.get('model') or drive.get('name')} ({drive['physical_path']})")
        _eprint(f"  serial: {_serial_of(drive)}")
        _eprint(f"  method: {method}")
        return _result("ok", "dry run — no changes made", EXIT_OK)
    letters = drive.get("letters") or (
        [drive["letter"]] if drive.get("letter") else []
    )
    worker = WipeWorker(drive["physical_path"], letters, method=method)
    ok, message = _run_worker(worker)
    if not ok:
        return _result(
            "canceled" if message == "cancelled" else "fail",
            message,
            EXIT_CANCELLED if message == "cancelled" else EXIT_FAIL,
        )
    return _result("ok", f"wiped ({method})", EXIT_OK)


def _cmd_backup(opts: dict[str, object]) -> int:
    from core.backup import BackupWorker

    missing = _missing(opts, "drive", "serial|letter|path")
    if missing:
        return _result("fail", missing, EXIT_USAGE)
    drives = _detect_drives()
    drive = _resolve_drive(str(opts.get("drive", "")), drives)
    if drive is None:
        return _result(
            "fail",
            "drive not found — run 'flint list' to see available drives",
            EXIT_USAGE,
        )
    missing = _missing(opts, "out", "file")
    if missing:
        return _result("fail", missing, EXIT_USAGE)
    out = str(opts.get("out", ""))
    confirm = str(opts.get("confirm", "")) if opts.get("confirm") else None
    if confirm and bool(opts.get("yes")):
        return _result(
            "fail",
            "pass either --confirm <serial> or --yes, not both",
            EXIT_USAGE,
        )
    issue: str | None = None
    if confirm and not bool(opts.get("yes")):
        issue = _require_confirm(drive, confirm)
    if issue:
        return _result("fail", issue, EXIT_USAGE)
    if opts.get("check-fake"):
        denied = _check_fake_drive(drive, bool(opts.get("yes")))
        if denied is not None:
            return denied
    if opts.get("dry-run"):
        _eprint("DRY RUN — would backup:")
        _eprint(f"  drive: {drive.get('model') or drive.get('name')} ({drive['physical_path']})")
        _eprint(f"  serial: {_serial_of(drive)}")
        _eprint(f"  output: {out}")
        return _result("ok", "dry run — no changes made", EXIT_OK)
    letters = drive.get("letters") or (
        [drive["letter"]] if drive.get("letter") else []
    )
    worker = BackupWorker(drive["physical_path"], out, letters=letters)
    ok, message = _run_worker(worker)
    from core.history import append_audited_history

    append_audited_history(
        {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "operation": "backup",
            "success": bool(ok),
            "iso": os.path.basename(out),
            "drive_serial": _serial_of(drive),
            "duration": None,
            "verified": bool(ok),
            "error": None if ok else message,
        }
    )
    if not ok:
        return _result(
            "canceled" if message == "cancelled" else "fail",
            message,
            EXIT_CANCELLED if message == "cancelled" else EXIT_FAIL,
        )
    return _result("ok", f"backup saved to {out}", EXIT_OK)


def _cmd_clone(opts: dict[str, object]) -> int:
    from core.clone import CloneWorker

    missing = _missing(opts, "from", "serial|letter|path")
    if missing:
        return _result("fail", missing, EXIT_USAGE)
    missing = _missing(opts, "to", "serial|letter|path")
    if missing:
        return _result("fail", missing, EXIT_USAGE)
    drives = _detect_drives()
    source = _resolve_drive(str(opts.get("from", "")), drives)
    target = _resolve_drive(str(opts.get("to", "")), drives)
    if source is None or target is None:
        return _result(
            "fail",
            "source or target drive not found — run 'flint list' to see "
            "available drives",
            EXIT_USAGE,
        )
    if source.get("physical_path") == target.get("physical_path"):
        return _result("fail", "source and target are the same drive", EXIT_USAGE)
    if _is_system_disk(target):
        return _result(
            "fail",
            "refusing to clone to the system disk (the OS is running from this drive)",
            EXIT_USAGE,
        )
    issue = _confirm_drive(
        target,
        str(opts.get("confirm", "")),
        assume_yes=bool(opts.get("yes")),
    )
    if issue:
        return _result("fail", issue, EXIT_USAGE)
    if _drive_capacity(target) < _drive_capacity(source):
        return _result("fail", "target drive is smaller than the source", EXIT_USAGE)
    if opts.get("dry-run"):
        _eprint("DRY RUN — would clone:")
        _eprint(f"  from: {source.get('model') or source.get('name')} ({source['physical_path']})")
        _eprint(f"  to: {target.get('model') or target.get('name')} ({target['physical_path']})")
        return _result("ok", "dry run — no changes made", EXIT_OK)
    worker = CloneWorker(
        source["physical_path"],
        target["physical_path"],
        source_letters=source.get("letters") or [],
        target_letters=target.get("letters") or [],
    )
    ok, message = _run_worker(worker)
    from core.history import append_audited_history

    append_audited_history(
        {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "operation": "clone",
            "success": bool(ok),
            "source_drive_serial": _serial_of(source),
            "drive_serial": _serial_of(target),
            "verified": bool(ok),
            "duration": None,
            "error": None if ok else message,
        }
    )
    if not ok:
        return _result(
            "canceled" if message == "cancelled" else "fail",
            message,
            EXIT_CANCELLED if message == "cancelled" else EXIT_FAIL,
        )
    return _result("ok", "clone complete", EXIT_OK)


def parse_queue_file(
    queue_file: str,
    *,
    base_dir: str | None = None,
) -> tuple[list[str], list[str]]:
    images: list[str] = []
    warnings: list[str] = []
    try:
        with open(queue_file, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                line = line.lstrip("\ufeff")
                if not line:
                    continue
                line = line.strip('"').strip("'")
                if not line:
                    continue
                line = line.split(" #")[0].strip()
                if not line:
                    continue
                if not os.path.isabs(line):
                    line = os.path.normpath(os.path.join(base_dir or "", line))
                if not os.path.isfile(line):
                    warnings.append(f"queue item not found: {line}")
                    continue
                images.append(line)
    except OSError as exc:
        warnings.append(f"could not read queue: {exc}")
    return images, warnings


def _cmd_queue(opts: dict[str, object]) -> int:
    """Flash every image listed in a file (one per line, # comments
    allowed) to the same drive, stopping on the first failure."""
    from core.decompress import decompress_image
    from core.writer import UsbWriter

    missing = _missing(opts, "file", "file")
    if missing:
        return _result("fail", missing, EXIT_USAGE)
    queue_file = str(opts.get("file", ""))
    if not os.path.isfile(queue_file):
        return _result("fail", f"--file not found: {queue_file}", EXIT_USAGE)
    base_dir = os.path.dirname(os.path.abspath(queue_file))
    images, warnings = parse_queue_file(queue_file, base_dir=base_dir)
    for w in warnings:
        _eprint(f"  warning: {w}")
    if not images:
        return _result("fail", "queue file has no valid images", EXIT_USAGE)
    rejected = _prepare_images(images)
    if rejected is not None:
        return rejected
    bypass_error = _bypass_tpm_error(opts, images)
    if bypass_error:
        return _result("fail", bypass_error, EXIT_USAGE)
    missing = _missing(opts, "drive", "serial|letter|path")
    if missing:
        return _result("fail", missing, EXIT_USAGE)
    drives = _detect_drives()
    drive = _resolve_drive(str(opts.get("drive", "")), drives)
    if drive is None:
        return _result(
            "fail",
            "drive not found — run 'flint list' to see available drives",
            EXIT_USAGE,
        )
    issue = _confirm_drive(
        drive,
        str(opts.get("confirm", "")),
        assume_yes=bool(opts.get("yes")),
    )
    if issue:
        return _result("fail", issue, EXIT_USAGE)
    letters = drive.get("letters") or (
        [drive["letter"]] if drive.get("letter") else []
    )
    verify = bool(opts.get("verify")) or _env_flag("FLINT_VERIFY")
    bypass_tpm = bool(opts.get("bypass-tpm"))
    if opts.get("dry-run"):
        _eprint("DRY RUN — would flash queue:")
        _eprint(f"  drive: {drive.get('model') or drive.get('name')} ({drive['physical_path']})")
        _eprint(f"  serial: {_serial_of(drive)}")
        _eprint(f"  images: {len(images)}")
        for i, img in enumerate(images, 1):
            _eprint(f"    {i}. {img} [{_effective_write_mode(opts, img)}]")
        _eprint(f"  verify: {verify}")
        _eprint(f"  bypass-tpm: {bypass_tpm}")
        return _result("ok", "dry run — no changes made", EXIT_OK)
    for index, image in enumerate(images, 1):
        _eprint(f"--- queue {index}/{len(images)}: {os.path.basename(image)}")
        # B04: each archive is extracted right before its own write and the
        # temp file is deleted before the next image starts.
        with ExitStack() as stack:
            try:
                write_image = stack.enter_context(decompress_image(image))
            except Exception as exc:  # never raw-write an archive
                return _result(
                    "fail",
                    f"could not decompress {os.path.basename(image)}: {exc}",
                    EXIT_FAIL,
                )
            bypass_error = _bypass_tpm_error(opts, [write_image])
            if bypass_error:
                return _result("fail", bypass_error, EXIT_USAGE)
            worker = UsbWriter(
                write_image,
                drive["physical_path"],
                letters=letters,
                write_mode=_effective_write_mode(opts, write_image),
                verify_after_write=verify,
                bypass_tpm=bypass_tpm,
            )
            started = time.monotonic()
            ok, message = _run_worker(worker)
            _record_flash_history(
                drive, image, started=started, ok=ok, verify=verify
            )
        if not ok:
            return _result(
                "canceled" if message == "cancelled" else "fail",
                f"queue stopped at {image} ({message})",
                EXIT_CANCELLED if message == "cancelled" else EXIT_FAIL,
            )
    return _result("ok", f"queue complete ({len(images)} images)", EXIT_OK)


def _cmd_flash_all(opts: dict[str, object]) -> int:
    """Fleet mode: flash every queued image to every drive that is (or
    becomes) plugged in until the time budget expires. The session uses
    the same policy as the GUI's fleet mode (core.fleet): skipped drives
    never re-flash, non-fitting drives are passed over, a failed flash
    aborts immediately, Ctrl+C between flashes cancels."""
    from core.decompress import decompress_image
    from core.fleet import FleetSession, pick_candidate
    from core.writer import UsbWriter

    raw_images = opts.get("images")
    images = [str(i) for i in raw_images] if isinstance(raw_images, list) else []
    if not images:
        return _result(
            "fail",
            "flash-all requires at least one --image <file>",
            EXIT_USAGE,
        )
    # B5: the same file listed twice was treated as two distinct images, so
    # the campaign assigned it two slots per stick, exhausted them on a
    # single drive and then sat on the full --timeout (3600s by default)
    # before reporting success.  Reject it while it is still a usage error.
    seen: dict[str, str] = {}
    for image in images:
        key = os.path.normcase(os.path.abspath(image))
        if key in seen:
            return _result(
                "fail",
                f"duplicate --image: {image} (already given as {seen[key]})",
                EXIT_USAGE,
            )
        seen[key] = image
    for image in images:
        if not os.path.isfile(image):
            return _result("fail", f"--image file not found: {image}", EXIT_USAGE)
    rejected = _prepare_images(images)
    if rejected is not None:
        return rejected
    bypass_error = _bypass_tpm_error(opts, images)
    if bypass_error:
        return _result("fail", bypass_error, EXIT_USAGE)
    issue = _arm_fleet_confirmation(
        str(opts.get("confirm", "")) if opts.get("confirm") else None,
        image_count=len(images),
        assume_yes=bool(opts.get("yes")),
    )
    if issue:
        return _result("fail", issue, EXIT_USAGE)
    try:
        budget = int(str(opts.get("timeout", "3600")))
    except ValueError:
        return _result("fail", "--timeout must be a number of seconds", EXIT_USAGE)
    if budget <= 0:
        return _result("fail", "--timeout must be positive", EXIT_USAGE)
    try:
        parallel = int(str(opts.get("parallel", "1")))
    except ValueError:
        return _result("fail", "--parallel must be a number from 1 to 8", EXIT_USAGE)
    if not 1 <= parallel <= 8:
        return _result("fail", "--parallel must be a number from 1 to 8", EXIT_USAGE)

    session = FleetSession(images=images)
    skip = bool(opts.get("skip-flashed"))
    verify = bool(opts.get("verify")) or _env_flag("FLINT_VERIFY")
    if opts.get("dry-run"):
        _eprint("DRY RUN — would flash fleet:")
        _eprint(f"  images: {len(images)}")
        for i, img in enumerate(images, 1):
            _eprint(f"    {i}. {img} [{_effective_write_mode(opts, img)}]")
        _eprint(f"  timeout: {budget}s")
        _eprint(f"  skip-flashed: {skip}")
        _eprint(f"  parallel: {parallel}")
        _eprint(f"  verify: {verify}")
        _eprint(f"  bypass-tpm: {bool(opts.get('bypass-tpm'))}")
        return _result("ok", "dry run — no changes made", EXIT_OK)
    _eprint(
        f"fleet armed: {len(session.images)} image(s); flashing every "
        f"fitting drive until {budget}s pass"
    )
    from core.drives import DriveDetector

    detector = DriveDetector()
    end = time.monotonic() + budget
    try:
        while time.monotonic() < end:
            drives = _detect_drives(detector)
            # L11: every fleet honours --parallel, not only single-image
            # ones — one campaign job per target drive.
            if parallel > 1:
                from core.campaigns import Campaign, CampaignJob, CampaignRunner
                from core.fleet import drive_fingerprint
                from core.jobs import JobManifest, save_manifest, source_sha256
                from core.paths import APP_DIR

                candidates: list[dict[str, Any]] = []
                available = list(drives)
                while len(candidates) < parallel:
                    candidate = pick_candidate(
                        available,
                        session,
                        now=time.monotonic(),
                        skip_flashed=skip,
                    )
                    if candidate is None:
                        break
                    candidates.append(candidate)
                    fingerprint = drive_fingerprint(candidate)
                    available = [
                        drive
                        for drive in available
                        if drive_fingerprint(drive) != fingerprint
                    ]
                if candidates:
                    # L11: one job per target drive. The job flashes every
                    # queued image in order, so --parallel fans the fleet
                    # out across drives and never runs two writers on one
                    # drive. The manifest tracks the first image (the job's
                    # bookkeeping source); the rest ride along in the job.
                    image = session.images[0]
                    image_size = os.path.getsize(image)
                    digest = source_sha256(image)
                    jobs: list[CampaignJob] = []
                    for drive in candidates:
                        capacity = _drive_capacity(drive)
                        fingerprint = str(
                            drive.get("serial") or drive.get("physical_path")
                        )
                        manifest = JobManifest(
                            source_path=image,
                            source_size=image_size,
                            source_sha256=digest,
                            target_fingerprint=fingerprint,
                            target_size=capacity,
                            options={
                                "chunk_size": 8 * 1024 * 1024,
                                "verify_after_write": verify,
                                "verify_sha256": True,
                                "bad_block_scan": False,
                            },
                            state="queued",
                        )
                        path = APP_DIR / "jobs" / f"{manifest.job_id}.json"
                        save_manifest(path, manifest)
                        jobs.append(CampaignJob(manifest, str(path)))

                    def run_campaign_job(
                        job: CampaignJob,
                        batch_candidates: list[dict[str, Any]] = candidates,
                        batch_images: list[str] = session.images,
                    ) -> None:
                        drive = next(
                            item
                            for item in batch_candidates
                            if str(
                                item.get("serial") or item.get("physical_path")
                            )
                            == job.manifest.target_fingerprint
                        )
                        letters = drive.get("letters") or (
                            [drive["letter"]] if drive.get("letter") else []
                        )
                        for position, image in enumerate(batch_images):
                            # B04: extract per image, delete the temp file
                            # before the next one starts.
                            with ExitStack() as job_stack:
                                try:
                                    write_image = job_stack.enter_context(
                                        decompress_image(image)
                                    )
                                except Exception as exc:
                                    raise RuntimeError(
                                        f"could not decompress "
                                        f"{os.path.basename(image)}: {exc}"
                                    ) from exc
                                problem = _bypass_tpm_error(opts, [write_image])
                                if problem:
                                    raise RuntimeError(problem)
                                worker = UsbWriter(
                                    write_image,
                                    str(drive["physical_path"]),
                                    letters=letters,
                                    write_mode=_effective_write_mode(
                                        opts, write_image
                                    ),
                                    verify_after_write=verify,
                                    bypass_tpm=bool(opts.get("bypass-tpm")),
                                    target_fingerprint=(
                                        job.manifest.target_fingerprint
                                    ),
                                    # The writer unlinks the manifest on
                                    # success, so only the image the manifest
                                    # describes may own it.
                                    manifest_path=(
                                        job.manifest_path if position == 0 else ""
                                    ),
                                    cancel_event=job.cancel_event,
                                )
                                started = time.monotonic()
                                ok, message = _run_worker(worker)
                                _record_flash_history(
                                    drive,
                                    image,
                                    started=started,
                                    ok=ok,
                                    verify=verify,
                                )
                            if not ok:
                                raise RuntimeError(message)

                    campaign = Campaign(jobs, max_workers=parallel)
                    counts = CampaignRunner(campaign, run_campaign_job).run()
                    if counts["failed"] or counts["cancelled"]:
                        # C03: record the failures on the session exactly
                        # like the GUI's fleet mode does, before returning.
                        for _ in range(counts["failed"] + counts["cancelled"]):
                            session.mark_failed()
                        return _result(
                            "fail",
                            f"fleet campaign stopped: {counts['passed']} passed, "
                            f"{counts['failed']} failed, "
                            f"{counts['cancelled']} cancelled",
                            EXIT_FAIL,
                        )
                    for drive in candidates:
                        session.mark_flashed(drive)
                    _eprint(
                        f"--- {session.done_count} drive(s) flashed, "
                        "waiting for more\u2026"
                    )
                    continue
            drive_candidate = pick_candidate(
                drives,
                session,
                now=time.monotonic(),
                skip_flashed=skip,
            )
            if drive_candidate is None:
                time.sleep(2)
                continue
            drive = drive_candidate
            name = drive.get("model") or drive.get("name") or "a drive"
            serial = _serial_of(drive)
            _eprint(f"--- flashing {name} (serial={serial!r})")
            letters = drive.get("letters") or (
                [drive["letter"]] if drive.get("letter") else []
            )
            for image in session.images:
                with ExitStack() as image_stack:
                    try:
                        write_image = image_stack.enter_context(
                            decompress_image(image)
                        )
                    except Exception as exc:  # never raw-write it
                        return _result(
                            "fail",
                            f"could not decompress {os.path.basename(image)}: {exc}",
                            EXIT_FAIL,
                        )
                    problem = _bypass_tpm_error(opts, [write_image])
                    if problem:
                        return _result("fail", problem, EXIT_USAGE)
                    worker = UsbWriter(
                        write_image,
                        str(drive["physical_path"]),
                        letters=letters,
                        write_mode=_effective_write_mode(opts, write_image),
                        verify_after_write=verify,
                        bypass_tpm=bool(opts.get("bypass-tpm")),
                    )
                    started = time.monotonic()
                    ok, message = _run_worker(worker)
                    _record_flash_history(
                        drive, image, started=started, ok=ok, verify=verify
                    )
                if not ok:
                    # C03: record the failed image on the session before
                    # the fleet stops, like the GUI's fleet mode.
                    session.mark_failed()
                    return _result(
                        "canceled" if message == "cancelled" else "fail",
                        f"fleet stopped at {image} ({message})",
                        EXIT_CANCELLED if message == "cancelled" else EXIT_FAIL,
                    )
            session.mark_flashed(drive)
            _eprint(
                f"--- {session.done_count} drive(s) flashed, "
                "waiting for more\u2026"
            )
    except KeyboardInterrupt:
        return _result(
            "canceled",
            f"interrupted after {session.done_count} drive(s) flashed",
            EXIT_CANCELLED,
        )
    return _result(
        "ok", f"fleet complete: {session.done_count} drive(s) flashed", EXIT_OK
    )


def _cmd_deploy(opts: dict[str, object]) -> int:
    """Deploy the selected images to explicit drives as one campaign."""
    from core.campaigns import Campaign, CampaignJob, CampaignRunner
    from core.jobs import (
        JobManifest,
        load_manifest,
        prune_manifests,
        save_manifest,
        source_sha256,
    )
    from core.paths import APP_DIR

    jobs_dir = APP_DIR / "jobs"
    if opts.get("status"):
        # L10: stale terminal-state manifests are dropped first so the
        # report only lists jobs that still matter. Only the read-only
        # status listing prunes — a running deploy never touches manifests.
        try:
            prune_manifests(jobs_dir)
        except OSError as exc:
            logger.warning("deploy status: could not prune manifests: %s", exc)
        manifest_paths = (
            sorted(jobs_dir.glob("*.json")) if jobs_dir.is_dir() else []
        )
        records = []
        for path in manifest_paths:
            try:
                manifest = load_manifest(path)
            except (OSError, TypeError, ValueError):
                continue
            records.append(
                {
                    "job": manifest.job_id,
                    "target": manifest.target_fingerprint,
                    "source": manifest.source_path,
                    "state": manifest.state,
                    "checkpoint": manifest.checkpoint_bytes,
                    "error": manifest.error,
                }
            )
        if _JSON:
            _emit_json(type="deployment_jobs", jobs=records)
        else:
            for record in records:
                _print(
                    f"JOB {record['job']} {record['state']} "
                    f"target={record['target']} checkpoint={record['checkpoint']}"
                )
        return _result("ok", f"{len(records)} deployment job(s)", EXIT_OK)

    if opts.get("cancel") or opts.get("retry") or opts.get("run"):
        job_id = str(opts.get("job", "")).strip()
        if not job_id:
            return _result("fail", "--job is required for --cancel/--retry", EXIT_USAGE)
        path = jobs_dir / f"{job_id}.json"
        try:
            manifest = load_manifest(path)
        except (OSError, TypeError, ValueError):
            return _result("fail", f"deployment job not found: {job_id}", EXIT_USAGE)
        if opts.get("cancel"):
            if manifest.state in {"passed", "failed", "cancelled"}:
                return _result("fail", f"job is already {manifest.state}", EXIT_USAGE)
            manifest.state = "cancelled"
            manifest.error = "cancelled by operator"
            save_manifest(path, manifest)
            return _result("ok", f"deployment job cancelled: {job_id}", EXIT_OK)
        if opts.get("run"):
            source = manifest.source_path
            if not os.path.isfile(source):
                return _result("fail", "job source image is missing", EXIT_USAGE)
            # B05: this path performs a destructive raw write, so it is
            # validated (and elevated, see main()) exactly like `flash`.
            rejected = _prepare_images([source])
            if rejected is not None:
                return rejected
            bypass_error = _bypass_tpm_error(opts, [source])
            if bypass_error:
                return _result("fail", bypass_error, EXIT_USAGE)
            drives = _detect_drives()
            drive = next(
                (
                    item
                    for item in drives
                    if str(item.get("serial") or item.get("physical_path"))
                    == manifest.target_fingerprint
                ),
                None,
            )
            if drive is None:
                return _result("fail", "job target drive is not connected", EXIT_USAGE)
            if _is_system_disk(drive):
                return _result("fail", "refusing to run on the system disk", EXIT_USAGE)
            from core.decompress import decompress_image
            from core.writer import UsbWriter

            letters = drive.get("letters") or (
                [drive["letter"]] if drive.get("letter") else []
            )
            options = dict(manifest.options)
            with ExitStack() as run_stack:
                try:
                    write_image = run_stack.enter_context(
                        decompress_image(source)
                    )
                except Exception as exc:  # never raw-write it
                    return _result(
                        "fail",
                        f"could not decompress {os.path.basename(source)}: {exc}",
                        EXIT_FAIL,
                    )
                bypass_error = _bypass_tpm_error(opts, [write_image])
                if bypass_error:
                    return _result("fail", bypass_error, EXIT_USAGE)
                worker = UsbWriter(
                    write_image,
                    str(drive["physical_path"]),
                    letters=letters,
                    chunk_size=int(options.get("chunk_size", 8 * 1024 * 1024)),
                    verify_after_write=bool(options.get("verify_after_write", False)),
                    verify_sha256=bool(options.get("verify_sha256", True)),
                    bad_block_scan=bool(options.get("bad_block_scan", False)),
                    write_mode=_effective_write_mode(opts, write_image),
                    resume=True,
                    target_fingerprint=manifest.target_fingerprint,
                    manifest_path=str(path),
                )
                manifest.state = "writing"
                manifest.error = None
                save_manifest(path, manifest)
                started = time.monotonic()
                ok, message = _run_worker(worker)
                _record_flash_history(
                    drive,
                    source,
                    started=started,
                    ok=ok,
                    verify=bool(options.get("verify_after_write", False)),
                )
            if ok:
                manifest.state = "passed"
                manifest.error = None
            else:
                manifest.state = "resumable" if message != "cancelled" else "cancelled"
                manifest.error = message
            save_manifest(path, manifest)
            return _result(
                "ok" if ok else ("canceled" if message == "cancelled" else "fail"),
                f"deployment job {'completed' if ok else 'failed'}: {job_id}"
                if ok
                else message,
                EXIT_OK if ok else (EXIT_CANCELLED if message == "cancelled" else EXIT_FAIL),
            )
        if manifest.state not in {"failed", "cancelled", "resumable"}:
            return _result("fail", f"job is not retryable: {manifest.state}", EXIT_USAGE)
        manifest.state = "queued"
        manifest.error = None
        save_manifest(path, manifest)
        return _result("ok", f"deployment job queued: {job_id}", EXIT_OK)

    raw_images = opts.get("images")
    raw_drives = opts.get("drives")
    images = [str(item) for item in raw_images] if isinstance(raw_images, list) else []
    drive_specs = [str(item) for item in raw_drives] if isinstance(raw_drives, list) else []
    if not images:
        return _result("fail", "deploy requires at least one --image <file>", EXIT_USAGE)
    if len(images) != 1:
        return _result(
            "fail",
            "deploy accepts exactly one --image; use queue for multiple images",
            EXIT_USAGE,
        )
    if not drive_specs:
        return _result("fail", "deploy requires at least one --drive <serial|letter|path>", EXIT_USAGE)
    for image in images:
        if not os.path.isfile(image):
            return _result("fail", f"--image file not found: {image}", EXIT_USAGE)
    rejected = _prepare_images(images)
    if rejected is not None:
        return rejected
    bypass_error = _bypass_tpm_error(opts, images)
    if bypass_error:
        return _result("fail", bypass_error, EXIT_USAGE)
    issue = _arm_fleet_confirmation(
        str(opts.get("confirm", "")) if opts.get("confirm") else None,
        image_count=len(images),
        assume_yes=bool(opts.get("yes")),
    )
    if issue:
        return _result("fail", issue, EXIT_USAGE)
    try:
        parallel = int(str(opts.get("parallel", "2")))
    except ValueError:
        return _result("fail", "--parallel must be a number from 1 to 8", EXIT_USAGE)
    if not 1 <= parallel <= 8:
        return _result("fail", "--parallel must be a number from 1 to 8", EXIT_USAGE)

    drives = _detect_drives()
    selected: list[dict[str, Any]] = []
    for spec in drive_specs:
        drive = _resolve_drive(spec, drives)
        if drive is None:
            return _result("fail", f"drive not found: {spec}", EXIT_USAGE)
        if _is_system_disk(drive):
            return _result("fail", "refusing to deploy to the system disk", EXIT_USAGE)
        if any(d.get("physical_path") == drive.get("physical_path") for d in selected):
            return _result("fail", f"duplicate target drive: {spec}", EXIT_USAGE)
        selected.append(drive)

    verify = bool(opts.get("verify")) or _env_flag("FLINT_VERIFY")
    if opts.get("dry-run"):
        _eprint("DRY RUN - would deploy:")
        _eprint(f"  images: {len(images)}")
        _eprint(f"  drives: {len(selected)}")
        _eprint(f"  parallel: {parallel}")
        _eprint(f"  verify: {verify}")
        _eprint(f"  write-mode: {_effective_write_mode(opts, images[0])}")
        _eprint(f"  bypass-tpm: {bool(opts.get('bypass-tpm'))}")
        return _result("ok", "dry run - no changes made", EXIT_OK)

    from core.decompress import decompress_image
    from core.paths import APP_DIR
    from core.writer import DEFAULT_CHUNK_SIZE, UsbWriter

    image = images[0]
    image_size = os.path.getsize(image)
    image_digest = source_sha256(image)
    options = {
        "chunk_size": DEFAULT_CHUNK_SIZE,
        "verify_after_write": verify,
        "verify_sha256": True,
        "bad_block_scan": False,
    }
    jobs: list[CampaignJob] = []
    for drive in selected:
        capacity = _drive_capacity(drive)
        if capacity < image_size:
            return _result("fail", f"image is larger than target {_serial_of(drive)}", EXIT_USAGE)
        fingerprint = str(drive.get("serial") or drive.get("physical_path"))
        manifest = JobManifest(
            source_path=image,
            source_size=image_size,
            source_sha256=image_digest,
            target_fingerprint=fingerprint,
            target_size=capacity,
            options=options,
            state="queued",
        )
        path = APP_DIR / "jobs" / f"{manifest.job_id}.json"
        save_manifest(path, manifest)
        jobs.append(CampaignJob(manifest, str(path)))

    def run_job(job: CampaignJob) -> None:
        drive = next(
            item for item in selected
            if str(item.get("serial") or item.get("physical_path"))
            == job.manifest.target_fingerprint
        )
        letters = drive.get("letters") or ([drive["letter"]] if drive.get("letter") else [])
        # B04/L13: extract the image inside the job (the temp file lives
        # exactly as long as the write) and honour --write-mode / the
        # bypass-tpm pre-flight here as well.
        with ExitStack() as job_stack:
            try:
                write_image = job_stack.enter_context(decompress_image(image))
            except Exception as exc:  # never raw-write an archive
                raise RuntimeError(
                    f"could not decompress {os.path.basename(image)}: {exc}"
                ) from exc
            problem = _bypass_tpm_error(opts, [write_image])
            if problem:
                raise RuntimeError(problem)
            worker = UsbWriter(
                write_image,
                str(drive["physical_path"]),
                letters=letters,
                write_mode=_effective_write_mode(opts, write_image),
                verify_after_write=verify,
                bypass_tpm=bool(opts.get("bypass-tpm")),
                target_fingerprint=job.manifest.target_fingerprint,
                manifest_path=job.manifest_path,
                cancel_event=job.cancel_event,
            )
            started = time.monotonic()
            ok, message = _run_worker(worker)
            _record_flash_history(
                drive, image, started=started, ok=ok, verify=verify
            )
        if not ok:
            raise RuntimeError(message)

    def publish(job: CampaignJob) -> None:
        if _JSON:
            _emit_json(
                type="campaign_job",
                campaign=campaign.campaign_id,
                job=job.manifest.job_id,
                target=job.manifest.target_fingerprint,
                state=job.manifest.state,
                error=job.manifest.error,
            )
        else:
            _eprint(
                f"deploy {job.manifest.target_fingerprint}: {job.manifest.state}"
            )

    campaign = Campaign(jobs, max_workers=parallel)
    _eprint(
        f"deploy campaign {campaign.campaign_id}: {len(jobs)} target(s), "
        f"parallel={parallel}"
    )
    counts = CampaignRunner(campaign, run_job, on_state=publish).run()
    if counts["failed"] or counts["cancelled"]:
        return _result(
            "fail",
            f"deploy complete: {counts['passed']} passed, "
            f"{counts['failed']} failed, {counts['cancelled']} cancelled",
            EXIT_FAIL,
        )
    return _result("ok", f"deploy complete: {counts['passed']} target(s) passed", EXIT_OK)


def _cmd_doctor(opts: dict[str, object]) -> int:
    import platform

    from core.version import APP_VERSION
    from core.writer import _load_native_writer

    drives = _detect_drives()
    try:
        admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        admin = False
    native_available = _load_native_writer() is not None
    native = "✓ present" if native_available else "✗ missing"
    info: dict[str, object] = {
        "version": APP_VERSION,
        "runtime": "frozen exe" if getattr(sys, "frozen", False) else "python",
        "python": platform.python_version(),
        "os": platform.platform(),
        "arch": platform.machine(),
        "admin": "✓ yes" if admin else "✗ no",
        "native_writer": native,
        "drives": len(drives),
    }
    if _JSON:
        _emit_json(
            type="doctor",
            version=info["version"],
            runtime=info["runtime"],
            python=info["python"],
            os=info["os"],
            arch=info["arch"],
            admin=info["admin"],
            native_writer=info["native_writer"],
            drives=_drives_json(drives),
        )
    else:
        title = "Flint Doctor"
        key_width = max(len(key) for key in info)
        rows = [
            f"  {key:<{key_width}}  {value}  "
            for key, value in info.items()
        ]
        width = max(
            max(len(row) for row in rows),
            len(title) + 4,
        )
        _eprint(
            f"┌─ {title} "
            f"{'─' * (width - len(title) - 3)}┐"
        )
        for row in rows:
            _eprint(f"│{row:<{width}}│")
        _eprint(f"└{'─' * width}┘")
        for index, drive in enumerate(drives, 1):
            _print(
                f"DRIVE {index} {drive.get('name') or drive.get('model')} "
                f"serial={_serial_of(drive)!r} "
                f"size={drive.get('size_gb', 0)}GB "
                f"letters={','.join(drive.get('letters') or [])}"
            )
    return _result(
        "ok",
        f"doctor report: {len(drives)} drive(s), native writer {native}",
        EXIT_OK,
    )


def _cmd_report(opts: dict[str, object]) -> int:
    from core.history import (
        export_history,
        export_history_csv,
        export_history_markdown,
        load_history,
        verify_history_integrity,
    )

    if opts.get("integrity"):
        valid, index = verify_history_integrity()
        if not valid:
            message = f"history integrity failed at record {index}"
            if _JSON:
                _emit_json(type="report", integrity=False, record=index)
            return _result("fail", message, EXIT_FAIL)
        count = len(load_history())
        if _JSON:
            _emit_json(type="report", integrity=True, records=count)
        else:
            _print(f"history integrity: valid ({count} record(s))")

    output = str(opts.get("out", "")).strip()
    if output:
        format_name = str(opts.get("format", "json")).lower()
        exporters = {
            "json": export_history,
            "csv": export_history_csv,
            "markdown": export_history_markdown,
        }
        exporter = exporters.get(format_name)
        if exporter is None:
            return _result("fail", "--format must be json, csv, or markdown", EXIT_USAGE)
        if not exporter(output):
            return _result("fail", f"could not export history to {output}", EXIT_FAIL)
        if _JSON:
            _emit_json(type="report_export", path=output)
        else:
            _print(f"history exported: {output}")
    elif not opts.get("integrity"):
        entries = load_history()
        if _JSON:
            _emit_json(type="report", records=entries)
        else:
            _print(json.dumps(entries, indent=2))
    return _result("ok", "report complete", EXIT_OK)


_POWERSHELL_COMPLETION = """# flint PowerShell completion
# Save to $PROFILE:  flint completions | Out-File -Append $PROFILE
Register-ArgumentCompleter -CommandName flint -Native -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $commands = @('list','flash','verify','wipe','backup','clone','queue','flash-all','deploy','doctor','report','completions','scan','launch','help','--version')
    $options  = @(__PS_OPTIONS__)
    try {
        $raw = & flint list --json 2>$null
        $drives = @()
        foreach ($line in $raw) {
            $obj = $line | ConvertFrom-Json
            if ($obj.type -eq 'drives') { $drives = $obj.drives }
        }
    } catch { $drives = @() }
    $serialCandidates = @()
    $modelCandidates  = @()
    foreach ($d in $drives) {
        $serialCandidates += $d.serial
        foreach ($l in $d.letters) { $serialCandidates += $l }
    }
    $tokens = $commandAst.CommandElements | ForEach-Object { $_.ToString() } | Select-Object -Skip 1
    if ($tokens.Count -lt 2) {
        ($commands + $options) |
            Where-Object { $_ -like "$wordToComplete*" } |
            ForEach-Object {
                [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterName', $_)
            }
    } elseif ($tokens[-1] -match '^--') {
        $options |
            Where-Object { $_ -like "$wordToComplete*" } |
            ForEach-Object {
                [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterName', $_)
            }
    } else {
        ($serialCandidates + $modelCandidates) |
            Where-Object { $_ -like "$wordToComplete*" } |
            Sort-Object -Unique |
            ForEach-Object {
                [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
            }
    }
}
"""
_POWERSHELL_COMPLETION = _POWERSHELL_COMPLETION.replace(
    "__PS_OPTIONS__", ",".join(f"'{opt}'" for opt in _ALL_OPTIONS)
)

_BASH_COMPLETION = """# flint bash completion
# Save to /etc/bash_completion.d/flint or source from ~/.bashrc
_flint_completions() {
    local cur prev commands options
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    commands='list flash verify wipe backup clone queue flash-all deploy doctor report completions scan launch help'
    options='__BASH_OPTIONS__'

    if [[ ${cur} == -* ]]; then
        COMPREPLY=( $(compgen -W "${options}" -- ${cur}) )
    elif [[ ${COMP_CWORD} -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "${commands}" -- ${cur}) )
    elif [[ ${prev} == --image || ${prev} == --out || ${prev} == --file ]]; then
        COMPREPLY=( $(compgen -f -- ${cur}) )
    elif [[ ${prev} == --drive || ${prev} == --from || ${prev} == --to ]]; then
        local drives
        drives=$(flint list --json 2>/dev/null | grep -o '"serial":"[^"]*"' | cut -d'"' -f4)
        COMPREPLY=( $(compgen -W "${drives}" -- ${cur}) )
    fi
    return 0
}
complete -F _flint_completions flint
"""
_BASH_COMPLETION = _BASH_COMPLETION.replace(
    "__BASH_OPTIONS__", " ".join(_ALL_OPTIONS)
)

_ZSH_COMPLETION = """# flint zsh completion
# Save to a directory in $fpath (e.g. /usr/local/share/zsh/site-functions/_flint)
#compdef flint

_flint() {
    local -a commands options
    commands=(
__ZSH_COMMANDS__
    )
    options=(
__ZSH_OPTIONS__
    )

    _arguments -C \
        '1:command:->command' \
        '*::arg:->args'

    case $state in
        command)
            _describe 'command' commands
            ;;
        args)
            _describe 'option' options
            ;;
    esac
}

_flint "$@"
"""
# Generated from the overview source so zsh descriptions can never drift.
_ZSH_COMPLETION = _ZSH_COMPLETION.replace(
    "__ZSH_COMMANDS__",
    "\n".join(
        f"        '{name}:{_COMMAND_SUMMARY[name]}'"
        for name in _COMMAND_SUMMARY
    ),
)
_ZSH_COMPLETION = _ZSH_COMPLETION.replace("__ZSH_OPTIONS__", _zsh_option_lines())


def _cmd_scan(opts: dict[str, object]) -> int:
    from core.verify import whole_drive_scan

    missing = _missing(opts, "drive", "serial|letter|path")
    if missing:
        return _result("fail", missing, EXIT_USAGE)
    drives = _detect_drives()
    drive = _resolve_drive(str(opts.get("drive", "")), drives)
    if drive is None:
        if _JSON:
            _emit_json(type="drives", drives=_drives_json(drives))
        else:
            _eprint("drive not found; detected drives:")
            for d in drives:
                _eprint(
                    f"  serial={_serial_of(d)!r} "
                    f"path={d.get('physical_path')} "
                    f"letters={d.get('letters')}"
                )
        return _result(
            "fail",
            "drive not found — run 'flint list' to see available drives",
            EXIT_USAGE,
        )
    retries_raw = str(opts.get("retries", "3"))
    try:
        retries = max(1, min(10, int(retries_raw)))
    except ValueError:
        retries = 3
    _eprint(
        f"scanning "
        f"{drive.get('name') or drive.get('model') or drive['physical_path']}..."
    )
    started_at = time.monotonic()
    verbose = bool(opts.get("verbose"))

    def on_progress(done: int, total: int) -> None:
        if total > 0:
            elapsed_seconds = max(time.monotonic() - started_at, 1e-6)
            speed_mbps = done / 1_000_000 / elapsed_seconds
            remaining_bytes = max(total - done, 0)
            eta_seconds = (
                int(remaining_bytes / 1_000_000 / speed_mbps)
                if speed_mbps > 0
                else 0
            )
            if _JSON:
                _emit_json(
                    type="progress",
                    pct=round(done / total * 100, 1),
                    speed=round(speed_mbps, 1),
                    eta=eta_seconds,
                    written=done,
                    total=total,
                )
            else:
                bar = _block_bar(done / total * 100)
                _eprint(
                    f"FLINT {bar} {done / total * 100:.1f}% "
                    f"{speed_mbps:.1f}MB/s ETA {eta_seconds}s"
                )

    result = whole_drive_scan(
        drive["physical_path"],
        retries=retries,
        progress=on_progress,
    )
    if verbose and not result["ok"] and result["bad_sectors"]:
        _eprint(f"\nBad sectors found ({len(result['bad_sectors'])}):")
        for bs in result["bad_sectors"][:50]:
            _eprint(f"  offset: {bs['offset']:,} (length: {bs['length']})")
        if len(result["bad_sectors"]) > 50:
            _eprint(f"  ... and {len(result['bad_sectors']) - 50} more")
    if _JSON:
        _emit_json(
            type="scan",
            ok=result["ok"],
            bad_sectors=result["bad_sectors"],
            drive_size=result["drive_size"],
            speed_mbps=round(result["speed_mbps"], 1),
            error=result["error"],
        )
    if result["ok"]:
        return _result(
            "ok",
            (
                "no bad sectors "
                f"({result['drive_size']} bytes, "
                f"{result['speed_mbps']:.1f} MB/s)"
            ),
            EXIT_OK,
        )
    return _result(
        "fail",
        (
            f"{len(result['bad_sectors'])} bad sector(s) "
            f"({result['error']})"
        ),
        EXIT_FAIL,
    )


def _cmd_completions(opts: dict[str, object]) -> int:
    shell = str(opts.get("shell", "powershell")).lower()
    if shell not in ("powershell", "bash", "zsh"):
        return _result(
            "fail",
            "--shell must be one of powershell, bash, zsh",
            EXIT_USAGE,
        )
    if shell == "bash":
        script = _BASH_COMPLETION
    elif shell == "zsh":
        script = _ZSH_COMPLETION
    else:
        script = _POWERSHELL_COMPLETION
    if _JSON:
        return _result("ok", script, EXIT_OK)
    _print(script)
    # L19: every command ends with a RESULT line, text mode included —
    # here it goes to stderr, because stdout is a shell script meant to be
    # redirected verbatim (flint completions bash > flint.sh).
    return _result(
        "ok", f"{shell} completion script", EXIT_OK, to_stderr=True
    )


def _cmd_launch(opts: dict[str, object]) -> int:
    """Reject an embedded ``launch`` — the real one never reaches the CLI.

    ``flint launch`` is intercepted by the process entry point
    (``main.main``) before the CLI parser runs, because opening the GUI
    must never import ``ui/*`` from ``core/*``. This handler exists only
    so ``launch`` is a known command for help, completions and option
    validation; reaching it means the caller bypassed the launcher
    (an embedder or a test calling ``cli.main(["launch"])`` directly).
    """
    return _result(
        "fail",
        "'flint launch' opens the desktop app; run it from a terminal, not embedded",
        EXIT_USAGE,
    )


_COMMANDS = {
    "list": _cmd_list,
    "flash": _cmd_flash,
    "verify": _cmd_verify,
    "wipe": _cmd_wipe,
    "backup": _cmd_backup,
    "clone": _cmd_clone,
    "queue": _cmd_queue,
    "flash-all": _cmd_flash_all,
    "deploy": _cmd_deploy,
    "doctor": _cmd_doctor,
    "report": _cmd_report,
    "completions": _cmd_completions,
    "scan": _cmd_scan,
    "launch": _cmd_launch,
}

# The parser accepts every option for every command; each command then
# validates that the options it was given are the ones it honors. A flag
# that belongs to another command ("flint wipe --verify") is a usage error
# instead of being silently ignored.
_COMMAND_OPTS: dict[str, frozenset[str]] = {
    "list": frozenset({"quiet"}),
    "flash": frozenset(
        {
            "image",
            "drive",
            "confirm",
            "yes",
            "verify",
            "bypass-tpm",
            "partition-scheme",
            "filesystem",
            "write-mode",
            "check-fake",
            "dry-run",
            "copy-report",
            "resume",
            "quiet",
        }
    ),
    "verify": frozenset({"drive", "sha256", "image", "quiet"}),
    "wipe": frozenset({"drive", "confirm", "yes", "method", "dry-run", "quiet"}),
    "backup": frozenset(
        {"drive", "out", "confirm", "yes", "check-fake", "dry-run", "quiet"}
    ),
    "clone": frozenset({"from", "to", "confirm", "yes", "dry-run", "quiet"}),
    "queue": frozenset(
        {
            "file",
            "drive",
            "confirm",
            "yes",
            "verify",
            "bypass-tpm",
            "write-mode",
            "dry-run",
            "quiet",
        }
    ),
    "flash-all": frozenset(
        {
            "image",
            "confirm",
            "yes",
            "timeout",
            "parallel",
            "skip-flashed",
            "verify",
            "bypass-tpm",
            "write-mode",
            "dry-run",
            "quiet",
        }
    ),
    "deploy": frozenset(
        {
            "image",
            "drive",
            "confirm",
            "yes",
            "parallel",
            "job",
            "status",
            "cancel",
            "retry",
            "run",
            "verify",
            "bypass-tpm",
            "write-mode",
            "dry-run",
            "quiet",
        }
    ),
    "doctor": frozenset({"quiet"}),
    "report": frozenset({"out", "format", "integrity", "quiet"}),
    "completions": frozenset({"shell", "quiet"}),
    "scan": frozenset({"drive", "retries", "verbose", "quiet"}),
    "launch": frozenset(),
}


def _validate_command_opts(command: str, opts: dict[str, object]) -> str | None:
    """Return an error when *opts* contains a flag the command does not use."""
    allowed = _COMMAND_OPTS.get(command, frozenset())
    for name in opts:
        if name not in allowed:
            return f"option --{name} is not valid for command {command}"
    return None


# Commands whose write options can still change what lands on the drive.
_WRITE_OPTION_COMMANDS = frozenset({"flash", "queue", "flash-all", "deploy"})

# Spellings resolve_write_mode() accepts alongside its canonical tokens.
_WRITE_MODE_ALIASES = {
    "raw": "dd",
    "file-copy": "filecopy",
    "file copy": "filecopy",
}


def _validate_write_opts(command: str, opts: dict[str, object]) -> str | None:
    """Reject bad write options before elevation or any disk work.

    B12: --filesystem was validated only inside run_format(), i.e. after
    diskpart had already wiped the partition table, and --partition-scheme /
    --write-mode were never validated at all: an unknown value was silently
    coerced by resolve_partition_scheme()/resolve_write_mode() instead of
    being reported as a usage error.
    """
    if command not in _WRITE_OPTION_COMMANDS:
        return None
    from core.diskpart import FILESYSTEMS, SCHEMES, WRITE_MODES

    checks = (
        ("partition-scheme", SCHEMES),
        ("filesystem", FILESYSTEMS),
        ("write-mode", WRITE_MODES),
    )
    for name, allowed in checks:
        if name not in opts:
            continue
        value = str(opts[name] if opts[name] is not None else "").strip().lower()
        if name == "write-mode":
            value = _WRITE_MODE_ALIASES.get(value, value)
        if value not in allowed:
            return f"--{name} must be one of {', '.join(allowed)}"
    return None


def main(argv: list[str] | None = None) -> int:
    _ensure_cli_stdio()
    global _JSON, _QUIET
    # L21: one stderr handler for every core logger, configured here and
    # nowhere else (main.py's setup_logging covers the GUI path only).
    # Runs after _ensure_cli_stdio so the handler binds the real stderr.
    # C02: honor the user's log_level setting (previously CLI-only
    # WARNING); a missing/corrupt setting falls back to WARNING inside
    # setup_cli_logging.
    from core.log import setup_cli_logging

    try:
        from core import settings as _settings

        cli_level = str(_settings.get("log_level") or "WARNING")
    except Exception:
        cli_level = "WARNING"
    setup_cli_logging(level=cli_level)
    # L20: remember the caller's own argument list. Dispatch and the UAC
    # relaunch must use the same arguments — an embedded cli.main([...])
    # must never re-launch the host process's real sys.argv.
    original = list(sys.argv[1:] if argv is None else argv)
    argv = list(original)

    # Reset invocation-specific flags so repeated main() calls in tests or
    # embedded use do not inherit the previous invocation's output mode.
    _JSON = (
        os.environ.get("FLINT_PROGRESS", "").strip().lower() == "json"
        or "--json" in argv
    )
    _QUIET = "--quiet" in argv

    # L18: strip every --cli token, not just the first one, so a doubled
    # "flint --cli --cli list" does not fail strict validation.
    if "--cli" in argv:
        argv = [arg for arg in argv if arg != "--cli"]
    if "--json" in argv:
        argv = [arg for arg in argv if arg != "--json"]
    if argv == ["--version"]:
        # Only a bare top-level --version prints the version. Anywhere else
        # it is an unknown option, so `flint flash ... --version` can never
        # silently turn a destructive command into an exit-0 no-op.
        from core.version import APP_VERSION

        # L19: the banner is informational; the RESULT line (both modes)
        # is what makes the exit-0 outcome visible to scripts.
        if _JSON:
            return _result("ok", f"Flint v{APP_VERSION}", EXIT_OK)
        title = f"\u2b21 Flint  v{APP_VERSION}"
        tagline = "Write. Verify. Trust."
        w = max(len(title), len(tagline)) + 2
        bar = "\u2500" * w
        _eprint(f"\u250c{bar}\u2510")
        _eprint(f"\u2502 {title:<{w}}\u2502")
        _eprint(f"\u2502 {tagline:<{w}}\u2502")
        _eprint(f"\u2514{bar}\u2518")
        return _result("ok", f"Flint v{APP_VERSION}", EXIT_OK)
    if not argv:
        # The overview itself carries the banner; stderr stays clean so
        # piping stdout into a file does not duplicate it.
        if _JSON:
            return _result("ok", "no command given", EXIT_OK)
        _print(_usage())
        return _result("ok", "no command given", EXIT_OK)
    first = argv[0]
    if first in ("help", "--help", "-h"):
        text = (
            _command_help(argv[1])
            if len(argv) >= 2 and argv[1] in _COMMANDS
            else _usage()
        )
        if _JSON:
            return _result("ok", text, EXIT_OK)
        _print(text)
        return _result("ok", "help", EXIT_OK)
    command = first
    if command not in _COMMANDS:
        _eprint(f"unknown command: {command}")
        _result("fail", f"unknown command: {command}", EXIT_USAGE)
        _eprint(_usage())
        return EXIT_USAGE
    rest = argv[1:]
    if "--help" in rest or "-h" in rest:
        text = _command_help(command)
        if _JSON:
            return _result("ok", text, EXIT_OK)
        _print(text)
        return _result("ok", "help", EXIT_OK)

    opts, error = _opts(rest)
    if error:
        _result("fail", error, EXIT_USAGE)
        _eprint(_usage())
        return EXIT_USAGE

    invalid = _validate_command_opts(command, opts)
    if invalid:
        _result("fail", invalid, EXIT_USAGE)
        _eprint(_usage())
        return EXIT_USAGE

    bad_write_opt = _validate_write_opts(command, opts)
    if bad_write_opt:
        _result("fail", bad_write_opt, EXIT_USAGE)
        _eprint(_usage())
        return EXIT_USAGE

    # D09/B05: only the genuinely read-only paths skip the UAC relaunch.
    # ``deploy --run`` performs a destructive raw write, so it leaves this
    # bucket and elevates (and gets its QCoreApplication) like ``flash``.
    deploy_read_only = command == "deploy" and not opts.get("run") and any(
        opts.get(name) for name in ("status", "cancel", "retry")
    )
    # ``launch`` is included so an embedded call never triggers a UAC
    # relaunch: the real 'flint launch' is intercepted by main.main and
    # handles elevation inside the GUI itself.
    if command in ("list", "doctor", "report", "completions", "launch") or deploy_read_only:
        # No privileges needed: skip the UAC relaunch.
        return _COMMANDS[command](opts)

    if command in ("flash-all", "deploy"):
        images = [
            rest[i + 1]
            for i in range(len(rest) - 1)
            if rest[i] == "--image"
        ]
        opts["images"] = images
        if command == "deploy":
            opts["drives"] = [
                rest[i + 1]
                for i in range(len(rest) - 1)
                if rest[i] == "--drive"
            ]

    # L20: rebuild the relaunch from the arguments this invocation was
    # given (not from sys.argv). When running from a pip console-script
    # launcher or frozen .exe, sys.argv[0] is the launcher exe itself —
    # pass it directly.
    program = sys.argv[0]
    if program.lower().endswith(".exe") and os.path.isfile(program):
        launch_argv = [program, *original]
    else:
        launch_argv = [sys.executable, program, *original]
    elevated = ensure_elevated(launch_argv)
    if elevated is not None:
        return elevated

    _app = QCoreApplication.instance() or QCoreApplication([])
    _DESTRUCTIVE = {"flash", "wipe", "clone", "backup", "queue", "flash-all", "deploy"}
    try:
        rc = _COMMANDS[command](opts)
        if rc == EXIT_OK and command in _DESTRUCTIVE:
            _signoff()
        return rc
    except KeyboardInterrupt:
        return _result("canceled", "interrupted by user", EXIT_CANCELLED)
    except Exception as exc:  # never die silently in scripts
        hint = _winerror_hint(exc)
        message = f"internal error: {exc}" + (f" \u2014 {hint}" if hint else "")
        return _result("fail", message, EXIT_FAIL)
