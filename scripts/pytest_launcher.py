from __future__ import annotations

import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Final, Protocol, cast

DARWIN_VOLUME: Final = Path("/Volumes/ssd_1")
DARWIN_SHARED_ROOT: Final = DARWIN_VOLUME / "codex/tmp/saxo-bank-mcp-analytics"
SYSTEM_DATA_VOLUME: Final = Path("/System/Volumes/Data")
MIN_SYSTEM_DATA_FREE_BYTES: Final = 50 * 1024**3
REFUSAL_EXIT_CODE: Final = 2
_TEMP_ENVIRONMENT_VARIABLES: Final = ("TMPDIR", "TMP", "TEMP")
_RUN_NAME_CHARACTERS: Final = "abcdefghijklmnopqrstuvwxyz0123456789"
_SHORT_TOKEN_LENGTH: Final = 3
_RUN_CREATE_ATTEMPTS: Final = 128
_PRIVATE_DIRECTORY_MODE: Final = 0o700


class _DiskUsage(Protocol):
    @property
    def free(self) -> int: ...


class LauncherRefusalError(RuntimeError):
    """Fail closed before pytest when launcher safety cannot be proven."""


@dataclass(frozen=True, slots=True)
class _RunLayout:
    run_root: Path
    temp: Path
    basetemp: Path
    shared_root: Path | None
    shared_identity: tuple[int, int] | None


class _ReceivedSignal(BaseException):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def main(  # noqa: PLR0913
    argv: Sequence[str] | None = None,
    *,
    platform_name: str = sys.platform,
    darwin_volume: Path = DARWIN_VOLUME,
    darwin_shared_root: Path = DARWIN_SHARED_ROOT,
    system_data_volume: Path = SYSTEM_DATA_VOLUME,
    portable_temp_parent: Path | None = None,
    disk_usage: Callable[[Path], _DiskUsage] = shutil.disk_usage,
    mount_checker: Callable[[Path], bool] = os.path.ismount,
    environ: Mapping[str, str] | None = None,
) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    environment = dict(os.environ if environ is None else environ)
    try:
        return _run_pytest(
            arguments,
            platform_name=platform_name,
            darwin_volume=darwin_volume,
            darwin_shared_root=darwin_shared_root,
            system_data_volume=system_data_volume,
            portable_temp_parent=portable_temp_parent,
            disk_usage=disk_usage,
            mount_checker=mount_checker,
            environment=environment,
        )
    except LauncherRefusalError as exc:
        sys.stderr.write(f"pytest launcher refused: {exc}\n")
        return REFUSAL_EXIT_CODE


def _run_pytest(  # noqa: C901, PLR0912, PLR0913, PLR0915
    arguments: tuple[str, ...],
    *,
    platform_name: str,
    darwin_volume: Path,
    darwin_shared_root: Path,
    system_data_volume: Path,
    portable_temp_parent: Path | None,
    disk_usage: Callable[[Path], _DiskUsage],
    mount_checker: Callable[[Path], bool],
    environment: dict[str, str],
) -> int:
    layout: _RunLayout | None = None
    process: subprocess.Popen[bytes] | None = None
    process_return_code: int | None = None
    caught_signal: int | None = None
    received_signal: int | None = None
    defer_signals = False
    previous_handlers: dict[int, object] = {}
    handled_signals = tuple(
        signum
        for name in ("SIGHUP", "SIGINT", "SIGTERM")
        if (signum := getattr(signal, name, None)) is not None
    )

    def forward_signal(signum: int, _frame: FrameType | None) -> None:
        nonlocal received_signal
        received_signal = signum
        if defer_signals:
            return
        raise _ReceivedSignal(signum)

    try:
        for signum in handled_signals:
            previous_handlers[signum] = signal.signal(signum, forward_signal)

        if platform_name == "darwin":
            _require_darwin_preflight(
                volume=darwin_volume,
                shared_root=darwin_shared_root,
                system_data_volume=system_data_volume,
                disk_usage=disk_usage,
                mount_checker=mount_checker,
            )
            defer_signals = True
            try:
                layout = _create_darwin_layout(darwin_volume, darwin_shared_root)
            finally:
                defer_signals = False
                if received_signal is not None:
                    raise _ReceivedSignal(received_signal)
        else:
            defer_signals = True
            try:
                layout = _create_portable_layout(portable_temp_parent)
            finally:
                defer_signals = False
                if received_signal is not None:
                    raise _ReceivedSignal(received_signal)

        uv = shutil.which("uv", path=environment.get("PATH"))
        if uv is None:
            raise LauncherRefusalError("uv executable is unavailable")
        for name in _TEMP_ENVIRONMENT_VARIABLES:
            environment[name] = os.fspath(layout.temp)
        command = (
            uv,
            "run",
            "pytest",
            *arguments,
            f"--basetemp={layout.basetemp}",
        )
        defer_signals = True
        try:
            process = subprocess.Popen(
                command,
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                start_new_session=True,
            )
        except OSError as exc:
            raise LauncherRefusalError(f"uv could not start: {type(exc).__name__}") from exc
        finally:
            defer_signals = False
            if received_signal is not None:
                raise _ReceivedSignal(received_signal)
        process_return_code = process.wait()
    except _ReceivedSignal as received:
        caught_signal = received.signum
    finally:
        defer_signals = True
        try:
            if process is not None and process.poll() is None:
                _stop_process_group(
                    process,
                    terminate_signal=caught_signal or received_signal or signal.SIGTERM,
                )
            if layout is not None:
                _cleanup_layout(layout)
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(
                    signum,
                    cast("signal.Handlers | Callable[[int, FrameType | None], object]", handler),
                )
            defer_signals = False

    if caught_signal is not None:
        return 128 + caught_signal
    if received_signal is not None:
        return 128 + received_signal
    if process_return_code is None:
        raise LauncherRefusalError("pytest process ended without a return code")
    return process_return_code


