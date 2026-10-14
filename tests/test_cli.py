"""CLI headless mode: argument parsing, validation and command dispatch
(real drives and workers are never touched; both are faked). Data and
``RESULT`` lines go to stdout, progress/notes go to stderr."""

import json
import os
import subprocess

import pytest

from core import cli


def _fake_drives(_detector: object | None = None):
    return [
        {
            "physical_path": r"\\.\PHYSICALDRIVE3",
            "serial": "ABC1234",
            "model": "USB Stick",
            "size_gb": 16,
            "letter": "E",
            "letters": ["E"],
            "bus_type": "USB",
            "name": "USB Stick",
        }
    ]


@pytest.fixture(autouse=True)
def _no_elevation(monkeypatch):
    monkeypatch.setattr(cli, "ensure_elevated", lambda argv: None)


@pytest.fixture(autouse=True)
def _reset_json():
    """main() sets the module-global _JSON flag; each test starts clean."""
    cli._JSON = False
    yield
    cli._JSON = False


def test_opts_parsing():
    opts, error = cli._opts(
        ["--image", "a.iso", "--drive", "E", "--confirm", "ABC1234", "--verify"]
    )
    assert error is None
    assert opts == {
        "image": "a.iso",
        "drive": "E",
        "confirm": "ABC1234",
        "verify": True,
    }


def test_opts_missing_value():
    _, error = cli._opts(["--image"])
    assert error == "requires --image <file>"


def test_opts_unknown_option():
    _, error = cli._opts(["--explode"])
    assert error == "unknown option: --explode"


def test_main_unknown_command(capsys):
    assert cli.main(["frobnicate"]) == cli.EXIT_USAGE
    assert "unknown command: frobnicate" in capsys.readouterr().err


def test_flash_rejects_missing_image_file(tmp_path):
    rc = cli._cmd_flash({"image": str(tmp_path / "nope.iso"), "drive": "E"})
    assert rc == cli.EXIT_USAGE


