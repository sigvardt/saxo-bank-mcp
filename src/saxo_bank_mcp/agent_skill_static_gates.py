from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from saxo_bank_mcp.agent_skill_static_gate_checks import (
    cache_dangerous_findings,
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
from saxo_bank_mcp.agent_skill_static_gate_fixtures import (
    self_test_fixture_errors as run_self_test_fixture_errors,
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
    return run_self_test_fixture_errors(fixture, root=root)
