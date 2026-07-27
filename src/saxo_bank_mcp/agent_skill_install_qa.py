from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp.agent_skill_evidence_io import git_output, resolve_commit
from saxo_bank_mcp.agent_skill_install_models import (
    ClientInstallEvidence,
    InstallEvidenceReport,
    InstallManifestOptions,
)
from saxo_bank_mcp.agent_skill_install_paths import (
    OWNER_ONLY_MODE,
    PRESERVED_ROOT_LABELS,
    REQUIRED_CACHE_FILES,
    global_state_fingerprint,
    owner_only_from_modes,
    owner_only_mode,
    publishable_tracked_files,
    required_cache_files,
)

EXPECTED_SKILL_COUNT = 8
EXPECTED_MCP_SERVER_COUNT = 1
EXPECTED_TOOL_COUNT = 39
SHA256_HEX_LENGTH = 64
JSON_OBJECT_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def write_install_fixture(fixture: str, out: Path) -> int:
    if fixture == "private-file":
        write_json(
            out,
            {
                "status": "failed",
                "fixture": "private-file",
                "forbidden_path_class": ".omo",
            },
        )
        return 1
    write_json(
        out,
        {
            "status": "failed",
            "fixture": "version-drift",
            "version_field": "project.version",
        },
    )
    return 1


def manifest_install_report(options: InstallManifestOptions) -> int:
    error = planning_error(options)
    if error is not None:
        write_json(options.out, {"status": "failed", "reason": error})
        return 1
    commit = resolve_commit(options.repo, options.commit)
    if commit is None:
        write_json(options.out, {"status": "failed", "reason": "commit_unresolved"})
        return 1
    if options.codex_global_home is None or options.claude_global_home is None:
        write_json(options.out, {"status": "failed", "reason": "isolated_home_invalid"})
        return 1
    isolated = {
        "home": str(options.run_root / "home"),
        "codex_home": str(options.run_root / "codex-home"),
        "claude_home": str(options.run_root / "claude-home"),
    }
    before = global_state_fingerprint(options.codex_global_home, options.claude_global_home)
    write_json(
        options.out,
        {
            "status": "planned",
            "execution_mode": "manifest_validation",
            "repo": str(options.repo.resolve()),
            "candidate_commit": commit,
            "isolated_roots": isolated,
            "global_state_before": before,
            "expected_skills": options.expected_skills,
            "expected_mcp_servers": EXPECTED_MCP_SERVER_COUNT,
            "expected_tools": options.expected_tools,
            "preserve_for": options.preserve_for,
            "installed": False,
        },
    )
    return 0


def verify_install_report(path: Path, out: Path) -> int:
    report, errors = load_verified_install_report(path)
    if report is None:
        write_json(out, {"status": "failed", "errors": list(errors)})
        return 1
    write_json(
        out,
        {
            "status": "passed",
            "execution_mode": "installed_verification",
            "candidate_commit": report.candidate_commit,
            "expected_skills": report.expected_skills,
            "expected_mcp_servers": report.expected_mcp_servers,
            "expected_tools": report.expected_tools,
            "global_state_unchanged": report.global_state_unchanged,
            "errors": [],
        },
    )
    return 0


def load_verified_install_report(
    path: Path,
) -> tuple[InstallEvidenceReport | None, tuple[str, ...]]:
    try:
        report = InstallEvidenceReport.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError:
        return None, ("invalid_install_report",)
    except (ValidationError, json.JSONDecodeError) as exc:
        if isinstance(exc, ValidationError):
            if any(item["type"] == "json_invalid" for item in exc.errors()):
                return None, ("invalid_install_report",)
            roots = {str(item["loc"][0]) for item in exc.errors() if item["loc"]}
            errors = ["install_report_schema_invalid"]
            if "global_state" in roots:
                errors.append("global_state_fingerprints_missing")
            return None, tuple(errors)
        return None, ("invalid_install_report",)
    errors = _install_report_errors(report)
    return (report, ()) if not errors else (None, tuple(errors))


