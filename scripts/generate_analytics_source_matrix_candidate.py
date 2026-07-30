from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

from saxo_bank_mcp.analytics_source_contracts import source_contract_catalog_sha256
from saxo_bank_mcp.qa_analytics_source_matrix import (
    _source_candidate_files,  # pyright: ignore[reportPrivateUsage]
)

_CANDIDATE_RESOURCE = "saxo_bank_mcp/_analytics_source_matrix/source_matrix_candidate.json"
_SOURCE_EXCLUSION = "data/analytics/source_matrix_candidate.json"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()


def _installed_files(wheel: Path) -> tuple[dict[str, str], tuple[str, ...]]:
    with zipfile.ZipFile(wheel) as archive:
        names = tuple(
            sorted(info.filename for info in archive.infolist() if not info.is_dir()),
        )
        records = tuple(name for name in names if name.endswith(".dist-info/RECORD"))
        if len(records) != 1 or _CANDIDATE_RESOURCE not in names:
            raise ValueError("wheel does not have the expected candidate and RECORD")
        exclusions = (_CANDIDATE_RESOURCE, records[0])
        installed = {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in names
            if name not in exclusions
        }
    return installed, exclusions


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seal the complete analytics source-matrix candidate closure.",
    )
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/analytics/source_matrix_candidate.json"),
    )
    arguments = parser.parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    source_files = _source_candidate_files(repository_root)
    installed_files, installed_exclusions = _installed_files(
        arguments.wheel.resolve(strict=True),
    )
    source_build_sha256 = _digest(source_files)
    installed_build_sha256 = _digest(installed_files)
    source_exclusions = (_SOURCE_EXCLUSION,)
    harness_build_sha256 = _digest(
        {
            "installed_build_sha256": installed_build_sha256,
            "installed_exclusions": installed_exclusions,
            "schema_version": "2",
            "source_build_sha256": source_build_sha256,
            "source_exclusions": source_exclusions,
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
        "harness_build_sha256": harness_build_sha256,
        "installed_build_sha256": installed_build_sha256,
        "installed_exclusions": installed_exclusions,
        "installed_files": installed_files,
        "schema_version": "2",
        "source_build_sha256": source_build_sha256,
        "source_contract_catalog_sha256": catalog_sha256,
        "source_exclusions": source_exclusions,
        "source_files": source_files,
    }
    arguments.out.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    sys.stdout.write(candidate_identity_sha256 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
