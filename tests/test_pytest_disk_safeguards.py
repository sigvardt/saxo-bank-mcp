from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from analytics_source_matrix_support import REPOSITORY_COPY_IGNORE
from pytest_disk_guard import (
    MIN_SYSTEM_DATA_FREE_BYTES,
    PytestDiskGuardSnapshot,
    pytest_disk_guard_errors,
)

_EXTERNAL_TEMP_ROOT = Path("/Volumes/ssd_1/codex/tmp/saxo-bank-mcp-analytics")


def _snapshot() -> PytestDiskGuardSnapshot:
    run_root = _EXTERNAL_TEMP_ROOT / "rabc"
    external_tmp = run_root / "t"
    return PytestDiskGuardSnapshot(
        platform="darwin",
        effective_temp=external_tmp,
        basetemp=run_root / "b",
        temp_environment=(("TMPDIR", external_tmp), ("TMP", external_tmp), ("TEMP", external_tmp)),
        data_volume_free_bytes=MIN_SYSTEM_DATA_FREE_BYTES,
    )


def test_macos_guard_accepts_external_temp_and_minimum_free_space() -> None:
    assert pytest_disk_guard_errors(_snapshot()) == ()


def test_macos_guard_refuses_direct_pytest_temp_outside_external_volume() -> None:
    errors = pytest_disk_guard_errors(
        replace(
            _snapshot(),
            effective_temp=Path("/private/var/folders/pytest"),
            basetemp=Path("/private/var/folders/pytest/base"),
            temp_environment=(
                ("TMPDIR", Path("/private/var/folders/pytest")),
                ("TMP", Path("/private/var/folders/pytest")),
                ("TEMP", Path("/private/var/folders/pytest")),
            ),
        ),
    )

    assert any("effective temporary path" in error for error in errors)
    assert any("pytest basetemp" in error for error in errors)
    assert {
        name for name in ("TMPDIR", "TMP", "TEMP") if any(name in error for error in errors)
    } == {
        "TMPDIR",
        "TMP",
        "TEMP",
    }


def test_macos_guard_refuses_low_or_unknown_system_data_free_space() -> None:
    low_errors = pytest_disk_guard_errors(
        replace(_snapshot(), data_volume_free_bytes=MIN_SYSTEM_DATA_FREE_BYTES - 1),
    )
    unknown_errors = pytest_disk_guard_errors(replace(_snapshot(), data_volume_free_bytes=None))

    assert low_errors == ("system Data volume has less than 50 GiB free",)
    assert unknown_errors == ("system Data volume free space could not be measured",)


def test_macos_guard_refuses_external_paths_without_one_private_run_root() -> None:
    errors = pytest_disk_guard_errors(
        replace(
            _snapshot(),
            effective_temp=_EXTERNAL_TEMP_ROOT / "first/t",
            basetemp=_EXTERNAL_TEMP_ROOT / "second/b",
            temp_environment=(
                ("TMPDIR", _EXTERNAL_TEMP_ROOT / "first/t"),
                ("TMP", _EXTERNAL_TEMP_ROOT / "first/t"),
                ("TEMP", _EXTERNAL_TEMP_ROOT / "first/t"),
            ),
        ),
    )

    assert errors == ("pytest temporary paths do not share one private run root",)


def test_linux_guard_has_an_explicit_noop_platform_boundary() -> None:
    unsafe_paths = replace(
        _snapshot(),
        platform="linux",
        effective_temp=Path("/linux-ci-work"),
        basetemp=None,
        temp_environment=(("TMPDIR", None), ("TMP", None), ("TEMP", None)),
        data_volume_free_bytes=None,
    )

    assert pytest_disk_guard_errors(unsafe_paths) == ()


def test_test_subprocess_inherits_external_temp_environment() -> None:
    expected = {name: os.environ[name] for name in ("TMPDIR", "TMP", "TEMP")}
    result = subprocess.run(
        (
            sys.executable,
            "-c",
            "import json,os; print(json.dumps({name: os.environ[name] "
            "for name in ('TMPDIR', 'TMP', 'TEMP')}, sort_keys=True))",
        ),
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == expected
    assert all(
        Path(value).resolve(strict=False).is_relative_to(_EXTERNAL_TEMP_ROOT)
        for value in expected.values()
    )


def test_repository_copy_excludes_runtime_build_cache_and_evidence_trees(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    excluded = (
        ".superpowers/sdd/prepared-runtime/bin/python",
        ".git/objects/object",
        ".venv/bin/python",
        ".pytest_cache/v/cache/nodeids",
        ".hypothesis/examples/example",
        ".ruff_cache/index",
        ".mypy_cache/index",
        ".basedpyright/index",
        "__pycache__/module.cpython-312.pyc",
        "build/lib/module.py",
        "dist/saxo_bank_mcp-0.1.0-py3-none-any.whl",
        "node_modules/package/index.js",
        ".omo/evidence/run/result.json",
        "evidence/run/result.json",
        "bootstrap-runtime/bin/python",
        "final-runtime-a/bin/python",
        "bootstrap-wheel/saxo_bank_mcp-0.1.0-py3-none-any.whl",
    )
    for relative in excluded:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated", encoding="utf-8")
    allowed = source / "src/saxo_bank_mcp/module.py"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("VALUE = 1\n", encoding="utf-8")

    shutil.copytree(source, target, ignore=REPOSITORY_COPY_IGNORE)

    assert (target / allowed.relative_to(source)).read_text(encoding="utf-8") == "VALUE = 1\n"
    assert all(not (target / relative).exists() for relative in excluded)
