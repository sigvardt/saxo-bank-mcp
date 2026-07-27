from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Final, Literal

from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_install_models import PrivacyEvidenceBinding
from saxo_bank_mcp.agent_skill_static_gate_constants import PUBLIC_SECRET_SCAN_PATHS
from saxo_bank_mcp.secret_scan import scan_secret_paths, scan_secret_text

_JSON_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
REQUIRED_SCOPES: Final = (
    "manual_json",
    "installed_caches",
    "auth_copies",
    "client_state",
    "todo13_public_clone",
)
type DigestOrder = Literal[
    "provisional_install_then_privacy_report_then_self_scan_then_finalize_digests"
]
DIGEST_ORDER: DigestOrder = (
    "provisional_install_then_privacy_report_then_self_scan_then_finalize_digests"
)
PRIVACY_DIGEST_ZERO: Final = "0" * 64
_PRIVACY_DIGEST_KEYS: Final = frozenset(
    {"privacy_report_sha256", "privacy_self_scan_sha256"},
)
_PRIVACY_OUTPUT_NAMES: Final = frozenset({"privacy-report.json", "privacy-self-scan.json"})
_VERIFY_MANUAL_NAME: Final = "verify.json"
# Known Todo 14 manuals (documentation + tests). Derivation uses every report_dir/*.json.
TODO14_MANUAL_NAMES: Final = (
    "install.json",
    "verify.json",
    "global-before.json",
    "global-after.json",
    "pytest-counts.json",
    "self-private-file.json",
    "self-version-drift.json",
    "privacy-report.json",
    "privacy-self-scan.json",
)
type LexKind = Literal["missing", "dir", "file", "symlink", "special"]


class PrivacyPipelineError(ValueError):
    def __init__(self, reason: str) -> None:  # noqa: D107
        super().__init__(reason)
        self.reason = reason


def provisional_privacy_binding(
    *,
    candidate_commit: str,
    clone_commit: str,
) -> dict[str, JsonValue]:
    """Complete privacy shape with only the two digests set to zeros."""
    return {
        "privacy_report_path": "privacy-report.json",
        "privacy_self_scan_path": "privacy-self-scan.json",
        "privacy_report_sha256": PRIVACY_DIGEST_ZERO,
        "privacy_self_scan_sha256": PRIVACY_DIGEST_ZERO,
        "candidate_commit": candidate_commit,
        "clone_commit": clone_commit,
        "clean": True,
        "findings_count": 0,
        "scan_errors_count": 0,
        "scopes_covered": list(REQUIRED_SCOPES),
        "digest_order": DIGEST_ORDER,
    }


