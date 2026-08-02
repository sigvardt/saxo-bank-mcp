from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

EXTERNAL_VOLUME_ROOT: Final = Path("/Volumes/ssd_1")
SYSTEM_DATA_VOLUME: Final = Path("/System/Volumes/Data")
MIN_SYSTEM_DATA_FREE_BYTES: Final = 50 * 1024**3
TEMP_ENVIRONMENT_VARIABLES: Final = ("TMPDIR", "TMP", "TEMP")


@dataclass(frozen=True, slots=True)
class PytestDiskGuardSnapshot:
    platform: str
    effective_temp: Path
    basetemp: Path | None
    temp_environment: tuple[tuple[str, Path | None], ...]
    data_volume_free_bytes: int | None


def current_pytest_disk_guard_snapshot(basetemp: Path | None) -> PytestDiskGuardSnapshot:
    free_bytes: int | None = None
    if sys.platform == "darwin":
        try:
            free_bytes = shutil.disk_usage(SYSTEM_DATA_VOLUME).free
        except OSError:
            free_bytes = None
    return PytestDiskGuardSnapshot(
        platform=sys.platform,
        effective_temp=Path(tempfile.gettempdir()),
        basetemp=basetemp,
        temp_environment=tuple(
            (name, _optional_path(os.environ.get(name))) for name in TEMP_ENVIRONMENT_VARIABLES
        ),
        data_volume_free_bytes=free_bytes,
    )


def pytest_disk_guard_errors(snapshot: PytestDiskGuardSnapshot) -> tuple[str, ...]:
    if snapshot.platform != "darwin":
        return ()

    errors: list[str] = []
    if not _is_beneath_external_volume(snapshot.effective_temp):
        errors.append(
            "effective temporary path resolves outside /Volumes/ssd_1: "
            f"{snapshot.effective_temp.resolve(strict=False)}",
        )
    if snapshot.basetemp is None:
        errors.append("pytest basetemp is unset")
    elif not _is_beneath_external_volume(snapshot.basetemp):
        errors.append(
            "pytest basetemp resolves outside /Volumes/ssd_1: "
            f"{snapshot.basetemp.resolve(strict=False)}",
        )
    for name, path in snapshot.temp_environment:
        if path is None:
            errors.append(f"{name} is unset")
        elif not _is_beneath_external_volume(path):
            errors.append(
                f"{name} resolves outside /Volumes/ssd_1: {path.resolve(strict=False)}",
            )
    if snapshot.data_volume_free_bytes is None:
        errors.append("system Data volume free space could not be measured")
    elif snapshot.data_volume_free_bytes < MIN_SYSTEM_DATA_FREE_BYTES:
        errors.append("system Data volume has less than 50 GiB free")
    return tuple(errors)


def _optional_path(value: str | None) -> Path | None:
    return None if value is None else Path(value)


def _is_beneath_external_volume(path: Path) -> bool:
    resolved_root = EXTERNAL_VOLUME_ROOT.resolve(strict=False)
    resolved_path = path.expanduser().resolve(strict=False)
    return resolved_path == resolved_root or resolved_path.is_relative_to(resolved_root)
