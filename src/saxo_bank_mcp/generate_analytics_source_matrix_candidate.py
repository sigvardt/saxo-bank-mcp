from __future__ import annotations

import argparse
import configparser
import hashlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path

from saxo_bank_mcp.analytics_source_contracts import source_contract_catalog_sha256
from saxo_bank_mcp.qa_analytics_source_matrix import (
    _dependency_distributions,  # pyright: ignore[reportPrivateUsage]
    _normalize_official_import_machinery,  # pyright: ignore[reportPrivateUsage]
    _runtime_identity,  # pyright: ignore[reportPrivateUsage]
    _source_candidate_files,  # pyright: ignore[reportPrivateUsage]
    _validate_official_interpreter_state,  # pyright: ignore[reportPrivateUsage]
)

_CANDIDATE_RESOURCE = "saxo_bank_mcp/_analytics_source_matrix/source_matrix_candidate.json"
_SOURCE_EXCLUSION = "data/analytics/source_matrix_candidate.json"
_EXPECTED_INSTALLER = "uv"
_SHARED_SCRIPT_PREFIX = "saxo_bank_mcp-0.1.0.data/scripts/"
_SHARED_SCRIPTS = {
    "saxo-bank-analytics-source-matrix",
    "saxo-bank-analytics-source-matrix-generate",
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()


def _source_path_for_wheel_path(name: str) -> str | None:
    resource_paths = {
        "saxo_bank_mcp/_analytics_source_contracts/source_contracts.json": (
            "data/analytics/source_contracts.json"
        ),
        "saxo_bank_mcp/_endpoint_registry/openapi_inventory.json": (
            "data/saxo/openapi_inventory.json"
        ),
    }
    if name in resource_paths:
        return resource_paths[name]
    if name.startswith(_SHARED_SCRIPT_PREFIX):
        script_name = name.removeprefix(_SHARED_SCRIPT_PREFIX)
        if script_name in _SHARED_SCRIPTS:
            return f"scripts/{script_name}"
    migration_prefix = "saxo_bank_mcp/_analytics_migrations/"
    if name.startswith(migration_prefix):
        return "data/analytics/migrations/" + name.removeprefix(migration_prefix)
    package_prefix = "saxo_bank_mcp/"
    if (
        name.startswith(package_prefix)
        and not name.startswith(
            "saxo_bank_mcp/_analytics_",
        )
        and not name.startswith("saxo_bank_mcp/_endpoint_registry/")
    ):
        return "src/" + name
    return None


def _installed_files(
    wheel: Path,
    source_files: dict[str, str],
) -> tuple[dict[str, str], tuple[str, ...], dict[str, str], str]:
    with zipfile.ZipFile(wheel) as archive:
        names = tuple(
            sorted(info.filename for info in archive.infolist() if not info.is_dir()),
        )
        records = tuple(name for name in names if name.endswith(".dist-info/RECORD"))
        if len(records) != 1 or _CANDIDATE_RESOURCE not in names:
            raise ValueError("wheel does not have the expected candidate and RECORD")
        exclusions = (_CANDIDATE_RESOURCE, records[0])
        installed: dict[str, str] = {}
        for name in names:
            if name in exclusions:
                continue
            installed_name = name
            if name.startswith(_SHARED_SCRIPT_PREFIX):
                installed_name = f"../../../bin/{name.removeprefix(_SHARED_SCRIPT_PREFIX)}"
            installed[installed_name] = hashlib.sha256(archive.read(name)).hexdigest()
        projected: dict[str, str] = {}
        for name in names:
            if name in exclusions or ".dist-info/" in name:
                continue
            source_name = _source_path_for_wheel_path(name)
            installed_name = (
                f"../../../bin/{name.removeprefix(_SHARED_SCRIPT_PREFIX)}"
                if name.startswith(_SHARED_SCRIPT_PREFIX)
                else name
            )
            if source_name is None or source_files.get(source_name) != installed.get(
                installed_name,
            ):
                raise ValueError("wheel does not match the source execution projection")
            projected[source_name] = name
        expected_projected = {
            name
            for name in source_files
            if name.startswith(
                (
                    "src/saxo_bank_mcp/",
                    "data/analytics/migrations/",
                    "scripts/saxo-bank-analytics-source-matrix",
                ),
            )
            or name
            in {
                "data/analytics/source_contracts.json",
                "data/saxo/openapi_inventory.json",
            }
        }
        if set(projected) != expected_projected:
            raise ValueError("wheel source projection is incomplete")
        entry_points_name = next(
            (name for name in names if name.endswith(".dist-info/entry_points.txt")),
            None,
        )
        if entry_points_name is None:
            raise ValueError("wheel console entry point projection is unavailable")
        parser = configparser.ConfigParser()
        parser.read_file(
            io.StringIO(archive.read(entry_points_name).decode("utf-8")),
        )
        console_scripts = dict(parser.items("console_scripts"))
    return installed, exclusions, console_scripts, _digest(projected)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seal the complete analytics source-matrix candidate closure.",
    )
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--repository-root", required=True, type=Path)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/analytics/source_matrix_candidate.json"),
    )
    arguments = parser.parse_args()
    launcher_mode = os.environ.get("SAXO_BANK_MCP_OFFICIAL_ISOLATED_LAUNCHER")
    if launcher_mode == "1":
        _normalize_official_import_machinery()
        _validate_official_interpreter_state()
    elif launcher_mode != "dev" or (
        sys.flags.isolated != 1
        or sys.flags.dont_write_bytecode != 1
        or sys.flags.no_site != 1
        or sys.flags.ignore_environment != 1
        or sys.flags.safe_path is not True
    ):
        raise ValueError("candidate generator requires an isolated launcher")
    repository_root = arguments.repository_root.resolve(strict=True)
    source_files = _source_candidate_files(repository_root)
    (
        installed_files,
        installed_exclusions,
        console_scripts,
        source_wheel_projection_sha256,
    ) = _installed_files(
        arguments.wheel.resolve(strict=True),
        source_files,
    )
    runtime_identity = _runtime_identity().model_dump(mode="json")
    dependency_distributions = {
        name: item.model_dump(mode="json") for name, item in _dependency_distributions().items()
    }
    installed_metadata_projection = {
        "INSTALLER": hashlib.sha256(_EXPECTED_INSTALLER.encode()).hexdigest(),
    }
    source_build_sha256 = _digest(source_files)
    installed_build_sha256 = _digest(installed_files)
    source_exclusions = (_SOURCE_EXCLUSION,)
    harness_build_sha256 = _digest(
        {
            "console_scripts": console_scripts,
            "dependency_distributions": dependency_distributions,
            "installed_build_sha256": installed_build_sha256,
            "installed_exclusions": installed_exclusions,
            "installed_metadata_projection": installed_metadata_projection,
            "runtime_identity": runtime_identity,
            "schema_version": "4",
            "source_build_sha256": source_build_sha256,
            "source_exclusions": source_exclusions,
            "source_wheel_projection_sha256": source_wheel_projection_sha256,
        },
    )
    catalog_sha256 = source_contract_catalog_sha256()
    candidate_identity_sha256 = _digest(
        {
            "harness_build_sha256": harness_build_sha256,
            "source_contract_catalog_sha256": catalog_sha256,
        },
    )
    payload = {
        "candidate_identity_sha256": candidate_identity_sha256,
        "console_scripts": console_scripts,
        "dependency_distributions": dependency_distributions,
        "harness_build_sha256": harness_build_sha256,
        "installed_build_sha256": installed_build_sha256,
        "installed_exclusions": installed_exclusions,
        "installed_files": installed_files,
        "installed_metadata_projection": installed_metadata_projection,
        "runtime_identity": runtime_identity,
        "schema_version": "4",
        "source_build_sha256": source_build_sha256,
        "source_contract_catalog_sha256": catalog_sha256,
        "source_exclusions": source_exclusions,
        "source_files": source_files,
        "source_wheel_projection_sha256": source_wheel_projection_sha256,
    }
    arguments.out.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    sys.stdout.write(candidate_identity_sha256 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
