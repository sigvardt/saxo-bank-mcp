from __future__ import annotations

import json
from pathlib import Path
from typing import Final, cast

import yaml

from saxo_bank_mcp.agent_skill_static_gate_constants import (
    EXPECTED_SKILLS,
    FRONTMATTER_PATTERN,
    GRANT_WILDCARD_PATTERN,
)

MANIFEST_RELATIVES: Final[tuple[str, ...]] = (
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    ".agents/plugins/marketplace.json",
    ".mcp.json",
)
GRANT_KEY_NAMES: Final = frozenset(
    {
        "allowedTools",
        "allowed-tools",
        "permissions",
        "exact_tool_grants",
    },
)


def eval_wildcard_findings(root: Path) -> tuple[str, ...]:
    findings: list[str] = []
    eval_root = root / "evals"
    if not eval_root.is_dir():
        return ()
    for path in sorted(eval_root.rglob("case.yaml")):
        payload, error = _load_mapping(path)
        if error is not None:
            findings.append(error)
            continue
        if payload is None:
            continue
        grants = payload.get("exact_tool_grants")
        if grants is None:
            continue
        if _values_have_wildcard(grants) or _mapping_has_wildcard_grant(grants):
            findings.append("wildcard_grant")
    return tuple(dict.fromkeys(findings))


def skill_frontmatter_wildcard_findings(root: Path) -> tuple[str, ...]:
    findings: list[str] = []
    for skill in EXPECTED_SKILLS:
        path = root / "skills" / skill / "SKILL.md"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        match = FRONTMATTER_PATTERN.match(text)
        if match is None:
            continue
        try:
            loaded = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            findings.append("bad_skill_frontmatter")
            continue
        if not isinstance(loaded, dict):
            findings.append("bad_skill_frontmatter")
            continue
        typed = cast("dict[str, object]", loaded)
        findings.extend(
            "wildcard_grant"
            for key in GRANT_KEY_NAMES
            if key in typed and _values_have_wildcard(typed[key])
        )
    return tuple(dict.fromkeys(findings))


def manifest_wildcard_findings(root: Path) -> tuple[str, ...]:
    findings: list[str] = []
    for relative in MANIFEST_RELATIVES:
        path = root / relative
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            findings.append("malformed_manifest_json")
            continue
        if _decoded_tree_has_wildcard_grant(payload):
            findings.append("wildcard_grant")
    return tuple(dict.fromkeys(findings))


def _decoded_tree_has_wildcard_grant(value: object) -> bool:
    if isinstance(value, dict):
        typed = cast("dict[str, object]", value)
        for key, item in typed.items():
            if key in GRANT_KEY_NAMES and (
                _values_have_wildcard(item) or _mapping_has_wildcard_grant(item)
            ):
                return True
            if _decoded_tree_has_wildcard_grant(item):
                return True
        return False
    if isinstance(value, list):
        return any(_decoded_tree_has_wildcard_grant(item) for item in cast("list[object]", value))
    return False


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


def _load_mapping(path: Path) -> tuple[dict[str, object] | None, str | None]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None, "malformed_eval_yaml"
    if not isinstance(loaded, dict):
        return None, None
    return cast("dict[str, object]", loaded), None
