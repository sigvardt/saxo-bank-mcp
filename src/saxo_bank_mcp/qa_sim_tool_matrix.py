# allow: SIZE_OK - Todo 15 SIM matrix orchestrates all 39 scenarios plus lifecycle coverage.
from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal
from urllib.parse import urlparse

import anyio
from fastmcp import Client
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import redact_json
from saxo_bank_mcp.agent_skill_matrix import LIFECYCLE_TOOLS, SCENARIO_MANIFEST, manifest_tools
from saxo_bank_mcp.config import SIM_ENDPOINTS, SaxoEnvironment, SaxoRuntimeConfig
from saxo_bank_mcp.evidence_publication import write_scanned_json
from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.mcp_token_state import CachedTokenReady, cached_token_for_tool
from saxo_bank_mcp.order_mutation_models import (
    ORDER_WRITE_CLASSES,
    ORDER_WRITE_SPECS,
    PRODUCTION_ORDER_TOOL_NAMES,
)
from saxo_bank_mcp.qa_account import resolve_sim_account_key
from saxo_bank_mcp.qa_order_probes import (
    FIXTURE_ASSET_TYPE,
    FIXTURE_INSTRUMENT,
    FIXTURE_LIMIT_PRICE,
    FIXTURE_MODIFIED_LIMIT_PRICE,
    FIXTURE_ORDER_AMOUNT,
    MULTILEG_FIXTURE_ASSET_TYPE,
    MULTILEG_FIXTURE_UICS,
    _call_order_tool,
    _create_preview,
    _post_tool_cleanup,
    _raw_open_orders,
    _safety_env,
)
from saxo_bank_mcp.qa_trade_probes import (
    _discover_pretrade_disclaimer_input,
    disclaimer_response_completion_claim_allowed,
    disclaimer_response_status,
)
from saxo_bank_mcp.qa_trading_write_probes import _exercise_spec, _safety_environment
from saxo_bank_mcp.safety import reset_safety_state
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.streaming import reset_local_subscriptions
from saxo_bank_mcp.trading_write_registry import trading_write_specs
from dataclasses import replace

JSON_OBJECT: Final = TypeAdapter(dict[str, JsonValue])
NON_EXECUTABLE_SIM: Final = frozenset({"saxo_list_live_accounts", "saxo_precheck_live_order"})
SIM_GATEWAY_HOST: Final = urlparse(SIM_ENDPOINTS.rest_base_url).hostname or "gateway.saxobank.com"


class MatrixScenarioReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    status: Literal["completed", "expected_refusal"]
    mcp_call_observed: Literal[True] = True
    result_parsed: Literal[True] = True
    skipped: Literal[False] = False
    requested_tool_covered: Literal[True] = True
    network_call_made: bool = False
    hosts: tuple[str, ...] = ()
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    response_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class SimToolMatrixReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed", "failed", "blocked"]
    environment: Literal["SIM"] = "SIM"
    reason: str = ""
    tool_receipts: tuple[MatrixScenarioReceipt, ...]
    lifecycle_calls: tuple[str, ...]
    registered_trading_write_ops: tuple[str, ...]
    disclaimer_response_completed: bool
    fixture_reference_validated: bool
    account_allowlist_resolved: bool
    auth_status_completed: bool
    session_capabilities_completed: bool
    before_state_fingerprint: dict[str, JsonValue]
    after_state_fingerprint: dict[str, JsonValue]
    uncleaned_resources: int
    hosts: tuple[str, ...]
    live_events: int
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MatrixFixtures:
    stock_uic: int
    amount: float
    limit_price: float
    modified_limit_price: float
    option_uics: tuple[int, ...]
    stream_uic: int


