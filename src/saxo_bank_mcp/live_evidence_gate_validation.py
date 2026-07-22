"""Strict release-gate manifest verification. # noqa: SIZE_OK."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, Literal, Never, assert_never, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from saxo_bank_mcp.strict_json import StrictJsonError, parse_json_value

type GateName = Literal[
    "01-focused-pytest",
    "02-full-pytest",
    "03-ruff",
    "04-basedpyright",
    "05-no-excuse",
    "06-secret-scan",
    "07-live-write-refusal",
    "08-live-read-refusal",
    "09-retained-evidence-scan",
]

_GATE_NAMES: Final[tuple[GateName, ...]] = (
    "01-focused-pytest",
    "02-full-pytest",
    "03-ruff",
    "04-basedpyright",
    "05-no-excuse",
    "06-secret-scan",
    "07-live-write-refusal",
    "08-live-read-refusal",
    "09-retained-evidence-scan",
)
_DOCUMENTATION_PATHS: Final = frozenset(
    {"README.md", "docs/operator-guide.md", "docs/incident-cleanup.md"},
)
_PUBLIC_SCAN_PATHS: Final = (
    "README.md", "docs", "src", "tests", "data", "pyproject.toml", "uv.lock",
    ".github", ".gitignore",
)
_RETAINED_SCAN_PATHS: Final = (".omo/evidence", ".omo/tmp")
FOCUSED_RELEASE_TEST_PATHS: Final = (
    "tests/test_fastmcp_validation_safety.py",
    "tests/test_final_verify_code_timeout.py",
    "tests/test_hard_task_summary.py",
    "tests/test_live_evidence_bundle.py",
    "tests/test_live_mode.py",
    "tests/test_live_policy.py",
    "tests/test_live_precheck_proof.py",
    "tests/test_live_precheck_proof_cli_failures.py",
    "tests/test_live_precheck_proof_publication.py",
    "tests/test_live_precheck_proof_source_coverage.py",
    "tests/test_live_precheck_proof_state_comparison.py",
    "tests/test_live_precheck_proof_support.py",
    "tests/test_live_precheck_proof_transport_ledger.py",
    "tests/test_live_token_refresh.py",
    "tests/test_live_token_refresh_locking.py",
    "tests/test_live_trade_precheck.py",
    "tests/test_live_trade_precheck_http_failures.py",
    "tests/test_live_trade_precheck_mcp_schema.py",
    "tests/test_live_trade_precheck_response_safety.py",
    "tests/test_live_trade_precheck_selection.py",
    "tests/test_live_trade_precheck_success.py",
    "tests/test_qa.py",
    "tests/test_qa_auth_probes.py",
    "tests/test_qa_evidence_publication.py",
    "tests/test_qa_live_evidence.py",
    "tests/test_qa_live_read_safety.py",
    "tests/test_qa_live_refusal.py",
    "tests/test_qa_manual_live.py",
    "tests/test_qa_probes.py",
    "tests/test_qa_prod_readiness.py",
    "tests/test_qa_secret_scan.py",
    "tests/test_redaction.py",
    "tests/test_read_fingerprints_money_state.py",
    "tests/test_read_tools_response_handling.py",
    "tests/test_read_tools_sim_policy.py",
    "tests/test_secret_scan.py",
    "tests/test_token_cache_auth_tools.py",
)


class TestCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    tests: int = Field(gt=0)
    errors: Literal[0]
    failures: Literal[0]
    skipped: Literal[0]


class GateCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: GateName
    command: list[str] = Field(min_length=1)
    cwd: Literal["."]
    repository_root: Literal["."]
    path_base: Literal["repository_root"]
    exit_code: Literal[0]
    stdout_path: str = Field(min_length=1)
    stdout_bytes: int = Field(ge=0)
    stdout_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    stderr_path: str = Field(min_length=1)
    stderr_bytes: int = Field(ge=0)
    stderr_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    started_at: str = Field(min_length=1)
    duration_seconds: int = Field(ge=0)
    test_counts: TestCounts | None = None


class ArtifactEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str = Field(min_length=1)
    path_base: Literal["repository_root"]
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ProducedArtifact(ArtifactEntry):
    producer_gate: Literal[
        "06-secret-scan",
        "07-live-write-refusal",
        "08-live-read-refusal",
        "09-retained-evidence-scan",
    ]


class NoExcuseChecker(ArtifactEntry):
    provenance: Literal["retained exact copy executed by gate 05-no-excuse"]
    execution_result_path: str = Field(min_length=1)
    command_and_input_paths_recorded_in: str = Field(min_length=1)


class ReplayInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    pyproject_toml_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    uv_lock_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    no_excuse_checker: NoExcuseChecker


class GateManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["saxo-live-precheck-release-gates-v6"]
    status: Literal["passed"]
    repository_root: Literal["."]
    path_base: Literal["repository_root"]
    checks: list[GateCheck] = Field(min_length=9, max_length=9)
    check_count: Literal[9]
    all_checks_exit_zero: Literal[True]
    python_source_test_file_count: int = Field(gt=0)
    python_source_test_sha256: dict[str, str] = Field(min_length=1)
    documentation_sha256: dict[str, str] = Field(min_length=3, max_length=3)
    policy_python_scope: Literal["git-dirty and untracked .py files under src/ and tests/"]
    policy_python_file_count: int = Field(ge=0)
    git_head: str = Field(pattern=r"^[a-f0-9]{40}$")
    replay_inputs: ReplayInputs
    produced_artifacts: list[ProducedArtifact] = Field(min_length=4, max_length=4)
    created_at: str = Field(min_length=1)
    environment: Literal["LIVE_SAFETY_CERTIFICATION"]
    cwd: Literal["."]
    source_hash_algorithm: Literal["sha256"]
    python_source_test_scope: Literal["all .py files under src/ and tests/"]
    documentation_scope: Literal["README.md and operator/incident guides"]
    junit_retained: Literal[False]
    temporary_junit_removed_on_exit: Literal[True]


def raw_gate_manifest_passed(manifest_path: Path, repo_root: Path) -> bool:
    if not manifest_path.is_absolute() or not repo_root.is_absolute():
        return False
    manifest_path = manifest_path.resolve(strict=False)
    repo_root = repo_root.resolve(strict=False)
    if not manifest_path.is_relative_to(repo_root):
        return False
    try:
        manifest = GateManifest.model_validate(
            parse_json_value(manifest_path.read_bytes()),
            strict=True,
        )
    except (OSError, StrictJsonError, ValidationError):
        return False
    raw_gates = manifest_path.parent
    source_paths = _python_source_paths(repo_root)
    policy_paths = _changed_python_paths(repo_root)
    if policy_paths is None:
        return False
    return bool(
        tuple(check.name for check in manifest.checks) == _GATE_NAMES
        and _git_head_matches(manifest.git_head, repo_root)
        and manifest.python_source_test_file_count == len(source_paths)
        and manifest.policy_python_file_count == len(policy_paths)
        and _hash_mapping_matches(
            manifest.python_source_test_sha256,
            source_paths,
            repo_root,
        )
        and _hash_mapping_matches(
            manifest.documentation_sha256,
            _DOCUMENTATION_PATHS,
            repo_root,
        )
        and _checks_match(manifest, raw_gates, repo_root, policy_paths)
        and _replay_inputs_match(manifest.replay_inputs, repo_root)
        and _produced_artifacts_match(manifest.produced_artifacts, raw_gates, repo_root)
    )


def _python_source_paths(repo_root: Path) -> frozenset[str]:
    return frozenset(
        path.relative_to(repo_root).as_posix()
        for base in (repo_root / "src", repo_root / "tests")
        if base.is_dir()
        for path in base.rglob("*.py")
        if path.is_file()
    )


def _hash_mapping_matches(
    entries: Mapping[str, str],
    expected_paths: frozenset[str],
    repo_root: Path,
) -> bool:
    return set(entries) == set(expected_paths) and all(
        _file_matches(repo_root / path, digest) for path, digest in entries.items()
    )


def _checks_match(
    manifest: GateManifest,
    raw_gates: Path,
    repo_root: Path,
    policy_paths: frozenset[str],
) -> bool:
    raw_relative = raw_gates.relative_to(repo_root).as_posix()
    return all(
        _check_record_matches(check, raw_gates, repo_root)
        and _command_matches(check, raw_relative, manifest, policy_paths)
        and _gate_count_metadata_matches(check, repo_root, manifest.policy_python_file_count)
        for check in manifest.checks
    )


def _gate_count_metadata_matches(
    check: GateCheck,
    repo_root: Path,
    policy_python_file_count: int,
) -> bool:
    stdout_path = _repository_path(check.stdout_path, repo_root)
    if stdout_path is None or not stdout_path.is_file():
        return False
    try:
        stdout = stdout_path.read_text(encoding="utf-8")
    except OSError:
        return False
    has_unexpected_test_counts = bool(
        check.test_counts is not None
        and check.name not in {"01-focused-pytest", "02-full-pytest"}
    )
    name = str(check.name)
    match name:
        case "01-focused-pytest" | "02-full-pytest":
            matches = _pytest_count_metadata_matches(check.test_counts, stdout)
        case "03-ruff":
            matches = not has_unexpected_test_counts and stdout == "All checks passed!\n"
        case "04-basedpyright":
            matches = (
                not has_unexpected_test_counts
                and stdout == "0 errors, 0 warnings, 0 notes\n"
            )
        case "05-no-excuse":
            count_match = re.search(r"(?m)^no violations in (\d+) file\(s\)$", stdout)
            matches = (
                not has_unexpected_test_counts
                and count_match is not None
                and int(count_match.group(1)) == policy_python_file_count
            )
        case (
            "06-secret-scan"
            | "07-live-write-refusal"
            | "08-live-read-refusal"
            | "09-retained-evidence-scan"
        ):
            matches = not has_unexpected_test_counts
        case _ as unreachable:
            assert_never(cast("Never", unreachable))
    return matches


def _pytest_count_metadata_matches(counts: TestCounts | None, stdout: str) -> bool:
    if counts is None:
        return False
    match = re.search(
        r"(?m)^release_test_counts tests=(\d+) errors=(\d+) "
        r"failures=(\d+) skipped=(\d+)$",
        stdout,
    )
    return bool(
        match is not None
        and tuple(map(int, match.groups()))
        == (counts.tests, counts.errors, counts.failures, counts.skipped)
    )


def _check_record_matches(check: GateCheck, raw_gates: Path, repo_root: Path) -> bool:
    try:
        retained = GateCheck.model_validate_json(
            (raw_gates / f"{check.name}.result.json").read_bytes(),
            strict=True,
        )
    except (OSError, ValidationError):
        return False
    return bool(
        retained == check
        and _artifact_fields_match(
            check.stdout_path, check.stdout_bytes, check.stdout_sha256, repo_root,
        )
        and _artifact_fields_match(
            check.stderr_path, check.stderr_bytes, check.stderr_sha256, repo_root,
        )
    )


def _command_matches(
    check: GateCheck,
    raw_relative: str,
    manifest: GateManifest,
    policy_paths: frozenset[str],
) -> bool:
    command = tuple(check.command)
    name = str(check.name)
    match name:
        case "01-focused-pytest":
            matches = command == (
                ".venv/bin/pytest",
                "-q",
                "--junitxml=<temporary-junit>",
                *FOCUSED_RELEASE_TEST_PATHS,
            )
        case "02-full-pytest":
            matches = command == (
                ".venv/bin/pytest", "-q", "--junitxml=<temporary-junit>",
            )
        case "03-ruff":
            matches = command == (".venv/bin/ruff", "check", "--no-cache", ".")
        case "04-basedpyright":
            matches = command == (".venv/bin/basedpyright",)
        case "05-no-excuse":
            checker = manifest.replay_inputs.no_excuse_checker.path
            paths = command[2:]
            matches = bool(
                command[:2] == (".venv/bin/python", checker)
                and tuple(paths) == tuple(sorted(policy_paths))
                and len(paths) == manifest.policy_python_file_count
            )
        case "06-secret-scan":
            matches = command == _qa_command(
                "secret-scan", f"{raw_relative}/secret-scan.json", _PUBLIC_SCAN_PATHS,
            )
        case "07-live-write-refusal":
            matches = command == _qa_command(
                "live-write-refusal", f"{raw_relative}/live-write-refusal.json",
            )
        case "08-live-read-refusal":
            matches = command == _qa_command(
                "live-read-refusal", f"{raw_relative}/live-read-refusal.json",
            )
        case "09-retained-evidence-scan":
            matches = command == _qa_command(
                "secret-scan",
                f"{raw_relative}/retained-evidence-scan.json",
                _RETAINED_SCAN_PATHS,
            )
        case _ as unreachable:
            assert_never(cast("Never", unreachable))
    return matches


def _qa_command(command: str, out: str, paths: Sequence[str] = ()) -> tuple[str, ...]:
    base = (".venv/bin/python", "-m", "saxo_bank_mcp.qa", command, "--out", out)
    return base if not paths else (*base, "--paths", *paths)


def _replay_inputs_match(value: ReplayInputs, repo_root: Path) -> bool:
    checker_path = _repository_path(value.no_excuse_checker.path, repo_root)
    if checker_path is None:
        return False
    result_path = checker_path.parent.parent / "05-no-excuse.result.json"
    try:
        result_relative = result_path.relative_to(repo_root).as_posix()
    except ValueError:
        return False
    return bool(
        _file_matches(repo_root / "pyproject.toml", value.pyproject_toml_sha256)
        and _file_matches(repo_root / "uv.lock", value.uv_lock_sha256)
        and _artifact_matches(value.no_excuse_checker, repo_root)
        and value.no_excuse_checker.execution_result_path == result_relative
        and value.no_excuse_checker.command_and_input_paths_recorded_in == result_relative
    )


def _produced_artifacts_match(
    artifacts: Sequence[ProducedArtifact],
    raw_gates: Path,
    repo_root: Path,
) -> bool:
    expected = {
        "06-secret-scan": raw_gates / "secret-scan.json",
        "07-live-write-refusal": raw_gates / "live-write-refusal.json",
        "08-live-read-refusal": raw_gates / "live-read-refusal.json",
        "09-retained-evidence-scan": raw_gates / "retained-evidence-scan.json",
    }
    return {item.producer_gate for item in artifacts} == expected.keys() and all(
        _repository_path(item.path, repo_root) == expected[item.producer_gate].resolve(strict=False)
        and _artifact_matches(item, repo_root)
        for item in artifacts
    )


def _artifact_matches(value: ArtifactEntry, repo_root: Path) -> bool:
    path = _repository_path(value.path, repo_root)
    return bool(
        path is not None
        and path.is_file()
        and path.stat().st_size == value.bytes
        and _sha256(path) == value.sha256
    )


def _artifact_fields_match(path: str, size: int, digest: str, repo_root: Path) -> bool:
    return _artifact_matches(
        ArtifactEntry(path=path, path_base="repository_root", bytes=size, sha256=digest),
        repo_root,
    )


def _file_matches(path: Path, digest: str) -> bool:
    return path.is_file() and _sha256(path) == digest


def _git_head_matches(expected: str, repo_root: Path) -> bool:
    git = shutil.which("git")
    if git is None:
        return False
    try:
        result = subprocess.run(
            [git, "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == expected


def _changed_python_paths(repo_root: Path) -> frozenset[str] | None:
    git = shutil.which("git")
    if git is None:
        return None
    commands = (
        (git, "diff", "--name-only", "--no-renames", "HEAD", "--", "*.py"),
        (git, "ls-files", "--others", "--exclude-standard", "--", "*.py"),
    )
    paths: set[str] = set()
    try:
        for command in commands:
            result = subprocess.run(
                command,
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            if result.returncode != 0:
                return None
            paths.update(
                path
                for path in result.stdout.splitlines()
                if path.endswith(".py") and path.startswith(("src/", "tests/"))
            )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return frozenset(paths)


def _repository_path(relative_path: str, repo_root: Path) -> Path | None:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    resolved = (repo_root / candidate).resolve(strict=False)
    try:
        resolved.relative_to(repo_root)
    except ValueError:
        return None
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
