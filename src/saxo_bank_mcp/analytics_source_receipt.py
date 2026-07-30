from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Final, cast

import httpx2

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.analytics_provider import (
    SaxoAnalyticsProvider,
    SourceAccessError,
    SourceEntitlementError,
    SourceHttpError,
    SourceProviderError,
    SourceRateLimitError,
    SourceSchemaDriftError,
)
from saxo_bank_mcp.analytics_source_contracts import (
    FrozenSourceJsonValue,
    SourcePage,
    build_source_capture_context,
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.endpoint_registry import EndpointOperation, RegisteredEndpoint
from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.live_token_refresh import live_token_for_tool
from saxo_bank_mcp.registered_read_execution import (
    RegisteredReadClientFactory,
    RegisteredReadResponse,
    execute_registered_get,
)

_TIMESTAMP_PATTERN: Final = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$",
)


class _ReceiptExecutor:
    def __init__(self) -> None:
        self.network_call_count = 0
        self.attempt_count = 0
        self.initial_attempt_count = 0
        self.continuation_attempt_count = 0
        self.retry_count = 0
        self.environment: str | None = None
        self._initial_target: str | None = None
        self._targets: set[str] = set()

    @property
    def distinct_target_count(self) -> int:
        return len(self._targets)

    @property
    def continuation_call_count(self) -> int:
        return max(0, self.distinct_target_count - 1)

    async def __call__(
        self,
        operation: EndpointOperation,
        request_target: str,
        params: Mapping[str, str],
    ) -> httpx2.Response:
        target = _digest(
            {
                "operation_id": operation.operation_id,
                "params": dict(params),
                "request_target": request_target,
            },
        )
        self.attempt_count += 1
        if self._initial_target is None:
            self._initial_target = target
        if target in self._targets:
            self.retry_count += 1
        else:
            self._targets.add(target)
        if target == self._initial_target:
            self.initial_attempt_count += 1
        else:
            self.continuation_attempt_count += 1
        outcome = await execute_registered_get(
            operation,
            request_target,
            params,
            client_factory=cast("RegisteredReadClientFactory", _create_receipt_client),
            live_token_loader=live_token_for_tool,
        )
        if not isinstance(outcome, RegisteredReadResponse):
            if outcome.get("network_call_made") is True:
                self.network_call_count += 1
            raw_environment = outcome.get("environment") or outcome.get(
                "requested_environment",
            )
            if raw_environment in {"SIM", "LIVE"}:
                self.environment = raw_environment
            status = outcome.get("status")
            reason = outcome.get("reason")
            if status == "network_error":
                request = httpx2.Request("GET", "https://registered.invalid")
                raise httpx2.ReadError("registered_read_failed", request=request)
            safe_reason = reason if isinstance(reason, str) else "access_unavailable"
            raise SourceAccessError(operation.operation_id, safe_reason)
        self.network_call_count += 1
        self.environment = outcome.context.environment
        return outcome.response


