from __future__ import annotations

import json
import shutil
import tomllib
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType

from mcp.types import ToolAnnotations

from saxo_bank_mcp.agent_catalog_render import catalog_inputs, rows, set_rows
from saxo_bank_mcp.agent_catalog_runtime import CatalogValidationError
from saxo_bank_mcp.agent_catalog_statuses import emitted_statuses
from saxo_bank_mcp.agent_catalog_validation import CatalogValidationInputs, validate_catalog_sources
from saxo_bank_mcp.agent_skill_static_gate_checks import (
    frontmatter_errors_for_text,
    version_parity_errors,
    wildcard_findings,
)
from saxo_bank_mcp.agent_skill_static_gate_constants import FIXTURE_CANARY, VERSION_PATHS
from saxo_bank_mcp.secret_scan import scan_secret_text
from saxo_bank_mcp.tool_annotations import ToolAnnotationDriftError, assert_tool_annotations_cover


def self_test_fixture_errors(fixture: str, root: Path | None = None) -> tuple[str, ...]:
    base = root if root is not None else Path.cwd()
    canary = FIXTURE_CANARY
    handlers = {
        "missing-annotation": lambda: missing_annotation_errors(canary),
        "stale-generated-row": lambda: stale_generated_row_errors(base, canary),
        "bad-skill-frontmatter": lambda: bad_skill_frontmatter_errors(canary),
        "manifest-version-drift": lambda: manifest_version_drift_errors(base, canary),
        "wildcard-grant": lambda: wildcard_grant_errors(canary),
        "fake-secret": lambda: fake_secret_errors(canary),
    }
    handler = handlers.get(fixture)
    if handler is None:
        return ("unknown_fixture",)
    return handler()


def missing_annotation_errors(canary: str) -> tuple[str, ...]:
    empty_annotations: Mapping[str, ToolAnnotations] = MappingProxyType({})
    # Canary is the malformed missing runtime tool ID supplied to the production validator.
    missing_tool_id = f"saxo_fixture_missing_{canary}"
    try:
        assert_tool_annotations_cover((missing_tool_id,), annotations=empty_annotations)
    except ToolAnnotationDriftError as error:
        if missing_tool_id in error.missing_tool_ids and not error.unknown_tool_ids:
            return ("missing_annotation",)
        return ("fixture_setup_error",)
    return ()


def stale_generated_row_errors(root: Path, canary: str) -> tuple[str, ...]:
    inputs = catalog_inputs(root)
    route_rows = rows(inputs.routes, "tool_routes")
    route_rows[0]["operation_ids"] = [f"get.fixture.{canary}"]
    set_rows(inputs.routes, "tool_routes", route_rows)
    try:
        validate_catalog_sources(
            CatalogValidationInputs(
                inputs.inventory,
                inputs.routes,
                inputs.statuses,
                inputs.scenarios,
                inputs.tools,
                emitted_statuses(root, inputs.statuses),
            ),
        )
    except CatalogValidationError as error:
        issues = error.issues
        if issues and all(issue.code == "stale_operation" for issue in issues):
            return ("stale_generated_row",)
        return ("fixture_setup_error",)
    return ("fixture_setup_error",)


def bad_skill_frontmatter_errors(canary: str) -> tuple[str, ...]:
    text = (
        "---\n"
        "name: saxo-bank\n"
        f"description: {canary}\n"
        "allowed-tools: '*'\n"
        "---\n"
    )
    return ("bad_skill_frontmatter",) if frontmatter_errors_for_text(text) else ()


def manifest_version_drift_errors(root: Path, canary: str) -> tuple[str, ...]:
    with TemporaryDirectory(prefix="saxo-static-version-") as tmp:
        temp_root = Path(tmp)
        _copy_version_surface(root, temp_root)
        plugin = temp_root / ".claude-plugin" / "plugin.json"
        payload = json.loads(plugin.read_text(encoding="utf-8"))
        payload["version"] = f"9.9.9+{canary}"
        plugin.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return version_parity_errors(temp_root)


def wildcard_grant_errors(canary: str) -> tuple[str, ...]:
    with TemporaryDirectory(prefix="saxo-static-wildcard-") as tmp:
        temp_root = Path(tmp)
        case_dir = temp_root / "evals" / "fixture-case"
        case_dir.mkdir(parents=True)
        grant = f"mcp__plugin_*{canary}"
        case_dir.joinpath("case.yaml").write_text(
            "exact_tool_grants:\n"
            f"  codex: ['{grant}']\n"
            "  claude: ['saxo_health']\n",
            encoding="utf-8",
        )
        plugin_dir = temp_root / ".claude-plugin"
        plugin_dir.mkdir(parents=True)
        plugin_dir.joinpath("plugin.json").write_text(
            "{\n"
            '  "name": "saxo-bank-mcp",\n'
            '  "version": "0.1.0",\n'
            f'  "permi\\u0073sions": ["*{canary}*"]\n'
            "}\n",
            encoding="utf-8",
        )
        findings = wildcard_findings(temp_root)
        return ("wildcard_grant",) if "wildcard_grant" in findings else tuple(findings)


def fake_secret_errors(canary: str) -> tuple[str, ...]:
    field_name = "access" + "_token"
    field_value = f"{canary}-portal-token-value"
    findings, scan_errors = scan_secret_text(
        "fixture",
        f'{field_name} = "{field_value}"',
    )
    if scan_errors:
        return ("fixture_scan_error",)
    if findings:
        return ("fake_secret",)
    return ()


def _copy_version_surface(root: Path, target: Path) -> None:
    pyproject = root / "pyproject.toml"
    target.joinpath("pyproject.toml").write_text(pyproject.read_text(encoding="utf-8"))
    tomllib.loads(pyproject.read_text(encoding="utf-8"))
    for relative in VERSION_PATHS[1:]:
        source = root / relative
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