def produce_privacy_evidence(  # noqa: PLR0913
    *,
    install_report_path: Path,
    report_dir: Path,
    run_root: Path,
    version: str,
    candidate_commit: str,
    clone_commit: str,
    codex_cache: Path,
    claude_cache: Path,
) -> PrivacyEvidenceBinding:
    """Producer-owned privacy sequence after provisional install is written."""
    if not install_report_path.is_file():
        raise PrivacyPipelineError("provisional_install_missing")
    targets = derive_existing_privacy_targets(
        report_dir=report_dir,
        run_root=run_root,
        version=version,
        codex_cache=codex_cache,
        claude_cache=claude_cache,
        include_privacy_outputs=False,
        include_verify=False,
    )
    findings, scan_errors = _scan_all_targets(
        targets,
        run_root=run_root,
        install_report_path=install_report_path,
    )
    if findings or scan_errors:
        raise PrivacyPipelineError("privacy_scan_failed")
    privacy_report_path = report_dir / "privacy-report.json"
    privacy_report: dict[str, JsonValue] = {
        "status": "passed",
        "clean": True,
        "candidate_commit": candidate_commit,
        "clone_commit": clone_commit,
        "findings": [],
        "scan_errors": [],
        "scanned_file_count": _count_scanned(targets),
        "scope": {
            key: [_publicize_path(path, run_root=run_root) for path in paths]
            for key, paths in targets.items()
        },
        "notes": (
            "Order: provisional install (zero digests) → privacy-report over existing "
            "targets → self-scan privacy-report + canonical install → finalize digests."
        ),
    }
    _atomic_write_json(privacy_report_path, privacy_report)

    self_findings, self_errors = _self_scan(
        privacy_report_path=privacy_report_path,
        install_report_path=install_report_path,
        run_root=run_root,
    )
    if self_findings or self_errors:
        raise PrivacyPipelineError("privacy_self_scan_failed")
    privacy_self_scan_path = report_dir / "privacy-self-scan.json"
    self_payload: dict[str, JsonValue] = {
        "status": "passed",
        "clean": True,
        "candidate_commit": candidate_commit,
        "clone_commit": clone_commit,
        "findings": [],
        "scan_errors": [],
        "scanned_file_count": 2,
        "scope": {
            "privacy_report": [privacy_report_path.name],
            "install_report_canonical": [install_report_path.name],
        },
        "notes": (
            "Self-scan covers privacy-report.json and canonical install bytes "
            "(only the two privacy digests normalized)."
        ),
    }
    _atomic_write_json(privacy_self_scan_path, self_payload)

    report_sha = _sha256_file(privacy_report_path)
    self_sha = _sha256_file(privacy_self_scan_path)
    _finalize_install_privacy_digests(
        install_report_path,
        privacy_report_sha256=report_sha,
        privacy_self_scan_sha256=self_sha,
    )
    return PrivacyEvidenceBinding(
        privacy_report_path=privacy_report_path.name,
        privacy_self_scan_path=privacy_self_scan_path.name,
        privacy_report_sha256=report_sha,
        privacy_self_scan_sha256=self_sha,
        candidate_commit=candidate_commit,
        clone_commit=clone_commit,
        clean=True,
        findings_count=0,
        scan_errors_count=0,
        scopes_covered=REQUIRED_SCOPES,
        digest_order=DIGEST_ORDER,
    )


def verify_privacy_binding(  # noqa: C901, PLR0913
    binding: PrivacyEvidenceBinding,
    *,
    report_dir: Path,
    install_report_path: Path,
    candidate_commit: str,
    clone_commit: str,
    run_root: Path,
    version: str,
    codex_cache: Path,
    claude_cache: Path,
) -> list[str]:
    report_path = report_dir / binding.privacy_report_path
    self_scan_path = report_dir / binding.privacy_self_scan_path
    if not report_path.is_file():
        return ["privacy_report_missing"]
    if not self_scan_path.is_file():
        return ["privacy_self_scan_missing"]
    if _sha256_file(report_path) != binding.privacy_report_sha256:
        return ["privacy_report_digest_mismatch"]
    if _sha256_file(self_scan_path) != binding.privacy_self_scan_sha256:
        return ["privacy_self_scan_digest_mismatch"]
    try:
        report = _load_object(report_path)
        self_scan = _load_object(self_scan_path)
    except ValueError:
        return ["privacy_payload_invalid"]
    errors = _meta_errors(
        report,
        self_scan,
        candidate_commit=candidate_commit,
        clone_commit=clone_commit,
    )
    if binding.candidate_commit != candidate_commit or binding.clone_commit != clone_commit:
        errors.append("privacy_binding_commit_mismatch")
    if binding.digest_order != DIGEST_ORDER:
        errors.append("privacy_digest_order_invalid")
    if set(binding.scopes_covered) != set(REQUIRED_SCOPES):
        errors.append("privacy_binding_scopes_incomplete")
    expected = derive_existing_privacy_targets(
        report_dir=report_dir,
        run_root=run_root,
        version=version,
        codex_cache=codex_cache,
        claude_cache=claude_cache,
        include_privacy_outputs=True,
        include_verify=True,
    )
    errors.extend(
        _scope_membership_errors(
            report,
            expected,
            include_privacy_outputs=True,
            run_root=run_root,
        ),
    )
    # Independent rescan of exact existing targets (no missing soft-allow).
    rescan_targets = derive_existing_privacy_targets(
        report_dir=report_dir,
        run_root=run_root,
        version=version,
        codex_cache=codex_cache,
        claude_cache=claude_cache,
        include_privacy_outputs=False,
        include_verify=True,
    )
    findings, scan_errors = _scan_all_targets(
        rescan_targets,
        run_root=run_root,
        install_report_path=install_report_path,
    )
    if findings:
        errors.append("privacy_rescan_findings")
    if scan_errors:
        errors.append("privacy_rescan_errors")
    self_findings, self_errors = _self_scan(
        privacy_report_path=report_path,
        install_report_path=install_report_path,
        run_root=run_root,
    )
    if self_findings:
        errors.append("privacy_self_rescan_findings")
    if self_errors:
        errors.append("privacy_self_rescan_errors")
    errors.extend(coverage_union_errors(report, self_scan, report_dir=report_dir))
    return errors


