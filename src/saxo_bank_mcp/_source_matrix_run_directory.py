from __future__ import annotations

import ctypes
import os
import secrets
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, cast

_OWNER_DIRECTORY_MODE: Final = 0o700
_READ_FLAGS: Final = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
_DIRECTORY_FLAGS: Final = _READ_FLAGS | getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW_FLAGS: Final = getattr(os, "O_NOFOLLOW", 0)
_REMOVAL_NAME_PREFIX: Final = ".saxo-source-matrix-remove-"
_LINUX_RENAME_NOREPLACE: Final = 1
_DARWIN_RENAME_EXCL: Final = 4


class RunDirectoryError(RuntimeError):
    pass


@dataclass(slots=True)
class HeldRunDirectory:
    root: Path = field(repr=False)
    root_name: str
    parent_descriptor: int = field(repr=False)
    root_descriptor: int = field(repr=False)
    child_descriptors: dict[str, int] = field(default_factory=dict, repr=False)
    closed: bool = False


def _validate_name(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name:
        raise RunDirectoryError("run directory name is invalid")


def _validate_directory(descriptor: int) -> None:
    try:
        metadata = os.fstat(descriptor)
    except OSError as error:
        raise RunDirectoryError("run directory descriptor is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != _OWNER_DIRECTORY_MODE
    ):
        raise RunDirectoryError("run directory metadata is invalid")


def _same_directory(left: int, right: int) -> bool:
    try:
        first = os.fstat(left)
        second = os.fstat(right)
    except OSError as error:
        raise RunDirectoryError("run directory descriptor changed") from error
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _open_directory(name: str, parent_descriptor: int) -> int:
    try:
        descriptor = os.open(
            name,
            _DIRECTORY_FLAGS | _NOFOLLOW_FLAGS,
            dir_fd=parent_descriptor,
        )
    except OSError as error:
        raise RunDirectoryError("run directory path changed") from error
    try:
        _validate_directory(descriptor)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_parent_descriptor(root: Path) -> int:
    try:
        return os.open(
            root.parent,
            _DIRECTORY_FLAGS | _NOFOLLOW_FLAGS,
        )
    except OSError as error:
        raise RunDirectoryError("run directory parent is unavailable") from error


def open_held_run_directory(root: Path) -> HeldRunDirectory:
    if not root.is_absolute():
        raise RunDirectoryError("run directory path is invalid")
    _validate_name(root.name)
    parent_descriptor = _open_parent_descriptor(root)
    try:
        root_descriptor = _open_directory(root.name, parent_descriptor)
    except Exception:
        os.close(parent_descriptor)
        raise
    return HeldRunDirectory(
        root=root,
        root_name=root.name,
        parent_descriptor=parent_descriptor,
        root_descriptor=root_descriptor,
    )


def create_held_run_directory(root: Path) -> HeldRunDirectory:
    if not root.is_absolute():
        raise RunDirectoryError("run directory path is invalid")
    _validate_name(root.name)
    parent_descriptor = _open_parent_descriptor(root)
    try:
        os.mkdir(root.name, _OWNER_DIRECTORY_MODE, dir_fd=parent_descriptor)
        root_descriptor = _open_directory(root.name, parent_descriptor)
    except OSError as error:
        os.close(parent_descriptor)
        raise RunDirectoryError("run directory creation failed") from error
    except Exception:
        os.close(parent_descriptor)
        raise
    return HeldRunDirectory(
        root=root,
        root_name=root.name,
        parent_descriptor=parent_descriptor,
        root_descriptor=root_descriptor,
    )


def hold_existing_run_directories(
    held: HeldRunDirectory,
    names: tuple[str, ...],
) -> None:
    if held.closed or len(set(names)) != len(names):
        raise RunDirectoryError("run directory handle is invalid")
    for name in names:
        _validate_name(name)
    if set(os.listdir(held.root_descriptor)) != set(names):  # noqa: PTH208
        raise RunDirectoryError("run directory entries are invalid")
    for name in names:
        descriptor = _open_directory(name, held.root_descriptor)
        if os.listdir(descriptor):  # noqa: PTH208
            os.close(descriptor)
            raise RunDirectoryError("run directory is not empty")
        held.child_descriptors[name] = descriptor


def create_held_run_directories(
    held: HeldRunDirectory,
    names: tuple[str, ...],
) -> None:
    if held.closed or len(set(names)) != len(names):
        raise RunDirectoryError("run directory handle is invalid")
    for name in names:
        _validate_name(name)
        if name in held.child_descriptors:
            raise RunDirectoryError("run directory already exists")
        try:
            os.mkdir(name, _OWNER_DIRECTORY_MODE, dir_fd=held.root_descriptor)
        except OSError as error:
            raise RunDirectoryError("run directory creation failed") from error
        held.child_descriptors[name] = _open_directory(name, held.root_descriptor)


def close_held_run_directory(held: HeldRunDirectory) -> None:
    if held.closed:
        return
    held.closed = True
    for descriptor in reversed(tuple(held.child_descriptors.values())):
        try:
            os.close(descriptor)
        except OSError:
            continue
    for descriptor in (held.root_descriptor, held.parent_descriptor):
        try:
            os.close(descriptor)
        except OSError:
            continue


def _rename_no_replace(
    name: str,
    isolated_name: str,
    parent_descriptor: int,
) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    function_name: str
    flag: int
    if sys.platform == "darwin":
        function_name = "renameatx_np"
        flag = _DARWIN_RENAME_EXCL
    elif sys.platform.startswith("linux"):
        function_name = "renameat2"
        flag = _LINUX_RENAME_NOREPLACE
    else:
        raise RunDirectoryError("atomic run directory isolation is unavailable")
    try:
        rename = cast(
            "Callable[[int, bytes, int, bytes, int], int]",
            getattr(library, function_name),
        )
    except AttributeError as error:
        raise RunDirectoryError("atomic run directory isolation is unavailable") from error
    ctypes.set_errno(0)
    result = rename(
        parent_descriptor,
        os.fsencode(name),
        parent_descriptor,
        os.fsencode(isolated_name),
        flag,
    )
    if result != 0:
        raise RunDirectoryError("atomic run directory isolation failed")


def _entry_exists(name: str, parent_descriptor: int) -> bool:
    try:
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise RunDirectoryError("run directory entry is unavailable") from error
    return True


def _isolate_verified_directory(
    name: str,
    parent_descriptor: int,
    expected_descriptor: int,
    reopened: list[int],
) -> str:
    isolated_name = f"{_REMOVAL_NAME_PREFIX}{secrets.token_hex(16)}"
    _rename_no_replace(name, isolated_name, parent_descriptor)
    current = _open_directory(isolated_name, parent_descriptor)
    reopened.append(current)
    if not _same_directory(current, expected_descriptor):
        raise RunDirectoryError("run directory entry changed")
    return isolated_name


def _verify_named_directory(
    name: str,
    parent_descriptor: int,
    expected_descriptor: int,
    reopened: list[int],
) -> None:
    current = _open_directory(name, parent_descriptor)
    reopened.append(current)
    if not _same_directory(current, expected_descriptor):
        raise RunDirectoryError("run directory entry changed")


def _cleanup_entries(  # noqa: C901, PLR0912
    held: HeldRunDirectory,
    remove_names: tuple[str, ...],
    remove_root: bool,  # noqa: FBT001
    reopened: list[int],
) -> None:
    if held.closed or len(set(remove_names)) != len(remove_names):
        raise RunDirectoryError("run directory handle is invalid")
    if any(name not in held.child_descriptors for name in remove_names):
        raise RunDirectoryError("run directory residue is untracked")
    isolated_root_name: str | None = None
    if remove_root:
        isolated_root_name = _isolate_verified_directory(
            held.root_name,
            held.parent_descriptor,
            held.root_descriptor,
            reopened,
        )
    else:
        _verify_named_directory(
            held.root_name,
            held.parent_descriptor,
            held.root_descriptor,
            reopened,
        )
    if set(os.listdir(held.root_descriptor)) != set(  # noqa: PTH208
        held.child_descriptors,
    ):
        raise RunDirectoryError("run directory entries changed")
    remove_set = set(remove_names)
    for name, descriptor in held.child_descriptors.items():
        if os.listdir(descriptor):  # noqa: PTH208
            raise RunDirectoryError("run directory is not empty")
        if name not in remove_set:
            _verify_named_directory(
                name,
                held.root_descriptor,
                descriptor,
                reopened,
            )
    for name in reversed(remove_names):
        isolated_name = _isolate_verified_directory(
            name,
            held.root_descriptor,
            held.child_descriptors[name],
            reopened,
        )
        if _entry_exists(name, held.root_descriptor):
            raise RunDirectoryError("run directory child changed")
        os.rmdir(isolated_name, dir_fd=held.root_descriptor)
        if _entry_exists(name, held.root_descriptor):
            raise RunDirectoryError("run directory child changed")
    expected_residue = set(held.child_descriptors).difference(remove_set)
    if set(os.listdir(held.root_descriptor)) != expected_residue:  # noqa: PTH208
        raise RunDirectoryError("run directory cleanup changed")
    for name in expected_residue:
        _verify_named_directory(
            name,
            held.root_descriptor,
            held.child_descriptors[name],
            reopened,
        )
    if remove_root:
        if expected_residue:
            raise RunDirectoryError("run directory root is not empty")
        if isolated_root_name is None or _entry_exists(
            held.root_name,
            held.parent_descriptor,
        ):
            raise RunDirectoryError("run directory root changed")
        os.rmdir(isolated_root_name, dir_fd=held.parent_descriptor)
        if _entry_exists(held.root_name, held.parent_descriptor):
            raise RunDirectoryError("run directory root changed")
    else:
        _verify_named_directory(
            held.root_name,
            held.parent_descriptor,
            held.root_descriptor,
            reopened,
        )


def cleanup_held_run_directory(
    held: HeldRunDirectory,
    *,
    remove_names: tuple[str, ...],
    remove_root: bool,
) -> None:
    cleanup_error: Exception | None = None
    reopened: list[int] = []
    try:
        _cleanup_entries(held, remove_names, remove_root, reopened)
    except Exception as error:  # noqa: BLE001
        cleanup_error = error
    finally:
        for descriptor in reversed(reopened):
            try:
                os.close(descriptor)
            except OSError:
                continue
        close_held_run_directory(held)
    if cleanup_error is not None:
        if isinstance(cleanup_error, RunDirectoryError):
            raise cleanup_error
        raise RunDirectoryError("run directory cleanup failed") from cleanup_error


__all__ = [
    "HeldRunDirectory",
    "RunDirectoryError",
    "cleanup_held_run_directory",
    "close_held_run_directory",
    "create_held_run_directories",
    "create_held_run_directory",
    "hold_existing_run_directories",
    "open_held_run_directory",
]
