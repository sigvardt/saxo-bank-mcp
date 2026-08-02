from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_RUN_PYTEST = _ROOT / "scripts/run-pytest"
_SHORT_RUN_NAME_LENGTH = 4


def _load_launcher() -> ModuleType:
    spec = spec_from_file_location("saxo_pytest_launcher", _ROOT / "scripts/pytest_launcher.py")
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pytest_launcher = _load_launcher()


def _fake_uv(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    log = tmp_path / "fake-uv.log"
    executable = fake_bin / "uv"
    executable.write_text(
        """#!/bin/sh
set -eu
{
    printf 'TMPDIR=%s\\n' "$TMPDIR"
    printf 'TMP=%s\\n' "$TMP"
    printf 'TEMP=%s\\n' "$TEMP"
    for argument in "$@"; do
        printf 'ARG=%s\\n' "$argument"
    done
} > "$FAKE_UV_LOG"
mkdir "$TMPDIR/readonly"
: > "$TMPDIR/readonly/artifact"
chmod 400 "$TMPDIR/readonly/artifact"
exit "${FAKE_UV_EXIT:-0}"
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return fake_bin, log


def _sleeping_fake_uv(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "sleeping-fake-bin"
    fake_bin.mkdir()
    log = tmp_path / "sleeping-fake-uv.log"
    executable = fake_bin / "uv"
    executable.write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' "$TMPDIR" > "$FAKE_UV_LOG"
while :; do sleep 1; done
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return fake_bin, log


def _launcher_environment(fake_bin: Path, log: Path, *, exit_code: int = 0) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": os.pathsep.join((str(fake_bin), os.environ.get("PATH", "/usr/bin:/bin"))),
        "FAKE_UV_LOG": str(log),
        "FAKE_UV_EXIT": str(exit_code),
    }


def _darwin_options(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    volume = tmp_path / "volume"
    volume.mkdir()
    shared_root = volume / "codex/tmp/saxo-bank-mcp-analytics"

    def disk_usage(_path: Path) -> SimpleNamespace:
        return SimpleNamespace(free=pytest_launcher.MIN_SYSTEM_DATA_FREE_BYTES)

    def mount_checker(path: Path) -> bool:
        return path == volume

    options: dict[str, Any] = {
        "platform_name": "darwin",
        "darwin_volume": volume,
        "darwin_shared_root": shared_root,
        "system_data_volume": tmp_path,
        "disk_usage": disk_usage,
        "mount_checker": mount_checker,
    }
    return shared_root, options


def test_real_darwin_launcher_rejects_nested_symlink_before_fake_uv(
    tmp_path: Path,
) -> None:
    if os.uname().sysname != "Darwin":
        pytest.skip("real external-volume launcher boundary is macOS-only")
    shared_root = pytest_launcher.DARWIN_SHARED_ROOT
    assert shared_root.is_dir()
    assert not shared_root.is_symlink()
    escape = tmp_path / "escape"
    escape.mkdir(mode=0o755)
    original_mode = escape.stat().st_mode & 0o777
    nested_link = shared_root / "tmp"
    assert not nested_link.exists()
    assert not nested_link.is_symlink()
    nested_link.symlink_to(escape, target_is_directory=True)
    fake_bin, log = _fake_uv(tmp_path)
    try:
        result = subprocess.run(
            (_RUN_PYTEST, "tests/does-not-run.py"),
            cwd=_ROOT,
            env=_launcher_environment(fake_bin, log),
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        nested_link.unlink(missing_ok=True)

    assert result.returncode != 0
    assert "symlink" in result.stderr.casefold()
    assert not log.exists()
    assert escape.stat().st_mode & 0o777 == original_mode


def test_darwin_launcher_rejects_parent_traversal_before_fake_uv(tmp_path: Path) -> None:
    fake_bin, log = _fake_uv(tmp_path)
    volume = tmp_path / "volume"
    volume.mkdir()
    escaped_root = volume / "codex/tmp/../../../escape"

    def disk_usage(_path: Path) -> SimpleNamespace:
        return SimpleNamespace(free=pytest_launcher.MIN_SYSTEM_DATA_FREE_BYTES)

    def mount_checker(path: Path) -> bool:
        return path == volume

    result = pytest_launcher.main(
        ["tests/example.py"],
        platform_name="darwin",
        darwin_volume=volume,
        darwin_shared_root=escaped_root,
        system_data_volume=tmp_path,
        disk_usage=disk_usage,
        mount_checker=mount_checker,
        environ=_launcher_environment(fake_bin, log),
    )

    assert result == pytest_launcher.REFUSAL_EXIT_CODE
    assert not log.exists()
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("exit_code", [0, 17])
def test_darwin_launcher_cleans_private_run_after_fake_uv_exit(
    tmp_path: Path,
    exit_code: int,
) -> None:
    fake_bin, log = _fake_uv(tmp_path)
    shared_root, options = _darwin_options(tmp_path)

    result = pytest_launcher.main(
        ["tests/example.py"],
        environ=_launcher_environment(fake_bin, log, exit_code=exit_code),
        **options,
    )

    assert result == exit_code
    lines = log.read_text(encoding="utf-8").splitlines()
    values = dict(line.split("=", 1) for line in lines[:3])
    temp = Path(values["TMPDIR"])
    assert values["TMP"] == str(temp)
    assert values["TEMP"] == str(temp)
    assert temp.name == "t"
    run_root = temp.parent
    assert run_root.parent == shared_root
    assert len(run_root.name) == _SHORT_RUN_NAME_LENGTH
    assert run_root.name.startswith("r")
    assert f"ARG=--basetemp={run_root / 'b'}" in lines
    assert not run_root.exists()
    assert not shared_root.exists() or not any(shared_root.iterdir())


def test_darwin_launcher_cleans_private_run_when_uv_is_missing(tmp_path: Path) -> None:
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    shared_root, options = _darwin_options(tmp_path)

    result = pytest_launcher.main(
        ["tests/example.py"],
        environ={**os.environ, "PATH": str(empty_bin)},
        **options,
    )

    assert result == pytest_launcher.REFUSAL_EXIT_CODE
    assert not shared_root.exists() or not any(shared_root.iterdir())


def test_darwin_launcher_cleans_private_run_when_uv_cannot_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bin, log = _fake_uv(tmp_path)
    shared_root, options = _darwin_options(tmp_path)

    def fail_spawn(*_args: object, **_kwargs: object) -> None:
        raise OSError("controlled spawn failure")

    monkeypatch.setattr(pytest_launcher.subprocess, "Popen", fail_spawn)
    result = pytest_launcher.main(
        ["tests/example.py"],
        environ=_launcher_environment(fake_bin, log),
        **options,
    )

    assert result == pytest_launcher.REFUSAL_EXIT_CODE
    assert not log.exists()
    assert not shared_root.exists() or not any(shared_root.iterdir())


def test_darwin_launcher_cleans_private_run_when_signalled_during_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_root, options = _darwin_options(tmp_path)
    original_create = pytest_launcher._create_short_private_run_root  # noqa: SLF001

    def signal_after_run_root_create(root: Path) -> Path:
        run_root = original_create(root)
        os.kill(os.getpid(), signal.SIGTERM)
        return run_root

    monkeypatch.setattr(
        pytest_launcher,
        "_create_short_private_run_root",
        signal_after_run_root_create,
    )
    result = pytest_launcher.main(
        ["tests/example.py"],
        environ=dict(os.environ),
        **options,
    )

    assert result == 128 + signal.SIGTERM
    assert not shared_root.exists() or not any(shared_root.iterdir())


def test_darwin_launcher_stops_child_when_signalled_during_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        running = True

        def poll(self) -> int | None:
            return None if self.running else 0

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            raise AssertionError("signal should be handled before waiting")

    fake_bin, log = _fake_uv(tmp_path)
    shared_root, options = _darwin_options(tmp_path)
    fake_process = FakeProcess()
    stopped_with: list[int] = []

    def signal_after_spawn(*_args: object, **_kwargs: object) -> FakeProcess:
        os.kill(os.getpid(), signal.SIGTERM)
        return fake_process

    def record_stop(process: FakeProcess, *, terminate_signal: int = signal.SIGTERM) -> None:
        assert process is fake_process
        process.running = False
        stopped_with.append(terminate_signal)

    monkeypatch.setattr(pytest_launcher.subprocess, "Popen", signal_after_spawn)
    monkeypatch.setattr(pytest_launcher, "_stop_process_group", record_stop)
    result = pytest_launcher.main(
        ["tests/example.py"],
        environ=_launcher_environment(fake_bin, log),
        **options,
    )

    assert result == 128 + signal.SIGTERM
    assert stopped_with == [signal.SIGTERM]
    assert not shared_root.exists() or not any(shared_root.iterdir())


def test_real_darwin_launcher_cleans_private_run_after_sigterm(tmp_path: Path) -> None:
    if os.uname().sysname != "Darwin":
        pytest.skip("real external-volume launcher boundary is macOS-only")
    fake_bin, log = _sleeping_fake_uv(tmp_path)
    process = subprocess.Popen(
        (_RUN_PYTEST, "tests/does-not-run.py"),
        cwd=_ROOT,
        env=_launcher_environment(fake_bin, log),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while not log.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert log.is_file()
    run_root = Path(log.read_text(encoding="utf-8").strip()).parent

    process.terminate()
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == 128 + signal.SIGTERM, stderr
    assert stdout == ""
    assert stderr == ""
    assert not run_root.exists()


@pytest.mark.parametrize("free_bytes", [pytest_launcher.MIN_SYSTEM_DATA_FREE_BYTES - 1, None])
def test_darwin_launcher_refuses_low_or_unknown_space_before_fake_uv(
    tmp_path: Path,
    free_bytes: int | None,
) -> None:
    fake_bin, log = _fake_uv(tmp_path)
    shared_root, options = _darwin_options(tmp_path)

    def disk_usage(_path: Path) -> SimpleNamespace:
        if free_bytes is None:
            raise OSError("unavailable")
        return SimpleNamespace(free=free_bytes)

    options["disk_usage"] = disk_usage
    result = pytest_launcher.main(
        ["tests/example.py"],
        environ=_launcher_environment(fake_bin, log),
        **options,
    )

    assert result == pytest_launcher.REFUSAL_EXIT_CODE
    assert not log.exists()
    assert not shared_root.exists()


def test_darwin_launcher_rejects_injected_child_symlink_without_following_it(
    tmp_path: Path,
) -> None:
    fake_bin, log = _fake_uv(tmp_path)
    shared_root, options = _darwin_options(tmp_path)
    shared_root.mkdir(parents=True, mode=0o700)
    escape = tmp_path / "escape"
    escape.mkdir(mode=0o755)
    original_mode = escape.stat().st_mode & 0o777
    (shared_root / "tmp").symlink_to(escape, target_is_directory=True)

    result = pytest_launcher.main(
        ["tests/example.py"],
        environ=_launcher_environment(fake_bin, log),
        **options,
    )

    assert result == pytest_launcher.REFUSAL_EXIT_CODE
    assert not log.exists()
    assert escape.stat().st_mode & 0o777 == original_mode


def test_linux_launcher_uses_portable_private_temp_without_darwin_checks(
    tmp_path: Path,
) -> None:
    fake_bin, log = _fake_uv(tmp_path)
    portable_parent = tmp_path / "portable"
    portable_parent.mkdir()

    def unexpected_check(_path: Path) -> object:
        raise AssertionError("Darwin-only check ran on Linux")

    result = pytest_launcher.main(
        ["tests/example.py"],
        platform_name="linux",
        portable_temp_parent=portable_parent,
        disk_usage=unexpected_check,
        mount_checker=unexpected_check,
        environ=_launcher_environment(fake_bin, log),
    )

    assert result == 0
    lines = log.read_text(encoding="utf-8").splitlines()
    temp = Path(lines[0].split("=", 1)[1])
    assert temp.is_relative_to(portable_parent)
    assert not temp.parent.exists()