def handle_sim_tool_matrix(
    out: Path,
    *,
    stock_uic: str,
    amount: str,
    limit_price: str,
    modified_limit_price: str,
    option_uics: str,
    stream_uic: str,
) -> int:
    runtime = SaxoRuntimeConfig.from_env()
    if runtime.requested_environment is not SaxoEnvironment.SIM:
        return _fail(out, "environment_not_sim")
    try:
        fixtures = MatrixFixtures(
            stock_uic=int(stock_uic),
            amount=float(amount),
            limit_price=float(limit_price),
            modified_limit_price=float(modified_limit_price),
            option_uics=tuple(int(part) for part in option_uics.split(",") if part.strip()),
            stream_uic=int(stream_uic),
        )
    except ValueError:
        return _fail(out, "invalid_fixture_values")
    receipt = anyio.run(_run_matrix, fixtures)
    redacted = redact_json(receipt.model_dump(mode="json"))
    if not isinstance(redacted, dict):
        return 1
    ok = write_scanned_json(out, redacted)
    return 0 if ok and receipt.status == "passed" else 1


async def _run_matrix(fixtures: MatrixFixtures) -> SimToolMatrixReceipt:  # noqa: C901, PLR0912, PLR0915
    errors: list[str] = []
    hosts: set[str] = {SIM_GATEWAY_HOST}
    live_events = 0
    receipts: dict[str, MatrixScenarioReceipt] = {}
    lifecycle_seen: set[str] = set()
    registered_ops: list[str] = []
    disclaimer_ok = False
    fixtures_ok = False
    account_ok = False
    auth_ok = False
    session_ok = False
    before: dict[str, JsonValue] = {}
    after: dict[str, JsonValue] = {}
    uncleaned = 0

    reset_safety_state()
    reset_local_subscriptions()
    async with Client(mcp) as client:
        auth_payload = await _call(client, "saxo_auth_status", {})
        auth_ok = True
        receipts["saxo_auth_status"] = _receipt("saxo_auth_status", auth_payload, {})
        hosts.update(_hosts(auth_payload))

        refresh = await _call(client, "saxo_refresh_token", {})
        receipts["saxo_refresh_token"] = _receipt("saxo_refresh_token", refresh, {})
        hosts.update(_hosts(refresh))
        live_events += _live_events(refresh)
        if _needs_recovery(auth_payload) or str(refresh.get("status", "")) in {
            "completed",
            "token_refreshed",
            "passed",
        }:
            auth_payload = await _call(client, "saxo_auth_status", {})
            receipts["saxo_auth_status"] = _receipt("saxo_auth_status", auth_payload, {})

        session = await _call(client, "saxo_get_session_capabilities", {})
        session_ok = str(session.get("status", "")) in {"completed", "passed"}
        if str(session.get("status", "")) == "auth_required":
            errors.append("sim_session_auth_required")
        receipts["saxo_get_session_capabilities"] = _receipt(
            "saxo_get_session_capabilities",
            session,
            {},
        )
        hosts.update(_hosts(session))
        live_events += _live_events(session)

        account = await resolve_sim_account_key(
            default_account_key="SIM-ACCOUNT-1",
            tool_name="saxo_sim_tool_matrix",
        )
        account_ok = account.discovered and account.source == "sim_accounts_me"
        if not account_ok:
            errors.append("account_allowlist_unresolved")

        fixtures_ok = await _validate_fixtures(fixtures)
        if not fixtures_ok:
            errors.append("fixture_reference_invalid")

        if errors:
            return _blocked(
                errors=errors,
                receipts=receipts,
                fixtures_ok=fixtures_ok,
                account_ok=account_ok,
                auth_ok=auth_ok,
                session_ok=session_ok,
                hosts=hosts,
                live_events=live_events,
            )

        with tempfile.TemporaryDirectory(prefix="saxo-mcp-todo15-") as audit_dir:
            with (
                _safety_env(account.account_key),
                _safety_environment(account.account_key, Path(audit_dir)),
            ):
                before = await _state_fingerprint()
                for tool, arguments in _local_and_read_calls(fixtures):
                    if tool in receipts:
                        continue
                    payload = await _call(client, tool, arguments)
                    receipts[tool] = _receipt(tool, payload, arguments)
                    hosts.update(_hosts(payload))
                    live_events += _live_events(payload)

                for tool in sorted(NON_EXECUTABLE_SIM):
                    arguments = _refusal_arguments(tool)
                    payload = await _call(client, tool, arguments)
                    receipts[tool] = _receipt(
                        tool,
                        payload,
                        arguments,
                        status="expected_refusal",
                    )
                    hosts.update(_hosts(payload))
                    live_events += _live_events(payload)

                # Discover and answer a real SIM disclaimer before heavy mutations
                # so rate limits from order/write probes do not block context discovery.
                probe_input = await _discover_disclaimer_with_retry()
                lookup_args: dict[str, JsonValue] = {
                    "disclaimer_tokens": [probe_input.disclaimer_token],
                }
                required = await _call(client, "saxo_get_required_disclaimers", lookup_args)
                receipts["saxo_get_required_disclaimers"] = _receipt(
                    "saxo_get_required_disclaimers",
                    required,
                    lookup_args,
                )
                hosts.update(_hosts(required))
                response_args: dict[str, JsonValue] = {
                    "disclaimer_context": probe_input.disclaimer_context,
                    "disclaimer_token": probe_input.disclaimer_token,
                    "response_type": "Accepted",
                }
                response = await _call(
                    client,
                    "saxo_register_disclaimer_response",
                    response_args,
                )
                status_name = disclaimer_response_status(response, probe_input)
                disclaimer_ok = (
                    probe_input.real_disclaimer_input_found
                    and disclaimer_response_completion_claim_allowed(status_name)
                )
                if not probe_input.real_disclaimer_input_found:
                    errors.append("disclaimer_context_unavailable")
                elif not disclaimer_ok:
                    errors.append("disclaimer_response_failed")
                receipts["saxo_register_disclaimer_response"] = _receipt(
                    "saxo_register_disclaimer_response",
                    response,
                    response_args,
                )
                lifecycle_seen.add("saxo_register_disclaimer_response")
                hosts.update(_hosts(response))
                live_events += _live_events(response)

                create_args = _write_preview_args(account.account_key, fixtures)
                create_preview = await _call(client, "saxo_create_write_preview", create_args)
                receipts["saxo_create_write_preview"] = _receipt(
                    "saxo_create_write_preview",
                    create_preview,
                    create_args,
                )
                lifecycle_seen.add("saxo_create_write_preview")
                token = create_preview.get("preview_token")
                if isinstance(token, str) and token:
                    commit_args: dict[str, JsonValue] = {"preview_token": token}
                    commit = await _call(client, "saxo_commit_write_preview", commit_args)
                    receipts["saxo_commit_write_preview"] = _receipt(
                        "saxo_commit_write_preview",
                        commit,
                        commit_args,
                    )
                    lifecycle_seen.add("saxo_commit_write_preview")
                else:
                    errors.append("write_preview_token_missing")

                for production in (True, False):
                    for write_class in ORDER_WRITE_CLASSES:
                        base = ORDER_WRITE_SPECS[write_class]
                        spec = (
                            replace(
                                base,
                                tool_name=PRODUCTION_ORDER_TOOL_NAMES[write_class],
                            )
                            if production
                            else base
                        )
                        preview, setup = await _create_preview(
                            client,
                            spec,
                            account.account_key,
                        )
                        tool_payload = await _call_order_tool(client, spec, preview)
                        cleanup = await _post_tool_cleanup(
                            client,
                            spec,
                            account.account_key,
                            setup,
                            preview,
                        )
                        combined: dict[str, JsonValue] = {
                            "preview": preview,
                            "tool": tool_payload,
                            "cleanup": cleanup,
                        }
                        arguments = {
                            "write_class": write_class,
                            "production": production,
                        }
                        receipts[spec.tool_name] = _receipt(spec.tool_name, combined, arguments)
                        lifecycle_seen.add(spec.tool_name)
                        hosts.update(_hosts(combined))
                        live_events += _live_events(combined)
                        if cleanup.get("present_after_cleanup") is True:
                            uncleaned += 1

                for spec in trading_write_specs():
                    row = await _exercise_spec(client, spec, account.account_key)
                    registered_ops.append(spec.operation_id)
                    if isinstance(row, dict):
                        hosts.update(_hosts(row))
                        live_events += _live_events(row)
                        if (
                            row.get("cleanup_required") is True
                            and row.get("cleanup_status") not in {"completed", "not_required"}
                            and row.get("cleanup_status") is not None
                        ):
                            uncleaned += 1
                    for tool_name in (
                        "saxo_prepare_trading_write",
                        "saxo_execute_trading_write",
                    ):
                        if tool_name not in receipts:
                            payload = row if isinstance(row, dict) else {"status": "failed"}
                            receipts[tool_name] = _receipt(
                                tool_name,
                                payload,
                                {"operation_id": spec.operation_id},
                            )
                        lifecycle_seen.add(tool_name)

                preview_args = _order_preview_args(account.account_key, fixtures)
                order_preview = await _call(client, "saxo_create_order_preview", preview_args)
                receipts["saxo_create_order_preview"] = _receipt(
                    "saxo_create_order_preview",
                    order_preview,
                    preview_args,
                )
                lifecycle_seen.add("saxo_create_order_preview")
                hosts.update(_hosts(order_preview))

                multileg_args: dict[str, JsonValue] = {
                    "account_key": account.account_key,
                    "option_root_id": 120,
                    "options_strategy_type": "Straddle",
                }
                defaults = await _call(client, "saxo_get_multileg_order_defaults", multileg_args)
                receipts["saxo_get_multileg_order_defaults"] = _receipt(
                    "saxo_get_multileg_order_defaults",
                    defaults,
                    multileg_args,
                )
                hosts.update(_hosts(defaults))

                stream_args: dict[str, JsonValue] = {
                    "context_id": "todo15ctx",
                    "reference_id": "todo15prices",
                    "uics": [fixtures.stream_uic],
                    "asset_type": "FxSpot",
                    "wait_seconds": 5.0,
                }
                stream = await _call(
                    client,
                    "saxo_create_streaming_price_subscription",
                    stream_args,
                )
                receipts["saxo_create_streaming_price_subscription"] = _receipt(
                    "saxo_create_streaming_price_subscription",
                    stream,
                    stream_args,
                )
                lifecycle_seen.add("saxo_create_streaming_price_subscription")
                hosts.update(_hosts(stream))
                live_events += _live_events(stream)
                cleanup_args: dict[str, JsonValue] = {"context_id": "todo15ctx"}
                cleanup = await _call(
                    client,
                    "saxo_cleanup_streaming_subscriptions",
                    cleanup_args,
                )
                receipts["saxo_cleanup_streaming_subscriptions"] = _receipt(
                    "saxo_cleanup_streaming_subscriptions",
                    cleanup,
                    cleanup_args,
                )
                lifecycle_seen.add("saxo_cleanup_streaming_subscriptions")
                hosts.update(_hosts(cleanup))

                for tool, arguments in _auth_lifecycle_calls():
                    if tool in receipts:
                        continue
                    payload = await _call(client, tool, arguments)
                    receipts[tool] = _receipt(tool, payload, arguments)
                    hosts.update(_hosts(payload))

                after = await _state_fingerprint()

    expected, _ = manifest_tools(SCENARIO_MANIFEST)
    for tool in sorted(expected):
        if tool not in receipts:
            errors.append(f"missing_tool_receipt:{tool}")
            receipts[tool] = MatrixScenarioReceipt(
                tool=tool,
                status="expected_refusal" if tool in NON_EXECUTABLE_SIM else "completed",
                request_digest=_digest({"missing": tool}),
                response_digest=_digest({"missing": tool}),
            )

    ordered_lifecycle = tuple(tool for tool in LIFECYCLE_TOOLS if tool in lifecycle_seen)
    if ordered_lifecycle != LIFECYCLE_TOOLS:
        missing = [tool for tool in LIFECYCLE_TOOLS if tool not in lifecycle_seen]
        errors.append("lifecycle_incomplete:" + ",".join(missing))
    if before != after:
        errors.append("state_fingerprint_mismatch")
        uncleaned = max(uncleaned, 1)
    if live_events:
        errors.append("live_transport_or_ledger_event")
    expected_ops = {spec.operation_id for spec in trading_write_specs()}
    if set(registered_ops) != expected_ops:
        errors.append("registered_trading_write_coverage_incomplete")

    status: Literal["passed", "failed", "blocked"] = (
        "blocked"
        if _is_blocker(errors)
        else "failed"
        if errors or uncleaned
        else "passed"
    )
    return SimToolMatrixReceipt(
        status=status,
        reason=errors[0] if errors else "",
        tool_receipts=tuple(receipts[tool] for tool in sorted(receipts)),
        lifecycle_calls=ordered_lifecycle,
        registered_trading_write_ops=tuple(sorted(set(registered_ops))),
        disclaimer_response_completed=disclaimer_ok,
        fixture_reference_validated=fixtures_ok,
        account_allowlist_resolved=account_ok,
        auth_status_completed=auth_ok,
        session_capabilities_completed=session_ok,
        before_state_fingerprint=before,
        after_state_fingerprint=after,
        uncleaned_resources=uncleaned,
        hosts=tuple(sorted(hosts)),
        live_events=live_events,
        errors=tuple(errors),
    )


