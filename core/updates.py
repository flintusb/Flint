"""Update checks against the GitHub release feed.

Fetches the latest release of the Flint repository, compares versions and
optional downloads + SHA-256 verification of the portable executable.

The repo is private today, so an unauthenticated API call returns 404 —
that is reported as "no public update feed" instead of an error. The feed
can be pointed anywhere via the FLINT_UPDATE_URL environment variable, so
the check works unchanged once the repository is public or a mirror feed
is hosted.
"""

import hashlib
import itertools
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal

from core.checksum import parse_sidecar
from core.version import APP_VERSION

_DEFAULT_URL = "https://api.github.com/repos/flintusb/Flint/releases/latest"
_UA = f"Flint/{APP_VERSION} (update-check)"


def resolve_url() -> str:
    return os.environ.get("FLINT_UPDATE_URL", _DEFAULT_URL)


def fetch_latest(
    url: str | None = None, timeout: float = 8.0
) -> tuple[bool, Any]:
    """Fetch the latest release metadata.

    Returns ``(True, release_dict)`` on success or ``(False, message)``
    with a human-readable reason for the failure.
    """
    target = url or resolve_url()
    request = urllib.request.Request(target, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return (
                False,
                "no public update feed \u2014 the repository is private",
            )
        return False, f"update feed returned HTTP {exc.code}"
    except Exception as exc:
        return False, f"could not reach the update feed ({exc})"
    if not isinstance(data, dict) or not data.get("tag_name"):
        return False, "update feed returned an unexpected payload"
    return True, data


def release_executable(release: dict[str, Any]) -> dict[str, Any] | None:
    """Locate the flint.exe asset in a release dict, or None."""
    for asset in release.get("assets") or []:
        if asset.get("name") == "flint.exe":
            return dict(asset) if isinstance(asset, dict) else None
    return None


def compare_version(current: str, latest: str) -> int:
    """Version comparison: -1 (newer), 0 (same), 1 (older).

    Strips a leading ``v`` and compares numeric dot segments only;
    ``1.0.1rc1`` compares as ``1.0.1``.
    """

    def _segments(version: str) -> tuple[int, ...]:
        cleaned = version.strip().lstrip("vV")
        parts: list[int] = []
        for raw in cleaned.split("."):
            digits = "".join(itertools.takewhile(str.isdigit, raw))
            parts.append(int(digits) if digits else 0)
        return tuple(parts)

    current_segments = _segments(current)
    latest_segments = _segments(latest)
    if latest_segments > current_segments:
        return -1
    if latest_segments < current_segments:
        return 1
    return 0


def download_and_verify(
    url: str,
    dest: str | Path,
    expected_sha256: str | None,
    progress: Callable[[int, int], None] | None = None,
    timeout: float = 30.0,
    is_cancelled: Callable[[], bool] | None = None,
) -> tuple[bool, str]:
    """Stream ``url`` to ``dest`` and verify its SHA-256 when provided.

    Returns ``(True, digest)`` on success, ``(False, "cancelled")`` when
    ``is_cancelled()`` turns true mid-stream (the partial file is removed),
    or ``(False, message)`` on failure.
    """
    target = Path(dest)
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    digest = hashlib.sha256()
    cancelled = False
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            with open(target, "wb") as out:
                while True:
                    if is_cancelled is not None and is_cancelled():
                        cancelled = True
                        break
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    out.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total or done)
    except Exception as exc:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return False, f"download failed ({exc})"
    if cancelled:
        # Never leave a half-written executable behind for the user to run.
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return False, "cancelled"
    if total > 0 and done != total:
        # A connection that drops mid-transfer surfaces as a clean EOF from
        # read(), not as an exception. Without this check the partial file
        # would be reported as a successful download - which is exactly what
        # happened whenever no expected digest was available to catch it
        # (the sidecar fetch failed, or the release shipped no .sha256).
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return False, f"download incomplete ({done:,} of {total:,} bytes)"
    hexdigest = digest.hexdigest()
    if expected_sha256 and hexdigest != expected_sha256.lower():
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return (
            False,
            (
                "downloaded file failed the SHA-256 check "
                f"(got {hexdigest[:12]}\u2026)"
            ),
        )
    return True, hexdigest