def derive_existing_privacy_targets(  # noqa: PLR0913
    *,
    report_dir: Path,
    run_root: Path,
    version: str,
    codex_cache: Path,
    claude_cache: Path,
    include_privacy_outputs: bool,
    include_verify: bool,
) -> dict[str, list[str]]:
    """Exact existing targets only — never invent nonexistent paths."""
    _ = version
    root = run_root.resolve()
    manual = _existing_manual_json_paths(
        report_dir,
        include_privacy_outputs=include_privacy_outputs,
        include_verify=include_verify,
    )
    caches = [str(codex_cache.resolve()), str(claude_cache.resolve())]
    for cache in caches:
        if not Path(cache).exists():
            msg = f"cache_missing:{cache}"
            raise PrivacyPipelineError(msg)
    auth_root = root / "home" / ".saxo-bank-mcp-auth"
    auth_copies = _existing_auth_copy_paths(auth_root)
    client_state = _retained_client_state_entries(
        root,
        exclude_prefixes=(
            Path(caches[0]),
            Path(caches[1]),
            auth_root,
        ),
    )
    public_clone = _public_clone_existing(root / "source-clone")
    return {
        "manual_json": sorted(manual),
        "installed_caches": caches,
        "auth_copies": auth_copies,
        "client_state": client_state,
        "todo13_public_clone": public_clone,
    }


def _existing_manual_json_paths(
    report_dir: Path,
    *,
    include_privacy_outputs: bool,
    include_verify: bool,
) -> list[str]:
    """Every existing report_dir/*.json with fail-closed non-regular rejection."""
    if not report_dir.is_dir():
        return []
    manual: list[str] = []
    for path in sorted(report_dir.iterdir(), key=lambda item: item.name):
        if not path.name.endswith(".json"):
            continue
        kind = _lex_kind(path)
        # Never soft-skip: symlink / special / non-file manuals fail closed.
        if kind == "symlink":
            msg = f"manual_json_symlink_rejected:{path.name}"
            raise PrivacyPipelineError(msg)
        if kind == "special":
            msg = f"manual_json_special_rejected:{path.name}"
            raise PrivacyPipelineError(msg)
        if kind == "dir":
            msg = f"manual_json_directory_rejected:{path.name}"
            raise PrivacyPipelineError(msg)
        if kind != "file":
            msg = f"manual_json_not_regular:{path.name}"
            raise PrivacyPipelineError(msg)
        name = path.name
        if name in _PRIVACY_OUTPUT_NAMES and not include_privacy_outputs:
            continue
        if name == _VERIFY_MANUAL_NAME and not include_verify:
            continue
        manual.append(str(path.resolve(strict=False)))
    return manual


def _existing_auth_copy_paths(auth_root: Path) -> list[str]:
    """Every existing auth entry (file/symlink/special); never soft-skip non-files."""
    if not auth_root.is_dir():
        return []
    found: list[str] = []
    for path in auth_root.rglob("*"):
        kind = _lex_kind(path)
        if kind == "dir":
            continue
        if kind == "missing":
            continue
        # List the path as-is; scanning fail-closes on specials / dangling links.
        found.append(_lex_path_key(path))
    return sorted(set(found))