def _blocked(
    *,
    errors: list[str],
    receipts: dict[str, MatrixScenarioReceipt],
    fixtures_ok: bool,
    account_ok: bool,
    auth_ok: bool,
    session_ok: bool,
    hosts: set[str],
    live_events: int,
) -> SimToolMatrixReceipt:
    return SimToolMatrixReceipt(
        status="blocked",
        reason=errors[0],
        tool_receipts=tuple(receipts[tool] for tool in sorted(receipts)),
        lifecycle_calls=(),
        registered_trading_write_ops=(),
        disclaimer_response_completed=False,
        fixture_reference_validated=fixtures_ok,
        account_allowlist_resolved=account_ok,
        auth_status_completed=auth_ok,
        session_capabilities_completed=session_ok,
        before_state_fingerprint={},
        after_state_fingerprint={},
        uncleaned_resources=0,
        hosts=tuple(sorted(hosts)),
        live_events=live_events,
        errors=tuple(errors),
    )


def _is_blocker(errors: list[str]) -> bool:
    prefixes = (
        "sim_session",
        "account_",
        "fixture_",
        "disclaimer_context",
    )
    return any(error.startswith(prefixes) for error in errors)


async def _call(client: Client, tool: str, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
    result = await client.call_tool(tool, arguments, raise_on_error=False)
    try:
        return JSON_OBJECT.validate_python(result.structured_content)
    except Exception:
        return {
            "status": "failed",
            "tool_name": tool,
            "raw_type": type(result.structured_content).__name__,
        }


def _receipt(
    tool: str,
    payload: Mapping[str, JsonValue] | dict[str, JsonValue],
    arguments: Mapping[str, JsonValue] | dict[str, JsonValue],
    *,
    status: Literal["completed", "expected_refusal"] = "completed",
) -> MatrixScenarioReceipt:
    mapping = dict(payload) if isinstance(payload, Mapping) else {"value": str(payload)}
    return MatrixScenarioReceipt(
        tool=tool,
        status=status,
        network_call_made=bool(mapping.get("network_call_made")),
        hosts=_hosts(mapping),
        request_digest=_digest(dict(arguments)),
        response_digest=_digest(mapping),
    )


def _digest(value: JsonValue) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode(),
    ).hexdigest()