def planning_error(options: InstallManifestOptions) -> str | None:
    repo_valid = (
        options.repo.is_dir() and git_output(options.repo, "rev-parse", "--git-dir") is not None
    )
    commit_valid = repo_valid and resolve_commit(options.repo, options.commit) is not None
    codex_home_valid = options.codex_global_home is not None and options.codex_global_home.is_dir()
    claude_home_valid = (
        options.claude_global_home is not None and options.claude_global_home.is_dir()
    )
    basic_checks = (
        (repo_valid, "repo_not_found"),
        (commit_valid, "commit_unresolved"),
        (codex_home_valid, "codex_global_home_invalid"),
        (claude_home_valid, "claude_global_home_invalid"),
    )
    basic_error = next((reason for valid, reason in basic_checks if not valid), None)
    if basic_error is not None:
        return basic_error
    codex_global_home = options.codex_global_home
    claude_global_home = options.claude_global_home
    if codex_global_home is None or claude_global_home is None:
        return "isolated_home_invalid"
    roots = (
        options.run_root.resolve(),
        (options.run_root / "home").resolve(),
        (options.run_root / "codex-home").resolve(),
        (options.run_root / "claude-home").resolve(),
        codex_global_home.resolve(),
        claude_global_home.resolve(),
    )
    contract_checks = (
        (len(set(roots)) == len(roots), "isolated_home_collision"),
        (options.expected_skills == EXPECTED_SKILL_COUNT, "expected_skill_count_invalid"),
        (options.expected_tools == EXPECTED_TOOL_COUNT, "expected_tool_count_invalid"),
    )
    return next((reason for valid, reason in contract_checks if not valid), None)


def path_fingerprint(path: Path | None) -> str:
    if path is None:
        return "missing"
    # Compatibility helper for older tests; prefer global_state_fingerprint.
    return global_state_fingerprint(path, path)["codex"]


def _install_report_errors(report: InstallEvidenceReport) -> list[str]:
    repo = report.repo.resolve()
    clone = report.clone.path.resolve()
    run_root = report.fixture_cleanup.run_root.resolve()
    errors = _repository_errors(report, repo, clone, run_root)
    errors.extend(_evidence_contract_errors(report))
    for name, client in (("codex", report.codex), ("claude", report.claude)):
        errors.extend(_client_errors(name, client, clone, run_root))
    return errors


def _repository_errors(
    report: InstallEvidenceReport,
    repo: Path,
    clone: Path,
    run_root: Path,
) -> list[str]:
    errors: list[str] = []
    if resolve_commit(repo, report.candidate_commit) != report.candidate_commit:
        errors.append("candidate_commit_unresolved")
    if (
        clone == repo
        or report.clone.commit != report.candidate_commit
        or git_output(clone, "rev-parse", "HEAD") != report.candidate_commit
    ):
        errors.append("clone_commit_mismatch")
    if (
        report.clone.source_repo.resolve() != repo
        or _origin_path(clone) != repo
        or (clone / ".git/objects/info/alternates").exists()
    ):
        errors.append("clone_not_no_local")
    if git_output(clone, "status", "--porcelain") != "":
        errors.append("clone_not_clean")
    if not clone.is_relative_to(run_root):
        errors.append("clone_outside_run_root")
    return errors


def _evidence_contract_errors(report: InstallEvidenceReport) -> list[str]:  # noqa: C901
    errors: list[str] = []
    if report.global_state.before != report.global_state.after or not _valid_fingerprints(
        report.global_state.before,
    ):
        errors.append("global_state_fingerprint_mismatch")
    if report.errors or report.installed_byte_checks.mismatches:
        errors.append("install_report_contains_errors")
    if report.process_cleanup.remaining_pids:
        errors.append("process_cleanup_incomplete")
    expected = (EXPECTED_SKILL_COUNT, EXPECTED_MCP_SERVER_COUNT, EXPECTED_TOOL_COUNT)
    if (report.expected_skills, report.expected_mcp_servers, report.expected_tools) != expected:
        errors.append("expected_counts_mismatch")
    if not report.help_syntax or not all(
        _help_validated(item) for item in report.help_syntax.values()
    ):
        errors.append("help_syntax_unvalidated")
    if report.update_probe.get("candidate_restored") is not True:
        errors.append("update_probe_restore_missing")
    if report.update_probe.get("codex_reached_bumped") is not True:
        errors.append("codex_update_version_not_reached")
    if report.update_probe.get("claude_reached_bumped") is not True:
        errors.append("claude_update_version_not_reached")
    if not _auth_metadata_owner_only(report.auth_files):
        errors.append("auth_file_metadata_invalid")
    if report.fixture_cleanup.teardown_owner != "post-final-completion-gate":
        errors.append("fixture_teardown_owner_invalid")
    errors.extend(_preserved_mode_errors(report))
    return errors