def test_flash_drive_not_found(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", list)
    rc = cli._cmd_flash({"image": str(image), "drive": "E"})
    assert rc == cli.EXIT_USAGE
    assert "drive not found" in capsys.readouterr().out


def test_flash_confirmation_must_match_serial(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    rc = cli._cmd_flash({"image": str(image), "drive": "E", "confirm": "WRONG"})
    assert rc == cli.EXIT_USAGE
    assert "does not match the drive serial" in capsys.readouterr().out


def test_flash_happy_path(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_run_worker", lambda worker: (True, ""))

    rc = cli._cmd_flash(
        {"image": str(image), "drive": "E", "confirm": "ABC1234"}
    )

    assert rc == cli.EXIT_OK
    assert "RESULT ok" in capsys.readouterr().out


def test_flash_failure_exit_code(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_run_worker", lambda worker: (False, "boom"))

    rc = cli._cmd_flash(
        {"image": str(image), "drive": "E", "confirm": "ABC1234"}
    )

    assert rc == cli.EXIT_FAIL
    assert "RESULT fail: boom" in capsys.readouterr().out


def test_wipe_unknown_method_rejected(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    rc = cli._cmd_wipe(
        {
            "drive": "E",
            "confirm": "ABC1234",
            "method": "flames",
        }
    )
    assert rc == cli.EXIT_USAGE
    assert "must be one of" in capsys.readouterr().out


def test_wipe_valid_method_dispatches(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_run_worker", lambda worker: (True, ""))
    worker_used = []

    def _capture_worker(worker):
        worker_used.append(worker.method)
        return True, ""

    monkeypatch.setattr(cli, "_run_worker", _capture_worker)
    rc = cli._cmd_wipe(
        {"drive": "E", "confirm": "ABC1234", "method": "dod"}
    )
    assert rc == cli.EXIT_OK
    assert worker_used == ["dod"]


def test_backup_requires_out(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    rc = cli._cmd_backup({"drive": "E", "confirm": "ABC1234"})
    assert rc == cli.EXIT_USAGE
    assert "--out" in capsys.readouterr().out


def test_clone_rejects_same_drive(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    rc = cli._cmd_clone(
        {"from": "E", "to": r"\\.\PHYSICALDRIVE3", "confirm": "ABC1234"}
    )
    assert rc == cli.EXIT_USAGE
    assert "same drive" in capsys.readouterr().out


def test_clone_requires_target_confirmation(tmp_path, monkeypatch, capsys):
    two_drives = [dict(d) for d in _fake_drives()]
    second = dict(two_drives[0])
    second["physical_path"] = r"\\.\PHYSICALDRIVE4"
    second["serial"] = "ZZZ999"
    second["letter"] = "F"
    second["letters"] = ["F"]
    two_drives.append(second)
    monkeypatch.setattr(cli, "_detect_drives", lambda: two_drives)

    rc = cli._cmd_clone({"from": "E", "to": "F", "confirm": "NOPE"})
    assert rc == cli.EXIT_USAGE
    assert "does not match the drive serial" in capsys.readouterr().out


def test_queue_missing_file():
    assert cli._cmd_queue({"file": "does-not-exist.txt"}) == cli.EXIT_USAGE


def test_queue_empty_file(tmp_path, capsys):
    queue_file = tmp_path / "queue.txt"
    queue_file.write_text("# nothing here\n\n")
    rc = cli._cmd_queue({"file": str(queue_file)})
    assert rc == cli.EXIT_USAGE
    assert "no valid images" in capsys.readouterr().out


def test_queue_happy_path_with_quoted_paths(tmp_path, monkeypatch, capsys):
    first = tmp_path / "one.iso"
    second = tmp_path / "two.iso"
    first.write_bytes(b"1")
    second.write_bytes(b"2")
    queue_file = tmp_path / "queue.txt"
    queue_file.write_text(f'"{first}"\n"{second}"\n')
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    started: list[str] = []

    def _fake_run(worker):
        started.append(worker.iso_path)
        return True, ""

    monkeypatch.setattr(cli, "_run_worker", _fake_run)
    rc = cli._cmd_queue(
        {"file": str(queue_file), "drive": "E", "confirm": "ABC1234"}
    )
    assert rc == cli.EXIT_OK
    assert started == [str(first), str(second)]


def test_queue_happy_path_with_relative_paths(tmp_path, monkeypatch, capsys):
    img = tmp_path / "a.iso"
    img.write_bytes(b"data")
    queue_file = tmp_path / "queue.txt"
    queue_file.write_text("a.iso\n")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    started: list[str] = []

    def _fake_run(worker):
        started.append(worker.iso_path)
        return True, ""

    monkeypatch.setattr(cli, "_run_worker", _fake_run)
    rc = cli._cmd_queue(
        {"file": str(queue_file), "drive": "E", "confirm": "ABC1234"}
    )
    assert rc == cli.EXIT_OK
    assert started == [str(img)]


def test_queue_flashes_every_image_then_reports_count(
    tmp_path, monkeypatch, capsys
):
    first = tmp_path / "one.iso"
    second = tmp_path / "two.iso"
    first.write_bytes(b"1")
    second.write_bytes(b"2")
    queue_file = tmp_path / "queue.txt"
    queue_file.write_text(f"{first}\n\n# comment\n{second}\n")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    started: list[str] = []

    def _fake_run(worker):
        started.append(worker.iso_path)
        return True, ""

    monkeypatch.setattr(cli, "_run_worker", _fake_run)
    rc = cli._cmd_queue(
        {"file": str(queue_file), "drive": "E", "confirm": "ABC1234"}
    )
    assert rc == cli.EXIT_OK
    assert started == [str(first), str(second)]
    assert "queue complete (2 images)" in capsys.readouterr().out


def test_main_dispatches_flash_command(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_run_worker", lambda worker: (True, ""))

    rc = cli.main(
        [
            "--cli",
            "flash",
            "--image",
            str(image),
            "--drive",
            "E",
            "--confirm",
            "ABC1234",
            "--verify",
        ]
    )

    assert rc == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "RESULT ok" in out


def test_help_prints_usage(capsys):
    assert cli.main(["--help"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Usage: flint <command>" in out
    assert "list" in out
    assert "nist" in out


def test_help_command_exits_ok(capsys):
    assert cli.main(["help"]) == cli.EXIT_OK
    assert "Usage: flint <command>" in capsys.readouterr().out


def test_version_flag(capsys):
    from core.version import APP_VERSION

    assert cli.main(["--cli", "--version"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert f"Flint  v{APP_VERSION}" in captured.err


def test_list_prints_drives_with_serials(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    rc = cli.main(["--cli", "list"])
    assert rc == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "ABC1234" in captured.err
    assert "RESULT ok: 1 drive(s) listed" in captured.out


def test_list_no_drives(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", list)
    assert cli.main(["--cli", "list"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "no removable drives" in captured.out


def test_list_does_not_relaunch_elevated(monkeypatch, capsys):
    """list needs no privileges, so it must skip the UAC relaunch."""
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    called = []

    def _ensure_elevated(argv):
        called.append(argv)
        raise AssertionError("list must not relaunch elevated")

    monkeypatch.setattr(cli, "ensure_elevated", _ensure_elevated)
    rc = cli.main(["--cli", "list"])
    assert rc == cli.EXIT_OK
    assert called == []
    capsys.readouterr()


def test_run_worker_emits_flint_progress_lines(monkeypatch, capsys):
    """The documented machine format is FLINT <pct> <speed>MB/s ETA <s>s."""
    from PyQt6.QtCore import QCoreApplication, QThread, pyqtSignal

    class _FakeWorker(QThread):
        progress = pyqtSignal(float)
        speed_mbps = pyqtSignal(float)
        written_bytes = pyqtSignal(int)
        total_bytes = pyqtSignal(int)
        done = pyqtSignal(bool, str)

        def run(self) -> None:
            self.total_bytes.emit(1_000_000_000)
            self.progress.emit(10.0)
            self.progress.emit(50.0)
            self.speed_mbps.emit(42.5)
            self.written_bytes.emit(500_000_000)
            self.progress.emit(100.0)
            self.done.emit(True, "ok")

    app = QCoreApplication([])
    worker = _FakeWorker()
    ok, message = cli._run_worker(worker)
    assert QCoreApplication.instance() is app
    assert ok and message == "ok"
    err = capsys.readouterr().err
    assert "FLINT" in err and "42.5MB/s ETA 0s" in err


def test_verify_sha256_requires_image(monkeypatch, capsys):
    """The digest covers only image bytes, so --image must be given to
    know how many bytes to compare (whole-drive comparison of a larger
    disk would never match the image digest)."""
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)

    rc = cli.main(
        ["--cli", "verify", "--drive", "E", "--sha256", "a" * 64]
    )
    assert rc == cli.EXIT_USAGE
    out = capsys.readouterr().out
    assert "--image" in out


def test_verify_sha256_missing_image_file(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)

    rc = cli.main(
        [
            "--cli",
            "verify",
            "--drive",
            "E",
            "--sha256",
            "a" * 64,
            "--image",
            "nope.iso",
        ]
    )
    assert rc == cli.EXIT_USAGE
    assert "not found" in capsys.readouterr().out


def test_verify_sha256_derives_size_from_image(tmp_path, monkeypatch):
    image = tmp_path / "img.iso"
    image.write_bytes(os.urandom(4096))
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    calls: list[tuple] = []

    def _fake_verify_raw(path, size, expected):
        calls.append((path, size, expected))
        return cli.EXIT_OK

    monkeypatch.setattr(cli, "_cmd_verify_raw", _fake_verify_raw)
    rc = cli.main(
        [
            "--cli",
            "verify",
            "--drive",
            "E",
            "--sha256",
            "a" * 64,
            "--image",
            str(image),
        ]
    )
    assert rc == cli.EXIT_OK
    assert calls == [(r"\\.\PHYSICALDRIVE3", 4096, "a" * 64)]


# --- top-level dispatch and help -----------------------------------------


def test_no_args_prints_usage(capsys):
    assert cli.main([]) == cli.EXIT_OK
    assert "Usage: flint <command>" in capsys.readouterr().out


def test_overview_lists_every_command(capsys):
    """The bare-flint overview is the full catalog, grouped and documented."""
    assert cli.main([]) == cli.EXIT_OK
    out = capsys.readouterr().out
    for name in (
        "flash",
        "queue",
        "flash-all",
        "deploy",
        "list",
        "scan",
        "verify",
        "doctor",
        "wipe",
        "backup",
        "clone",
        "report",
        "launch",
        "completions",
        "help",
    ):
        assert name in out
    assert "Get started" in out
    assert "Docs: https://flintweb.vercel.app/cli" in out


def test_summary_covers_every_command():
    """_COMMAND_SUMMARY feeds the overview and zsh completions."""
    assert set(cli._COMMAND_SUMMARY) == set(cli._COMMANDS) | {"help"}


def test_launch_help(capsys):
    assert cli.main(["help", "launch"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "flint launch" in out
    assert "desktop app" in out


def test_launch_flag_help(capsys):
    assert cli.main(["launch", "--help"]) == cli.EXIT_OK
    assert "flint launch" in capsys.readouterr().out


def test_launch_rejected_when_reaching_cli(capsys):
    """main.main intercepts 'flint launch'; the CLI fallback fails closed."""
    assert cli.main(["launch"]) == cli.EXIT_USAGE
    assert "desktop app" in capsys.readouterr().out


def test_launch_rejects_options(capsys):
    assert cli.main(["launch", "--yes"]) == cli.EXIT_USAGE
    assert "not valid for command launch" in capsys.readouterr().out


def test_launch_never_triggers_elevation(monkeypatch):
    def _boom(argv: list[str]) -> None:
        raise AssertionError("launch must not go through the UAC relaunch")

    monkeypatch.setattr(cli, "ensure_elevated", _boom)
    assert cli.main(["launch"]) == cli.EXIT_USAGE


def test_completions_offer_launch():
    for script in (
        cli._POWERSHELL_COMPLETION,
        cli._BASH_COMPLETION,
        cli._ZSH_COMPLETION,
    ):
        assert "launch" in script


def test_zsh_descriptions_match_summary():
    for name, summary in cli._COMMAND_SUMMARY.items():
        assert f"'{name}:{summary}'" in cli._ZSH_COMPLETION


def test_flash_has_own_help(capsys):
    assert cli.main(["flash", "--help"]) == cli.EXIT_OK
    assert "flint flash --image" in capsys.readouterr().out


def test_flash_help_short_flag(capsys):
    assert cli.main(["flash", "-h"]) == cli.EXIT_OK
    assert "flint flash --image" in capsys.readouterr().out


def test_help_for_specific_command(capsys):
    assert cli.main(["help", "flash-all"]) == cli.EXIT_OK
    assert "flash-all" in capsys.readouterr().out


def test_help_unknown_target_falls_back_to_usage(capsys):
    assert cli.main(["help", "bogus"]) == cli.EXIT_OK
    assert "Usage: flint <command>" in capsys.readouterr().out


def test_unexpected_positional_argument_rejected(capsys):
    assert cli.main(["flash", "extra"]) == cli.EXIT_USAGE
    assert "unexpected argument" in capsys.readouterr().out


# --- JSON (NDJSON) -------------------------------------------------------


def test_list_json_shape(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    assert cli.main(["list", "--json"]) == cli.EXIT_OK
    objects = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    kinds = [obj["type"] for obj in objects]
    assert "drives" in kinds and "result" in kinds
    drives = next(o for o in objects if o["type"] == "drives")["drives"]
    assert drives[0]["serial"] == "ABC1234"
    assert drives[0]["letters"] == ["E"]
    assert drives[0]["path"] == r"\\.\PHYSICALDRIVE3"


def test_flint_progress_json_env_equivalent(monkeypatch, capsys):
    monkeypatch.setenv("FLINT_PROGRESS", "json")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    assert cli.main(["list"]) == cli.EXIT_OK
    objects = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert all("type" in obj for obj in objects)
    assert any(
        obj["type"] == "result" and obj["status"] == "ok" for obj in objects
    )


def test_result_json_object(monkeypatch, capsys, tmp_path):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_run_worker", lambda worker: (False, "boom"))
    monkeypatch.setattr(cli, "_JSON", True)
    assert cli._cmd_flash(
        {"image": str(image), "drive": "E", "confirm": "ABC1234"}
    ) == cli.EXIT_FAIL
    objects = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert objects == [
        {
            "type": "result",
            "status": "fail",
            "message": "boom",
            "exit": cli.EXIT_FAIL,
        }
    ]


def test_json_result_uses_exit_field_for_status(capsys):
    assert cli.main(["backup", "--json", "--drive", "E"]) == cli.EXIT_USAGE
    objects = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert objects[-1]["type"] == "result"
    assert objects[-1]["status"] == "fail"
    assert objects[-1]["exit"] == cli.EXIT_USAGE


# --- confirmations -------------------------------------------------------


def test_flash_prompts_for_serial_when_tty(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_run_worker", lambda worker: (True, ""))
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: "ABC1234")

    assert cli._cmd_flash({"image": str(image), "drive": "E"}) == cli.EXIT_OK
    assert "RESULT ok" in capsys.readouterr().out


def test_flash_prompt_wrong_serial_rejected(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: "WRONG")

    assert cli._cmd_flash({"image": str(image), "drive": "E"}) == cli.EXIT_USAGE
    assert "does not match the drive serial" in capsys.readouterr().out


def test_flash_confirm_required_when_not_tty(tmp_path, monkeypatch):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)

    assert cli._cmd_flash({"image": str(image), "drive": "E"}) == cli.EXIT_USAGE


# --- flash-all (fleet) ---------------------------------------------------


def test_flash_all_requires_images(capsys):
    assert cli._cmd_flash_all({"confirm": "ARM"}) == cli.EXIT_USAGE
    assert "--image" in capsys.readouterr().out


def test_flash_all_requires_arm_when_not_interactive(tmp_path, monkeypatch):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    assert cli._cmd_flash_all({"images": [str(image)]}) == cli.EXIT_USAGE


def test_flash_all_rejects_other_confirm_words(tmp_path, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    assert (
        cli._cmd_flash_all({"images": [str(image)], "confirm": "yes"})
        == cli.EXIT_USAGE
    )
    assert "literal word ARM" in capsys.readouterr().out


def test_flash_all_missing_image_file(tmp_path, capsys):
    assert (
        cli._cmd_flash_all(
            {"images": [str(tmp_path / "nope.iso")], "confirm": "ARM"}
        )
        == cli.EXIT_USAGE
    )
    assert "file not found" in capsys.readouterr().out


def test_flash_all_timeout_validation(tmp_path):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    base = {"images": [str(image)], "confirm": "ARM"}
    assert cli._cmd_flash_all({**base, "timeout": "abc"}) == cli.EXIT_USAGE
    assert cli._cmd_flash_all({**base, "timeout": "0"}) == cli.EXIT_USAGE


def test_flash_all_rejects_duplicate_images(tmp_path, capsys):
    """B5: the same --image twice used to claim two slots per stick, exhaust
    them on one drive and then sit on the full --timeout (3600s default)."""
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    same_path_other_spelling = os.path.join(str(tmp_path), ".", "a.iso")
    rc = cli._cmd_flash_all(
        {
            "images": [str(image), same_path_other_spelling],
            "confirm": "ARM",
        }
    )
    assert rc == cli.EXIT_USAGE
    captured = capsys.readouterr()
    assert "duplicate --image" in captured.out + captured.err


def test_flash_all_distinct_images_are_accepted(tmp_path, capsys):
    first = tmp_path / "a.iso"
    second = tmp_path / "b.iso"
    first.write_bytes(b"data")
    second.write_bytes(b"data")
    rc = cli._cmd_flash_all(
        {"images": [str(first), str(second)], "confirm": "ARM", "dry-run": True}
    )
    assert rc == cli.EXIT_OK
    assert "duplicate" not in capsys.readouterr().out


def test_flash_all_flashes_every_image_to_every_drive(
    tmp_path, monkeypatch, capsys
):
    one = tmp_path / "one.iso"
    two = tmp_path / "two.iso"
    one.write_bytes(b"1")
    two.write_bytes(b"2")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    started: list[str] = []

    def _fake_run(worker):
        started.append(worker.iso_path)
        return True, ""

    monkeypatch.setattr(cli, "_run_worker", _fake_run)
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    rc = cli.main(
        [
            "flash-all",
            "--image",
            str(one),
            "--image",
            str(two),
            "--confirm",
            "ARM",
            "--timeout",
            "1",
        ]
    )

    assert rc == cli.EXIT_OK
    assert started == [str(one), str(two)]
    assert "1 drive(s) flashed" in capsys.readouterr().out


def test_flash_all_interrupt_cancels(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)

    def _interrupt(worker):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "_run_worker", _interrupt)
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    rc = cli._cmd_flash_all(
        {"images": [str(image)], "confirm": "ARM", "timeout": "30"}
    )

    assert rc == cli.EXIT_CANCELLED
    assert "interrupted after 0 drive(s) flashed" in capsys.readouterr().out


def test_flash_all_parallel_uses_campaign_batches(
    tmp_path, monkeypatch, capsys
):
    image = tmp_path / "agent.iso"
    image.write_bytes(b"data")
    first = _fake_drives()[0]
    second = dict(first)
    second.update(
        physical_path=r"\\.\PHYSICALDRIVE4",
        serial="DEF5678",
        letter="F",
        letters=["F"],
    )
    monkeypatch.setattr(cli, "_detect_drives", lambda *_: [first, second])
    monkeypatch.setattr("core.paths.APP_DIR", tmp_path / "app")
    started: list[str] = []

    def _fake_run(worker):
        started.append(worker.target_fingerprint)
        return True, ""

    monkeypatch.setattr(cli, "_run_worker", _fake_run)
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    rc = cli._cmd_flash_all(
        {
            "images": [str(image)],
            "confirm": "ARM",
            "timeout": "1",
            "parallel": "2",
        }
    )

    assert rc == cli.EXIT_OK
    assert sorted(started) == ["ABC1234", "DEF5678"]
    assert len(list((tmp_path / "app" / "jobs").glob("*.json"))) == 2
    assert "2 drive(s) flashed" in capsys.readouterr().out


def test_flash_all_rejects_invalid_parallelism(tmp_path, capsys):
    image = tmp_path / "agent.iso"
    image.write_bytes(b"data")

    rc = cli._cmd_flash_all(
        {
            "images": [str(image)],
            "confirm": "ARM",
            "timeout": "1",
            "parallel": "9",
        }
    )

    assert rc == cli.EXIT_USAGE
    assert "--parallel must be a number" in capsys.readouterr().out


def test_flash_all_marks_failed_image_on_session(tmp_path, monkeypatch, capsys):
    """C03: a failed flash is recorded on the session like the GUI's."""
    from core import fleet

    sessions: list[fleet.FleetSession] = []
    base = fleet.FleetSession

    class Recording(base):  # type: ignore[misc]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            sessions.append(self)

    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(fleet, "FleetSession", Recording)
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_run_worker", lambda worker: (False, "boom"))
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    rc = cli._cmd_flash_all(
        {"images": [str(image)], "confirm": "ARM", "timeout": "1"}
    )

    assert rc == cli.EXIT_FAIL
    assert len(sessions) == 1
    assert sessions[0].failed_count == 1
    assert sessions[0].done_count == 0
    assert "fleet stopped" in capsys.readouterr().out


def test_flash_all_parallel_marks_failed_campaigns(
    tmp_path, monkeypatch, capsys
):
    """C03: parallel fleets record every failed target too."""
    from core import fleet

    sessions: list[fleet.FleetSession] = []
    base = fleet.FleetSession

    class Recording(base):  # type: ignore[misc]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            sessions.append(self)

    image = tmp_path / "agent.iso"
    image.write_bytes(b"data")
    first = _fake_drives()[0]
    second = dict(first)
    second.update(
        physical_path=r"\\.\PHYSICALDRIVE4",
        serial="DEF5678",
        letter="F",
        letters=["F"],
    )
    monkeypatch.setattr(fleet, "FleetSession", Recording)
    monkeypatch.setattr(cli, "_detect_drives", lambda *_: [first, second])
    monkeypatch.setattr("core.paths.APP_DIR", tmp_path / "app")
    monkeypatch.setattr(cli, "_run_worker", lambda worker: (False, "boom"))
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    rc = cli._cmd_flash_all(
        {
            "images": [str(image)],
            "confirm": "ARM",
            "timeout": "1",
            "parallel": "2",
        }
    )

    assert rc == cli.EXIT_FAIL
    assert sessions[0].failed_count == 2
    assert sessions[0].done_count == 0
    assert "campaign stopped" in capsys.readouterr().out


def test_deploy_runs_one_image_to_multiple_targets(
    tmp_path, monkeypatch, capsys
):
    image = tmp_path / "agent.iso"
    image.write_bytes(b"image")
    first = _fake_drives()[0]
    second = dict(first)
    second.update(
        physical_path=r"\\.\PHYSICALDRIVE4",
        serial="DEF5678",
        letter="F",
        letters=["F"],
    )
    monkeypatch.setattr(cli, "_detect_drives", lambda: [first, second])
    monkeypatch.setattr("core.paths.APP_DIR", tmp_path / "app")
    started: list[str] = []

    def _fake_run(worker):
        started.append(worker.target_fingerprint)
        return True, ""

    monkeypatch.setattr(cli, "_run_worker", _fake_run)

    rc = cli.main(
        [
            "deploy",
            "--image",
            str(image),
            "--drive",
            "ABC1234",
            "--drive",
            "DEF5678",
            "--confirm",
            "ARM",
            "--parallel",
            "2",
        ]
    )

    assert rc == cli.EXIT_OK
    assert sorted(started) == ["ABC1234", "DEF5678"]
    assert "deploy complete: 2 target(s) passed" in capsys.readouterr().out
    manifests = list((tmp_path / "app" / "jobs").glob("*.json"))
    assert len(manifests) == 2


def test_deploy_rejects_multiple_images(tmp_path, capsys):
    first = tmp_path / "one.iso"
    second = tmp_path / "two.iso"
    first.write_bytes(b"1")
    second.write_bytes(b"2")

    rc = cli._cmd_deploy(
        {
            "images": [str(first), str(second)],
            "drives": ["E"],
            "confirm": "ARM",
        }
    )

    assert rc == cli.EXIT_USAGE
    assert "exactly one --image" in capsys.readouterr().out


def test_deploy_rejects_invalid_parallelism(tmp_path, capsys):
    image = tmp_path / "one.iso"
    image.write_bytes(b"1")

    rc = cli._cmd_deploy(
        {
            "images": [str(image)],
            "drives": ["E"],
            "confirm": "ARM",
            "parallel": "9",
        }
    )

    assert rc == cli.EXIT_USAGE
    assert "--parallel must be a number" in capsys.readouterr().out


def test_deploy_status_cancel_and_retry(tmp_path, monkeypatch, capsys):
    from core import paths
    from core.jobs import JobManifest, save_manifest

    monkeypatch.setattr("core.paths.APP_DIR", tmp_path / "app")
    manifest = JobManifest(
        source_path="image.iso",
        source_size=10,
        source_sha256="a" * 64,
        target_fingerprint="serial:ONE",
        target_size=100,
        state="resumable",
    )
    path = paths.APP_DIR / "jobs" / f"{manifest.job_id}.json"
    save_manifest(path, manifest)

    assert cli.main(["deploy", "--status"]) == cli.EXIT_OK
    assert manifest.job_id in capsys.readouterr().out
    assert cli.main(["deploy", "--cancel", "--job", manifest.job_id]) == cli.EXIT_OK
    assert "cancelled" in capsys.readouterr().out
    assert cli.main(["deploy", "--retry", "--job", manifest.job_id]) == cli.EXIT_OK
    assert "queued" in capsys.readouterr().out


def test_deploy_status_prunes_stale_manifests(tmp_path, monkeypatch, capsys):
    """L10: the status listing drops old terminal-state manifests first."""
    import time

    from core import paths
    from core.jobs import JobManifest, save_manifest

    monkeypatch.setattr("core.paths.APP_DIR", tmp_path / "app")
    stale = JobManifest(
        source_path="old.iso",
        source_size=10,
        source_sha256="a" * 64,
        target_fingerprint="serial:OLD",
        target_size=100,
        state="failed",
    )
    stale_path = paths.APP_DIR / "jobs" / f"{stale.job_id}.json"
    save_manifest(stale_path, stale)
    ancient = time.time() - 40 * 86400
    os.utime(stale_path, (ancient, ancient))
    live = JobManifest(
        source_path="new.iso",
        source_size=10,
        source_sha256="b" * 64,
        target_fingerprint="serial:NEW",
        target_size=100,
        state="resumable",
    )
    live_path = paths.APP_DIR / "jobs" / f"{live.job_id}.json"
    save_manifest(live_path, live)

    assert cli.main(["deploy", "--status"]) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert live.job_id in out
    assert stale.job_id not in out
    assert not stale_path.exists()
    assert live_path.exists()


def test_deploy_status_survives_prune_failure(
    tmp_path, monkeypatch, capsys, caplog
):
    """A prune that blows up warns but never fails the listing."""
    from core import jobs as jobs_mod
    from core import paths
    from core.jobs import JobManifest, save_manifest

    monkeypatch.setattr("core.paths.APP_DIR", tmp_path / "app")
    manifest = JobManifest(
        source_path="image.iso",
        source_size=10,
        source_sha256="a" * 64,
        target_fingerprint="serial:ONE",
        target_size=100,
        state="resumable",
    )
    path = paths.APP_DIR / "jobs" / f"{manifest.job_id}.json"
    save_manifest(path, manifest)

    def _boom(directory):
        raise OSError("jobs directory is locked")

    monkeypatch.setattr(jobs_mod, "prune_manifests", _boom)

    assert cli.main(["deploy", "--status"]) == cli.EXIT_OK
    assert manifest.job_id in capsys.readouterr().out
    assert "could not prune" in caplog.text
    assert path.exists()


# --- verify --------------------------------------------------------------


def test_verify_rejects_bad_hex(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    assert (
        cli.main(["verify", "--drive", "E", "--sha256", "z" * 64])
        == cli.EXIT_USAGE
    )
    assert "64-hex digest" in capsys.readouterr().out


def test_verify_bad_block_scan_ok(monkeypatch, capsys):
    from core import verify as verify_mod

    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(
        verify_mod,
        "verify_device",
        lambda path: {"ok": True, "bad_sectors": []},
    )
    assert cli.main(["verify", "--drive", "E"]) == cli.EXIT_OK
    assert "no bad sectors" in capsys.readouterr().out


def test_verify_bad_block_scan_fails(monkeypatch, capsys):
    from core import verify as verify_mod

    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(
        verify_mod,
        "verify_device",
        lambda path: {"ok": False, "bad_sectors": [1, 2], "error": "boom"},
    )
    assert cli.main(["verify", "--drive", "E"]) == cli.EXIT_FAIL
    assert "bad sectors: 2 (boom)" in capsys.readouterr().out


# --- doctor and completions ----------------------------------------------


def test_doctor_reports_and_skips_elevation(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    called = []

    def _never(argv):
        called.append(argv)
        raise AssertionError("doctor must not relaunch elevated")

    monkeypatch.setattr(cli, "ensure_elevated", _never)
    assert cli.main(["doctor"]) == cli.EXIT_OK
    assert called == []
    captured = capsys.readouterr()
    assert "doctor report: 1 drive(s)" in captured.out
    assert "Flint Doctor" in captured.err
    assert "ABC1234" in captured.out


def test_doctor_json_shape(monkeypatch, capsys):
    from core.version import APP_VERSION

    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    assert cli.main(["doctor", "--json"]) == cli.EXIT_OK
    objects = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    report = next(o for o in objects if o["type"] == "doctor")
    assert report["version"] == APP_VERSION
    assert len(report["drives"]) == 1
    assert report["drives"][0]["serial"] == "ABC1234"
    assert any(o["type"] == "result" for o in objects)


def test_report_integrity_and_export(tmp_path, monkeypatch, capsys):
    from core import history

    history.HISTORY_PATH = tmp_path / "history.json"
    history.append_audited_history({"job_id": "one", "state": "passed"})
    output = tmp_path / "export.json"

    assert cli.main(
        ["report", "--integrity", "--out", str(output)]
    ) == cli.EXIT_OK
    assert output.exists()
    assert "history integrity: valid" in capsys.readouterr().out


def test_report_integrity_rejects_tampering(tmp_path, monkeypatch, capsys):
    from core import history

    history.HISTORY_PATH = tmp_path / "history.json"
    history.append_audited_history({"job_id": "one", "state": "passed"})
    entries = history.load_history()
    entries[0]["state"] = "failed"
    history.save_history(entries)

    assert cli.main(["report", "--integrity"]) == cli.EXIT_FAIL
    assert "history integrity failed" in capsys.readouterr().out


def test_completions_prints_powershell_script(capsys):
    assert cli.main(["completions"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Register-ArgumentCompleter" in out
    assert "flash-all" in out


@pytest.mark.parametrize(
    "shell,script",
    [
        ("powershell", cli._POWERSHELL_COMPLETION),
        ("bash", cli._BASH_COMPLETION),
        ("zsh", cli._ZSH_COMPLETION),
    ],
)
def test_completions_stdout_is_the_script_only(shell, script, capsys):
    """Redirected stdout must be the script and nothing else (no RESULT)."""
    assert cli.main(["completions", "--shell", shell]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert captured.out == script + "\n"
    assert "RESULT" not in captured.out
    assert f"RESULT ok: {shell} completion script" in captured.err


# --- streams -------------------------------------------------------------


def test_flash_progress_lines_never_pollute_stdout(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_run_worker", lambda worker: (True, ""))
    assert cli.main(["flash", "--image", str(image), "--drive", "E",
                     "--confirm", "ABC1234"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "FLINT " not in captured.out
    for line in captured.out.splitlines():
        assert "RESULT" in line


def test_main_wires_cli_logging_once(capsys, monkeypatch):
    """L21/C02: main() attaches exactly one stderr handler, at the
    settings log_level (patched here so the level is deterministic)."""
    import logging

    from core import settings as settings_mod

    flint = logging.getLogger("flint")

    def _cli_handlers():
        return [
            handler
            for handler in flint.handlers
            if getattr(handler, "_flint_cli_handler", False)
        ]

    def _purge():
        for handler in _cli_handlers():
            flint.removeHandler(handler)

    # Detach whatever a previous test's cli.main() attached so this run
    # exercises the attach path itself.
    _purge()
    real_level = str(settings_mod.get("log_level") or "WARNING")
    monkeypatch.setattr(
        settings_mod,
        "get",
        lambda key, default=None: "ERROR" if key == "log_level" else default,
    )

    assert cli.main(["help", "flash"]) == cli.EXIT_OK
    cli.main(["help", "flash"])
    capsys.readouterr()

    handlers = _cli_handlers()
    assert len(handlers) == 1
    assert handlers[0].level == logging.ERROR
    assert isinstance(handlers[0], logging.StreamHandler)

    # Restore process state for later tests: real settings level again.
    _purge()
    monkeypatch.undo()
    cli.main(["help", "flash"])
    restored = _cli_handlers()
    assert len(restored) == 1
    assert restored[0].level == getattr(logging, real_level.upper(), logging.WARNING)
    capsys.readouterr()


def test_scan_happy_path(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    from core import verify as verify_mod
    monkeypatch.setattr(
        verify_mod,
        "whole_drive_scan",
        lambda *a, **kw: {"ok": True, "bad_sectors": [], "digest": "ab", "speed_mbps": 1.0, "drive_size": 1000, "error": ""},
    )
    rc = cli.main(["--cli", "scan", "--drive", "E"])
    assert rc == cli.EXIT_OK
    assert "RESULT ok" in capsys.readouterr().out


def test_scan_reports_bad_sectors(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    from core import verify as verify_mod
    monkeypatch.setattr(
        verify_mod,
        "whole_drive_scan",
        lambda *a, **kw: {"ok": False, "bad_sectors": [{"offset": 0, "length": 4096}] * 3, "digest": "", "speed_mbps": 1.0, "drive_size": 1000, "error": ""},
    )
    rc = cli.main(["--cli", "scan", "--drive", "E"])
    assert rc == cli.EXIT_FAIL
    assert "RESULT fail" in capsys.readouterr().out


def test_scan_drive_not_found(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", list)
    rc = cli._cmd_scan({"drive": "NOPE"})
    assert rc == cli.EXIT_USAGE
    assert "drive not found" in capsys.readouterr().out


# --- v1.9.0: system disk guard, partition/filesystem/write-mode, --check-fake


def test_opts_parses_v1_9_flags():
    opts, error = cli._opts(
        [
            "--image", "a.iso", "--drive", "E",
            "--confirm", "ABC1234",
            "--partition-scheme", "gpt",
            "--filesystem", "ntfs",
            "--write-mode", "filecopy",
            "--check-fake",
        ]
    )
    assert error is None
    assert opts["partition-scheme"] == "gpt"
    assert opts["filesystem"] == "ntfs"
    assert opts["write-mode"] == "filecopy"
    assert opts["check-fake"] is True


def test_flash_passes_partition_and_write_options_to_worker(
    tmp_path, monkeypatch, capsys
):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    seen: dict[str, str] = {}

    def _capture(worker):
        seen["partition_scheme"] = worker.partition_scheme
        seen["filesystem"] = worker.filesystem
        seen["write_mode"] = worker.write_mode
        return True, ""

    monkeypatch.setattr(cli, "_run_worker", _capture)
    rc = cli._cmd_flash(
        {
            "image": str(image), "drive": "E", "confirm": "ABC1234",
            "partition-scheme": "gpt",
            "filesystem": "ntfs",
            "write-mode": "filecopy",
        }
    )

    assert rc == cli.EXIT_OK
    assert seen == {
        "partition_scheme": "gpt",
        "filesystem": "ntfs",
        "write_mode": "filecopy",
    }


class _FakeDetector:
    def __init__(self, system: set[str]) -> None:
        self.system = system

    def _system_disk_paths(self) -> set[str]:
        return self.system


def test_flash_refuses_system_disk(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    from core import drives

    monkeypatch.setattr(
        drives,
        "DriveDetector",
        lambda: _FakeDetector({r"\\.\PHYSICALDRIVE3"}),
    )

    rc = cli._cmd_flash(
        {"image": str(image), "drive": "E", "confirm": "ABC1234"}
    )

    assert rc == cli.EXIT_USAGE
    assert "system disk" in capsys.readouterr().out


def test_wipe_refuses_system_disk(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    from core import drives

    monkeypatch.setattr(
        drives,
        "DriveDetector",
        lambda: _FakeDetector({r"\\.\PHYSICALDRIVE3"}),
    )

    rc = cli._cmd_wipe(
        {"drive": "E", "confirm": "ABC1234", "method": "zero"}
    )

    assert rc == cli.EXIT_USAGE
    assert "system disk" in capsys.readouterr().out


def test_flash_allows_non_system_drive(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_run_worker", lambda worker: (True, ""))
    from core import drives

    monkeypatch.setattr(
        drives, "DriveDetector", lambda: _FakeDetector(set())
    )

    rc = cli._cmd_flash(
        {"image": str(image), "drive": "E", "confirm": "ABC1234"}
    )

    assert rc == cli.EXIT_OK
    assert "RESULT ok" in capsys.readouterr().out


def test_check_fake_helper_records_probe(monkeypatch, capsys):
    from core import fake_detect

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        fake_detect,
        "probe_capacity",
        lambda path, reported: calls.append((path, reported)) or (False, "clean"),
    )

    rc = cli._check_fake_drive({"physical_path": "X", "size_gb": 64}, False)

    assert rc is None
    assert calls == [("X", 64 * 1_000_000_000)]


def test_check_fake_helper_rejects_when_suspicious(monkeypatch, capsys):
    from core import fake_detect

    monkeypatch.setattr(
        fake_detect, "probe_capacity", lambda p, r: (True, "capacity lies")
    )

    rc = cli._check_fake_drive({"physical_path": "X", "size_gb": 64}, False)

    assert rc == cli.EXIT_USAGE
    assert "fake drive detected" in capsys.readouterr().out


def test_check_fake_helper_proceeds_with_yes(monkeypatch, capsys):
    from core import fake_detect

    monkeypatch.setattr(
        fake_detect, "probe_capacity", lambda p, r: (True, "capacity lies")
    )

    rc = cli._check_fake_drive({"physical_path": "X", "size_gb": 64}, True)

    assert rc is None
    assert "proceeding anyway" in capsys.readouterr().err


def test_check_fake_helper_skips_when_no_size(monkeypatch):
    from core import fake_detect

    hit: list[tuple] = []
    monkeypatch.setattr(
        fake_detect,
        "probe_capacity",
        lambda p, r: hit.append((p, r)) or (True, "x"),
    )

    rc = cli._check_fake_drive({"physical_path": "X", "size_gb": 0}, False)

    assert rc is None
    assert hit == []


def test_check_fake_helper_surfaces_clean_probe(monkeypatch, capsys):
    from core import fake_detect

    monkeypatch.setattr(
        fake_detect, "probe_capacity", lambda p, r: (False, "clean")
    )

    rc = cli._check_fake_drive({"physical_path": "X", "size_gb": 64}, False)

    assert rc is None
    assert "clean" in capsys.readouterr().err


def test_flash_check_fake_aborts_without_yes(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    from core import fake_detect

    monkeypatch.setattr(
        fake_detect, "probe_capacity", lambda p, r: (True, "capacity lies")
    )

    rc = cli._cmd_flash(
        {"image": str(image), "drive": "E", "confirm": "ABC1234",
         "check-fake": True}
    )

    assert rc == cli.EXIT_USAGE
    assert "fake drive detected" in capsys.readouterr().out


def test_flash_check_fake_aborts_backup_without_yes(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    from core import fake_detect

    monkeypatch.setattr(
        fake_detect, "probe_capacity", lambda p, r: (True, "capacity lies")
    )

    rc = cli._cmd_backup(
        {"drive": "E", "out": str(tmp_path / "b.img"), "check-fake": True}
    )

    assert rc == cli.EXIT_USAGE
    assert "fake drive detected" in capsys.readouterr().out


def test_flash_check_fake_proceeds_with_yes(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_run_worker", lambda worker: (True, ""))
    from core import fake_detect

    monkeypatch.setattr(
        fake_detect, "probe_capacity", lambda p, r: (True, "capacity lies")
    )

    rc = cli._cmd_flash(
        {"image": str(image), "drive": "E",
         "check-fake": True, "yes": True}
    )

    assert rc == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "proceeding anyway" in captured.err
    assert "RESULT ok" in captured.out


def test_backup_without_confirm_runs(tmp_path, monkeypatch, capsys):
    """Regression: plain ``backup`` runs (no ``--confirm``) used to crash
    with NameError because ``issue`` was only assigned in the confirm
    branch."""
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_run_worker", lambda worker: (True, ""))
    out = tmp_path / "b.img"

    rc = cli._cmd_backup({"drive": "E", "out": str(out)})

    assert rc == cli.EXIT_OK
    assert "RESULT ok" in capsys.readouterr().out


# --- v1.10.0: scripting contract, strict per-command flags, flash --resume


def test_main_unknown_command_emits_result(capsys):
    """The one failure path that previously dropped the RESULT line now
    emits it in both text and JSON modes."""
    assert cli.main(["frobnicate"]) == cli.EXIT_USAGE
    captured = capsys.readouterr()
    assert "RESULT fail: unknown command: frobnicate" in captured.out
    assert "unknown command: frobnicate" in captured.err


def test_json_unknown_command_emits_result_object(capsys):
    assert cli.main(["--json", "frobnicate"]) == cli.EXIT_USAGE
    objects = [
        json.loads(line) for line in capsys.readouterr().out.splitlines()
    ]
    assert objects == [
        {
            "type": "result",
            "status": "fail",
            "message": "unknown command: frobnicate",
            "exit": cli.EXIT_USAGE,
        }
    ]


def test_json_version_emits_result_object(capsys):
    from core.version import APP_VERSION

    assert cli.main(["--json", "--version"]) == cli.EXIT_OK
    objects = [
        json.loads(line) for line in capsys.readouterr().out.splitlines()
    ]
    assert objects[0]["type"] == "result"
    assert objects[0]["status"] == "ok"
    assert "flint" in objects[0]["message"].lower()
    assert APP_VERSION in objects[0]["message"]


def test_json_help_emits_result_object(capsys):
    assert cli.main(["--json", "help", "flash"]) == cli.EXIT_OK
    objects = [
        json.loads(line) for line in capsys.readouterr().out.splitlines()
    ]
    assert objects[0]["type"] == "result"
    assert objects[0]["status"] == "ok"
    assert "flint flash --image" in objects[0]["message"]


def test_json_no_command_emits_result_object(capsys):
    assert cli.main(["--json"]) == cli.EXIT_OK
    objects = [
        json.loads(line) for line in capsys.readouterr().out.splitlines()
    ]
    assert objects[0]["type"] == "result"
    assert objects[0]["status"] == "ok"


def test_flash_missing_image_reports_required(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    rc = cli._cmd_flash({"drive": "E"})
    assert rc == cli.EXIT_USAGE
    assert "missing required option --image" in capsys.readouterr().out


def test_flash_missing_drive_reports_required(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    rc = cli._cmd_flash({"image": str(image)})
    assert rc == cli.EXIT_USAGE
    assert "missing required option --drive" in capsys.readouterr().out


def test_flash_image_not_found_includes_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    rc = cli._cmd_flash({"image": str(tmp_path / "nope.iso"), "drive": "E"})
    assert rc == cli.EXIT_USAGE
    assert "nope.iso" in capsys.readouterr().out


def test_verify_missing_drive_reports_required(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    assert cli._cmd_verify({}) == cli.EXIT_USAGE
    assert "missing required option --drive" in capsys.readouterr().out


def test_queue_missing_file_reports_required(capsys):
    assert cli._cmd_queue({}) == cli.EXIT_USAGE
    assert "missing required option --file" in capsys.readouterr().out


def test_queue_missing_drive_reports_required(tmp_path, capsys):
    img = tmp_path / "x.iso"
    img.write_bytes(b"data")
    queue_file = tmp_path / "queue.txt"
    queue_file.write_text(f"{img}\n")
    rc = cli._cmd_queue({"file": str(queue_file)})
    assert rc == cli.EXIT_USAGE
    assert "missing required option --drive" in capsys.readouterr().out


def test_backup_missing_drive_reports_required(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    assert cli._cmd_backup({"out": "b.img"}) == cli.EXIT_USAGE
    assert "missing required option --drive" in capsys.readouterr().out


def test_flash_oversize_image_is_usage(tmp_path, monkeypatch, capsys):
    image = tmp_path / "big.iso"
    image.write_bytes(b"data")
    tiny = dict(_fake_drives()[0])
    tiny["size_gb"] = 0
    monkeypatch.setattr(cli, "_detect_drives", lambda *args: [tiny])

    rc = cli._cmd_flash(
        {"image": str(image), "drive": "E", "confirm": "ABC1234"}
    )

    assert rc == cli.EXIT_USAGE
    out = capsys.readouterr().out
    assert "larger than the target drive" in out


def test_flash_drive_not_found_listing_stays_on_stderr(
    tmp_path, monkeypatch, capsys
):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", list)
    rc = cli._cmd_flash({"image": str(image), "drive": "E"})
    assert rc == cli.EXIT_USAGE
    captured = capsys.readouterr()
    assert "RESULT fail: drive not found" in captured.out
    assert "detected drives:" not in captured.out
    assert "detected drives:" in captured.err


def test_scan_drive_not_found_listing_stays_on_stderr(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", list)
    rc = cli._cmd_scan({"drive": "NOPE"})
    assert rc == cli.EXIT_USAGE
    captured = capsys.readouterr()
    assert "detected drives:" not in captured.out
    assert "detected drives:" in captured.err


def test_cross_command_flag_rejected(capsys):
    """flint wipe --verify must be a usage error, not silently ignored."""
    assert cli.main(["wipe", "--verify"]) == cli.EXIT_USAGE
    out = capsys.readouterr().out
    assert "--verify is not valid for command wipe" in out


def test_write_options_validated_before_any_work(capsys):
    """B12: --filesystem/--partition-scheme/--write-mode are usage errors.

    --filesystem used to be checked only inside run_format(), i.e. after
    diskpart had already wiped the target, and the other two were silently
    coerced instead of rejected."""
    cases = [
        ("--filesystem", "ext4", "fat32, ntfs, exfat"),
        ("--partition-scheme", "mbrx", "auto, gpt, mbr"),
    ]
    for flag, value, allowed in cases:
        rc = cli.main(
            ["flash", "--image", "x.iso", "--drive", "E", flag, value]
        )
        assert rc == cli.EXIT_USAGE, flag
        captured = capsys.readouterr()
        assert f"{flag} must be one of {allowed}" in captured.out + captured.err

    assert cli._validate_write_opts("flash", {"write-mode": "dd2"}) == (
        "--write-mode must be one of auto, dd, filecopy"
    )
    # D02 spellings advertised by the help text stay accepted.
    assert cli._validate_write_opts("flash", {"write-mode": "file-copy"}) is None
    # Commands without these options are unaffected.
    assert cli._validate_write_opts("list", {"filesystem": "ext4"}) is None


def test_conflicting_confirm_and_yes_flash(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    rc = cli._cmd_flash(
        {"image": str(image), "drive": "E", "confirm": "WRONG", "yes": True}
    )
    assert rc == cli.EXIT_USAGE
    assert "not both" in capsys.readouterr().out


def test_conflicting_confirm_and_yes_fleet(tmp_path, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    rc = cli._cmd_flash_all(
        {"images": [str(image)], "confirm": "ARM", "yes": True}
    )
    assert rc == cli.EXIT_USAGE
    assert "not both" in capsys.readouterr().out


def test_list_json_with_no_drives_emits_empty_array(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_drives", list)
    assert cli.main(["list", "--json"]) == cli.EXIT_OK
    objects = [
        json.loads(line) for line in capsys.readouterr().out.splitlines()
    ]
    drives = next(o for o in objects if o["type"] == "drives")
    assert drives["drives"] == []
    assert any(o["type"] == "result" and o["status"] == "ok" for o in objects)


def test_quiet_never_prompts_and_fails_cleanly(tmp_path, monkeypatch, capsys):
    """--quiet with a TTY must not block on an invisible input() read."""
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    prompt_calls: list[str] = []
    monkeypatch.setattr(cli, "_QUIET", True)
    monkeypatch.setattr(cli, "_prompt", lambda prompt: prompt_calls.append(prompt) or "x")

    rc = cli._cmd_flash({"image": str(image), "drive": "E"})

    assert rc == cli.EXIT_USAGE
    assert prompt_calls == []
    assert "pass --confirm" in capsys.readouterr().out


def test_completions_rejects_unknown_shell(capsys):
    assert cli._cmd_completions({"shell": "fish"}) == cli.EXIT_USAGE
    assert "--shell must be one of" in capsys.readouterr().out


def test_flash_resume_flag_passes_to_worker(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    seen: list[bool] = []

    def _capture(worker):
        seen.append(worker.resume)
        return True, ""

    monkeypatch.setattr(cli, "_run_worker", _capture)
    rc = cli._cmd_flash(
        {"image": str(image), "drive": "E", "confirm": "ABC1234", "resume": True}
    )
    assert rc == cli.EXIT_OK
    assert seen == [True]


def test_flash_help_documents_resume(capsys):
    assert cli.main(["flash", "--help"]) == cli.EXIT_OK
    assert "--resume" in capsys.readouterr().out

# --- agent C findings: B04 B05 B09 B10 B11 L05 L06 L08 L11 L13 L16
# --- L18 L19 L20 D01 D02 D09 D10 C03


def test_flash_decompresses_archive_before_writing(tmp_path, monkeypatch, capsys):
    """B04: an archive is extracted; the raw stream is never written."""
    import gzip as gzip_lib

    payload = b"image payload"
    archive = tmp_path / "disk.img.gz"
    archive.write_bytes(gzip_lib.compress(payload))
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    seen: dict[str, object] = {}

    def _fake_run(worker):
        seen["path"] = worker.iso_path
        seen["exists"] = os.path.isfile(worker.iso_path)
        with open(worker.iso_path, "rb") as handle:
            seen["bytes"] = handle.read()
        return True, ""

    monkeypatch.setattr(cli, "_run_worker", _fake_run)

    rc = cli._cmd_flash(
        {"image": str(archive), "drive": "E", "confirm": "ABC1234"}
    )

    assert rc == cli.EXIT_OK
    assert seen["path"] != str(archive)
    assert str(seen["path"]).endswith("disk.img")
    assert seen["bytes"] == payload
    assert seen["exists"] is True
    # the temp file is gone once the write finished
    assert not os.path.exists(str(seen["path"]))
    assert "decompressed disk.img.gz" in capsys.readouterr().err


def test_flash_rejects_unsupported_compression(tmp_path, monkeypatch, capsys):
    from core import decompress

    image = tmp_path / "a.7z"
    image.write_bytes(b"data")
    monkeypatch.setattr(decompress, "compressed_format", lambda path: ".7z")
    ran: list[object] = []
    monkeypatch.setattr(cli, "_run_worker", lambda worker: ran.append(1) or (True, ""))

    rc = cli._cmd_flash(
        {"image": str(image), "drive": "E", "confirm": "ABC1234"}
    )

    assert rc == cli.EXIT_USAGE
    out = capsys.readouterr().out
    assert "unsupported compression" in out
    assert "RESULT fail" in out
    assert ran == []


def test_flash_rejects_zst_without_library(tmp_path, monkeypatch, capsys):
    import sys

    image = tmp_path / "a.img.zst"
    image.write_bytes(b"data")
    monkeypatch.setitem(sys.modules, "zstandard", None)
    ran: list[object] = []
    monkeypatch.setattr(cli, "_run_worker", lambda worker: ran.append(1) or (True, ""))

    rc = cli._cmd_flash(
        {"image": str(image), "drive": "E", "confirm": "ABC1234"}
    )

    assert rc == cli.EXIT_USAGE
    assert "zstandard" in capsys.readouterr().out
    assert ran == []


def test_flash_backup_image_writes_raw(tmp_path, monkeypatch):
    """B10: restoring a backup must not fall into the file-copy path."""
    image = tmp_path / "flint-backup-20240101-010101.img"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    seen: list[str] = []

    monkeypatch.setattr(cli, "_run_worker", lambda worker: seen.append(worker.write_mode) or (True, ""))

    rc = cli._cmd_flash(
        {"image": str(image), "drive": "E", "confirm": "ABC1234"}
    )

    assert rc == cli.EXIT_OK
    assert seen == ["dd"]


def test_flash_ignores_missing_image_size_bytes_when_absent(tmp_path, monkeypatch):
    """B10: an explicitly requested mode always wins over the backup rule."""
    image = tmp_path / "restore.img"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    seen: list[str] = []

    monkeypatch.setattr(cli, "_run_worker", lambda worker: seen.append(worker.write_mode) or (True, ""))

    rc = cli._cmd_flash(
        {
            "image": str(image),
            "drive": "E",
            "confirm": "ABC1234",
            "write-mode": "filecopy",
        }
    )

    assert rc == cli.EXIT_OK
    assert seen == ["filecopy"]


def test_flash_sidecar_mismatch_blocks_flash(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    (tmp_path / "a.iso.sha256").write_text("0" * 64 + "\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    ran: list[object] = []
    monkeypatch.setattr(cli, "_run_worker", lambda worker: ran.append(1) or (True, ""))

    rc = cli._cmd_flash(
        {"image": str(image), "drive": "E", "confirm": "ABC1234"}
    )

    assert rc == cli.EXIT_FAIL
    out = capsys.readouterr().out
    assert "does not match" in out
    assert "RESULT fail" in out
    assert ran == []


def test_flash_matching_sidecar_allows_flash(tmp_path, monkeypatch, capsys):
    import hashlib

    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    digest = hashlib.sha256(b"data").hexdigest()
    (tmp_path / "a.iso.sha256").write_text(digest + "\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    ran: list[object] = []
    monkeypatch.setattr(cli, "_run_worker", lambda worker: ran.append(1) or (True, ""))

    rc = cli._cmd_flash(
        {"image": str(image), "drive": "E", "confirm": "ABC1234"}
    )

    assert rc == cli.EXIT_OK
    assert ran == [1]
    assert "sidecar checksum OK" in capsys.readouterr().err


def test_sidecar_filename_column_selects_this_image(tmp_path, capsys):
    """E: the sidecar is validated against this image's basename."""
    import hashlib

    image = tmp_path / "ubuntu.iso"
    image.write_bytes(b"data")
    digest = hashlib.sha256(b"data").hexdigest()
    sidecar = tmp_path / "ubuntu.iso.sha256"

    sidecar.write_text(f"{digest}  other.iso\n", encoding="utf-8")
    assert cli._check_sidecar(str(image)) == cli.EXIT_USAGE
    assert "lists no SHA-256 digest for ubuntu.iso" in capsys.readouterr().out

    sidecar.write_text(f"{digest} *ubuntu.iso\n", encoding="utf-8")
    assert cli._check_sidecar(str(image)) is None

    sidecar.write_text(f"{digest}\n", encoding="utf-8")
    assert cli._check_sidecar(str(image)) is None



def test_clone_gates_on_exact_size_bytes(monkeypatch, capsys):
    """L05: rounded size_gb must not decide the clone gate."""
    source = {
        "physical_path": r"\\.\PHYSICALDRIVE3",
        "serial": "SRC1111",
        "model": "USB Stick",
        "size_gb": 16,
        "size_bytes": 15_000_000_000,
        "letter": "E",
        "letters": ["E"],
        "name": "USB Stick",
    }
    target = dict(
        source,
        physical_path=r"\\.\PHYSICALDRIVE4",
        serial="TGT1111",
        size_bytes=14_000_000_000,
        letter="F",
        letters=["F"],
    )
    monkeypatch.setattr(cli, "_detect_drives", lambda *_: [source, target])

    rc = cli._cmd_clone(
        {"from": "SRC1111", "to": "TGT1111", "confirm": "TGT1111"}
    )

    assert rc == cli.EXIT_USAGE
    assert "smaller than the source" in capsys.readouterr().out


def test_check_fake_probe_uses_size_bytes(monkeypatch):
    """L06: the probe reads at the exact capacity, not a rounded offset."""
    from core import fake_detect

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        fake_detect,
        "probe_capacity",
        lambda path, reported: calls.append((path, reported)) or (False, "clean"),
    )

    rc = cli._check_fake_drive(
        {"physical_path": "X", "size_gb": 64, "size_bytes": 64_000_000_012},
        False,
    )

    assert rc is None
    assert calls == [("X", 64_000_000_012)]


def test_flash_verify_reads_drive_back_exactly_once(tmp_path, monkeypatch, capsys):
    """L08: the writer's read-back is the only verification pass."""
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    runs: list[object] = []
    second_pass: list[object] = []
    monkeypatch.setattr(cli, "_run_worker", lambda worker: runs.append(worker) or (True, ""))
    monkeypatch.setattr(
        cli, "_cmd_verify_raw", lambda *a, **k: second_pass.append(a) or cli.EXIT_OK
    )

    rc = cli._cmd_flash(
        {"image": str(image), "drive": "E", "confirm": "ABC1234", "verify": True}
    )

    assert rc == cli.EXIT_OK
    assert len(runs) == 1
    assert second_pass == []
    assert "flashed and verified" in capsys.readouterr().out


def test_flash_all_parallel_serves_multi_image_fleets(
    tmp_path, monkeypatch, capsys
):
    """L11: --parallel fans out over drives even with several images."""
    import threading

    one = tmp_path / "one.iso"
    two = tmp_path / "two.iso"
    one.write_bytes(b"1")
    two.write_bytes(b"2")
    first = _fake_drives()[0]
    second = dict(
        first,
        physical_path=r"\\.\PHYSICALDRIVE4",
        serial="DEF5678",
        letter="F",
        letters=["F"],
    )
    monkeypatch.setattr(cli, "_detect_drives", lambda *_: [first, second])
    monkeypatch.setattr("core.paths.APP_DIR", tmp_path / "app")
    started: list[tuple[str, str]] = []
    lock = threading.Lock()

    def _fake_run(worker):
        with lock:
            started.append((worker.target_fingerprint, worker.iso_path))
        return True, ""

    monkeypatch.setattr(cli, "_run_worker", _fake_run)
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    rc = cli._cmd_flash_all(
        {
            "images": [str(one), str(two)],
            "confirm": "ARM",
            "timeout": "1",
            "parallel": "2",
        }
    )

    assert rc == cli.EXIT_OK
    # one campaign job per drive, each flashing every image
    assert len(started) == 4
    assert {fingerprint for fingerprint, _ in started} == {"ABC1234", "DEF5678"}
    assert sorted(path for _, path in started) == sorted(
        [str(one), str(one), str(two), str(two)]
    )
    assert len(list((tmp_path / "app" / "jobs").glob("*.json"))) == 2


def test_flash_bypass_tpm_requires_filecopy(tmp_path, monkeypatch, capsys):
    """L13: a bypass that can never patch fails instead of being ignored."""
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    ran: list[object] = []
    monkeypatch.setattr(cli, "_run_worker", lambda worker: ran.append(1) or (True, ""))

    rc = cli._cmd_flash(
        {
            "image": str(image),
            "drive": "E",
            "confirm": "ABC1234",
            "bypass-tpm": True,
        }
    )

    assert rc == cli.EXIT_USAGE
    out = capsys.readouterr().out
    assert "bypass-tpm" in out
    assert "filecopy" in out
    assert ran == []


def test_flash_all_rejects_bypass_tpm_without_filecopy(tmp_path, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")

    rc = cli._cmd_flash_all(
        {"images": [str(image)], "confirm": "ARM", "bypass-tpm": True}
    )

    assert rc == cli.EXIT_USAGE
    assert "bypass-tpm" in capsys.readouterr().out


def test_deploy_rejects_bypass_tpm_without_filecopy(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("core.paths.APP_DIR", tmp_path / "app")
    image = tmp_path / "a.iso"
    image.write_bytes(b"data")

    rc = cli._cmd_deploy(
        {
            "images": [str(image)],
            "drives": ["E"],
            "confirm": "ARM",
            "bypass-tpm": True,
        }
    )

    assert rc == cli.EXIT_USAGE
    assert "bypass-tpm" in capsys.readouterr().out


def test_queue_forwards_write_mode_and_bypass(tmp_path, monkeypatch, capsys):
    queue_file = tmp_path / "queue.txt"
    queue_file.write_text("disk.img\n", encoding="utf-8")
    disk = tmp_path / "disk.img"
    disk.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    seen: dict[str, object] = {}

    def _capture(worker):
        seen["write_mode"] = worker.write_mode
        seen["bypass_tpm"] = worker.bypass_tpm
        return True, ""

    monkeypatch.setattr(cli, "_run_worker", _capture)

    rc = cli._cmd_queue(
        {
            "file": str(queue_file),
            "drive": "E",
            "confirm": "ABC1234",
            "write-mode": "filecopy",
            "bypass-tpm": True,
        }
    )

    assert rc == cli.EXIT_OK
    assert seen == {"write_mode": "filecopy", "bypass_tpm": True}


def test_queue_bypass_tpm_without_filecopy_fails(tmp_path, monkeypatch, capsys):
    queue_file = tmp_path / "queue.txt"
    queue_file.write_text("disk.iso\n", encoding="utf-8")
    disk = tmp_path / "disk.iso"
    disk.write_bytes(b"data")
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)
    ran: list[object] = []
    monkeypatch.setattr(cli, "_run_worker", lambda worker: ran.append(1) or (True, ""))

    rc = cli._cmd_queue(
        {"file": str(queue_file), "drive": "E", "confirm": "ABC1234", "bypass-tpm": True}
    )

    assert rc == cli.EXIT_USAGE
    assert "bypass-tpm" in capsys.readouterr().out
    assert ran == []


def test_deploy_forwards_write_mode_to_worker(tmp_path, monkeypatch, capsys):
    image = tmp_path / "a.iso"
    image.write_bytes(b"image")
    first = _fake_drives()[0]
    second = dict(
        first,
        physical_path=r"\\.\PHYSICALDRIVE4",
        serial="DEF5678",
        letter="F",
        letters=["F"],
    )
    monkeypatch.setattr(cli, "_detect_drives", lambda *_: [first, second])
    monkeypatch.setattr("core.paths.APP_DIR", tmp_path / "app")
    seen: list[str] = []

    monkeypatch.setattr(cli, "_run_worker", lambda worker: seen.append(worker.write_mode) or (True, ""))

    rc = cli._cmd_deploy(
        {
            "images": [str(image)],
            "drives": ["ABC1234", "DEF5678"],
            "confirm": "ARM",
            "parallel": "1",
            "write-mode": "filecopy",
            "bypass-tpm": True,
        }
    )

    assert rc == cli.EXIT_OK
    assert seen == ["filecopy", "filecopy"]


def test_list_json_reports_size_bytes(monkeypatch, capsys):
    drives = [
        {
            "physical_path": r"\\.\PHYSICALDRIVE3",
            "serial": "ABC1234",
            "model": "USB Stick",
            "size_gb": 16,
            "size_bytes": 17_179_869_184,
            "letter": "E",
            "letters": ["E"],
            "name": "USB Stick",
        }
    ]
    monkeypatch.setattr(cli, "_detect_drives", lambda *_: drives)

    assert cli.main(["list", "--json"]) == cli.EXIT_OK

    objects = [
        json.loads(line) for line in capsys.readouterr().out.splitlines()
    ]
    payload = next(obj for obj in objects if obj["type"] == "drives")
    assert payload["drives"][0]["size_bytes"] == 17_179_869_184


def test_double_cli_flag_is_stripped(monkeypatch, capsys):
    """L18: every --cli token disappears, not just the first."""
    monkeypatch.setattr(cli, "_detect_drives", _fake_drives)

    rc = cli.main(["--cli", "--cli", "list"])

    assert rc == cli.EXIT_OK
    assert "RESULT ok" in capsys.readouterr().out


def test_version_text_mode_emits_result(capsys):
    assert cli.main(["--version"]) == cli.EXIT_OK
    assert "RESULT ok" in capsys.readouterr().out


def test_help_text_mode_emits_result(capsys):
    assert cli.main(["help", "flash"]) == cli.EXIT_OK
    assert "RESULT ok" in capsys.readouterr().out


def test_bare_argv_text_mode_emits_result(capsys):
    assert cli.main([]) == cli.EXIT_OK
    assert "RESULT ok" in capsys.readouterr().out


def test_completions_text_mode_emits_result(capsys):
    assert cli.main(["completions"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "Register-ArgumentCompleter" in captured.out
    # L19 keeps the RESULT line, but off the script stream.
    assert "RESULT ok" in captured.err



def test_elevation_rebuilds_from_passed_argv(monkeypatch, capsys):
    """L20: the relaunch uses argv given to main(), not the host's argv."""
    recorded: list[list[str]] = []
    monkeypatch.setattr(
        cli, "ensure_elevated", lambda argv: recorded.append(argv) or None
    )

    rc = cli.main(["wipe", "--yes"])

    assert rc == cli.EXIT_USAGE
    assert len(recorded) == 1
    assert recorded[0][-2:] == ["wipe", "--yes"]


def test_ensure_elevated_uses_timeout(monkeypatch):
    """The UAC relaunch must fail closed instead of hanging forever."""
    seen: dict[str, object] = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    result = cli._run_elevated_relaunch("echo ok", "out.txt", "err.txt")

    assert result.returncode == 0
    assert seen["kwargs"].get("timeout") == cli._ELEVATION_TIMEOUT_SECONDS


def test_flash_resume_documents_job_manifest(capsys):
    """D01: help names the manifest that actually exists."""
    assert cli.main(["flash", "--help"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert ".flint_job.json" in out
    assert ".flint_state" not in out


def test_flash_help_documents_write_mode_values(capsys):
    assert cli.main(["flash", "--help"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "auto|dd|filecopy" in out
    assert "--write-mode raw|file-copy" not in out


def test_resolve_write_mode_accepts_documented_aliases(tmp_path):
    """D02: the spellings the help used to advertise now resolve."""
    from core.diskpart import resolve_write_mode

    plain = tmp_path / "plain.iso"
    plain.write_bytes(b"data")

    assert resolve_write_mode("raw", str(plain)) == "dd"
    assert resolve_write_mode("file-copy", str(plain)) == "filecopy"
    assert resolve_write_mode("filecopy", str(plain)) == "filecopy"
    assert resolve_write_mode("auto", str(plain)) == "dd"


def test_deploy_run_elevates_while_status_does_not(
    tmp_path, monkeypatch, capsys
):
    """B05/D09: --run writes, so it leaves the privilege-free bucket."""
    monkeypatch.setattr("core.paths.APP_DIR", tmp_path / "app")
    recorded: list[str] = []
    monkeypatch.setattr(
        cli, "ensure_elevated", lambda argv: recorded.append("elevated") or None
    )

    assert cli.main(["deploy", "--status"]) == cli.EXIT_OK
    assert recorded == []

    assert cli.main(["deploy", "--run", "--job", "nope"]) == cli.EXIT_USAGE
    assert recorded == ["elevated"]
    assert "deployment job not found" in capsys.readouterr().out


def test_deploy_help_documents_elevation_split(capsys):
    assert cli.main(["deploy", "--help"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "no privileges" in out
    assert "elevates" in out


def test_completion_scripts_cover_every_option():
    """D10: the generated lists come from the parser's own option sets."""
    powershell = cli._POWERSHELL_COMPLETION
    bash = cli._BASH_COMPLETION
    zsh = cli._ZSH_COMPLETION
    assert cli._ALL_OPTIONS
    bash_options_line = next(
        line for line in bash.splitlines()
        if line.strip().startswith("options=")
    )
    bash_options = set(
        bash_options_line.split("=", 1)[1].strip().strip("'").split()
    )
    for option in cli._ALL_OPTIONS:
        assert f"'{option}'" in powershell, option
        assert option in bash_options, option
        assert f"'{option}[" in zsh, option

def test_dead_cli_api_removed():
    """C03: TOP_LEVEL_COMMANDS and _run_worker's label are gone."""
    import inspect

    assert not hasattr(cli, "TOP_LEVEL_COMMANDS")
    assert "label" not in inspect.signature(cli._run_worker).parameters


def test_run_worker_ctrl_c_cancels_worker(monkeypatch, capsys):
    """B09: Ctrl+C cancels the worker and waits for its finished signal."""
    from PyQt6.QtCore import QObject, pyqtSignal

    class _CancellableWorker(QObject):
        progress = pyqtSignal(float)
        done = pyqtSignal(bool, str)

        def __init__(self) -> None:
            super().__init__()
            self.cancel_calls = 0

        def start(self) -> None:  # no thread: the loop is faked below
            pass

        def cancel(self) -> None:
            self.cancel_calls += 1
            self.done.emit(False, "cancelled")

    class _InterruptingLoop:
        def exec(self) -> None:
            raise KeyboardInterrupt()

        def quit(self) -> None:
            pass

    monkeypatch.setattr(cli, "QEventLoop", _InterruptingLoop)
    # _run_worker's _ensure_app() pins a process-wide QCoreApplication;
    # restore _APP to None on teardown or every later MainWindow test
    # aborts Qt (0xC0000409) when it creates a QApplication.
    monkeypatch.setattr(cli, "_APP", None, raising=False)
    worker = _CancellableWorker()

    ok, message = cli._run_worker(worker)

    assert ok is False
    assert message == "cancelled"
    assert worker.cancel_calls == 1
    assert "interrupted - cancelling" in capsys.readouterr().err


def test_result_line_survives_ascii_codepage_pipe(monkeypatch):
    """B17: the Unicode RESULT icon must not crash an ANSI-codepage pipe."""
    import io as io_mod

    buf = io_mod.BytesIO()
    ascii_stream = io_mod.TextIOWrapper(buf, encoding="ascii", newline="")
    monkeypatch.setattr(cli.sys, "stdout", ascii_stream)
    monkeypatch.setattr(cli, "_JSON", False)
    monkeypatch.setattr(cli, "_QUIET", False)

    rc = cli._result("ok", "Flint v2.0.1", cli.EXIT_OK)

    ascii_stream.flush()
    assert rc == cli.EXIT_OK
    assert b"RESULT ok" in buf.getvalue()


def test_main_cli_honors_log_level_setting(monkeypatch):
    """C02: cli.main passes the settings log_level to setup_cli_logging."""
    from core import log as log_mod
    from core import settings as settings_mod

    seen: dict[str, str] = {}

    def fake_setup(name: str = "flint", level: str = "WARNING"):
        seen["level"] = level
        return log_mod.logging.getLogger(name)

    monkeypatch.setattr(log_mod, "setup_cli_logging", fake_setup)
    monkeypatch.setattr(
        settings_mod,
        "get",
        lambda key, default=None: "DEBUG" if key == "log_level" else default,
    )

    rc = cli.main(["--version"])

    assert rc == cli.EXIT_OK
    assert seen["level"] == "DEBUG"


def test_flash_records_history_for_skip_flashed(tmp_path, monkeypatch, capsys):
    """L07: CLI flashes land in audited history so --skip-flashed can
    match them next run — including a serial-less stick via its path."""
    from core import fleet, history

    image = tmp_path / "ubuntu.iso"
    image.write_bytes(b"data")
    monkeypatch.setattr(history, "HISTORY_PATH", tmp_path / "h.json")
    drive = _fake_drives()[0]
    monkeypatch.setattr(cli, "_detect_drives", lambda *a, **k: [drive])
    monkeypatch.setattr(cli, "_run_worker", lambda worker: (True, ""))

    rc = cli.main(
        ["flash", "--image", str(image), "--drive", "E", "--confirm", "ABC1234"]
    )

    assert rc == cli.EXIT_OK
    assert "RESULT ok" in capsys.readouterr().out
    entries = [
        entry
        for entry in history.load_history()
        if entry.get("operation") == "flash"
    ]
    assert entries and entries[-1]["success"] is True
    assert entries[-1]["physical_path"] == drive["physical_path"]
    # A serial-less stick with the same path is recognised; another is not.
    serial_less = {**drive, "serial": ""}
    assert fleet.was_recently_flashed(serial_less, str(image)) is True
    other = {**serial_less, "physical_path": r"\\.\PHYSICALDRIVE9"}
    assert fleet.was_recently_flashed(other, str(image)) is False


def test_copy_to_clipboard_caps_a_wedged_child(monkeypatch):
    """The clipboard hop must never be able to hang `flint report`."""
    seen: dict[str, object] = {}

    def hang(*args, **kwargs):
        seen.update(kwargs)
        raise cli.subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(cli.subprocess, "run", hang)
    cli._copy_to_clipboard("report text")

    assert seen["timeout"] == 15