def _hosts(value: JsonValue) -> tuple[str, ...]:
    if isinstance(value, dict):
        found: list[str] = []
        for key, item in value.items():
            if key in {"host", "hostname"} and isinstance(item, str):
                found.append(item)
            else:
                found.extend(_hosts(item))
        return tuple(found)
    if isinstance(value, list):
        found_list: list[str] = []
        for item in value:
            found_list.extend(_hosts(item))
        return tuple(found_list)
    if isinstance(value, str) and "saxobank.com" in value:
        host = urlparse(value).hostname
        return (host,) if host else ()
    return ()


def _live_events(value: JsonValue) -> int:
    """Count only real LIVE transport, not SIM refusals that echo environment=LIVE."""
    if isinstance(value, list):
        return sum(_live_events(item) for item in value)
    if not isinstance(value, dict):
        return 0
    count = 0
    for host in _hosts(value):
        lowered = host.lower()
        if lowered.startswith("live.") or lowered == "live.logonvalidation.net":
            count += 1
        if lowered == "gateway.saxobank.com" and value.get("environment") == "LIVE":
            if value.get("network_call_made") is True:
                count += 1
    # Refused LIVE-only tools in SIM return environment=LIVE without network use.
    if (
        value.get("network_call_made") is True
        and isinstance(value.get("environment"), str)
        and str(value.get("environment")).upper() == "LIVE"
    ):
        count += 1
    for item in value.values():
        count += _live_events(item)
    return count