def canonical_install_text_for_privacy_scan(
    install_report_path: Path,
    *,
    run_root: Path,
) -> str:
    """Normalize only the two fixed privacy digests and exact run_root prefix in memory."""
    text = install_report_path.read_text(encoding="utf-8")
    try:
        loaded: object = json.loads(text)
        payload = _JSON_OBJECT.validate_python(loaded)
    except (json.JSONDecodeError, ValidationError):
        return _normalize_run_root_prefix(text, run_root)
    privacy_raw = payload.get("privacy")
    if isinstance(privacy_raw, dict):
        try:
            privacy_map = _JSON_OBJECT.validate_python(privacy_raw)
        except ValidationError:
            return _normalize_run_root_prefix(text, run_root)
        for key in _PRIVACY_DIGEST_KEYS:
            if key in privacy_map:
                privacy_map[key] = PRIVACY_DIGEST_ZERO
        payload["privacy"] = privacy_map
    rendered = json.dumps(payload, sort_keys=True) + "\n"
    return _normalize_run_root_prefix(rendered, run_root)


def _scan_all_targets(
    targets: dict[str, list[str]],
    *,
    run_root: Path,
    install_report_path: Path,
) -> tuple[list[dict[str, JsonValue]], list[dict[str, JsonValue]]]:
    """Scan every existing target. Missing paths fail. Run_root prefix normalized in memory only."""
    findings: list[dict[str, JsonValue]] = []
    scan_errors: list[dict[str, JsonValue]] = []
    install_resolved = install_report_path.resolve()
    root_resolved = run_root.resolve()
    for paths in targets.values():
        for raw in paths:
            path = Path(raw)
            kind = _lex_kind(path)
            if kind == "missing":
                scan_errors.append({"path": raw, "error": "missing_path"})
                continue
            if kind == "special":
                scan_errors.append({"path": raw, "error": "special_node"})
                continue
            if kind == "symlink":
                symlink_errors = _symlink_scan_errors(path, retained_roots=(root_resolved,))
                if symlink_errors:
                    scan_errors.extend(symlink_errors)
                    continue
                if path.is_dir():
                    # Do not descend through directory symlinks.
                    scan_errors.append({"path": raw, "error": "symlink_directory"})
                    continue
            if kind == "dir":
                file_findings, file_errors = scan_directory_normalized(
                    path,
                    run_root=run_root,
                    retained_roots=(root_resolved, path.resolve()),
                )
            elif path.resolve() == install_resolved:
                text = canonical_install_text_for_privacy_scan(
                    install_report_path,
                    run_root=run_root,
                )
                file_findings, file_errors = scan_secret_text(raw, text)
            else:
                file_findings, file_errors = _scan_file_normalized(path, run_root=run_root)
            findings.extend(file_findings)
            scan_errors.extend(file_errors)
    return findings, scan_errors


def scan_directory_normalized(
    directory: Path,
    *,
    run_root: Path,
    retained_roots: tuple[Path, ...] | None = None,
) -> tuple[list[dict[str, JsonValue]], list[dict[str, JsonValue]]]:
    findings: list[dict[str, JsonValue]] = []
    scan_errors: list[dict[str, JsonValue]] = []
    roots = retained_roots or (run_root.resolve(), directory.resolve())
    for path in directory.rglob("*"):
        kind = _lex_kind(path)
        if kind == "dir":
            continue
        if kind == "missing":
            scan_errors.append({"path": str(path), "error": "missing_path"})
            continue
        if kind == "special":
            # FIFO / socket / device under a scanned directory: fail closed.
            scan_errors.append({"path": str(path), "error": "special_node"})
            continue
        if kind == "symlink":
            symlink_errors = _symlink_scan_errors(path, retained_roots=roots)
            if symlink_errors:
                scan_errors.extend(symlink_errors)
                continue
            if path.is_dir():
                scan_errors.append({"path": str(path), "error": "symlink_directory"})
                continue
        file_findings, file_errors = _scan_file_normalized(path, run_root=run_root)
        findings.extend(file_findings)
        scan_errors.extend(file_errors)
    return findings, scan_errors