class UpdateCheckWorker(QThread):
    """Background latest-release lookup."""

    finished_check = pyqtSignal(bool, str, object)

    def __init__(self, url: str | None = None) -> None:
        super().__init__()
        self._url = url
        self._cancelled = False

    def cancel(self) -> None:
        """T1: shutdown calls this on every live worker.

        The HTTP call itself is bounded by ``fetch_latest``'s 8 s timeout,
        so this only guarantees no signal is delivered into a window that
        is being torn down.
        """
        self._cancelled = True

    def run(self) -> None:
        ok, result = fetch_latest(self._url)
        if self._cancelled:
            return
        self.finished_check.emit(ok, "" if ok else str(result), result)


class UpdateDownloadWorker(QThread):
    """Background download + SHA-256 verification of the new executable."""

    progress = pyqtSignal(int, int)
    finished_download = pyqtSignal(bool, str)

    def __init__(
        self, url: str, dest: str, expected_sha256: str | None
    ) -> None:
        super().__init__()
        self._url = url
        self._dest = dest
        self._expected = expected_sha256
        self._cancelled = False

    def cancel(self) -> None:
        """Stop the stream between chunks and drop the partial file."""
        self._cancelled = True

    def run(self) -> None:
        ok, result = download_and_verify(
            self._url,
            self._dest,
            self._expected,
            progress=lambda done, total: self.progress.emit(done, total),
            is_cancelled=lambda: self._cancelled,
        )
        if self._cancelled and not ok:
            # Shutdown asked for this: do not pop an error dialog over a
            # window that is already closing.
            return
        self.finished_download.emit(ok, result)


class DigestFetchWorker(QThread):
    """Fetch sidecar digest in a background thread."""

    done = pyqtSignal(str, str)  # (url, digest or "")

    def __init__(self, url: str | None) -> None:
        super().__init__()
        self._url = url
        self._cancelled = False

    def cancel(self) -> None:
        """See ``UpdateCheckWorker.cancel`` — suppresses the late signal."""
        self._cancelled = True

    def run(self) -> None:
        digest = fetch_sidecar_digest(self._url)
        if self._cancelled:
            return
        self.done.emit(self._url or "", digest or "")


def default_download_path(version: str) -> str:
    """``~/Downloads/flint-<version>.exe`` (fall back to home)."""
    downloads = Path.home() / "Downloads"
    if not downloads.is_dir():
        downloads = Path.home()
    return str(downloads / f"flint-{version.strip().lstrip('vV')}.exe")


def sidecar_digest_url(release: dict[str, Any]) -> str | None:
    """URL of the bare-hex ``flint.exe.sha256`` asset, if any."""
    for asset in release.get("assets") or []:
        if asset.get("name") == "flint.exe.sha256":
            url = asset.get("browser_download_url")
            if isinstance(url, str):
                return url
    return None


def fetch_sidecar_digest(url: str | None, timeout: float = 8.0) -> str | None:
    """Download the checksum asset and extract its SHA-256; None on failure.

    The body is handed to :func:`core.checksum.parse_sidecar`, the same
    parser the pre-flash sidecar check uses, so a bare digest, ``sha256sum``
    output (``<digest>  <name>``) and ``certutil`` output are all accepted.
    ``updates`` used to keep its own "concatenate every hex character" loop,
    which rejected any sidecar that also carried a filename column - and a
    rejected sidecar silently disabled the digest check on the download.
    """
    if not url:
        return None
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read(4096).decode("ascii", errors="ignore")
    except Exception:
        return None
    return parse_sidecar(text)


def version_from_tag(tag: str) -> str:
    return tag.strip().lstrip("vV")


def should_auto_check(last_check_epoch: float | None, days: int = 7) -> bool:
    if last_check_epoch is None:
        return True
    return (time.time() - last_check_epoch) > days * 86400