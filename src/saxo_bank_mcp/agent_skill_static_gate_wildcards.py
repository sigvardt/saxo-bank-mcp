from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import yaml

from saxo_bank_mcp.agent_skill_static_gate_constants import (
    EXPECTED_SKILLS,
    FRONTMATTER_PATTERN,
    GRANT_WILDCARD_PATTERN,
)


def eval_wildcard_findings(root: Path) -> tuple[str, ...]:
    findings: list[str] = []
    for path in sorted((root / "evals").rglob("case.yaml")):
        payload = _load_mapping(path)
        if payload is None:
            continue
        grants = payload.get("exact_tool_grants")
        if not isinstance(grants, dict):
            continue
        grant_map = cast("dict[str, object]", grants)
        findings.extend(
            "wildcard_grant" for values in grant_map.values() if _values_have_wildcard(values)
        )
    return tuple(findings)


def skill_frontmatter_wildcard_findings(root: Path) -> tuple[str, ...]:
    findings: list[str] = []
    for skill in EXPECTED_SKILLS:
        path = root / "skills" / skill / "SKILL.md"
        if not path.is_file():
            continue
        match = FRONTMATTER_PATTERN.match(path.read_text(encoding="utf-8"))
        if match is None:
            continue
        loaded = yaml.safe_load(match.group(1))
        if not isinstance(loaded, dict):
            continue
        typed = cast("dict[str, object]", loaded)
        allowed = typed.get("allowed-tools") or typed.get("allowedTools")
        if _values_have_wildcard(allowed):
            findings.append("wildcard_grant")
    return tuple(findings)


def manifest_wildcard_findings(root: Path) -> tuple[str, ...]:
    findings: list[str] = []
    for relative in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        path = root / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        has_grant_field = (
            '"allowedTools"' in text or '"allowed-tools"' in text or '"permissions"' in text
        )
        if has_grant_field and _mapping_has_wildcard_grant(json.loads(text)):
            findings.append("wildcard_grant")
    return tuple(findings)


def _values_have_wildcard(allowed: object) -> bool:
    if isinstance(allowed, str):
        return GRANT_WILDCARD_PATTERN.search(allowed) is not None
    if isinstance(allowed, list):
        items = cast("list[object]", allowed)
        return any(isinstance(item, str) and GRANT_WILDCARD_PATTERN.search(item) for item in items)
    return False


def _mapping_has_wildcard_grant(value: object) -> bool:
    if isinstance(value, str):
        return GRANT_WILDCARD_PATTERN.search(value) is not None
    if isinstance(value, list):
        items = cast("list[object]", value)
        return any(_mapping_has_wildcard_grant(item) for item in items)
    if isinstance(value, dict):
        typed = cast("dict[str, object]", value)
        return any(_mapping_has_wildcard_grant(item) for item in typed.values())
    return False


def _load_mapping(path: Path) -> dict[str, object] | None:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return None
    return cast("dict[str, object]", loaded)


