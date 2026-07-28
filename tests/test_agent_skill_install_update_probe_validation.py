"""Sanitized UpdateProbeEvidence validation-location regression tests."""

from __future__ import annotations

from typing import Final, cast

from pydantic import ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_install_models import UpdateProbeEvidence
from saxo_bank_mcp.agent_skill_install_producer import (
    align_update_probe_registration_roots,
    sanitized_validation_locations,
)

DIGEST: Final = "a" * 64


def _proof(*, version: str, cache_root: str = "run_root/cache") -> dict[str, JsonValue]:
    return {
        "cache_root": cache_root,
        "version": version,
        "digest": DIGEST,
        "source_digest": DIGEST,
        "inventory_exact_match": True,
        "tool_count": 39,
        "annotations_missing": [],
        "probe_stdout_sha256": DIGEST,
        "list_receipt_name": "codex_plugin_list_restored",
        "registration_version": version,
        "registration_cache_root": cache_root,
    }


def test_update_probe_good_payload_validates() -> None:
    payload: dict[str, JsonValue] = {
        "original_version": "0.1.0",
        "bumped_version": "0.1.1",
        "codex_reached_bumped": True,
        "claude_reached_bumped": True,
        "candidate_restored": True,
        "bumped_proof": {
            "codex": _proof(version="0.1.1"),
            "claude": _proof(version="0.1.1"),
        },
        "restored_proof": {
            "codex": _proof(version="0.1.0"),
            "claude": _proof(version="0.1.0"),
        },
        "temporary_fixtures_removed": True,
        "remaining_temporary_paths": [],
    }
    UpdateProbeEvidence.model_validate(payload)


def test_align_registration_roots_after_path_rewrite() -> None:
    payload: dict[str, JsonValue] = {
        "restored_proof": {
            "codex": {
                "cache_root": "runtime/codex-cache",
                "registration_cache_root": "/Volumes/private/codex-cache",
            }
        }
    }
    align_update_probe_registration_roots(payload)
    restored = cast("dict[str, JsonValue]", payload["restored_proof"])
    proof = cast("dict[str, JsonValue]", restored["codex"])
    assert proof["registration_cache_root"] == proof["cache_root"]


def test_sanitized_validation_locations_omit_values() -> None:
    bad: dict[str, JsonValue] = {
        "original_version": "0.1.0",
        "bumped_version": "0.1.1",
        "codex_reached_bumped": False,
        "claude_reached_bumped": True,
        "candidate_restored": True,
        "bumped_proof": {
            "codex": _proof(version="0.1.1"),
            "claude": _proof(version="0.1.1"),
        },
        "restored_proof": {
            "codex": _proof(version="0.1.0"),
            "claude": _proof(version="0.1.0"),
        },
        "temporary_fixtures_removed": True,
        "remaining_temporary_paths": [],
    }
    try:
        UpdateProbeEvidence.model_validate(bad)
    except ValidationError as exc:
        locs = sanitized_validation_locations(exc)
        assert locs
        assert all(set(item) <= {"loc", "type"} for item in locs)
        assert all("ctx" not in item and "msg" not in item and "input" not in item for item in locs)
        assert any(item.get("loc") == ["codex_reached_bumped"] for item in locs)
        return
    raise AssertionError("expected ValidationError")