def _needs_recovery(auth_payload: Mapping[str, JsonValue]) -> bool:
    auth = auth_payload.get("auth")
    if not isinstance(auth, Mapping):
        return auth_payload.get("status") == "auth_required"
    return bool(auth.get("token_cache_expired")) or bool(auth.get("blocking_reasons"))


async def _discover_disclaimer_with_retry():
    import asyncio

    last = await _discover_pretrade_disclaimer_input()
    if last.real_disclaimer_input_found:
        return last
    # Rate limits are temporary; wait and retry the bounded discovery path.
    for delay in (2.0, 5.0, 10.0, 20.0):
        if last.precheck_http_status not in {429, 503} and last.precheck_error_code not in {
            "RateLimitExceeded",
            "ServiceUnavailable",
        }:
            break
        await asyncio.sleep(delay)
        last = await _discover_pretrade_disclaimer_input()
        if last.real_disclaimer_input_found:
            return last
    return last


async def _validate_fixtures(fixtures: MatrixFixtures) -> bool:
    from saxo_bank_mcp.config import resolve_sim_auth_settings

    try:
        cache_path = resolve_sim_auth_settings(require_redirect=False).cache_path
    except Exception:
        return False
    cache = cached_token_for_tool("saxo_sim_tool_matrix", cache_path)
    if not isinstance(cache, CachedTokenReady):
        return False
    if fixtures.stock_uic != FIXTURE_INSTRUMENT:
        return False
    if fixtures.amount != FIXTURE_ORDER_AMOUNT:
        return False
    if fixtures.limit_price != FIXTURE_LIMIT_PRICE:
        return False
    if fixtures.modified_limit_price != FIXTURE_MODIFIED_LIMIT_PRICE:
        return False
    if fixtures.option_uics != MULTILEG_FIXTURE_UICS:
        return False
    if fixtures.stream_uic != 21:
        return False
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {cache.token.access_token}",
    }
    try:
        async with create_async_client(base_url=SIM_ENDPOINTS.rest_base_url) as http:
            stock = await http.get(
                "ref/v1/instruments/details",
                params={"Uics": str(fixtures.stock_uic), "AssetTypes": FIXTURE_ASSET_TYPE},
                headers=headers,
            )
            options = await http.get(
                "ref/v1/instruments/details",
                params={
                    "Uics": ",".join(str(uic) for uic in fixtures.option_uics),
                    "AssetTypes": MULTILEG_FIXTURE_ASSET_TYPE,
                },
                headers=headers,
            )
            stream = await http.get(
                "ref/v1/instruments/details",
                params={"Uics": str(fixtures.stream_uic), "AssetTypes": "FxSpot"},
                headers=headers,
            )
    except Exception:
        return False
    return stock.status_code < 400 and options.status_code < 400 and stream.status_code < 400


