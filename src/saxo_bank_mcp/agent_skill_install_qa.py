from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp.agent_skill_evidence_io import git_output, resolve_commit
from saxo_bank_mcp.agent_skill_install_models import (
    ClientInstallEvidence,
    InstallEvidenceReport,
    InstallManifestOptions,
)

EXPECTED_SKILL_COUNT = 8
EXPECTED_MCP_SERVER_COUNT = 1
EXPECTED_TOOL_COUNT = 39
SHA256_HEX_LENGTH = 64
REQUIRED_CACHE_FILES = (
    ".mcp.json",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    "data/saxo/openapi_inventory.json",
    "pyproject.toml",
    "uv.lock",
)


def write_install_fixture(fixture: str, out: Path) -> int:
    reason = "forbidden_private_file" if fixture == "private-file" else "version_drift"
    write_json(out, {"status": "failed", "fixture": fixture, "reason": reason})
    return 1


def manifest_install_report(options: InstallManifestOptions) -> int:
    error = _planning_error(options)
    if error is not None:
        write_json(options.out, {"status": "failed", "reason": error})
        return 1
    commit = resolve_commit(options.repo, options.commit)
    if commit is None:
        write_json(options.out, {"status": "failed", "reason": "commit_unresolved"})
        return 1
    isolated = {
        "home": str(options.run_root / "home"),
        "codex_home": str(options.run_root / "codex-home"),
        "claude_home": str(options.run_root / "claude-home"),
    }
    before = {
        "codex": _path_fingerprint(options.codex_global_home),
        "claude": _path_fingerprint(options.claude_global_home),
    }
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
        write_json(out, {"status": "failed", "errors": errors})
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


def _planning_error(options: InstallManifestOptions) -> str | None:
    repo_valid = (
        options.repo.is_dir()
        and git_output(options.repo, "rev-parse", "--git-dir") is not None
    )
    commit_valid = repo_valid and resolve_commit(options.repo, options.commit) is not None
    codex_home_valid = (
        options.codex_global_home is not None and options.codex_global_home.is_dir()
    )
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


def _evidence_contract_errors(report: InstallEvidenceReport) -> list[str]:
    errors: list[str] = []
    if (
        report.global_state.before != report.global_state.after
        or not _valid_fingerprints(report.global_state.before)
    ):
        errors.append("global_state_fingerprint_mismatch")
    if report.errors or report.installed_byte_checks.mismatches:
        errors.append("install_report_contains_errors")
    if report.process_cleanup.remaining_pids:
        errors.append("process_cleanup_incomplete")
    expected = (EXPECTED_SKILL_COUNT, EXPECTED_MCP_SERVER_COUNT, EXPECTED_TOOL_COUNT)
    if (report.expected_skills, report.expected_mcp_servers, report.expected_tools) != expected:
        errors.append("expected_counts_mismatch")
    return errors


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
    expected = (EXPECTED_SKILL_COUNT, EXPECTED_MCP_SERVER_COUNT, EXPECTED_TOOL_COUNT)
    if (client.skill_count, client.mcp_server_count, client.tool_count) != expected:
        errors.append(f"{name}_count_mismatch")
    if client.annotations_missing or client.forbidden_cache_paths:
        errors.append(f"{name}_cache_inventory_invalid")
    checks = (client.startup.source, client.startup.cache, client.startup.list_tools)
    if any(check.tool_count != EXPECTED_TOOL_COUNT for check in checks):
        errors.append(f"{name}_startup_invalid")
    skills = tuple(str(path.relative_to(clone)) for path in clone.glob("skills/*/SKILL.md"))
    required = (*REQUIRED_CACHE_FILES, *skills)
    if len(required) != len(REQUIRED_CACHE_FILES) + EXPECTED_SKILL_COUNT:
        errors.append(f"{name}_skill_inventory_invalid")
    elif any(not _same_bytes(clone / relative, cache / relative) for relative in required):
        errors.append(f"{name}_installed_bytes_mismatch")
    return errors


def _origin_path(clone: Path) -> Path | None:
    raw = git_output(clone, "remote", "get-url", "origin")
    if raw is None:
        return None
    return Path(raw.removeprefix("file://")).resolve()


def _same_bytes(source: Path, installed: Path) -> bool:
    return (
        source.is_file()
        and installed.is_file()
        and source.read_bytes() == installed.read_bytes()
    )


def _path_fingerprint(path: Path | None) -> str:
    if path is None or not path.exists():
        return "missing"
    digest = hashlib.sha256()
    files = (path,) if path.is_file() else tuple(item for item in path.rglob("*") if item.is_file())
    for item in sorted(files):
        digest.update(str(item.relative_to(path) if path.is_dir() else item.name).encode())
        digest.update(str(item.stat().st_size).encode())
        digest.update(hashlib.sha256(item.read_bytes()).digest())
    return digest.hexdigest()
