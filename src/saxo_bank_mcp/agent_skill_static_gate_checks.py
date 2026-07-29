from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path
from typing import cast

import yaml

from saxo_bank_mcp.agent_skill_static_gate_constants import (
    CACHE_DANGEROUS_NAMES,
    EXPECTED_SKILLS,
    FRONTMATTER_PATTERN,
    HTTP_LINK_PATTERN,
    JSON_OBJECT_ADAPTER,
    OFFICIAL_LINK_INVENTORY_PATH,
    PORTABLE_FRONTMATTER_KEYS,
    PUBLIC_SECRET_SCAN_PATHS,
    SCAN_SUFFIXES,
    VERSION_PATHS,
    JsonObject,
)
from saxo_bank_mcp.agent_skill_static_gate_wildcards import (
    eval_wildcard_findings,
    manifest_wildcard_findings,
    skill_frontmatter_wildcard_findings,
)


class TrackedPublicPathError(Exception):
    """Raised when tracked public-path enumeration fails closed."""


def version_parity_errors(root: Path) -> tuple[str, ...]:
    try:
        project_version = project_version_of(root)
    except (OSError, TypeError, ValueError, tomllib.TOMLDecodeError):
        return ("manifest_version_drift",)
    errors: list[str] = []
    for relative in VERSION_PATHS[1:]:
        path = root / relative
        try:
            payload = json_object(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            errors.append("manifest_version_drift")
            continue
        versions = versions_from_payload(payload)
        if not versions or any(version != project_version for version in versions):
            errors.append("manifest_version_drift")
    return tuple(dict.fromkeys(errors))


def versions_from_payload(payload: JsonObject) -> tuple[str, ...]:
    versions: list[str] = []
    raw = payload.get("version")
    if isinstance(raw, str):
        versions.append(raw)
    plugins = payload.get("plugins")
    if isinstance(plugins, list):
        for entry in plugins:
            if not isinstance(entry, dict):
                continue
            version = entry.get("version")
            if isinstance(version, str):
                versions.append(version)
    return tuple(versions)


def frontmatter_findings(root: Path) -> tuple[str, ...]:
    findings: list[str] = []
    for skill in EXPECTED_SKILLS:
        path = root / "skills" / skill / "SKILL.md"
        if not path.is_file():
            findings.append("missing_skill_package")
            continue
        findings.extend(frontmatter_errors_for_text(path.read_text(encoding="utf-8")))
    return tuple(dict.fromkeys(findings))


def frontmatter_errors_for_text(text: str) -> tuple[str, ...]:
    match = FRONTMATTER_PATTERN.match(text)
    if match is None:
        return ("bad_skill_frontmatter",)
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return ("bad_skill_frontmatter",)
    if not _portable_frontmatter(loaded):
        return ("bad_skill_frontmatter",)
    return ()


def wildcard_findings(root: Path) -> tuple[str, ...]:
    findings = [
        *eval_wildcard_findings(root),
        *skill_frontmatter_wildcard_findings(root),
        *manifest_wildcard_findings(root),
    ]
    return tuple(dict.fromkeys(findings))


def link_findings(root: Path) -> tuple[str, ...]:
    try:
        allowed = _official_link_inventory(root)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return ("unofficial_link",)
    findings = [
        "unofficial_link"
        for path in scan_files(root, ("skills",))
        if path.suffix == ".md"
        for link in HTTP_LINK_PATTERN.findall(path.read_text(encoding="utf-8"))
        if link not in allowed
    ]
    return tuple(dict.fromkeys(findings))


def nested_reference_findings(root: Path) -> tuple[str, ...]:
    findings = [
        "nested_reference"
        for skill in EXPECTED_SKILLS
        for path in _reference_paths(root, skill)
        if _is_nested_reference(root, skill, path)
    ]
    return tuple(dict.fromkeys(findings))


def cache_dangerous_findings(root: Path) -> tuple[str, ...]:
    try:
        tracked = _tracked_public_paths(root)
    except TrackedPublicPathError:
        return ("cache_public_path_scan_error",)
    findings = [
        "cache_dangerous_public_file"
        for relative in tracked
        if Path(relative).name in CACHE_DANGEROUS_NAMES
    ]
    return tuple(dict.fromkeys(findings))


def project_version_of(root: Path) -> str:
    payload = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = payload.get("project")
    if not isinstance(project, dict):
        raise TypeError("pyproject.toml missing project table")
    typed_project = cast("dict[str, object]", project)
    version = typed_project.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("pyproject.toml missing project.version")
    return version


def json_object(path: Path) -> JsonObject:
    return JSON_OBJECT_ADAPTER.validate_json(path.read_text(encoding="utf-8"))


def scan_files(root: Path, relatives: tuple[str, ...]) -> tuple[Path, ...]:
    files: list[Path] = []
    for relative in relatives:
        base = root / relative
        if base.is_file():
            files.append(base)
            continue
        if base.is_dir():
            files.extend(
                path
                for path in sorted(base.rglob("*"))
                if path.is_file() and path.suffix in SCAN_SUFFIXES
            )
    return tuple(files)


def _official_link_inventory(root: Path) -> frozenset[str]:
    raw = json.loads((root / OFFICIAL_LINK_INVENTORY_PATH).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("official link inventory must be an object")
    payload = cast("dict[str, object]", raw)
    urls = payload.get("urls")
    if not isinstance(urls, list):
        raise TypeError("official link inventory urls must be a string list")
    items = cast("list[object]", urls)
    if not all(isinstance(item, str) for item in items):
        raise TypeError("official link inventory urls must be a string list")
    return frozenset(cast("list[str]", items))


def _tracked_public_paths(root: Path) -> tuple[str, ...]:
    try:
        completed = subprocess.run(
            ["/usr/bin/env", "git", "-C", str(root), "ls-files", "-z"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise TrackedPublicPathError from error
    if completed.returncode != 0:
        raise TrackedPublicPathError
    tracked = [item for item in completed.stdout.split("\0") if item]
    public_dirs = tuple(
        path for path in PUBLIC_SECRET_SCAN_PATHS if not Path(path).suffix and "/" not in path
    )
    public_files = frozenset(path for path in PUBLIC_SECRET_SCAN_PATHS if Path(path).suffix)
    selected: list[str] = []
    for relative in tracked:
        if relative in public_files or relative in PUBLIC_SECRET_SCAN_PATHS:
            selected.append(relative)
            continue
        if "/" not in relative:
            selected.append(relative)
            continue
        if any(relative.startswith(f"{directory}/") for directory in public_dirs):
            selected.append(relative)
            continue
        if relative.startswith("data/saxo/"):
            selected.append(relative)
    return tuple(selected)


def _portable_frontmatter(loaded: object) -> bool:
    if not isinstance(loaded, dict):
        return False
    typed = cast("dict[str, object]", loaded)
    keys = frozenset(str(key) for key in typed)
    if keys != PORTABLE_FRONTMATTER_KEYS:
        return False
    name = typed.get("name")
    description = typed.get("description")
    return (
        isinstance(name, str)
        and isinstance(description, str)
        and bool(name.strip())
        and bool(description.strip())
    )


def _reference_paths(root: Path, skill: str) -> tuple[Path, ...]:
    references = root / "skills" / skill / "references"
    if not references.is_dir():
        return ()
    return tuple(references.rglob("*"))


def _is_nested_reference(root: Path, skill: str, path: Path) -> bool:
    references = root / "skills" / skill / "references"
    if path.is_dir():
        return path != references and references in path.parents
    return path.parent != references
