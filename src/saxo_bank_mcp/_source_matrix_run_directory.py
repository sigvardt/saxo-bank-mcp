from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

_OWNER_DIRECTORY_MODE: Final = 0o700
_READ_FLAGS: Final = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
_DIRECTORY_FLAGS: Final = _READ_FLAGS | getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW_FLAGS: Final = getattr(os, "O_NOFOLLOW", 0)


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


def open_held_run_directory(root: Path) -> HeldRunDirectory:
    if not root.is_absolute():
        raise RunDirectoryError("run directory path is invalid")
    _validate_name(root.name)
    try:
        parent_descriptor = os.open(
            root.parent,
            _DIRECTORY_FLAGS | _NOFOLLOW_FLAGS,
        )
    except OSError as error:
        raise RunDirectoryError("run directory parent is unavailable") from error
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


def _cleanup_entries(  # noqa: C901
    held: HeldRunDirectory,
    remove_names: tuple[str, ...],
    remove_root: bool,  # noqa: FBT001
    reopened: list[int],
) -> None:
    if held.closed or len(set(remove_names)) != len(remove_names):
        raise RunDirectoryError("run directory handle is invalid")
    if any(name not in held.child_descriptors for name in remove_names):
        raise RunDirectoryError("run directory residue is untracked")
    current_root = _open_directory(held.root_name, held.parent_descriptor)
    reopened.append(current_root)
    if not _same_directory(current_root, held.root_descriptor):
        raise RunDirectoryError("run directory root changed")
    if set(os.listdir(held.root_descriptor)) != set(  # noqa: PTH208
        held.child_descriptors,
    ):
        raise RunDirectoryError("run directory entries changed")
    for name, descriptor in held.child_descriptors.items():
        if os.listdir(descriptor):  # noqa: PTH208
            raise RunDirectoryError("run directory is not empty")
        current = _open_directory(name, held.root_descriptor)
        reopened.append(current)
        if not _same_directory(current, descriptor):
            raise RunDirectoryError("run directory child changed")
    for name in reversed(remove_names):
        os.rmdir(name, dir_fd=held.root_descriptor)
    expected_residue = set(held.child_descriptors).difference(remove_names)
    if set(os.listdir(held.root_descriptor)) != expected_residue:  # noqa: PTH208
        raise RunDirectoryError("run directory cleanup changed")
    if remove_root:
        if expected_residue:
            raise RunDirectoryError("run directory root is not empty")
        current_root = _open_directory(held.root_name, held.parent_descriptor)
        reopened.append(current_root)
        if not _same_directory(current_root, held.root_descriptor):
            raise RunDirectoryError("run directory root changed")
        os.rmdir(held.root_name, dir_fd=held.parent_descriptor)


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
    "hold_existing_run_directories",
    "open_held_run_directory",
]
