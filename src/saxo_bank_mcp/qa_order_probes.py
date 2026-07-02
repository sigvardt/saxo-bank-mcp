from __future__ import annotations

import json
import os
import uuid
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import anyio
import httpx2
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp._redaction import redact_json, scan_secret_paths
from saxo_bank_mcp.config import SIM_ENDPOINTS, SimAuthSettingsError, resolve_sim_auth_settings
from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.loop_manifest import current_git_state
from saxo_bank_mcp.mcp_token_state import (
    CachedTokenBlocked,
    CachedTokenReady,
    cached_token_for_tool,
)
from saxo_bank_mcp.order_mutation_models import (
    HTTP_SUCCESS_MAX,
    HTTP_SUCCESS_MIN,
    ORDER_WRITE_CLASSES,
    ORDER_WRITE_SPECS,
    OrderWriteClass,
    OrderWriteSpec,
)
from saxo_bank_mcp.qa_account import resolve_sim_account_key
from saxo_bank_mcp.qa_events import base_event
from saxo_bank_mcp.safety import TEST_APPROVAL_FACTOR, reset_safety_state
from saxo_bank_mcp.server import mcp

FIXTURE_ACCOUNT: Final = "SIM-ACCOUNT-1"
FIXTURE_INSTRUMENT: Final = 211
FIXTURE_ASSET_TYPE: Final = "Stock"
FIXTURE_ORDER_AMOUNT: Final = 1
FIXTURE_ORDER_NOTIONAL: Final = 100
FIXTURE_LIMIT_PRICE: Final = 50
FIXTURE_MODIFIED_LIMIT_PRICE: Final = 51
MULTILEG_FIXTURE_UICS: Final = (58026125, 58026124)
MULTILEG_FIXTURE_ASSET_TYPE: Final = "StockIndexOption"
MULTILEG_LIMIT_PRICE: Final = 1
MULTILEG_MODIFIED_LIMIT_PRICE: Final = 2
ORDER_WRITE_SETTLE_SECONDS: Final = 1.2
JSON_OBJECT_ADAPTER: Final = TypeAdapter(dict[str, JsonValue])
JSON_VALUE_ADAPTER: Final[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)
ORDER_WRITE_CLASS_BY_NAME: Final[dict[str, OrderWriteClass]] = {
    "place": "place",
    "modify": "modify",
    "cancel": "cancel",
    "cancel-by-instrument": "cancel-by-instrument",
    "multileg-place": "multileg-place",
    "multileg-modify": "multileg-modify",
    "multileg-cancel": "multileg-cancel",
}


@dataclass(frozen=True, slots=True)
class SetupOrder:
    order_id: str
    order_kind: Literal["single", "multileg"]
    report: dict[str, JsonValue]


def handle_sim_order_mutation(out: Path, classes: str | None) -> int:
    requested = _requested_classes(classes)
    payload = anyio.run(_sim_order_mutation, requested)
    return _write_redacted_with_secret_scan(
        out,
        payload,
        ("passed", "exercised", "incomplete_auth_required"),
    )


def handle_trade_write_denied(out: Path, missing: str) -> int:
    payload = anyio.run(_trade_write_denied, missing)
    return _write_redacted_with_secret_scan(out, payload, ("denied",))


async def _sim_order_mutation(classes: tuple[OrderWriteClass, ...]) -> dict[str, JsonValue]:
    reset_safety_state()
    per_class: list[dict[str, JsonValue]] = []
    account = await resolve_sim_account_key(
        default_account_key=FIXTURE_ACCOUNT,
        tool_name="saxo_sim_order_mutation_qa",
    )
    with _safety_env(account.account_key):
        async with Client(mcp) as client:
            for write_class in classes:
                spec = ORDER_WRITE_SPECS[write_class]
                preview, setup = await _create_preview(client, spec, account.account_key)
                tool_payload = await _call_order_tool(client, spec, preview)
                cleanup = await _post_tool_cleanup(
                    client,
                    spec,
                    account.account_key,
                    setup,
                )
                per_class.append(
                    class_report_for_qa(
                        spec,
                        preview,
                        tool_payload,
                        setup=setup,
                        cleanup=cleanup,
                    ),
                )
    completed = [
        str(row["write_class"])
        for row in per_class
        if row.get("status") == "completed" and row.get("real_mutation_proven") is True
    ]
    auth_required = [
        str(row["write_class"]) for row in per_class if row.get("tool_status") == "auth_required"
    ]
    has_failed = any(row.get("status") == "failed" for row in per_class)
    was_exercised = any(row.get("network_call_made") is True for row in per_class)
    status = (
        "failed"
        if has_failed
        else "passed"
        if completed
        else "exercised"
        if was_exercised
        else "incomplete_auth_required"
    )
    all_classes_complete = len(completed) == len(classes)
    return {
        **base_event(
            "sim-order-mutation",
            status,
            "FastMCP SIM order mutation tools exercised through safety gates",
        ),
        "environment": "SIM",
        "classes_requested": list(classes),
        "completed_classes": completed,
        "auth_required_classes": auth_required,
        "per_class": per_class,
        "real_mutation_proven": all_classes_complete,
        "completion_claim_allowed": all_classes_complete,
        "fastmcp_called": True,
        "network_call_made": any(row.get("network_call_made") is True for row in per_class),
        "account_resolution": account.to_safe_json(),
        "live_write": any(row.get("live_write") is True for row in per_class),
        "order_or_subscription_created": any(
            row.get("order_or_subscription_created") is True for row in per_class
        ),
        "account_key_redacted": True,
        "git": current_git_state().model_dump(mode="json"),
    }