def _require_darwin_preflight(
    *,
    volume: Path,
    shared_root: Path,
    system_data_volume: Path,
    disk_usage: Callable[[Path], _DiskUsage],
    mount_checker: Callable[[Path], bool],
) -> None:
    if not volume.is_absolute() or not shared_root.is_absolute():
        raise LauncherRefusalError("Darwin temp paths must be absolute")
    _require_existing_directory_without_symlinks(volume, label="external volume")
    if not mount_checker(volume):
        raise LauncherRefusalError(f"external temp volume is not mounted at {volume}")
    if not _is_lexically_beneath(shared_root, volume):
        raise LauncherRefusalError("shared temp root escapes the external volume")
    try:
        free_bytes = disk_usage(system_data_volume).free
    except (OSError, ValueError):
        raise LauncherRefusalError(
            "system Data volume free space could not be measured",
        ) from None
    if free_bytes < MIN_SYSTEM_DATA_FREE_BYTES:
        raise LauncherRefusalError("system Data volume has less than 50 GiB free")


def _create_darwin_layout(volume: Path, shared_root: Path) -> _RunLayout:
    _create_directory_chain(volume, shared_root.parent)
    shared_created = _ensure_private_shared_root(shared_root)
    layout: _RunLayout | None = None
    try:
        _reject_symlink_children(shared_root)
        shared_stat = os.lstat(shared_root)
        run_root = _create_short_private_run_root(shared_root)
        temp = run_root / "t"
        layout = _RunLayout(
            run_root=run_root,
            temp=temp,
            basetemp=run_root / "b",
            shared_root=shared_root,
            shared_identity=(shared_stat.st_dev, shared_stat.st_ino),
        )
        temp.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
        return layout  # noqa: TRY300
    except BaseException:
        if layout is not None:
            _cleanup_layout(layout)
        elif shared_created:
            with suppress(OSError):
                shared_root.rmdir()
        raise


def _create_portable_layout(portable_temp_parent: Path | None) -> _RunLayout:
    if portable_temp_parent is None:
        parent = Path(tempfile.gettempdir()).resolve(strict=True)
    else:
        parent = portable_temp_parent
        _require_existing_directory_without_symlinks(parent, label="portable temp parent")
    run_root = Path(tempfile.mkdtemp(prefix="saxo-pytest-", dir=parent))
    run_root.chmod(_PRIVATE_DIRECTORY_MODE)
    temp = run_root / "t"
    layout = _RunLayout(
        run_root=run_root,
        temp=temp,
        basetemp=run_root / "b",
        shared_root=None,
        shared_identity=None,
    )
    try:
        temp.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    except BaseException:
        _cleanup_layout(layout)
        raise
    return layout