async def analytics_contract_receipt(  # noqa: C901, PLR0911
    registered: RegisteredEndpoint,
    *,
    contract_id: str,
    params: Mapping[str, str],
) -> dict[str, JsonValue]:
    """Validate raw registered pages server-side and return only value-free proof."""
    contract = source_contracts_by_id().get(contract_id)
    if (
        contract is None
        or registered.operation.operation_id != contract.operation_id
        or registered.operation.path_template != contract.path_template
    ):
        return _refusal(registered.operation, contract_id, "analytics_contract_mismatch")
    try:
        request = _contract_request(registered, contract.path_parameters, params)
        capture = build_source_capture_context({contract_id: request})
    except (TypeError, ValueError):
        return _refusal(registered.operation, contract_id, "analytics_request_invalid")

    executor = _ReceiptExecutor()
    provider = SaxoAnalyticsProvider(
        request_executor=executor,
        contracts={contract_id: contract},
    )
    pages: list[SourcePage] = []
    try:
        pages.extend(
            [
                page
                async for page in provider.fetch(
                    contract_id,
                    request,
                    capture=capture,
                )
            ],
        )
    except SourceEntitlementError as error:
        return _failure(
            registered.operation,
            contract_id,
            "source_entitlement_unavailable",
            executor,
            http_status=error.http_status,
            request_fingerprint_sha256=capture.request_fingerprints[contract_id],
        )
    except SourceRateLimitError:
        return _failure(
            registered.operation,
            contract_id,
            "source_rate_limited",
            executor,
            http_status=429,
            request_fingerprint_sha256=capture.request_fingerprints[contract_id],
        )
    except SourceHttpError as error:
        return _failure(
            registered.operation,
            contract_id,
            "source_http_error",
            executor,
            http_status=error.http_status,
            request_fingerprint_sha256=capture.request_fingerprints[contract_id],
        )
    except SourceSchemaDriftError:
        return _failure(
            registered.operation,
            contract_id,
            "source_schema_drift",
            executor,
            request_fingerprint_sha256=capture.request_fingerprints[contract_id],
        )
    except SourceAccessError:
        return _failure(
            registered.operation,
            contract_id,
            "source_access_unavailable",
            executor,
            request_fingerprint_sha256=capture.request_fingerprints[contract_id],
        )
    except SourceProviderError as error:
        return _failure(
            registered.operation,
            contract_id,
            error.code,
            executor,
            request_fingerprint_sha256=capture.request_fingerprints[contract_id],
        )

    page_receipts: list[dict[str, JsonValue]] = []
    for page in pages:
        timestamps: list[str] = []
        for row in page.rows:
            _collect_timestamps(row, timestamps)
        page_receipts.append(
            {
                "page_number": page.page_number,
                "row_count": page.row_count,
                "page_fingerprint_sha256": page.page_fingerprint_sha256,
                "source_revision_sha256": _digest(page.source_revision),
                "schema_fingerprint_sha256": _digest(
                    page.schema_comparison.model_dump(mode="json"),
                ),
                "timestamp_value_count": len(timestamps),
                "timestamp_fingerprint_sha256": _digest(sorted(timestamps)),
            },
        )
    response_fingerprint = _digest(page_receipts)
    timestamp_value_count = sum(
        cast("int", page["timestamp_value_count"]) for page in page_receipts
    )
    timestamp_fingerprints = [page["timestamp_fingerprint_sha256"] for page in page_receipts]
    environment = executor.environment or "UNKNOWN"
    return {
        "status": "passed",
        "tool_name": "saxo_call_registered_endpoint",
        "call_class": (
            "live_read_succeeded"
            if environment == "LIVE"
            else "sim_read_succeeded"
            if environment == "SIM"
            else "registered_read_succeeded"
        ),
        "operation_id": contract.operation_id,
        "service_group": registered.operation.service_group,
        "method": "GET",
        "path": contract.path_template,
        "environment": environment,
        "network_call_made": executor.network_call_count > 0,
        "network_call_count": executor.network_call_count,
        "attempt_count": executor.attempt_count,
        "initial_attempt_count": executor.initial_attempt_count,
        "continuation_attempt_count": executor.continuation_attempt_count,
        "retry_count": executor.retry_count,
        "distinct_target_count": executor.distinct_target_count,
        "successful_page_count": len(pages),
        "live_write_called": False,
        "order_or_subscription_created": False,
        "arbitrary_url_allowed": False,
        "live_write": False,
        "live_access": environment == "LIVE",
        "auth_exercised": registered.operation.auth_requirement != "none",
        "trading_ready": False,
        "http_status": 200,
        "response": None,
        "response_visibility": "analytics_contract_receipt",
        "response_fingerprint": response_fingerprint,
        "response_fingerprint_scope": "analytics_contract_receipt",
        "analytics_contract_id": contract_id,
        "analytics_contract_sha256": source_contract_fingerprint(contract),
        "page_count": len(pages),
        "row_count": sum(page.row_count for page in pages),
        "continuation_call_count": executor.continuation_call_count,
        "page_receipts": page_receipts,
        "source_revision_fingerprint_sha256": _digest(
            [page["source_revision_sha256"] for page in page_receipts],
        ),
        "timestamp_value_count": timestamp_value_count,
        "timestamp_fingerprint_sha256": _digest(timestamp_fingerprints),
        "request_fingerprint_sha256": capture.request_fingerprints[contract_id],
    }