def _scan_file_normalized(
    path: Path,
    *,
    run_root: Path,
) -> tuple[list[dict[str, JsonValue]], list[dict[str, JsonValue]]]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return [], [{"path": str(path), "error": type(exc).__name__}]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # Binary files: scan via path API without text normalization.
        return scan_secret_paths([str(path)])
    text = _normalize_run_root_prefix(text, run_root)
    return scan_secret_text(str(path), text)


def _symlink_scan_errors(
    path: Path,
    *,
    retained_roots: tuple[Path, ...],
) -> list[dict[str, JsonValue]]:
    """Fail closed on dangling links and links that escape retained roots."""
    if not path.exists():
        return [{"path": str(path), "error": "dangling_symlink"}]
    try:
        target = path.resolve(strict=True)
    except OSError as exc:
        return [{"path": str(path), "error": type(exc).__name__}]
    if not any(_is_within_root(target, root) for root in retained_roots):
        return [{"path": str(path), "error": "symlink_escapes_root"}]
    return []


def _self_scan(
    *,
    privacy_report_path: Path,
    install_report_path: Path,
    run_root: Path,
) -> tuple[list[dict[str, JsonValue]], list[dict[str, JsonValue]]]:
    findings: list[dict[str, JsonValue]] = []
    scan_errors: list[dict[str, JsonValue]] = []
    if not privacy_report_path.is_file():
        return findings, [{"path": str(privacy_report_path), "error": "missing_path"}]
    pf, pe = _scan_file_normalized(privacy_report_path, run_root=run_root)
    findings.extend(pf)
    scan_errors.extend(pe)
    text = canonical_install_text_for_privacy_scan(install_report_path, run_root=run_root)
    sf, se = scan_secret_text(str(install_report_path.resolve()), text)
    findings.extend(sf)
    scan_errors.extend(se)
    return findings, scan_errors


def _finalize_install_privacy_digests(
    install_report_path: Path,
    *,
    privacy_report_sha256: str,
    privacy_self_scan_sha256: str,
) -> None:
    try:
        payload = _JSON_OBJECT.validate_python(
            json.loads(install_report_path.read_text(encoding="utf-8")),
        )
    except (json.JSONDecodeError, ValidationError) as exc:
        raise PrivacyPipelineError("install_report_not_object") from exc
    privacy_raw = payload.get("privacy")
    if not isinstance(privacy_raw, dict):
        raise PrivacyPipelineError("privacy_section_missing")
    try:
        privacy = _JSON_OBJECT.validate_python(privacy_raw)
    except ValidationError as exc:
        raise PrivacyPipelineError("privacy_section_invalid") from exc
    if privacy.get("privacy_report_sha256") != PRIVACY_DIGEST_ZERO:
        raise PrivacyPipelineError("privacy_report_digest_not_provisional")
    if privacy.get("privacy_self_scan_sha256") != PRIVACY_DIGEST_ZERO:
        raise PrivacyPipelineError("privacy_self_scan_digest_not_provisional")
    privacy["privacy_report_sha256"] = privacy_report_sha256
    privacy["privacy_self_scan_sha256"] = privacy_self_scan_sha256
    payload["privacy"] = privacy
    _atomic_write_json(install_report_path, payload)