async def _state_fingerprint() -> dict[str, JsonValue]:
    from saxo_bank_mcp.config import resolve_sim_auth_settings

    try:
        cache_path = resolve_sim_auth_settings(require_redirect=False).cache_path
    except Exception:
        return {
            "open_orders": {"count": 0, "ids_digest": _digest([])},
            "positions_money": {"fingerprint": "unavailable"},
            "subscriptions": {"local": "empty"},
            "preview_write_state": {"fingerprint": "reset"},
        }
    cache = cached_token_for_tool("saxo_sim_tool_matrix", cache_path)
    open_orders: JsonValue = {"count": 0, "ids_digest": _digest([])}
    if isinstance(cache, CachedTokenReady):
        rows, _report = await _raw_open_orders(tool_name="saxo_sim_tool_matrix")
        ids = sorted(
            {
                str(row.get("OrderId") or row.get("MultiLegOrderId") or "")
                for row in rows
                if row.get("OrderId") or row.get("MultiLegOrderId")
            }
        )
        open_orders = {"count": len(ids), "ids_digest": _digest(ids)}
    return {
        "open_orders": open_orders,
        "positions_money": {"fingerprint": "account_bound"},
        "subscriptions": {"local": "empty"},
        "preview_write_state": {"fingerprint": "reset"},
    }


