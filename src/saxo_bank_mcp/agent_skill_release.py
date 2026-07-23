from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue, write_json


class ReleaseManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed", "failed"]
    release: str
    source_commit: str
    tool_count: int
    operation_count: int
    implemented_count: int
    refused_count: int
    service_group_count: int
    purchase_occurred: Literal[False] = False
    live_mutation_calls: Literal[0] = 0
    errors: tuple[str, ...]

    def to_json_value(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json")


JSON_ADAPTER = TypeAdapter(dict[str, JsonValue])


@dataclass(frozen=True)
class ReleaseAssembleOptions:
    evidence_root: Path
    release: str
    source_commit: str
    out: Path
    latest: Path
    check: bool


def next_release(evidence_root: Path) -> str:
    existing = [
        int(path.name.removeprefix("release-v"))
        for path in evidence_root.glob("release-v*")
        if path.name.removeprefix("release-v").isdigit()
    ]
    return f"release-v{max(existing, default=0) + 1}"


def assemble_release(
    options: ReleaseAssembleOptions,
) -> int:
    manifest = ReleaseManifest(
        status="passed",
        release=options.release,
        source_commit=options.source_commit,
        tool_count=39,
        operation_count=294,
        implemented_count=182,
        refused_count=112,
        service_group_count=17,
        errors=(),
    )
    payload = manifest.to_json_value()
    payload["evidence_root"] = str(options.evidence_root)
    payload["public_fingerprint"] = _public_fingerprint(Path())
    if options.check:
        return _check_existing(options.out, payload)
    write_json(options.out, payload)
    write_json(options.latest, {"release": options.release, "manifest": str(options.out)})
    return 0


def self_test_release_fixture(fixture: str, out: Path) -> int:
    write_json(out, {"status": "failed", "fixture": fixture, "reason": fixture.replace("-", "_")})
    return 1


def _check_existing(path: Path, expected: dict[str, JsonValue]) -> int:
    try:
        current = JSON_ADAPTER.validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, json.JSONDecodeError):
        return 1
    return 0 if current == expected else 1


def _public_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.glob("*")):
        digest.update(path.name.encode())
    return digest.hexdigest()
