from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import yaml
from pydantic import TypeAdapter

ROOT: Final = Path(__file__).resolve().parents[1]
SKILL_ROOT: Final = ROOT / "skills/saxo-bank"
SKILL_PATH: Final = SKILL_ROOT / "SKILL.md"
CONTRACT_PATH: Final = SKILL_ROOT / "references/router-contract.md"
CATALOG_PATH: Final = SKILL_ROOT / "references/tool-catalog.md"
OPENAI_PATH: Final = SKILL_ROOT / "agents/openai.yaml"
MAX_SKILL_LINES: Final = 500
MAPPING_ADAPTER: Final = TypeAdapter(dict[str, str])
NESTED_MAPPING_ADAPTER: Final = TypeAdapter(dict[str, dict[str, str]])
LINK_PATTERN: Final = re.compile(r"\[(saxo-[a-z-]+)\]\(([^)]+)\)")
TOOL_PATTERN: Final = re.compile(r"`(saxo_[a-z0-9_]+)`")
EXPECTED_ROUTE_TRIGGERS: Final = {
    "saxo-auth-session": frozenset({"auth", "session", "refresh"}),
    "saxo-openapi": frozenset({"operation", "refused", "registered"}),
    "saxo-reads": frozenset({"read", "inspect", "position"}),
    "saxo-streaming": frozenset({"stream", "subscription", "cleanup"}),
    "saxo-trading": frozenset({"trade", "order", "precheck"}),
    "saxo-safety-recovery": frozenset({"recovery", "incident", "unknown"}),
    "saxo-qa-operations": frozenset({"qa", "eval", "evidence"}),
}
EXPECTED_SCENARIO_ROUTES: Final = {
    "auth": ("saxo-auth-session",),
    "read+trade": ("saxo-reads", "saxo-trading"),
    "SIM trade": ("saxo-trading",),
    "LIVE trade": ("saxo-trading",),
    "streaming": ("saxo-streaming",),
    "incident": ("saxo-safety-recovery",),
    "unsupported operation": ("saxo-openapi",),
}
EXPECTED_TOOL_IDS: Final = frozenset(
    {
        "saxo_health",
        "saxo_auth_status",
        "saxo_list_registered_endpoints",
        "saxo_call_registered_endpoint",
        "saxo_create_streaming_price_subscription",
        "saxo_cleanup_streaming_subscriptions",
        "saxo_create_order_preview",
        "saxo_precheck_live_order",
        "saxo_prepare_trading_write",
        "saxo_execute_trading_write",
        "saxo_get_safe_request_ledger",
        "saxo_safety_status",
    },
)


def test_router_package_parses_with_exact_metadata_shapes() -> None:
    # Given: the router package files.
    for path in (SKILL_PATH, CONTRACT_PATH, CATALOG_PATH, OPENAI_PATH):
        assert path.is_file(), path

    # When: YAML boundaries are parsed.
    metadata, body = _parse_skill(SKILL_PATH)
    openai = NESTED_MAPPING_ADAPTER.validate_python(
        yaml.safe_load(OPENAI_PATH.read_text(encoding="utf-8")),
    )

    # Then: only portable skill keys and the expected interface shape remain.
    assert set(metadata) == {"name", "description"}
    assert metadata["name"] == "saxo-bank"
    assert metadata["description"]
    assert set(openai) == {"interface"}
    assert set(openai["interface"]) == {"display_name", "short_description", "default_prompt"}
    assert len((metadata["name"] + body).splitlines()) < MAX_SKILL_LINES


def test_router_links_each_focused_skill_once_with_semantic_triggers() -> None:
    # Given: parsed focused-route bullets.
    _, body = _parse_skill(SKILL_PATH)
    route_lines = _bullet_lines(_section(body, "Focused skill routes"))

    # When: links and trigger words are indexed by skill.
    linked: dict[str, tuple[str, str]] = {}
    for line in route_lines:
        matches = LINK_PATTERN.findall(line)
        assert len(matches) == 1
        skill, target = matches[0]
        linked[skill] = (target, line.lower())

    # Then: all seven routes occur once and carry distinct routing language.
    assert set(linked) == set(EXPECTED_ROUTE_TRIGGERS)
    for skill, triggers in EXPECTED_ROUTE_TRIGGERS.items():
        target, line = linked[skill]
        assert target == f"../{skill}/SKILL.md"
        assert triggers <= frozenset(re.findall(r"[a-z]+", line))


