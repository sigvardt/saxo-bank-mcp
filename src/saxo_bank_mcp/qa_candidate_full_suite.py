"""Owner-only JUnit and command receipt for one exact-candidate full suite."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

REQUIRED_EXTERNAL_TEMP_ROOT = Path("/Volumes/ssd_1/codex/tmp/saxo-bank-mcp-analytics")
_SHA256_PATTERN = r"^[a-f0-9]{64}$"
_GIT_OBJECT_PATTERN = r"^[a-f0-9]{40}$"
_TEMP_NAMES = ("TMPDIR", "TMP", "TEMP")
_OWNER_FILE_MODE = 0o600
_OWNER_DIRECTORY_MODE = 0o700


class CandidateFullSuiteError(RuntimeError):
    """Fail closed before publishing an unbound full-suite claim."""


class CandidateFullSuiteReceipt(BaseModel):
    """Private exact-candidate full-suite evidence without raw command output."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    schema_version: Literal["1"] = "1"
    receipt_kind: Literal["candidate_full_suite"] = "candidate_full_suite"
    status: Literal["passed", "failed"]
    candidate_commit: str = Field(pattern=_GIT_OBJECT_PATTERN)
    candidate_tree: str = Field(pattern=_GIT_OBJECT_PATTERN)
    candidate_clean_before: Literal[True] = True
    candidate_clean_after: bool
    external_temp_root: str = Field(min_length=1)
    command_name: Literal["candidate_full_suite_pytest"] = "candidate_full_suite_pytest"
    command_argv: tuple[str, ...] = Field(min_length=3)
    command_cwd: str = Field(min_length=1)
    exit_code: int
    test_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    junit_sha256: str = Field(pattern=_SHA256_PATTERN)
    stdout_sha256: str = Field(pattern=_SHA256_PATTERN)
    stderr_sha256: str = Field(pattern=_SHA256_PATTERN)
    junit_retained: Literal[True] = True
    raw_output_retained: Literal[False] = False
    owner_only: Literal[True] = True
    reason: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    receipt_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_receipt(self) -> Self:
        passed = (
            self.exit_code == 0
            and self.test_count > 0
            and self.failure_count == 0
            and self.error_count == 0
            and self.candidate_clean_after
        )
        if passed != (self.status == "passed"):
            raise ValueError("full-suite status does not match command evidence")
        if (self.status == "passed") != (self.reason is None):
            raise ValueError("full-suite reason does not match status")
        material = self.model_dump(mode="json", exclude={"receipt_sha256"})
        if self.receipt_sha256 != _digest(material):
            raise ValueError("full-suite receipt digest mismatch")
        return self