def _preserved_mode_errors(report: InstallEvidenceReport) -> list[str]:
    cleanup = report.fixture_cleanup
    modes = cleanup.modes
    errors: list[str] = []
    if set(modes) != set(PRESERVED_ROOT_LABELS):
        errors.append("preserved_modes_incomplete")
    if not cleanup.owner_only or not owner_only_from_modes(dict(modes)):
        errors.append("preserved_roots_not_owner_only")
    if report.clone.mode != modes.get("clone"):
        errors.append("clone_mode_mismatch")
    resolved_roots = _resolved_preserved_roots(report)
    for label in PRESERVED_ROOT_LABELS:
        path = resolved_roots.get(label)
        reported = modes.get(label)
        if path is None or not path.exists():
            errors.append(f"preserved_root_missing:{label}")
            continue
        on_disk = owner_only_mode(path)
        if reported != on_disk:
            errors.append(f"preserved_mode_mismatch:{label}")
        if on_disk != OWNER_ONLY_MODE:
            errors.append(f"preserved_root_not_owner_only:{label}")
    return errors


def _resolved_preserved_roots(report: InstallEvidenceReport) -> dict[str, Path]:
    run_root = report.fixture_cleanup.run_root.resolve()
    clone = report.clone.path.resolve()
    return {
        "run_root": run_root,
        "clone": clone,
        "codex_cache": report.codex.cache_root.resolve(),
        "claude_cache": report.claude.cache_root.resolve(),
        "home": run_root / "home",
        "codex_home": run_root / "codex-home",
        "claude_home": run_root / "claude-home",
    }


def _valid_fingerprints(values: Mapping[str, JsonValue]) -> bool:
    expected_keys = {"codex", "claude"}
    return set(values) == expected_keys and all(
        isinstance(value, str)
        and len(value) == SHA256_HEX_LENGTH
        and all(character in "0123456789abcdef" for character in value)
        for value in values.values()
    )


def _client_errors(
    name: str,
    client: ClientInstallEvidence,
    clone: Path,
    run_root: Path,
) -> list[str]:
    cache = client.cache_root.resolve()
    errors: list[str] = []
    if not cache.is_dir() or not cache.is_relative_to(run_root):
        errors.append(f"{name}_cache_invalid")
        return errors
    if client.cache_root_source in {"missing", "isolated_cache_tree", "isolated_cache_inventory"}:
        errors.append(f"{name}_cache_root_guessed")
    expected = (EXPECTED_SKILL_COUNT, EXPECTED_MCP_SERVER_COUNT, EXPECTED_TOOL_COUNT)
    if (client.skill_count, client.mcp_server_count, client.tool_count) != expected:
        errors.append(f"{name}_count_mismatch")
    if client.identity != "saxo-bank-mcp" or client.version != _project_version(clone):
        errors.append(f"{name}_identity_version_invalid")
    if client.annotations_missing or client.forbidden_cache_paths:
        errors.append(f"{name}_cache_inventory_invalid")
    checks = (client.startup.source, client.startup.cache, client.startup.list_tools)
    if any(check.tool_count != EXPECTED_TOOL_COUNT for check in checks):
        errors.append(f"{name}_startup_invalid")
    required = required_cache_files(clone)
    if len(required) != len(REQUIRED_CACHE_FILES) + EXPECTED_SKILL_COUNT:
        errors.append(f"{name}_skill_inventory_invalid")
    elif any(not _same_bytes(clone / relative, cache / relative) for relative in required):
        errors.append(f"{name}_installed_bytes_mismatch")
    public_files = publishable_tracked_files(clone)
    if not public_files or any(
        not _same_bytes(clone / relative, cache / relative) for relative in public_files
    ):
        errors.append(f"{name}_tracked_public_tree_mismatch")
    return errors


def _origin_path(clone: Path) -> Path | None:
    raw = git_output(clone, "remote", "get-url", "origin")
    if raw is None:
        return None
    return Path(raw.removeprefix("file://")).resolve()


def _same_bytes(source: Path, installed: Path) -> bool:
    return (
        source.is_file() and installed.is_file() and source.read_bytes() == installed.read_bytes()
    )


def _help_validated(value: JsonValue) -> bool:
    return isinstance(value, dict) and value.get("validated") is True


def _auth_metadata_owner_only(value: Mapping[str, JsonValue]) -> bool:
    copied = value.get("copied")
    if copied is None:
        return True
    if not isinstance(copied, list):
        return False
    for item in copied:
        if not isinstance(item, dict) or item.get("mode") != "0o600":
            return False
    return value.get("values_published") is False


def _project_version(root: Path) -> str:
    payload = JSON_OBJECT_ADAPTER.validate_python(
        tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8")),
    )
    project = payload.get("project")
    if not isinstance(project, dict):
        return ""
    version = project.get("version")
    return version if isinstance(version, str) else ""