async def _trade_write_denied(missing: str) -> dict[str, JsonValue]:
    reset_safety_state()
    spec = ORDER_WRITE_SPECS["place"]
    account = await resolve_sim_account_key(
        default_account_key=FIXTURE_ACCOUNT,
        tool_name="saxo_trade_write_denied_qa",
    )
    with _safety_env(account.account_key):
        async with Client(mcp) as client:
            preview, _ = await _create_preview(client, spec, account.account_key)
            token = str(preview.get("preview_token", ""))
            result = await client.call_tool(
                spec.tool_name,
                {"preview_token": token},
                raise_on_error=False,
            )
    payload = _payload(result.structured_content)
    same_fingerprint = payload.get("request_fingerprint") == preview.get("request_fingerprint")
    denied = (
        missing == "approval-factor"
        and payload.get("status") == "denied"
        and payload.get("denial_reason") == "approval_factor_missing"
        and payload.get("network_call_made") is False
    )
    return {
        **base_event(
            "trade-write-denied",
            "denied" if denied else "failed",
            "FastMCP order write refused missing approval factor before network",
        ),
        "tool_name": spec.tool_name,
        "fastmcp_called": True,
        "missing": missing,
        "denial_reason": str(payload.get("denial_reason", "")),
        "same_request_fingerprint": same_fingerprint,
        "preview_token_redacted": True,
        "account_resolution": account.to_safe_json(),
        "audit_path_inside_repo": payload.get("audit_path_inside_repo") is True,
        "network_call_made": payload.get("network_call_made") is True,
        "order_placed": payload.get("order_placed") is True,
        "order_modified": payload.get("order_modified") is True,
        "order_cancelled": payload.get("order_cancelled") is True,
        "live_write": payload.get("live_write") is True,
        "order_or_subscription_created": (payload.get("order_or_subscription_created") is True),
        "write_result": payload,
        "git": current_git_state().model_dump(mode="json"),
    }


async def _create_preview(
    client: Client[FastMCPTransport],
    spec: OrderWriteSpec,
    account_key: str,
) -> tuple[dict[str, JsonValue], SetupOrder | None]:
    request, setup = await _preview_request_for_probe(client, spec, account_key)
    result = await client.call_tool(
        "saxo_create_write_preview",
        request,
    )
    return _payload(result.structured_content), setup


async def _preview_request_for_probe(
    client: Client[FastMCPTransport],
    spec: OrderWriteSpec,
    account_key: str,
) -> tuple[dict[str, JsonValue], SetupOrder | None]:
    if spec.write_class not in {
        "modify",
        "cancel",
        "cancel-by-instrument",
        "multileg-modify",
        "multileg-cancel",
    }:
        return probe_preview_request(spec, account_key), None
    setup = (
        await _create_setup_multileg_order(client, account_key)
        if spec.write_class in {"multileg-modify", "multileg-cancel"}
        else await _create_setup_stock_order(client, account_key)
    )
    return (
        probe_preview_request(
            spec,
            account_key,
            request_body=_request_body(spec, account_key, order_id=setup.order_id),
        ),
        setup,
    )