def _scope_membership_errors(
    report: dict[str, JsonValue],
    expected: dict[str, list[str]],
    *,
    include_privacy_outputs: bool,
    run_root: Path,
) -> list[str]:
    _ = include_privacy_outputs
    errors: list[str] = []
    scope = report.get("scope")
    if not isinstance(scope, dict):
        return ["privacy_scope_missing"]
    for name in REQUIRED_SCOPES:
        raw = scope.get(name)
        if not isinstance(raw, list):
            errors.append(f"privacy_scope_empty:{name}")
            continue
        reported = {
            _publicize_path(str(item), run_root=run_root)
            for item in raw
            if isinstance(item, str)
        }
        want = {_publicize_path(item, run_root=run_root) for item in expected[name]}
        if name == "manual_json":
            # Produce-time manual set may exclude later manuals (privacy/verify).
            if not reported.issubset(want):
                errors.append("privacy_scope_mismatch:manual_json")
            if not any(item.endswith("install.json") for item in reported):
                errors.append("privacy_scope_missing_install")
        elif reported != want:
            errors.append(f"privacy_scope_mismatch:{name}")
    return errors


def coverage_union_errors(
    report: dict[str, JsonValue],
    self_scan: dict[str, JsonValue],
    *,
    report_dir: Path,
) -> list[str]:
    """Union of privacy-report scope and self-scan must cover required manuals except self-scan.

    Path keys are report_dir-bound only. Bare basenames never resolve against process cwd.
    verify.json is excluded: it is secret-scanned independently at verify publish time.
    privacy-self-scan.json is excluded: it cannot cover itself.
    """
    covered: set[str] = set()
    scope = report.get("scope")
    if isinstance(scope, dict):
        manual = scope.get("manual_json")
        if isinstance(manual, list):
            covered.update(
                _report_dir_bound_key(str(item), report_dir=report_dir)
                for item in manual
                if isinstance(item, str)
            )
    self_scope = self_scan.get("scope")
    if isinstance(self_scope, dict):
        for key in ("privacy_report", "install_report_canonical"):
            value = self_scope.get(key)
            if isinstance(value, list):
                covered.update(
                    _report_dir_bound_key(str(item), report_dir=report_dir)
                    for item in value
                    if isinstance(item, str)
                )
    required = _required_manual_coverage_keys(report_dir)
    missing = required - covered
    if missing:
        return ["privacy_manual_coverage_incomplete"]
    return []


def _required_manual_coverage_keys(report_dir: Path) -> set[str]:
    """Existing regular report_dir manuals that privacy union must cover."""
    if not report_dir.is_dir():
        return set()
    required: set[str] = set()
    for path in report_dir.iterdir():
        if not path.name.endswith(".json"):
            continue
        if path.name in {"privacy-self-scan.json", _VERIFY_MANUAL_NAME}:
            continue
        if _lex_kind(path) != "file":
            # Non-regular manuals are rejected at derive time; skip here for coverage.
            continue
        required.add(_report_dir_bound_key(path.name, report_dir=report_dir))
    return required


def _report_dir_bound_key(value: str, *, report_dir: Path) -> str:
    """Map a reported manual path/label to a report_dir-bound absolute key.

    Never resolves bare basenames against process cwd.
    """
    report_resolved = report_dir.resolve()
    if value.startswith("$RUN_ROOT"):
        return value
    raw = Path(value)
    if raw.is_absolute():
        try:
            resolved = raw.resolve(strict=False)
        except OSError:
            resolved = raw
        # Absolute under report_dir stays absolute; else bind basename to report_dir.
        if _is_within_root(resolved, report_resolved) or resolved == report_resolved:
            return str(resolved)
        return str((report_resolved / resolved.name).resolve(strict=False))
    # Relative / basename labels: always join under report_dir (structural, not cwd).
    return str((report_resolved / Path(value).name).resolve(strict=False))


def _meta_errors(
    report: dict[str, JsonValue],
    self_scan: dict[str, JsonValue],
    *,
    candidate_commit: str,
    clone_commit: str,
) -> list[str]:
    errors: list[str] = []
    for label, payload in (("privacy_report", report), ("privacy_self_scan", self_scan)):
        if payload.get("status") != "passed" or payload.get("clean") is not True:
            errors.append(f"{label}_not_clean")
        findings = payload.get("findings")
        scan_errors = payload.get("scan_errors")
        if not isinstance(findings, list) or findings:
            errors.append(f"{label}_findings_present")
        if not isinstance(scan_errors, list) or scan_errors:
            errors.append(f"{label}_scan_errors_present")
        if payload.get("candidate_commit") != candidate_commit:
            errors.append(f"{label}_candidate_commit_mismatch")
        if payload.get("clone_commit") != clone_commit:
            errors.append(f"{label}_clone_commit_mismatch")
    return errors