def _contract_request(
    registered: RegisteredEndpoint,
    path_parameters: Sequence[str],
    params: Mapping[str, str],
) -> dict[str, object]:
    request: dict[str, object] = dict(params)
    if path_parameters:
        template_parts = registered.operation.path_template.strip("/").split("/")
        resolved_parts = registered.resolved_path.strip("/").split("/")
        if len(template_parts) != len(resolved_parts):
            raise ValueError("registered path shape mismatch")
        for template, resolved in zip(template_parts, resolved_parts, strict=True):
            if template.startswith("{") and template.endswith("}"):
                name = template.removeprefix("{").removesuffix("}")
                if name not in path_parameters or not resolved:
                    raise ValueError("registered path parameter mismatch")
                request[name] = resolved
            elif template != resolved:
                raise ValueError("registered path mismatch")
    return request


def _failure(  # noqa: PLR0913
    operation: EndpointOperation,
    contract_id: str,
    reason: str,
    executor: _ReceiptExecutor,
    *,
    http_status: int | None = None,
    request_fingerprint_sha256: str | None = None,
) -> dict[str, JsonValue]:
    environment = executor.environment or "UNKNOWN"
    contract = source_contracts_by_id().get(contract_id)
    return {
        "status": "http_error" if http_status is not None else "refused",
        "tool_name": "saxo_call_registered_endpoint",
        "call_class": (
            "live_read_http_error"
            if environment == "LIVE" and http_status is not None
            else "live_read_refused"
            if environment == "LIVE"
            else "sim_read_http_error"
            if environment == "SIM" and http_status is not None
            else "sim_read_refused"
            if environment == "SIM"
            else "registered_read_refused"
        ),
        "operation_id": operation.operation_id,
        "service_group": operation.service_group,
        "method": "GET",
        "path": operation.path_template,
        "environment": environment,
        "network_call_made": executor.network_call_count > 0,
        "network_call_count": executor.network_call_count,
        "attempt_count": executor.attempt_count,
        "initial_attempt_count": executor.initial_attempt_count,
        "continuation_attempt_count": executor.continuation_attempt_count,
        "retry_count": executor.retry_count,
        "distinct_target_count": executor.distinct_target_count,
        "successful_page_count": 0,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "arbitrary_url_allowed": False,
        "live_write": False,
        "live_access": environment == "LIVE",
        "auth_exercised": operation.auth_requirement != "none",
        "trading_ready": False,
        "response": None,
        "response_visibility": "analytics_contract_receipt",
        "response_fingerprint": None,
        "response_fingerprint_scope": "analytics_contract_receipt",
        "analytics_contract_id": contract_id,
        "analytics_contract_sha256": (
            source_contract_fingerprint(contract) if contract is not None else None
        ),
        "page_count": 0,
        "row_count": 0,
        "continuation_call_count": executor.continuation_call_count,
        "reason": _safe_reason(reason),
        "http_status": http_status,
        "request_fingerprint_sha256": request_fingerprint_sha256,
    }


def _refusal(
    operation: EndpointOperation,
    contract_id: str,
    reason: str,
) -> dict[str, JsonValue]:
    return _failure(
        operation,
        contract_id,
        reason,
        _ReceiptExecutor(),
    )


def _collect_timestamps(
    value: FrozenSourceJsonValue,
    timestamps: list[str],
) -> None:
    if isinstance(value, Mapping):
        for child in value.values():
            _collect_timestamps(child, timestamps)
    elif isinstance(value, tuple):
        for child in value:
            _collect_timestamps(child, timestamps)
    elif isinstance(value, str) and _TIMESTAMP_PATTERN.fullmatch(value):
        timestamps.append(value)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            default=str,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()


def _safe_reason(value: str) -> str:
    return (
        value
        if re.fullmatch(r"[a-z][a-z0-9_:.-]{0,159}", value) is not None
        else "source_unavailable"
    )


def _create_receipt_client(*, base_url: str) -> httpx2.AsyncClient:
    """Leave transport retries to the provider's bounded retry policy."""
    return create_async_client(base_url=base_url, retries=0)


__all__ = ("analytics_contract_receipt",)