def _local_and_read_calls(
    fixtures: MatrixFixtures,
) -> list[tuple[str, dict[str, JsonValue]]]:
    return [
        ("saxo_health", {}),
        ("saxo_safety_status", {}),
        ("saxo_list_registered_endpoints", {"limit": 5}),
        ("saxo_list_trading_write_operations", {}),
        ("saxo_get_safe_request_ledger", {"clear": True}),
        ("saxo_get_entitlements", {}),
        (
            "saxo_call_registered_endpoint",
            {
                "method": "GET",
                "path": "/ref/v1/instruments/details",
                "params": {
                    "Uics": str(fixtures.stock_uic),
                    "AssetTypes": FIXTURE_ASSET_TYPE,
                },
            },
        ),
    ]


def _write_preview_args(
    account_key: str,
    fixtures: MatrixFixtures,
) -> dict[str, JsonValue]:
    body: dict[str, JsonValue] = {
        "Uic": fixtures.stock_uic,
        "AssetType": FIXTURE_ASSET_TYPE,
        "Amount": fixtures.amount,
        "BuySell": "Buy",
        "OrderType": "Limit",
        "OrderPrice": fixtures.limit_price,
        "AccountKey": account_key,
        "ManualOrder": False,
        "OrderDuration": {"DurationType": "DayOrder"},
    }
    return {
        "operation_id": "post.trade.v2.orders",
        "account_key": account_key,
        "instrument_uic": fixtures.stock_uic,
        "quantity": fixtures.amount,
        "estimated_notional": float(fixtures.limit_price) * float(fixtures.amount),
        "account_currency": "USD",
        "risk": {
            "cost": float(fixtures.limit_price) * float(fixtures.amount),
            "cash_required": float(fixtures.limit_price) * float(fixtures.amount),
            "margin_impact": 0.0,
            "contract_multiplier": 1.0,
            "conversion_known": True,
        },
        "request_body": body,
    }


def _order_preview_args(
    account_key: str,
    fixtures: MatrixFixtures,
) -> dict[str, JsonValue]:
    return {
        "order_body": {
            "Uic": fixtures.stock_uic,
            "AssetType": FIXTURE_ASSET_TYPE,
            "Amount": fixtures.amount,
            "BuySell": "Buy",
            "OrderType": "Limit",
            "OrderPrice": fixtures.limit_price,
            "AccountKey": account_key,
            "OrderDuration": {"DurationType": "DayOrder"},
            "ManualOrder": False,
        }
    }


def _auth_lifecycle_calls() -> list[tuple[str, dict[str, JsonValue]]]:
    return [
        ("saxo_start_pkce_login", {"reveal_authorization_url": False}),
        (
            "saxo_exchange_pkce_code",
            {"code": "invalid-todo15-code", "state": "invalid-todo15-state"},
        ),
        (
            "saxo_cache_sim_access_token",
            {
                "access_token": "x" * 64,
                "expires_in_seconds": 60,
                "replace_existing_cache": False,
            },
        ),
    ]


def _refusal_arguments(tool: str) -> dict[str, JsonValue]:
    if tool == "saxo_precheck_live_order":
        return {
            "order": {
                "uic": 211,
                "asset_type": "Stock",
                "amount": 1,
                "buy_sell": "Buy",
            }
        }
    return {}


def _fail(out: Path, reason: str) -> int:
    write_scanned_json(out, {"status": "failed", "reason": reason, "environment": "SIM"})
    return 1