def _retained_client_state_entries(
    run_root: Path,
    *,
    exclude_prefixes: tuple[Path, ...],
) -> list[str]:
    """Every existing retained regular/symlink/special state entry outside cache/auth."""
    roots = (run_root / "codex-home", run_root / "home", run_root / "claude-home")
    found: list[str] = []
    exclude = tuple(prefix.resolve() for prefix in exclude_prefixes if prefix.exists())
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            kind = _lex_kind(path)
            if kind in {"dir", "missing"}:
                continue
            # Include files, symlinks, and special nodes without soft-skip on OSError.
            key = _lex_path_key(path)
            try:
                resolved_for_exclude = (
                    path.resolve(strict=False) if kind != "symlink" else path
                )
            except OSError:
                resolved_for_exclude = path
            if any(
                _is_within_root(resolved_for_exclude, prefix) or resolved_for_exclude == prefix
                for prefix in exclude
            ):
                continue
            found.append(key)
    return sorted(set(found))


def _lex_kind(path: Path) -> LexKind:
    """Classify path without following symlinks. Never soft-skips specials."""
    if not os.path.lexists(path):
        return "missing"
    if path.is_symlink():
        return "symlink"
    try:
        mode = path.lstat().st_mode
    except OSError:
        return "special"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return "special"


def _lex_path_key(path: Path) -> str:
    """Stable string key for a lexists path without requiring resolve success."""
    try:
        if path.is_symlink() or not path.exists():
            return str(path)
        return str(path.resolve(strict=False))
    except OSError:
        return str(path)


def _is_within_root(path: Path, root: Path) -> bool:
    try:
        return path == root or path.is_relative_to(root)
    except (OSError, ValueError):
        return False


def _public_clone_existing(clone: Path) -> list[str]:
    if not clone.is_dir():
        return []
    paths: list[str] = []
    public_dirs = tuple(
        name for name in PUBLIC_SECRET_SCAN_PATHS if not Path(name).suffix and "/" not in name
    )
    for relative in (
        "README.md",
        "pyproject.toml",
        "uv.lock",
        ".gitignore",
        ".mcp.json",
        *public_dirs,
        "data/saxo",
    ):
        path = clone / relative
        if path.exists():
            paths.append(str(path.resolve()))
    return sorted(paths)


def _count_scanned(targets: dict[str, list[str]]) -> int:
    return sum(len(paths) for paths in targets.values())


def _normalize_run_root_prefix(text: str, run_root: Path) -> str:
    resolved = str(run_root.resolve())
    # macOS may surface both /var/folders and /private/var/folders forms.
    variants = {resolved}
    if resolved.startswith("/private"):
        variants.add(resolved.removeprefix("/private"))
    else:
        variants.add("/private" + resolved)
    scrubbed = text
    for absolute in sorted(variants, key=len, reverse=True):
        scrubbed = scrubbed.replace(absolute, "$RUN_ROOT")
    return scrubbed


def _publicize_path(value: str, *, run_root: Path) -> str:
    if value.startswith("$RUN_ROOT"):
        return value
    path = Path(value)
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return value
    root = run_root.resolve()
    if resolved == root:
        return "$RUN_ROOT"
    if resolved.is_relative_to(root):
        return f"$RUN_ROOT/{resolved.relative_to(root)}"
    # Manual JSONs live beside install, outside run_root: keep basename labels.
    return resolved.name


def _load_object(path: Path) -> dict[str, JsonValue]:
    try:
        return _JSON_OBJECT.validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        msg = f"privacy_payload_invalid:{path.name}"
        raise ValueError(msg) from exc


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, JsonValue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