def run_candidate_full_suite(
    *,
    repo: Path,
    candidate_commit: str,
    evidence_root: Path,
    environment: Mapping[str, str] | None = None,
    platform_name: str = sys.platform,
) -> CandidateFullSuiteReceipt:
    """Run only the guarded launcher and retain JUnit plus its private receipt."""
    resolved_repo = _verified_repo(repo, candidate_commit)
    candidate_tree = _git(resolved_repo, "rev-parse", "HEAD^{tree}")
    env = dict(os.environ if environment is None else environment)
    external_temp_root = _verified_temp_root(env, platform_name=platform_name)
    launcher = resolved_repo / "scripts/run-pytest"
    if launcher.is_symlink() or not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise CandidateFullSuiteError("guarded_pytest_launcher_unavailable")
    _prepare_evidence_root(evidence_root)
    junit_path = evidence_root / "full-suite.xml"
    receipt_path = evidence_root / "full-suite-receipt.json"
    _create_owner_only_empty(junit_path)
    command = (
        str(launcher.resolve()),
        "-q",
        f"--junitxml={junit_path.resolve()}",
    )
    try:
        completed = subprocess.run(
            command,
            cwd=resolved_repo,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise CandidateFullSuiteError("guarded_pytest_launcher_failed") from error
    _require_owner_only_file(junit_path)
    try:
        counts = _junit_counts(junit_path)
    except (ET.ParseError, OSError, ValueError) as error:
        raise CandidateFullSuiteError("full_suite_junit_invalid") from error
    clean_after = (
        _git(resolved_repo, "rev-parse", "HEAD") == candidate_commit
        and _git(resolved_repo, "rev-parse", "HEAD^{tree}") == candidate_tree
        and _git(resolved_repo, "status", "--porcelain", "--untracked-files=all") == ""
    )
    passed = (
        completed.returncode == 0
        and counts[1] == 0
        and counts[2] == 0
        and counts[0] > 0
        and clean_after
    )
    reason = (
        None
        if passed
        else ("candidate_changed_during_suite" if not clean_after else "full_suite_failed")
    )
    material = {
        "schema_version": "1",
        "receipt_kind": "candidate_full_suite",
        "status": "passed" if passed else "failed",
        "candidate_commit": candidate_commit,
        "candidate_tree": candidate_tree,
        "candidate_clean_before": True,
        "candidate_clean_after": clean_after,
        "external_temp_root": str(external_temp_root),
        "command_name": "candidate_full_suite_pytest",
        "command_argv": command,
        "command_cwd": str(resolved_repo),
        "exit_code": completed.returncode,
        "test_count": counts[0],
        "failure_count": counts[1],
        "error_count": counts[2],
        "skipped_count": counts[3],
        "junit_sha256": _file_digest(junit_path),
        "stdout_sha256": hashlib.sha256(completed.stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr.encode()).hexdigest(),
        "junit_retained": True,
        "raw_output_retained": False,
        "owner_only": True,
        "reason": reason,
    }
    receipt = CandidateFullSuiteReceipt.model_validate(
        {**material, "receipt_sha256": _digest(material)},
        strict=True,
    )
    _atomic_owner_only_write(receipt_path, receipt.model_dump_json(indent=2) + "\n")
    return receipt


def verify_candidate_full_suite_receipt(path: Path) -> CandidateFullSuiteReceipt:
    """Read back one private owner-only full-suite receipt and verify its digest."""
    _require_owner_only_file(path)
    return CandidateFullSuiteReceipt.model_validate_json(
        path.read_text(encoding="utf-8"),
        strict=True,
    )


def _verified_repo(repo: Path, candidate_commit: str) -> Path:
    try:
        resolved = repo.resolve(strict=True)
    except OSError as error:
        raise CandidateFullSuiteError("candidate_repo_unavailable") from error
    if (
        _git(resolved, "rev-parse", "HEAD") != candidate_commit
        or _git(resolved, "status", "--porcelain", "--untracked-files=all") != ""
    ):
        raise CandidateFullSuiteError("candidate_repo_not_exact_and_clean")
    return resolved


def _verified_temp_root(environment: Mapping[str, str], *, platform_name: str) -> Path:
    raw_values = tuple(environment.get(name) for name in _TEMP_NAMES)
    if any(value is None or not Path(value).is_absolute() for value in raw_values):
        raise CandidateFullSuiteError("external_temp_environment_invalid")
    try:
        values = tuple(
            Path(value).resolve(strict=True) for value in raw_values if value is not None
        )
    except OSError as error:
        raise CandidateFullSuiteError("external_temp_environment_invalid") from error
    if len(values) != len(_TEMP_NAMES) or len(set(values)) != 1:
        raise CandidateFullSuiteError("external_temp_environment_mismatch")
    root = values[0]
    if platform_name == "darwin" and root != REQUIRED_EXTERNAL_TEMP_ROOT.resolve(strict=True):
        raise CandidateFullSuiteError("external_temp_root_not_approved")
    return root


def _git(repo: Path, *args: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise CandidateFullSuiteError("candidate_git_check_failed")
    try:
        result = subprocess.run(
            (executable, *args),
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise CandidateFullSuiteError("candidate_git_check_failed") from error
    return result.stdout.strip()


def _prepare_evidence_root(path: Path) -> None:
    if path.exists() or path.is_symlink():
        metadata = os.lstat(path)
        if not (
            stat.S_ISDIR(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and stat.S_IMODE(metadata.st_mode) == _OWNER_DIRECTORY_MODE
            and not any(path.iterdir())
        ):
            raise CandidateFullSuiteError("full_suite_evidence_root_unsafe")
        return
    path.mkdir(parents=True, mode=_OWNER_DIRECTORY_MODE)
    path.chmod(_OWNER_DIRECTORY_MODE)


def _create_owner_only_empty(path: Path) -> None:
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            _OWNER_FILE_MODE,
        )
    except OSError as error:
        raise CandidateFullSuiteError("full_suite_artifact_exists_or_unsafe") from error
    os.close(descriptor)
    path.chmod(_OWNER_FILE_MODE)


def _require_owner_only_file(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise CandidateFullSuiteError("full_suite_artifact_missing") from error
    if not (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) == _OWNER_FILE_MODE
    ):
        raise CandidateFullSuiteError("full_suite_artifact_not_owner_only")


def _junit_counts(path: Path) -> tuple[int, int, int, int]:
    root = ET.parse(path).getroot()  # noqa: S314
    attributes = root.attrib
    if "tests" not in attributes:
        suites = root.findall("./testsuite")
        attributes = {
            name: str(sum(int(suite.attrib.get(name, "0")) for suite in suites))
            for name in ("tests", "failures", "errors", "skipped")
        }
    return tuple(  # type: ignore[return-value]
        int(attributes.get(name, "0")) for name in ("tests", "failures", "errors", "skipped")
    )


def _atomic_owner_only_write(path: Path, text: str) -> None:
    descriptor, raw_temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(raw_temporary)
    try:
        os.fchmod(descriptor, _OWNER_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(_OWNER_FILE_MODE)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