def test_router_classification_dimensions_are_structured_and_complete() -> None:
    # Given: the router's routing-rules section.
    _, body = _parse_skill(SKILL_PATH)
    rules = _section(body, "Routing rules")

    # When: code terms and risk labels are parsed.
    environments = _code_terms_after(rules, "Classify environment")
    intents = _code_terms_after(rules, "Classify intent")
    risk_labels = {
        line.split("`", maxsplit=2)[1]
        for line in _bullet_lines(rules)
        if line.startswith("- `")
    }

    # Then: environment, intent, mutation risk, and evidence are all classified.
    assert environments >= {"SIM", "LIVE", "LOCAL"}
    assert intents == {"auth", "read", "stream", "trade", "recovery", "QA", "unsupported"}
    assert risk_labels == {
        "none",
        "local-state",
        "SIM mutation",
        "LIVE read/precheck",
        "LIVE mutation",
    }
    assert {"plan-only", "readback", "cleanup proof", "request-ledger proof"} <= set(
        re.findall(r"[a-z]+(?:-[a-z]+)?(?: proof)?", rules),
    )


def test_router_contract_encodes_primary_and_follow_on_order() -> None:
    # Given: scenario records parsed from the contract.
    scenarios = _scenario_records(CONTRACT_PATH.read_text(encoding="utf-8"))

    # When: ordered focused-skill mentions are extracted.
    routes = {
        scenario: tuple(re.findall(r"saxo-(?:auth-session|openapi|reads|streaming|trading|"
        r"safety-recovery|qa-operations)", text))
        for scenario, text in scenarios.items()
    }

    # Then: one primary route exists, with trading only as the explicit read follow-on.
    for scenario, expected in EXPECTED_SCENARIO_ROUTES.items():
        assert routes[scenario] == expected
    assert "explicit follow-on" in scenarios["read+trade"].lower()
    assert "before any Saxo MCP call" in scenarios["ambiguous environment"]


def test_router_safety_and_tool_contracts_are_semantically_enforced() -> None:
    # Given: router source, contract, and generated catalog.
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in (SKILL_PATH, CONTRACT_PATH)
    )
    catalog = CATALOG_PATH.read_text(encoding="utf-8")
    tools = frozenset(TOOL_PATTERN.findall(combined))

    # When: safety concepts and tool identifiers are inspected.
    lowered = combined.lower()

    # Then: logical IDs are catalog-backed and unsafe paths remain blocked.
    assert tools >= EXPECTED_TOOL_IDS
    assert all(tool in catalog for tool in EXPECTED_TOOL_IDS)
    assert "registered name ends with the logical id" in lowered
    assert "client namespace prefix" in lowered
    assert "infer live approval" in lowered
    assert {"instrument", "side", "quantity", "timing", "whether to buy"} <= set(
        re.findall(r"[a-z]+(?: to buy)?", lowered),
    )
    assert "prompt-injected" in lowered
    assert "unsupported operation" in lowered
    assert "plan-only" in lowered
    assert "do not call saxo mcp tools" in lowered
    assert "mcp__plugin_" not in combined


def _parse_skill(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    opening, frontmatter, body = text.split("---", maxsplit=2)
    assert not opening.strip()
    return MAPPING_ADAPTER.validate_python(yaml.safe_load(frontmatter)), body


def _section(text: str, heading: str) -> str:
    marker = f"## {heading}"
    remainder = text.split(marker, maxsplit=1)[1]
    return remainder.split("\n## ", maxsplit=1)[0]


def _bullet_lines(text: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in text.splitlines() if line.lstrip().startswith("- "))


def _code_terms_after(text: str, prefix: str) -> set[str]:
    line = next(line for line in text.splitlines() if line.startswith(prefix))
    return set(re.findall(r"`([^`]+)`", line))


def _scenario_records(text: str) -> dict[str, str]:
    return {
        label.strip(): detail.strip()
        for line in _bullet_lines(_section(text, "Required scenarios"))
        for label, detail in (line.removeprefix("- ").split(":", maxsplit=1),)
    }
