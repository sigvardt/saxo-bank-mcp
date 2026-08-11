"""Sanitized UpdateProbeEvidence validation-location regression tests."""

from __future__ import annotations

from pathlib import Path
from typing import Final, cast

from pydantic import ValidationError

import saxo_bank_mcp.agent_skill_install_producer as producer_module
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
        "tool_count": 60,
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


def test_update_probe_sanitizer_preserves_live_bound_cache_roots(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    run_root = tmp_path / "run"
    cache = run_root / "codex-home" / "plugins" / "cache" / "candidate"
    repo.mkdir()
    run_root.mkdir()
    roots = producer_module._path_publish_roots(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        repo_root=repo,
        repo_field=".",
        run_root=run_root,
    )
    payload: dict[str, JsonValue] = {
        "cache_root": str(cache),
        "registration_cache_root": str(cache),
        "unrelated_path": str(run_root / "private-output"),
    }

    sanitized = producer_module._sanitize_json_paths(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        payload,
        roots,
    )

    assert isinstance(sanitized, dict)
    assert sanitized["cache_root"] == str(cache)
    assert sanitized["registration_cache_root"] == str(cache)
    assert sanitized["unrelated_path"] != str(run_root / "private-output")


def _publish_roots(base: str) -> object:
    return producer_module._path_publish_roots(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        repo_root=Path(base) / "repo",
        repo_field=".",
        run_root=Path(base) / "run",
    )


def _sanitize(payload: dict[str, JsonValue], base: str) -> dict[str, JsonValue]:
    sanitized = producer_module._sanitize_json_paths(  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        payload,
        _publish_roots(base),  # pyright: ignore[reportArgumentType]
    )
    assert isinstance(sanitized, dict)
    return sanitized


def test_sanitizer_redaction_does_not_depend_on_host_path_prefix() -> None:
    """A private run root outside /Users, /private, or /Volumes must still be redacted."""
    # Synthetic host layouts only: these strings are never created on disk. The /tmp case is
    # the layout a Linux CI runner actually uses, so it must stay covered here.
    for base in (
        "/srv/ci-runner/work",
        "/tmp/ci",  # noqa: S108
        "/home/runner/work",
        "/Volumes/ssd_1/tmp",
    ):
        run_root = Path(base) / "run"
        cache = run_root / "codex-home" / "plugins" / "cache" / "candidate"
        sanitized = _sanitize(
            {
                "cache_root": str(cache),
                "registration_cache_root": str(cache),
                "unrelated_path": str(run_root / "private-output"),
                "outside_path": f"{base}/other-secret/token.json",
            },
            base,
        )
        # The exactly two live-bound fields stay byte-exact for registration binding.
        assert sanitized["cache_root"] == str(cache), base
        assert sanitized["registration_cache_root"] == str(cache), base
        # Everything else must not publish the private absolute location.
        for field in ("unrelated_path", "outside_path"):
            value = sanitized[field]
            assert isinstance(value, str)
            assert not value.startswith("/"), (base, field, value)
            assert base not in value, (base, field, value)


def test_sanitizer_redacts_whole_private_path_not_only_its_first_segment() -> None:
    """Redaction must not leave the remaining private segments visible."""
    base = "/Volumes/ssd_1/codex/tmp/demo"
    sanitized = _sanitize(
        {"unrelated_path": f"{base}/run/private-output"},
        base,
    )
    value = sanitized["unrelated_path"]
    assert isinstance(value, str)
    for leaked in ("ssd_1", "codex", "demo", "/run/"):
        assert leaked not in value, (leaked, value)
    assert value.endswith("private-output"), value


def test_sanitizer_preserves_documentation_urls_and_relative_labels() -> None:
    """Redaction must not damage official URLs or already-relative labels."""
    base = "/srv/ci-runner/work"
    url = "https://www.developer.saxo/openapi/learn/security"
    sanitized = _sanitize(
        {"doc": f"see {url}", "relative": "codex-home/plugins/cache", "repo": f"{base}/repo"},
        base,
    )
    assert sanitized["doc"] == f"see {url}"
    assert sanitized["relative"] == "codex-home/plugins/cache"
    assert sanitized["repo"] == "."


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