async def _call_order_tool(
    client: Client[FastMCPTransport],
    spec: OrderWriteSpec,
    preview: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    token = str(preview.get("preview_token", ""))
    result = await client.call_tool(
        spec.tool_name,
        {"preview_token": token, "approval_factor": TEST_APPROVAL_FACTOR},
        raise_on_error=False,
    )
    payload = _payload(result.structured_content)
    if payload.get("network_call_made") is True:
        await anyio.sleep(ORDER_WRITE_SETTLE_SECONDS)
    return payload


async def _create_setup_stock_order(
    client: Client[FastMCPTransport],
    account_key: str,
) -> SetupOrder:
    await anyio.sleep(ORDER_WRITE_SETTLE_SECONDS)
    spec = ORDER_WRITE_SPECS["place"]
    preview_result = await client.call_tool(
        "saxo_create_write_preview",
        probe_preview_request(
            spec,
            account_key,
            request_body=_setup_place_body(account_key),
        ),
    )
    setup_payload = await _call_order_tool(
        client,
        spec,
        _payload(preview_result.structured_content),
    )
    order_id, read_report = await _raw_fixture_open_order_id(
        account_key,
        tool_name=spec.tool_name,
    )
    report: dict[str, JsonValue] = {
        "setup_required": True,
        "setup_tool_name": spec.tool_name,
        "setup_tool_status": str(setup_payload.get("status", "")),
        "setup_denial_reason": str(setup_payload.get("denial_reason") or ""),
        "setup_reason": str(setup_payload.get("reason") or ""),
        "setup_network_call_made": setup_payload.get("network_call_made") is True,
        "setup_order_created": setup_payload.get("order_or_subscription_created") is True,
        "setup_open_order_found": order_id is not None,
        **read_report,
    }
    return SetupOrder(
        order_id=order_id or "fixture-order-id",
        order_kind="single",
        report=report,
    )


async def _create_setup_multileg_order(
    client: Client[FastMCPTransport],
    account_key: str,
) -> SetupOrder:
    await anyio.sleep(ORDER_WRITE_SETTLE_SECONDS)
    spec = ORDER_WRITE_SPECS["multileg-place"]
    external_reference = _external_reference("setup")
    preview_result = await client.call_tool(
        "saxo_create_write_preview",
        probe_preview_request(
            spec,
            account_key,
            request_body=_multileg_body(
                account_key,
                external_reference=external_reference,
            ),
        ),
    )
    setup_payload = await _call_order_tool(
        client,
        spec,
        _payload(preview_result.structured_content),
    )
    order_id, read_report = await _raw_multileg_open_order_id(
        account_key,
        external_reference=external_reference,
        tool_name=spec.tool_name,
    )
    report: dict[str, JsonValue] = {
        "setup_required": True,
        "setup_order_kind": "multileg",
        "setup_tool_name": spec.tool_name,
        "setup_tool_status": str(setup_payload.get("status", "")),
        "setup_denial_reason": str(setup_payload.get("denial_reason") or ""),
        "setup_reason": str(setup_payload.get("reason") or ""),
        "setup_network_call_made": setup_payload.get("network_call_made") is True,
        "setup_order_created": setup_payload.get("order_or_subscription_created") is True,
        "setup_open_order_found": order_id is not None,
        **read_report,
    }
    return SetupOrder(
        order_id=order_id or "fixture-multileg-order-id",
        order_kind="multileg",
        report=report,
    )


async def _post_tool_cleanup(
    client: Client[FastMCPTransport],
    spec: OrderWriteSpec,
    account_key: str,
    setup: SetupOrder | None,
) -> dict[str, JsonValue]:
    if setup is None and spec.write_class == "place":
        return await _post_single_place_cleanup(client, account_key, spec.tool_name)
    if setup is None and spec.write_class == "multileg-place":
        return await _post_multileg_place_cleanup(client, account_key, spec.tool_name)
    if setup is None or setup.report.get("setup_open_order_found") is not True:
        return {"setup_cleanup_required": False}
    present_after_tool, read_report = await _raw_setup_order_present(setup, spec.tool_name)
    cleanup_report: dict[str, JsonValue] = {
        "setup_cleanup_required": True,
        "setup_order_absence_checked": True,
        "setup_order_absent_after_tool": present_after_tool is False,
        "setup_order_still_open_after_tool": present_after_tool is True,
        "setup_cleanup_attempted": False,
        **read_report,
    }
    if setup.order_kind == "multileg" and spec.write_class == "multileg-modify":
        price, price_report = await _raw_multileg_order_price(
            setup.order_id,
            tool_name=spec.tool_name,
        )
        cleanup_report.update(
            {
                "setup_modified_after_tool": price == MULTILEG_MODIFIED_LIMIT_PRICE,
                **price_report,
            },
        )
    if present_after_tool is not True:
        return cleanup_report

    cleanup_payload = (
        await _cancel_multileg_order(client, account_key, setup.order_id)
        if setup.order_kind == "multileg"
        else await _cancel_fixture_orders_by_instrument(client, account_key)
    )
    cleanup_tool_name = (
        ORDER_WRITE_SPECS["multileg-cancel"].tool_name
        if setup.order_kind == "multileg"
        else ORDER_WRITE_SPECS["cancel-by-instrument"].tool_name
    )
    present_after_cleanup, cleanup_read = await _raw_setup_order_present(
        setup,
        cleanup_tool_name,
    )
    cleanup_report.update(
        {
            "setup_cleanup_attempted": True,
            "setup_cleanup_tool_status": str(cleanup_payload.get("status", "")),
            "setup_cleanup_network_call_made": (
                cleanup_payload.get("network_call_made") is True
            ),
            "setup_cleanup_final_absent": present_after_cleanup is False,
            **cleanup_read,
        },
    )
    return cleanup_report


async def _post_single_place_cleanup(
    client: Client[FastMCPTransport],
    account_key: str,
    tool_name: str,
) -> dict[str, JsonValue]:
    order_id, read_report = await _raw_fixture_open_order_id(
        account_key,
        tool_name=tool_name,
    )
    cleanup_report: dict[str, JsonValue] = {
        "setup_cleanup_required": True,
        "setup_order_absence_checked": True,
        "setup_order_absent_after_tool": order_id is None,
        "setup_order_still_open_after_tool": order_id is not None,
        "setup_cleanup_attempted": False,
        **read_report,
    }
    if order_id is None:
        return cleanup_report
    cleanup_payload = await _cancel_fixture_orders_by_instrument(client, account_key)
    present_after_cleanup, cleanup_read = await _raw_order_id_present(
        order_id,
        tool_name=ORDER_WRITE_SPECS["cancel-by-instrument"].tool_name,
    )
    cleanup_report.update(
        {
            "setup_cleanup_attempted": True,
            "setup_cleanup_tool_status": str(cleanup_payload.get("status", "")),
            "setup_cleanup_network_call_made": (
                cleanup_payload.get("network_call_made") is True
            ),
            "setup_cleanup_final_absent": present_after_cleanup is False,
            **cleanup_read,
        },
    )
    return cleanup_report


async def _post_multileg_place_cleanup(
    client: Client[FastMCPTransport],
    account_key: str,
    tool_name: str,
) -> dict[str, JsonValue]:
    order_id, read_report = await _raw_multileg_open_order_id(
        account_key,
        external_reference=None,
        tool_name=tool_name,
    )
    cleanup_report: dict[str, JsonValue] = {
        "setup_cleanup_required": True,
        "setup_order_absence_checked": True,
        "setup_order_absent_after_tool": order_id is None,
        "setup_order_still_open_after_tool": order_id is not None,
        "setup_cleanup_attempted": False,
        **read_report,
    }
    if order_id is None:
        return cleanup_report
    cleanup_payload = await _cancel_multileg_order(client, account_key, order_id)
    present_after_cleanup, cleanup_read = await _raw_multileg_order_id_present(
        order_id,
        tool_name=ORDER_WRITE_SPECS["multileg-cancel"].tool_name,
    )
    cleanup_report.update(
        {
            "setup_cleanup_attempted": True,
            "setup_cleanup_tool_status": str(cleanup_payload.get("status", "")),
            "setup_cleanup_network_call_made": (
                cleanup_payload.get("network_call_made") is True
            ),
            "setup_cleanup_final_absent": present_after_cleanup is False,
            **cleanup_read,
        },
    )
    return cleanup_report


async def _raw_setup_order_present(
    setup: SetupOrder,
    tool_name: str,
) -> tuple[bool | None, dict[str, JsonValue]]:
    if setup.order_kind == "multileg":
        return await _raw_multileg_order_id_present(setup.order_id, tool_name=tool_name)
    return await _raw_order_id_present(setup.order_id, tool_name=tool_name)


async def _cancel_fixture_orders_by_instrument(
    client: Client[FastMCPTransport],
    account_key: str,
) -> dict[str, JsonValue]:
    spec = ORDER_WRITE_SPECS["cancel-by-instrument"]
    preview_result = await client.call_tool(
        "saxo_create_write_preview",
        probe_preview_request(spec, account_key),
    )
    return await _call_order_tool(client, spec, _payload(preview_result.structured_content))


async def _cancel_multileg_order(
    client: Client[FastMCPTransport],
    account_key: str,
    multi_leg_order_id: str,
) -> dict[str, JsonValue]:
    spec = ORDER_WRITE_SPECS["multileg-cancel"]
    preview_result = await client.call_tool(
        "saxo_create_write_preview",
        probe_preview_request(
            spec,
            account_key,
            request_body=_request_body(
                spec,
                account_key,
                order_id=multi_leg_order_id,
            ),
        ),
    )
    return await _call_order_tool(client, spec, _payload(preview_result.structured_content))


async def _raw_fixture_open_order_id(
    account_key: str,
    *,
    tool_name: str,
) -> tuple[str | None, dict[str, JsonValue]]:
    orders, report = await _raw_open_orders(tool_name=tool_name)
    matched = [
        order_id
        for row in orders
        if _is_fixture_stock_order(row, account_key)
        for order_id in _strings_at(row, "OrderId")
    ]
    return (
        matched[0] if matched else None,
        {
            **report,
            "setup_raw_matching_open_order_count": len(matched),
            "setup_raw_order_id_redacted": bool(matched),
        },
    )


async def _raw_multileg_open_order_id(
    account_key: str,
    *,
    external_reference: str | None,
    tool_name: str,
) -> tuple[str | None, dict[str, JsonValue]]:
    orders, report = await _raw_open_orders(tool_name=tool_name)
    matched = _unique_strings(
        _multileg_order_id(row)
        for row in orders
        if _is_fixture_multileg_order(row, account_key, external_reference)
    )
    return (
        matched[0] if matched else None,
        {
            **report,
            "setup_raw_matching_multileg_order_count": len(matched),
            "setup_raw_order_id_redacted": bool(matched),
        },
    )


async def _raw_order_id_present(
    order_id: str,
    *,
    tool_name: str,
) -> tuple[bool | None, dict[str, JsonValue]]:
    if not order_id or order_id == "fixture-order-id":
        return None, {"setup_raw_order_id_redacted": False, "setup_raw_read_skipped": True}
    orders, report = await _raw_open_orders(tool_name=tool_name)
    present = any(order_id in _strings_at(row, "OrderId") for row in orders)
    return (
        present,
        {
            **report,
            "setup_raw_order_id_redacted": True,
            "setup_raw_order_id_present": present,
        },
    )


async def _raw_multileg_order_id_present(
    order_id: str,
    *,
    tool_name: str,
) -> tuple[bool | None, dict[str, JsonValue]]:
    if not order_id or order_id == "fixture-multileg-order-id":
        return None, {"setup_raw_order_id_redacted": False, "setup_raw_read_skipped": True}
    orders, report = await _raw_open_orders(tool_name=tool_name)
    present = any(_multileg_order_id(row) == order_id for row in orders)
    return (
        present,
        {
            **report,
            "setup_raw_order_id_redacted": True,
            "setup_raw_order_id_present": present,
        },
    )


async def _raw_multileg_order_price(
    order_id: str,
    *,
    tool_name: str,
) -> tuple[float | None, dict[str, JsonValue]]:
    if not order_id or order_id == "fixture-multileg-order-id":
        return None, {"setup_raw_order_id_redacted": False, "setup_raw_read_skipped": True}
    orders, report = await _raw_open_orders(tool_name=tool_name)
    prices = [
        price
        for row in orders
        if _multileg_order_id(row) == order_id
        for price in (_multileg_order_price(row),)
        if price is not None
    ]
    return (
        prices[0] if prices else None,
        {
            **report,
            "setup_raw_order_id_redacted": True,
            "setup_raw_multileg_price_found": bool(prices),
        },
    )


async def _raw_open_orders(
    *,
    tool_name: str,
) -> tuple[tuple[dict[str, JsonValue], ...], dict[str, JsonValue]]:
    try:
        settings = resolve_sim_auth_settings(require_redirect=False)
    except SimAuthSettingsError as error:
        return (), _raw_read_report("auth_unavailable", reason=error.code)
    cache_check = cached_token_for_tool(tool_name, settings.cache_path)
    match cache_check:
        case CachedTokenReady(token=token):
            access_token = token.access_token
        case CachedTokenBlocked(result=result):
            return (), _raw_read_report(
                "auth_unavailable",
                reason=str(result.get("reason", "token_missing")),
            )
    try:
        async with create_async_client(base_url=SIM_ENDPOINTS.rest_base_url) as client:
            response = await client.get(
                "port/v1/orders/me",
                headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
            )
    except httpx2.HTTPError as error:
        return (), _raw_read_report("network_error", reason=type(error).__name__)
    rows = _order_rows(_response_json(response))
    return (
        rows,
        {
            "setup_raw_read_status": (
                "passed"
                if HTTP_SUCCESS_MIN <= response.status_code < HTTP_SUCCESS_MAX
                else "http_error"
            ),
            "setup_raw_read_network_call_made": True,
            "setup_raw_read_http_status": response.status_code,
            "setup_raw_open_order_count": len(rows),
        },
    )


def _raw_read_report(status: str, *, reason: str) -> dict[str, JsonValue]:
    return {
        "setup_raw_read_status": status,
        "setup_raw_read_network_call_made": False,
        "setup_raw_read_http_status": None,
        "setup_raw_open_order_count": 0,
        "setup_raw_read_reason": reason,
    }


def _response_json(response: httpx2.Response) -> JsonValue:
    try:
        return JSON_VALUE_ADAPTER.validate_python(response.json())
    except (ValueError, TypeError):
        return None


def _order_rows(value: JsonValue) -> tuple[dict[str, JsonValue], ...]:
    if not isinstance(value, Mapping):
        return ()
    data = value.get("Data")
    if isinstance(data, str) or not isinstance(data, Sequence):
        return ()
    return tuple(row for item in data if (row := _object(item)) is not None)


def _is_fixture_stock_order(row: Mapping[str, JsonValue], account_key: str) -> bool:
    account = row.get("AccountKey")
    account_matches = not isinstance(account, str) or account == account_key
    return (
        account_matches
        and _number_at(row, "Uic") == FIXTURE_INSTRUMENT
        and _string_at(row, "AssetType") == FIXTURE_ASSET_TYPE
    )


def _is_fixture_multileg_order(
    row: Mapping[str, JsonValue],
    account_key: str,
    external_reference: str | None,
) -> bool:
    account = row.get("AccountKey")
    account_matches = not isinstance(account, str) or account == account_key
    reference = _string_at(row, "ExternalReference")
    reference_matches = external_reference is None or reference == external_reference
    return (
        account_matches
        and reference_matches
        and _multileg_order_id(row) is not None
        and _number_at(row, "Uic") in MULTILEG_FIXTURE_UICS
    )


def _multileg_order_id(row: Mapping[str, JsonValue]) -> str | None:
    details = row.get("MultiLegOrderDetails")
    if not isinstance(details, Mapping):
        return None
    return _string_at(details, "MultiLegOrderId")


def _multileg_order_price(row: Mapping[str, JsonValue]) -> float | None:
    details = row.get("MultiLegOrderDetails")
    if not isinstance(details, Mapping):
        return None
    value = details.get("Price")
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _unique_strings(values: Iterable[str | None]) -> tuple[str, ...]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None or value in seen:
            continue
        unique.append(value)
        seen.add(value)
    return tuple(unique)


def _object(value: object) -> dict[str, JsonValue] | None:
    if not isinstance(value, Mapping):
        return None
    return JSON_OBJECT_ADAPTER.validate_python(value)


def _strings_at(row: Mapping[str, JsonValue], key: str) -> tuple[str, ...]:
    value = row.get(key)
    return (value.strip(),) if isinstance(value, str) and value.strip() else ()


def _string_at(row: Mapping[str, JsonValue], key: str) -> str | None:
    values = _strings_at(row, key)
    return values[0] if values else None


def _number_at(row: Mapping[str, JsonValue], key: str) -> int | None:
    value = row.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    return None


def class_report_for_qa(
    spec: OrderWriteSpec,
    preview: dict[str, JsonValue],
    tool_payload: dict[str, JsonValue],
    *,
    setup: SetupOrder | None = None,
    cleanup: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    setup_report = {} if setup is None else setup.report
    cleanup_report = {} if cleanup is None else cleanup
    completed = _completion_requirements_met(
        spec,
        tool_payload,
        setup=setup_report,
        cleanup=cleanup_report,
    )
    status = _class_status(tool_payload, completed=completed)
    reason = "" if completed else _agent_reason(tool_payload)
    return {
        "write_class": spec.write_class,
        "tool_name": spec.tool_name,
        "operation_id": spec.operation_id,
        "status": status,
        "preview_status": str(preview.get("status", "")),
        "tool_status": str(tool_payload.get("status", "")),
        "write_class_status": str(tool_payload.get("write_class_status", status)),
        "real_mutation_proven": completed,
        "completion_oracle": _completion_oracle(spec),
        "completion_not_claimed_reason": _completion_not_claimed_reason(
            spec,
            tool_payload,
            completed=completed,
        ),
        "fastmcp_called": tool_payload.get("fastmcp_called") is True,
        "preview_token_redacted": _preview_token_redacted(preview),
        "approval_factor_mode": str(tool_payload.get("approval_factor_mode", "test_only_sim")),
        "x_request_id_present": tool_payload.get("x_request_id_present") is True,
        "x_request_id_response_echo_verified": (
            tool_payload.get("x_request_id_response_echo_verified") is True
        ),
        "order_result_parsed": tool_payload.get("order_result_parsed") is True,
        "port_orders_readback": tool_payload.get("port_orders_readback") is True,
        "trade_messages_readback": tool_payload.get("trade_messages_readback") is True,
        "open_order_readback_matched_response_order": (
            tool_payload.get("open_order_readback_matched_response_order") is True
        ),
        "open_order_readback_confirmed_absent": (
            tool_payload.get("open_order_readback_confirmed_absent") is True
        ),
        "cleanup_attempted": tool_payload.get("cleanup_attempted") is True,
        "cleanup_status": str(tool_payload.get("cleanup_status", "not_run")),
        "qa_setup": setup_report,
        "qa_cleanup": cleanup_report,
        "raw_audit_path_inside_repo": tool_payload.get("raw_audit_path_inside_repo") is True,
        "account_key_redacted": _account_key_redacted(tool_payload),
        "mutation_may_have_occurred": tool_payload.get("mutation_may_have_occurred") is True,
        "mutation_content_verified": tool_payload.get("mutation_content_verified") is True,
        "retry_unsafe": tool_payload.get("retry_unsafe") is True,
        "committed_before_network_result": (
            tool_payload.get("committed_before_network_result") is True
        ),
        "order_placed": tool_payload.get("order_placed"),
        "order_modified": tool_payload.get("order_modified"),
        "order_cancelled": tool_payload.get("order_cancelled"),
        "network_call_made": tool_payload.get("network_call_made") is True,
        "live_write": tool_payload.get("live_write") is True,
        "order_or_subscription_created": (
            tool_payload.get("order_or_subscription_created") is True
        ),
        "denial_reason": str(tool_payload.get("denial_reason") or ""),
        "reason": reason,
        "next_action": str(tool_payload.get("next_action", "")),
        "does_not_verify": _does_not_verify(tool_payload),
    }


def _completion_requirements_met(  # noqa: PLR0911
    spec: OrderWriteSpec,
    tool_payload: dict[str, JsonValue],
    *,
    setup: dict[str, JsonValue] | None = None,
    cleanup: dict[str, JsonValue] | None = None,
) -> bool:
    setup_report = {} if setup is None else setup
    cleanup_report = {} if cleanup is None else cleanup
    tool_completed = tool_payload.get("status") == "completed"
    setup_proven_cancel_by_instrument = (
        spec.write_class == "cancel-by-instrument"
        and tool_payload.get("status") == "completed_unverified"
        and _setup_order_proven(setup_report)
        and _raw_absence_proven(cleanup_report)
    )
    cleanup_proven_multileg_place = (
        spec.write_class == "multileg-place"
        and tool_payload.get("status") == "completed_unverified"
        and _raw_cleanup_final_absent_proven(cleanup_report)
    )
    cleanup_proven_place = (
        spec.write_class == "place"
        and tool_payload.get("status") == "completed_unverified"
        and _raw_cleanup_final_absent_proven(cleanup_report)
    )
    readback_proven_multileg_modify = (
        spec.write_class == "multileg-modify"
        and tool_payload.get("status") == "completed_unverified"
        and _setup_order_proven(setup_report)
        and cleanup_report.get("setup_modified_after_tool") is True
    )
    common_requirements_met = (
        (
            tool_completed
            or setup_proven_cancel_by_instrument
            or cleanup_proven_place
            or cleanup_proven_multileg_place
            or readback_proven_multileg_modify
        )
        and tool_payload.get("network_call_made") is True
        and tool_payload.get("order_result_parsed") is True
        and tool_payload.get("x_request_id_present") is True
        and tool_payload.get("retry_unsafe") is not True
    )
    if not common_requirements_met:
        return False
    if spec.write_class == "cancel-by-instrument":
        return (
            _setup_order_proven(setup_report)
            and _raw_absence_proven(cleanup_report)
            and tool_payload.get("trade_messages_readback") is True
        )
    if spec.write_class == "cancel":
        return (
            _setup_order_proven(setup_report)
            and tool_payload.get("mutation_content_verified") is True
            and _raw_absence_proven(cleanup_report)
            and tool_payload.get("port_orders_readback") is True
            and tool_payload.get("trade_messages_readback") is True
        )
    if spec.write_class == "multileg-cancel":
        return (
            _setup_order_proven(setup_report)
            and tool_payload.get("mutation_content_verified") is True
            and _raw_absence_proven(cleanup_report)
            and tool_payload.get("port_orders_readback") is True
            and tool_payload.get("trade_messages_readback") is True
        )
    if spec.write_class == "multileg-modify":
        return (
            _setup_order_proven(setup_report)
            and cleanup_report.get("setup_modified_after_tool") is True
            and tool_payload.get("port_orders_readback") is True
            and tool_payload.get("trade_messages_readback") is True
            and _raw_cleanup_final_absent_proven(cleanup_report)
        )
    if spec.write_class == "modify":
        return (
            _setup_order_proven(setup_report)
            and tool_payload.get("mutation_content_verified") is True
            and tool_payload.get("port_orders_readback") is True
            and tool_payload.get("trade_messages_readback") is True
            and _raw_cleanup_final_absent_proven(cleanup_report)
        )
    if spec.write_class == "multileg-place":
        return (
            tool_payload.get("mutation_content_verified") is True
            and tool_payload.get("port_orders_readback") is True
            and tool_payload.get("trade_messages_readback") is True
            and _raw_cleanup_final_absent_proven(cleanup_report)
        )
    if spec.write_class == "place":
        return (
            tool_payload.get("mutation_content_verified") is True
            and tool_payload.get("port_orders_readback") is True
            and tool_payload.get("trade_messages_readback") is True
            and (
                tool_payload.get("cleanup_status") == "verified_no_open_order"
                or _raw_cleanup_final_absent_proven(cleanup_report)
            )
        )
    return (
        tool_payload.get("mutation_content_verified") is True
        and tool_payload.get("port_orders_readback") is True
        and tool_payload.get("trade_messages_readback") is True
    )


def _setup_order_proven(setup_report: dict[str, JsonValue]) -> bool:
    return (
        setup_report.get("setup_order_created") is True
        and setup_report.get("setup_open_order_found") is True
    )


def _raw_absence_proven(cleanup_report: dict[str, JsonValue]) -> bool:
    return (
        cleanup_report.get("setup_order_absent_after_tool") is True
        and cleanup_report.get("setup_raw_read_status") == "passed"
    )


def _raw_cleanup_final_absent_proven(cleanup_report: dict[str, JsonValue]) -> bool:
    return (
        cleanup_report.get("setup_cleanup_final_absent") is True
        and cleanup_report.get("setup_raw_read_status") == "passed"
    )


def _completion_oracle(spec: OrderWriteSpec) -> str:
    if spec.write_class == "cancel-by-instrument":
        return (
            "To claim completion, the output must show a generated x-request-id, "
            "retry_unsafe=false, trade-message readback, a setup SIM order that matched the "
            "delete filter before the tool call, and raw readback proving that setup order was "
            "absent after the tool call"
        )
    if spec.write_class in {"place", "multileg-place"}:
        return (
            "completed response parsed, x-request-id present, retry safe, mutation content "
            "verified, portfolio order-list readback, trade messages readback, and cleanup_status "
            "verified_no_open_order"
        )
    return (
        "completed response parsed, x-request-id present, retry safe, mutation content "
        "verified, portfolio order-list readback, and trade messages readback"
    )


def _completion_not_claimed_reason(
    spec: OrderWriteSpec,
    tool_payload: dict[str, JsonValue],
    *,
    completed: bool,
) -> str:
    if completed:
        return ""
    if (
        spec.write_class == "cancel-by-instrument"
        and tool_payload.get("status") == "completed_unverified"
    ):
        return (
            "empty-success delete-by-instrument did not prove any order matched the "
            "cancel filter"
        )
    if tool_payload.get("status") == "completed":
        return "completed response lacked the class-specific proof required by the oracle"
    return _agent_reason(tool_payload)


def _agent_reason(tool_payload: dict[str, JsonValue]) -> str:
    for key in ("reason", "denial_reason"):
        value = tool_payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    parsed = tool_payload.get("parsed_response")
    if isinstance(parsed, dict):
        status = str(tool_payload.get("status", "unknown"))
        http_status = str(tool_payload.get("http_status", "unknown"))
        error_codes = _error_codes(parsed)
        codes = ",".join(error_codes) if error_codes else "none"
        return (
            "saxo_order_write_not_completed "
            f"status={status} http_status={http_status} error_codes={codes}"
        )
    return ""


def _error_codes(parsed_response: dict[str, JsonValue]) -> list[str]:
    raw = parsed_response.get("error_codes")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _does_not_verify(tool_payload: dict[str, JsonValue]) -> list[str]:
    raw = tool_payload.get("does_not_verify")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


def _class_status(tool_payload: dict[str, JsonValue], *, completed: bool) -> str:
    if completed:
        return "completed"
    status = tool_payload.get("status")
    if status == "auth_required":
        return "incomplete"
    if status == "denied":
        return "refused"
    if (
        status in {"completed", "completed_unverified"}
        and tool_payload.get("network_call_made") is True
    ):
        return "exercised"
    if _safely_rejected_by_saxo(tool_payload):
        return "incomplete"
    return "failed"


def _safely_rejected_by_saxo(tool_payload: dict[str, JsonValue]) -> bool:
    return (
        tool_payload.get("status") == "failed"
        and tool_payload.get("network_call_made") is True
        and tool_payload.get("order_or_subscription_created") is not True
        and tool_payload.get("mutation_may_have_occurred") is not True
        and tool_payload.get("retry_unsafe") is not True
        and tool_payload.get("order_result_parsed") is True
    )


def _preview_token_redacted(preview: dict[str, JsonValue]) -> bool:
    return isinstance(preview.get("preview_token"), str)


def _account_key_redacted(tool_payload: dict[str, JsonValue]) -> bool:
    return "AccountKey" not in json.dumps(tool_payload, sort_keys=True)


def probe_preview_request(
    spec: OrderWriteSpec,
    account_key: str,
    *,
    request_body: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    instrument_uic = (
        MULTILEG_FIXTURE_UICS[0]
        if spec.write_class in {"multileg-place", "multileg-modify", "multileg-cancel"}
        else FIXTURE_INSTRUMENT
    )
    return {
        "operation_id": spec.operation_id,
        "account_key": account_key,
        "instrument_uic": instrument_uic,
        "quantity": FIXTURE_ORDER_AMOUNT,
        "estimated_notional": FIXTURE_ORDER_NOTIONAL,
        "account_currency": "USD",
        "risk": {
            "cost": FIXTURE_ORDER_NOTIONAL,
            "cash_required": FIXTURE_ORDER_NOTIONAL,
            "margin_impact": 1,
            "contract_multiplier": 1,
            "conversion_known": True,
        },
        "request_body": _request_body(spec, account_key)
        if request_body is None
        else request_body,
    }


def _request_body(  # noqa: PLR0911
    spec: OrderWriteSpec,
    account_key: str,
    *,
    order_id: str | None = None,
) -> dict[str, JsonValue]:
    common: dict[str, JsonValue] = {"AccountKey": account_key}
    match spec.write_class:
        case "cancel":
            return {**common, "OrderIds": order_id or "fixture-order-id"}
        case "modify":
            return _modify_body(account_key, order_id or "fixture-order-id")
        case "cancel-by-instrument":
            return {**common, "AssetType": FIXTURE_ASSET_TYPE, "Uic": FIXTURE_INSTRUMENT}
        case "multileg-cancel":
            return {**common, "MultiLegOrderId": order_id or "fixture-multileg-order-id"}
        case "multileg-place":
            return _multileg_body(account_key, external_reference=_external_reference("place"))
        case "multileg-modify":
            return _multileg_body(
                account_key,
                external_reference=_external_reference("modify"),
                multi_leg_order_id=order_id or "fixture-multileg-order-id",
                order_price=MULTILEG_MODIFIED_LIMIT_PRICE,
            )
        case _:
            return {
                **common,
                "Uic": FIXTURE_INSTRUMENT,
                "AssetType": FIXTURE_ASSET_TYPE,
                "Amount": FIXTURE_ORDER_AMOUNT,
                "BuySell": "Buy",
                "ManualOrder": False,
                "OrderType": "Market",
                "OrderDuration": {"DurationType": "DayOrder"},
            }


def _setup_place_body(account_key: str) -> dict[str, JsonValue]:
    return {
        "AccountKey": account_key,
        "Uic": FIXTURE_INSTRUMENT,
        "AssetType": FIXTURE_ASSET_TYPE,
        "Amount": FIXTURE_ORDER_AMOUNT,
        "BuySell": "Buy",
        "ManualOrder": False,
        "OrderType": "Limit",
        "OrderPrice": FIXTURE_LIMIT_PRICE,
        "OrderDuration": {"DurationType": "DayOrder"},
    }


def _modify_body(account_key: str, order_id: str) -> dict[str, JsonValue]:
    return {
        **_setup_place_body(account_key),
        "OrderId": order_id,
        "OrderPrice": FIXTURE_MODIFIED_LIMIT_PRICE,
    }


def _multileg_body(
    account_key: str,
    *,
    external_reference: str,
    multi_leg_order_id: str | None = None,
    order_price: int = MULTILEG_LIMIT_PRICE,
) -> dict[str, JsonValue]:
    body: dict[str, JsonValue] = {
        "AccountKey": account_key,
        "OrderType": "Limit",
        "OrderPrice": order_price,
        "ManualOrder": True,
        "OrderDuration": {"DurationType": "DayOrder"},
        "ExternalReference": external_reference,
        "Legs": [
            {
                "Uic": MULTILEG_FIXTURE_UICS[0],
                "AssetType": MULTILEG_FIXTURE_ASSET_TYPE,
                "Amount": FIXTURE_ORDER_AMOUNT,
                "BuySell": "Buy",
                "ToOpenClose": "ToOpen",
            },
            {
                "Uic": MULTILEG_FIXTURE_UICS[1],
                "AssetType": MULTILEG_FIXTURE_ASSET_TYPE,
                "Amount": FIXTURE_ORDER_AMOUNT,
                "BuySell": "Buy",
                "ToOpenClose": "ToOpen",
            },
        ],
    }
    if multi_leg_order_id is not None:
        body["MultiLegOrderId"] = multi_leg_order_id
    return body


def _external_reference(label: str) -> str:
    return f"qa-mleg-{label}-{uuid.uuid4().hex[:12]}"


def _requested_classes(classes: str | None) -> tuple[OrderWriteClass, ...]:
    if classes is None or not classes.strip():
        return ORDER_WRITE_CLASSES
    requested: list[OrderWriteClass] = []
    for raw in classes.split(","):
        parsed = _parse_order_write_class(raw)
        if parsed is not None:
            requested.append(parsed)
    return tuple(requested) if requested else ORDER_WRITE_CLASSES


def _parse_order_write_class(raw: str) -> OrderWriteClass | None:
    return ORDER_WRITE_CLASS_BY_NAME.get(raw.strip())


def _payload(value: object) -> dict[str, JsonValue]:
    return JSON_OBJECT_ADAPTER.validate_python(value)


def _write_redacted_with_secret_scan(
    out: Path,
    payload: dict[str, JsonValue],
    success_statuses: tuple[str, ...],
) -> int:
    redacted = redact_json(payload)
    if not isinstance(redacted, dict):
        raise TypeError("order probe redaction returned non-object")
    write_json(out, redacted)
    findings, scan_errors = scan_secret_paths([str(out)])
    redacted["secret_scan"] = {"findings": findings, "scan_errors": scan_errors}
    write_json(out, redacted)
    clean = not findings and not scan_errors
    return 0 if redacted.get("status") in success_statuses and clean else 1


@contextmanager
def _safety_env(account_key: str) -> Generator[None]:
    previous = {key: os.environ.get(key) for key in _SAFETY_ENV_DEFAULTS}
    try:
        for key, value in _SAFETY_ENV_DEFAULTS.items():
            os.environ[key] = value
        os.environ["SAXO_MCP_ACCOUNT_ALLOWLIST"] = account_key
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


_SAFETY_ENV_DEFAULTS: Final = {
    "SAXO_MCP_ENVIRONMENT": "SIM",
    "SAXO_MCP_ACCOUNT_ALLOWLIST": FIXTURE_ACCOUNT,
    "SAXO_MCP_INSTRUMENT_ALLOWLIST": ",".join(
        str(uic) for uic in (FIXTURE_INSTRUMENT, *MULTILEG_FIXTURE_UICS)
    ),
}
