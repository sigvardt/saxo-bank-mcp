from __future__ import annotations

from saxo_bank_mcp._evidence import JsonValue


def prod_readiness_requirements() -> list[dict[str, JsonValue]]:
    return [
        _requirement(
            "public_secret_containment",
            "implemented",
            "No AppSecret or RefreshToken belongs in public code or generated evidence.",
            ["src/saxo_bank_mcp/_redaction.py", "src/saxo_bank_mcp/token_cache.py"],
        ),
        _requirement(
            "pkce_saxo_login",
            "implemented",
            "Public login uses Saxo OAuth with PKCE and never asks agents to "
            "intercept credentials.",
            ["src/saxo_bank_mcp/oauth.py", "src/saxo_bank_mcp/mcp_auth_tools.py"],
        ),
        _requirement(
            "refresh_token_auth_server_only",
            "implemented",
            "Refresh tokens are sent only to the configured Saxo token endpoint.",
            ["src/saxo_bank_mcp/oauth.py"],
        ),
        _requirement(
            "public_secret_scan",
            "implemented",
            "The readiness command scans public paths for tokens and credentials.",
            ["src/saxo_bank_mcp/_redaction.py"],
        ),
        _requirement(
            "monkey_rapid_calls",
            "implemented",
            "The readiness command rapidly repeats a safe MCP health call with "
            "no network or trading write.",
            ["src/saxo_bank_mcp/qa_prod_readiness.py"],
        ),
        _requirement(
            "openapi_400_investigation",
            "implemented",
            "Order mutation responses preserve Saxo error codes for investigation evidence.",
            ["src/saxo_bank_mcp/order_mutation_models.py"],
        ),
        _requirement(
            "throttling_409_429",
            "implemented",
            "HTTP 429 is rate-limited, and HTTP 409 is duplicate-submit evidence.",
            ["src/saxo_bank_mcp/order_mutation_models.py", "src/saxo_bank_mcp/safety.py"],
        ),
        _requirement(
            "many_positions_orders",
            "implemented",
            "Readback code scans order collections recursively instead of assuming one row.",
            ["src/saxo_bank_mcp/order_mutation_execution.py"],
        ),
        _requirement(
            "currency_and_price_display",
            "evidence_required_live",
            "Read tools preserve Saxo response metadata; full display parity "
            "needs LIVE account evidence.",
            ["src/saxo_bank_mcp/read_tools.py", "src/saxo_bank_mcp/trade_preview.py"],
        ),
        _requirement(
            "unexpected_instruments_assets",
            "implemented",
            "Saxo-facing JSON is accepted as raw objects so added fields and "
            "new asset strings do not crash parsing.",
            ["src/saxo_bank_mcp/endpoint_registry.py", "src/saxo_bank_mcp/read_tools.py"],
        ),
        _requirement(
            "fractional_amounts",
            "implemented",
            "Order and preview paths accept JSON numeric quantities as floats "
            "and compare them with tolerance.",
            ["src/saxo_bank_mcp/order_mutation_guards.py", "src/saxo_bank_mcp/trade_preview.py"],
        ),
        _requirement(
            "all_order_mutation_shapes",
            "implemented",
            "Place, modify, cancel, related-order, and multileg write classes "
            "are registered and QA-covered.",
            ["src/saxo_bank_mcp/order_mutation_models.py", "tests/test_order_mutations.py"],
        ),
        _requirement(
            "invalid_order_prevention",
            "implemented",
            "Preview tokens, account allowlists, quantity/notional limits, and "
            "request-body checks stop invalid writes.",
            [
                "src/saxo_bank_mcp/safety.py",
                "src/saxo_bank_mcp/safety_checks.py",
                "src/saxo_bank_mcp/order_mutation_guards.py",
            ],
        ),
        _requirement(
            "automated_trading_limits",
            "implemented",
            "Automated write paths require preview plus approval factors and "
            "enforce configured size limits.",
            ["src/saxo_bank_mcp/safety.py", "src/saxo_bank_mcp/safety_models.py"],
        ),
        _requirement(
            "versioning_tolerance",
            "implemented",
            "Saxo response models avoid strict enum and extra-field rejection at the API edge.",
            ["src/saxo_bank_mcp/read_tools.py", "src/saxo_bank_mcp/endpoint_registry.py"],
        ),
        _requirement(
            "sim_before_live",
            "implemented",
            "SIM QA and final verification remain separate from LIVE read/write enablement.",
            ["src/saxo_bank_mcp/qa.py", "src/saxo_bank_mcp/final_verify_code.py"],
        ),
        _requirement(
            "live_write_refusal",
            "refused_until_live_enablement",
            "LIVE write tools refuse before network until the real-money enablement plan exists.",
            ["src/saxo_bank_mcp/live_mode.py", "src/saxo_bank_mcp/order_mutation_execution.py"],
        ),
        _requirement(
            "live_read_credentials",
            "evidence_required_live",
            "LIVE reads require approved LIVE credentials and a LIVE token cache "
            "outside the repository.",
            ["src/saxo_bank_mcp/live_mode.py", "src/saxo_bank_mcp/qa_probes.py"],
        ),
    ]


def _requirement(
    requirement_id: str,
    status: str,
    summary: str,
    evidence_refs: list[str],
) -> dict[str, JsonValue]:
    return {
        "id": requirement_id,
        "status": status,
        "summary": summary,
        "evidence_refs": evidence_refs,
    }