def _create_directory_chain(existing_root: Path, target: Path) -> None:
    try:
        relative_parts = target.relative_to(existing_root).parts
    except ValueError:
        raise LauncherRefusalError("shared temp parent escapes the external volume") from None
    current = existing_root
    for part in relative_parts:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            try:
                current.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
            except FileExistsError:
                metadata = os.lstat(current)
            else:
                metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode):
            raise LauncherRefusalError(f"temp path component is a symlink: {current}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise LauncherRefusalError(f"temp path component is not a directory: {current}")


def _ensure_private_shared_root(shared_root: Path) -> bool:
    created = False
    try:
        metadata = os.lstat(shared_root)
    except FileNotFoundError:
        try:
            shared_root.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
            created = True
        except FileExistsError:
            pass
        metadata = os.lstat(shared_root)
    if stat.S_ISLNK(metadata.st_mode):
        raise LauncherRefusalError(f"shared temp root is a symlink: {shared_root}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise LauncherRefusalError(f"shared temp root is not a directory: {shared_root}")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != _PRIVATE_DIRECTORY_MODE:
        raise LauncherRefusalError("shared temp root is not private and owner-controlled")
    return created


def _reject_symlink_children(shared_root: Path) -> None:
    try:
        entries = tuple(os.scandir(shared_root))
    except OSError as exc:
        raise LauncherRefusalError("shared temp root cannot be inspected") from exc
    for entry in entries:
        if entry.is_symlink():
            raise LauncherRefusalError(f"shared temp child is a symlink: {entry.name}")


def _create_short_private_run_root(shared_root: Path) -> Path:
    for _ in range(_RUN_CREATE_ATTEMPTS):
        token = "".join(secrets.choice(_RUN_NAME_CHARACTERS) for _ in range(_SHORT_TOKEN_LENGTH))
        candidate = shared_root / f"r{token}"
        try:
            candidate.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
        except FileExistsError:
            continue
        metadata = os.lstat(candidate)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != _PRIVATE_DIRECTORY_MODE
        ):
            raise LauncherRefusalError("private pytest run root could not be proven")
        return candidate
    raise LauncherRefusalError("private pytest run root could not be allocated")


def _cleanup_layout(layout: _RunLayout) -> None:
    run_root = layout.run_root
    try:
        metadata = os.lstat(run_root)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise LauncherRefusalError("pytest run cleanup root is not a real directory")
    if metadata.st_uid != os.getuid():
        raise LauncherRefusalError("pytest run cleanup root ownership changed")
    if layout.shared_root is not None:
        shared_root = layout.shared_root
        try:
            shared_metadata = os.lstat(shared_root)
        except FileNotFoundError:
            raise LauncherRefusalError("shared temp root disappeared before cleanup") from None
        if (
            stat.S_ISLNK(shared_metadata.st_mode)
            or not stat.S_ISDIR(shared_metadata.st_mode)
            or (shared_metadata.st_dev, shared_metadata.st_ino) != layout.shared_identity
            or run_root.parent != shared_root
        ):
            raise LauncherRefusalError("pytest run cleanup containment changed")
    _make_directories_owner_writable(run_root)
    shutil.rmtree(run_root)
    if run_root.exists() or run_root.is_symlink():
        raise LauncherRefusalError("pytest run cleanup left residue")
    if layout.shared_root is not None:
        with suppress(OSError):
            layout.shared_root.rmdir()


def _make_directories_owner_writable(run_root: Path) -> None:
    for directory, child_directories, _files in os.walk(run_root, topdown=True, followlinks=False):
        current = Path(directory)
        _make_one_directory_owner_writable(current)
        for name in child_directories:
            child = current / name
            metadata = os.lstat(child)
            if stat.S_ISLNK(metadata.st_mode):
                continue
            _make_one_directory_owner_writable(child)


def _make_one_directory_owner_writable(directory: Path) -> None:
    metadata = os.lstat(directory)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise LauncherRefusalError("pytest cleanup encountered an unsafe directory")
    if metadata.st_uid != os.getuid():
        raise LauncherRefusalError("pytest cleanup encountered foreign ownership")
    directory.chmod(
        stat.S_IMODE(metadata.st_mode) | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
    )


def _require_existing_directory_without_symlinks(path: Path, *, label: str) -> None:
    if not path.is_absolute():
        raise LauncherRefusalError(f"{label} must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            raise LauncherRefusalError(f"{label} is unavailable: {path}") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise LauncherRefusalError(f"{label} contains a symlink: {current}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise LauncherRefusalError(f"{label} is not a directory: {current}")


def _is_lexically_beneath(path: Path, root: Path) -> bool:
    if os.pardir in path.parts or os.pardir in root.parts:
        return False
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


def _stop_process_group(
    process: subprocess.Popen[bytes],
    *,
    terminate_signal: int = signal.SIGTERM,
) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, terminate_signal)
    except ProcessLookupError:
        pass
    except PermissionError:
        if process.poll() is None:
            process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError):
            if process.poll() is None:
                process.kill()
        process.wait(timeout=2)
