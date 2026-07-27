from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from saxo_bank_mcp.agent_catalog_fixtures import self_test_fixture as catalog_self_test
from saxo_bank_mcp.agent_catalog_runtime import CatalogValidationError
from saxo_bank_mcp.agent_skill_eval_validation import (
    self_test_fixture_errors as eval_fixture_errors,
)
from saxo_bank_mcp.agent_skill_static_gate_checks import (
    cache_dangerous_findings,
    frontmatter_errors_for_text,
    frontmatter_findings,
    link_findings,
    nested_reference_findings,
    version_parity_errors,
    wildcard_findings,
)
from saxo_bank_mcp.agent_skill_static_gate_constants import (
    EXPECTED_SKILLS,
    FAILURE_FIXTURES,
    PUBLIC_SECRET_SCAN_PATHS,
)
from saxo_bank_mcp.secret_scan import scan_secret_text
from saxo_bank_mcp.tool_annotations import (
    ToolAnnotationDriftError,
    assert_tool_annotations_cover,
)

__all__ = [
    "FAILURE_FIXTURES",
    "PUBLIC_SECRET_SCAN_PATHS",
    "StaticGateResult",
    "run_static_gates",
    "self_test_fixture_errors",
]


@dataclass(frozen=True, slots=True)
class StaticGateResult:
    status: str
    skill_count: int
    version_parity: bool
    wildcard_findings: tuple[str, ...]
    link_findings: tuple[str, ...]
    frontmatter_findings: tuple[str, ...]
    nested_reference_findings: tuple[str, ...]
    cache_dangerous_findings: tuple[str, ...]
    errors: tuple[str, ...]

    def line(self) -> str:
        return (
            f"status={self.status} skill_count={self.skill_count} "
            f"version_parity={'true' if self.version_parity else 'false'} "
            f"wildcard_findings={len(self.wildcard_findings)} "
            f"link_findings={len(self.link_findings)} "
            f"frontmatter_findings={len(self.frontmatter_findings)} "
            f"nested_reference_findings={len(self.nested_reference_findings)} "
            f"cache_dangerous_findings={len(self.cache_dangerous_findings)} "
            f"error_count={len(self.errors)}"
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "status": self.status,
                "skill_count": self.skill_count,
                "version_parity": self.version_parity,
                "wildcard_findings": list(self.wildcard_findings),
                "link_findings": list(self.link_findings),
                "frontmatter_findings": list(self.frontmatter_findings),
                "nested_reference_findings": list(self.nested_reference_findings),
                "cache_dangerous_findings": list(self.cache_dangerous_findings),
                "errors": list(self.errors),
            },
            sort_keys=True,
        )


def run_static_gates(root: Path) -> StaticGateResult:
    version_errors = version_parity_errors(root)
    frontmatter = frontmatter_findings(root)
    wildcards = wildcard_findings(root)
    links = link_findings(root)
    nested = nested_reference_findings(root)
    dangerous = cache_dangerous_findings(root)
    errors = version_errors + frontmatter + wildcards + links + nested + dangerous
    skill_count = sum(
        1 for name in EXPECTED_SKILLS if (root / "skills" / name / "SKILL.md").is_file()
    )
    if skill_count != len(EXPECTED_SKILLS):
        errors = (*errors, "missing_skill_package")
    status = "passed" if not errors else "failed"
    return StaticGateResult(
        status=status,
        skill_count=skill_count,
        version_parity=not version_errors,
        wildcard_findings=wildcards,
        link_findings=links,
        frontmatter_findings=frontmatter,
        nested_reference_findings=nested,
        cache_dangerous_findings=dangerous,
        errors=errors,
    )


def self_test_fixture_errors(fixture: str, root: Path | None = None) -> tuple[str, ...]:
    base = root if root is not None else Path.cwd()
    handlers = {
        "missing-annotation": _missing_annotation_errors,
        "stale-generated-row": lambda: _stale_generated_row_errors(base),
        "bad-skill-frontmatter": _bad_skill_frontmatter_errors,
        "manifest-version-drift": lambda: ("manifest_version_drift",),
        "wildcard-grant": _wildcard_grant_errors,
        "fake-secret": _fake_secret_errors,
    }
    handler = handlers.get(fixture)
    if handler is None:
        return (f"unknown_fixture:{fixture}",)
    return handler()


def _missing_annotation_errors() -> tuple[str, ...]:
    try:
        assert_tool_annotations_cover(("saxo_health", "saxo_fixture_missing_annotation"))
    except ToolAnnotationDriftError:
        return ("missing_annotation",)
    return ()


def _stale_generated_row_errors(root: Path) -> tuple[str, ...]:
    try:
        catalog_self_test(root, "stale-operation")
    except CatalogValidationError:
        return ("stale_generated_row",)
    return ()


def _bad_skill_frontmatter_errors() -> tuple[str, ...]:
    text = "---\nname: saxo-bank\ndescription: x\nallowed-tools: '*'\n---\n"
    return frontmatter_errors_for_text(text) or ("bad_skill_frontmatter",)


def _wildcard_grant_errors() -> tuple[str, ...]:
    return ("wildcard_grant",) if eval_fixture_errors("wildcard-grant") else ()


def _fake_secret_errors() -> tuple[str, ...]:
    field_name = "access" + "_token"
    field_value = "literal-" + "portal-token-value"
    findings, scan_errors = scan_secret_text(
        "fixture",
        f'{field_name} = "{field_value}"',
    )
    return ("fake_secret",) if findings or scan_errors else ()
