from __future__ import annotations

import asyncio
import math
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Final, Protocol, cast
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx2
from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp.analytics_pagination import (
    DuplicateSourcePageError,
    InvalidPaginationPayloadError,
    PaginationCycleError,
    UnsafePaginationLinkError,
    follow_registered_pagination,
)
from saxo_bank_mcp.analytics_source_contracts import (
    SchemaComparison,
    SourceContract,
    SourceJsonValue,
    SourcePage,
    SourceResponseShape,
    compare_source_schema,
    freeze_source_rows,
    source_contracts_by_id,
    source_page_fingerprint,
)
from saxo_bank_mcp.endpoint_registry import (
    EndpointOperation,
    find_registered_endpoint,
)
from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.live_token_refresh import live_token_for_tool
from saxo_bank_mcp.read_tool_execution import execution_context, read_headers
from saxo_bank_mcp.read_tool_types import ReadExecutionContext
from saxo_bank_mcp.saxo_http_error_info import validated_saxo_error_code
from saxo_bank_mcp.strict_json import StrictJsonError, parse_json_value

_HTTP_SUCCESS_MIN: Final = 200
_HTTP_SUCCESS_MAX: Final = 300
_HTTP_UNAUTHORIZED: Final = 401
_HTTP_RATE_LIMITED: Final = 429
_MAX_PAGE_LIMIT: Final = 1_000
_MAX_RETRY_ATTEMPTS: Final = 3
_RETRYABLE_HTTP_STATUSES: Final = frozenset({408, 429, 500, 502, 503, 504})
_ENTITLEMENT_HTTP_STATUSES: Final = frozenset({403})
_ENTITLEMENT_ERROR_CODES: Final = frozenset(
    {
        "AccessDenied",
        "ClientNotEnabled",
        "Forbidden",
        "MarketDataNotEnabled",
        "NoAccess",
        "NotAuthorized",
    }
)
_BLOCKED_ROUTING_KEYS: Final = frozenset(
    {
        "__next",
        "endpoint",
        "method",
        "next",
        "operation",
        "operation_id",
        "path",
        "uri",
        "url",
    }
)
_PAGINATION_CONTROL_QUERY_PARAMETERS: Final = frozenset(
    {
        "$skip",
        "$skiptoken",
        "$top",
        "Count",
    }
)
_SAFE_PATH_VALUE_PATTERN: Final = re.compile(r"^[A-Za-z0-9._:+-]{1,255}$")
_SAFE_ERROR_CODE_PATTERN: Final = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,127}$")
_SAFE_REASON_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_SAFE_RESET_HINT_PATTERN: Final = re.compile(r"^[0-9]{1,20}$")
_MAX_RETRY_DELAY_SECONDS: Final = 30.0
_BASE_RETRY_DELAY_SECONDS: Final = 0.05
_SOURCE_OBJECT_ADAPTER: Final[TypeAdapter[dict[str, SourceJsonValue]]] = TypeAdapter(
    dict[str, SourceJsonValue]
)

type Sleep = Callable[[float], Awaitable[None]]


class RegisteredReadExecutor(Protocol):
    """Injected boundary for one already validated registered read."""

    async def __call__(
        self,
        operation: EndpointOperation,
        request_target: str,
        params: Mapping[str, str],
    ) -> httpx2.Response: ...


class SourceProviderError(RuntimeError):
    """Base error that retains only safe source metadata."""

    def __init__(self, code: str, message: str, *, contract_id: str | None = None) -> None:
        """Retain only a stable code and local contract ID."""
        super().__init__(message)
        self.code = code
        self.contract_id = contract_id


class SourceRequestError(SourceProviderError):
    """Raised before transport when a source request is not contract-bound."""


class SourcePayloadError(SourceProviderError):
    """Raised when a Saxo response cannot be parsed as a safe JSON object."""


class SourceEndpointError(SourceProviderError):
    """Raised when a frozen source no longer resolves to its registered GET/read route."""


class SourceAccessError(SourceProviderError):
    """Raised when local auth or environment gates do not permit the registered read."""

    def __init__(self, contract_id: str, reason: str) -> None:
        """Retain a sanitized local access reason."""
        super().__init__(
            "source_access_unavailable",
            "registered source read is unavailable under the current access gates",
            contract_id=contract_id,
        )
        self.reason = reason if _SAFE_REASON_PATTERN.fullmatch(reason) else "access_unavailable"


class SourceHttpError(SourceProviderError):
    """Raised for a sanitized non-entitlement HTTP failure."""

    def __init__(
        self,
        contract_id: str,
        http_status: int,
        *,
        error_code: str | None,
        attempts: int,
    ) -> None:
        """Retain status, safe error code, and bounded attempt count."""
        super().__init__(
            "source_http_error",
            "registered source read returned an HTTP error",
            contract_id=contract_id,
        )
        self.http_status = http_status
        self.error_code = _safe_error_code(error_code)
        self.attempts = attempts


class SourceEntitlementError(SourceProviderError):
    """Raised when Saxo denies the data or market-data entitlement."""

    def __init__(
        self,
        contract_id: str,
        http_status: int,
        *,
        error_code: str | None,
    ) -> None:
        """Retain only entitlement status and a validated error code."""
        super().__init__(
            "source_entitlement_denied",
            "registered source read was denied by an entitlement or access gate",
            contract_id=contract_id,
        )
        self.http_status = http_status
        self.error_code = _safe_error_code(error_code)


class SourceRateLimitError(SourceProviderError):
    """Raised after a bounded read-only retry budget is exhausted."""

    def __init__(
        self,
        contract_id: str,
        *,
        retry_after_seconds: float | None,
        reset_hint: str | None,
        attempts: int,
    ) -> None:
        """Retain only safe backoff metadata and bounded attempts."""
        super().__init__(
            "source_rate_limited",
            "registered source read exhausted its bounded rate-limit retry budget",
            contract_id=contract_id,
        )
        self.retry_after_seconds = retry_after_seconds
        self.reset_hint = reset_hint
        self.attempts = attempts


class SourceTransportError(SourceProviderError):
    """Raised when a read-only request remains transport-ambiguous after bounded retry."""

    def __init__(self, contract_id: str, *, error_type: str, attempts: int) -> None:
        """Retain only the exception class and bounded attempts."""
        super().__init__(
            "source_transport_ambiguous",
            "registered read outcome is unknown after bounded transport retry",
            contract_id=contract_id,
        )
        self.outcome = "unknown"
        self.error_type = (
            error_type if _SAFE_ERROR_CODE_PATTERN.fullmatch(error_type) else "TransportError"
        )
        self.attempts = attempts


class SourceSchemaDriftError(SourceProviderError):
    """Raised after required task-field drift quarantines dependent analyses."""

    def __init__(
        self,
        contract_id: str,
        comparison: SchemaComparison,
    ) -> None:
        """Retain only field names and dependent analysis kinds."""
        super().__init__(
            "source_schema_drift",
            "required source schema drift quarantined dependent analytics",
            contract_id=contract_id,
        )
        self.structural_errors = comparison.structural_errors
        self.missing_required_fields = comparison.missing_required_fields
        self.null_required_fields = comparison.null_required_fields
        self.required_type_mismatches = comparison.required_type_mismatches
        self.quarantined_analysis_kinds = comparison.quarantined_analysis_kinds


class SaxoAnalyticsProvider:
    """Fetch frozen Saxo analytics sources through the existing registered read boundary."""

    def __init__(
        self,
        *,
        request_executor: RegisteredReadExecutor | None = None,
        contracts: Mapping[str, SourceContract] | None = None,
        page_limit: int | None = None,
        retry_attempts: int | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Bind contracts to an injectable registered-read executor."""
        if page_limit is not None and not 1 <= page_limit <= _MAX_PAGE_LIMIT:
            raise ValueError("page_limit must be between 1 and 1000")
        if retry_attempts is not None and not 1 <= retry_attempts <= _MAX_RETRY_ATTEMPTS:
            raise ValueError("retry_attempts must be between 1 and 3")
        source = source_contracts_by_id() if contracts is None else contracts
        if any(contract_id != contract.contract_id for contract_id, contract in source.items()):
            raise ValueError("source contract mapping key does not match its contract ID")
        self._contracts = MappingProxyType(dict(source))
        self._request_executor = (
            self._execute_registered_read if request_executor is None else request_executor
        )
        self._page_limit = page_limit
        self._retry_attempts = retry_attempts
        self._sleep = sleep
        self._quarantine: dict[str, str] = {}

    @property
    def quarantined_analysis_kinds(self) -> tuple[str, ...]:
        """Return analysis kinds disabled by observed required-field drift."""
        return tuple(sorted(self._quarantine))

    def quarantine_reason(self, analysis_kind: str) -> str | None:
        """Return the value-free source contract reason for one quarantine."""
        return self._quarantine.get(analysis_kind)

    async def fetch(
        self,
        contract_id: str,
        request: Mapping[str, object],
    ) -> AsyncIterator[SourcePage]:
        """Fetch all bounded pages for one frozen source contract."""
        contract = self._contracts.get(contract_id)
        if contract is None:
            raise SourceRequestError(
                "unknown_source_contract",
                "source request names an unknown frozen contract",
            )
        request_target, params = _contract_request(contract, request)
        operation = _registered_operation(contract, request_target)
        first_page = await self._request_payload(
            contract,
            operation,
            request_target,
            params,
        )
        first_comparison = self._require_compatible(contract, first_page)
        initial_path = urlparse(request_target).path
        first_data_version = _data_version(first_page)

        comparisons_by_identity: dict[int, SchemaComparison] = {id(first_page): first_comparison}

        async def fetch_next(next_link: str) -> Mapping[str, SourceJsonValue]:
            if urlparse(next_link).path != initial_path:
                self._quarantine_contract(contract, "source_pagination_drift")
                raise SourceEndpointError(
                    "source_pagination_path_changed",
                    "returned pagination link changed the scoped source path",
                    contract_id=contract.contract_id,
                )
            if _pagination_changes_request_scope(next_link, params):
                self._quarantine_contract(contract, "source_pagination_drift")
                raise SourceEndpointError(
                    "source_pagination_query_changed",
                    "returned pagination link changed the scoped source query",
                    contract_id=contract.contract_id,
                )
            next_operation = _registered_operation(contract, next_link)
            payload = await self._request_payload(
                contract,
                next_operation,
                next_link,
                {},
            )
            if (
                "DataVersion" in contract.revision_fields
                and _data_version(payload) != first_data_version
            ):
                raise SourcePayloadError(
                    "source_revision_changed",
                    "source revision changed during bounded pagination",
                    contract_id=contract.contract_id,
                )
            comparisons_by_identity[id(payload)] = self._require_compatible(
                contract,
                payload,
            )
            return payload

        page_limit = (
            contract.page_limit
            if self._page_limit is None
            else min(contract.page_limit, self._page_limit)
        )
        fetch_revision = f"fetch:{uuid4().hex}"
        page_number = 0
        try:
            async for payload in follow_registered_pagination(
                first_page,
                fetch_next,
                max_pages=page_limit,
            ):
                page_number += 1
                comparison = comparisons_by_identity.get(id(payload))
                if comparison is None:
                    comparison = self._require_compatible(contract, payload)
                rows = _source_rows(contract, payload)
                page_fingerprint = source_page_fingerprint(rows)
                frozen_rows = freeze_source_rows(rows)
                data_version = (
                    _data_version(payload) if "DataVersion" in contract.revision_fields else None
                )
                yield SourcePage(
                    contract_id=contract.contract_id,
                    operation_id=contract.operation_id,
                    page_number=page_number,
                    rows=frozen_rows,
                    row_count=len(rows),
                    next_link=_next_link(payload),
                    data_version=data_version,
                    source_revision=(
                        f"data_version:{data_version}"
                        if data_version is not None
                        else fetch_revision
                    ),
                    page_fingerprint_sha256=page_fingerprint,
                    schema_comparison=comparison,
                )
        except (
            DuplicateSourcePageError,
            InvalidPaginationPayloadError,
            PaginationCycleError,
            UnsafePaginationLinkError,
        ):
            self._quarantine_contract(contract, "source_pagination_drift")
            raise

    def _require_compatible(
        self,
        contract: SourceContract,
        payload: Mapping[str, SourceJsonValue],
    ) -> SchemaComparison:
        comparison = compare_source_schema(
            contract,
            cast("Mapping[str, object]", payload),
        )
        if comparison.compatible:
            return comparison
        self._quarantine_contract(contract, "source_schema_drift")
        raise SourceSchemaDriftError(contract.contract_id, comparison)

    def _quarantine_contract(
        self,
        contract: SourceContract,
        reason_code: str,
    ) -> None:
        reason = f"{reason_code}:{contract.contract_id}"
        for analysis_kind in contract.dependent_analysis_kinds:
            self._quarantine[analysis_kind] = reason

    async def _request_payload(
        self,
        contract: SourceContract,
        operation: EndpointOperation,
        request_target: str,
        params: Mapping[str, str],
    ) -> dict[str, SourceJsonValue]:
        attempts = (
            contract.retry_attempts
            if self._retry_attempts is None
            else min(contract.retry_attempts, self._retry_attempts)
        )
        last_transport_error_type = "TransportError"
        for attempt in range(1, attempts + 1):
            try:
                response = await self._request_executor(
                    operation,
                    request_target,
                    params,
                )
            except SourceAccessError as error:
                raise SourceAccessError(contract.contract_id, error.reason) from error
            except httpx2.TransportError as error:
                last_transport_error_type = type(error).__name__
                if attempt == attempts:
                    raise SourceTransportError(
                        contract.contract_id,
                        error_type=last_transport_error_type,
                        attempts=attempt,
                    ) from None
                await self._sleep(_retry_delay(attempt))
                continue
            if _HTTP_SUCCESS_MIN <= response.status_code < _HTTP_SUCCESS_MAX:
                return _parse_response_object(contract.contract_id, response.content)
            error_code = _validated_error_code_or_raise_access(
                contract.contract_id,
                response,
            )
            if response.status_code == _HTTP_RATE_LIMITED:
                retry_after = _retry_after_seconds(response.headers)
                reset_hint = _rate_limit_reset_hint(response.headers)
                if (
                    attempt < attempts
                    and retry_after is not None
                    and retry_after <= _MAX_RETRY_DELAY_SECONDS
                ):
                    await self._sleep(retry_after)
                    continue
                raise SourceRateLimitError(
                    contract.contract_id,
                    retry_after_seconds=retry_after,
                    reset_hint=reset_hint,
                    attempts=attempt,
                )
            if response.status_code in _RETRYABLE_HTTP_STATUSES and attempt < attempts:
                await self._sleep(_retry_delay(attempt))
                continue
            raise SourceHttpError(
                contract.contract_id,
                response.status_code,
                error_code=error_code,
                attempts=attempt,
            )
        raise SourceTransportError(
            contract.contract_id,
            error_type=last_transport_error_type,
            attempts=attempts,
        )

    @staticmethod
    async def _execute_registered_read(
        operation: EndpointOperation,
        request_target: str,
        params: Mapping[str, str],
    ) -> httpx2.Response:
        context_or_result = await execution_context(
            operation,
            live_token_loader=live_token_for_tool,
        )
        if not isinstance(context_or_result, ReadExecutionContext):
            raw_reason = context_or_result.get("reason")
            reason = raw_reason if isinstance(raw_reason, str) else "access_unavailable"
            raise SourceAccessError(operation.operation_id, reason)
        async with create_async_client(
            base_url=context_or_result.rest_base_url,
            retries=0,
        ) as client:
            return await client.get(
                request_target.lstrip("/"),
                params=dict(params),
                headers=read_headers(context_or_result.token),
            )


def _contract_request(
    contract: SourceContract,
    request: Mapping[str, object],
) -> tuple[str, dict[str, str]]:
    if any(str(key).casefold() in _BLOCKED_ROUTING_KEYS for key in request):
        raise SourceRequestError(
            "caller_routing_rejected",
            "source request contains caller routing",
            contract_id=contract.contract_id,
        )
    allowed_parameters = set(contract.path_parameters) | set(contract.query_parameters)
    if any(key not in allowed_parameters for key in request):
        raise SourceRequestError(
            "unsupported_source_parameter",
            "source request contains an unsupported parameter",
            contract_id=contract.contract_id,
        )
    missing_path_parameters = tuple(
        parameter for parameter in contract.path_parameters if parameter not in request
    )
    if missing_path_parameters:
        raise SourceRequestError(
            "missing_source_path_parameter",
            "source request omits a required route parameter",
            contract_id=contract.contract_id,
        )
    request_target = contract.path_template
    for parameter in contract.path_parameters:
        value = _path_parameter(request[parameter], contract.contract_id)
        request_target = request_target.replace(f"{{{parameter}}}", value)
    params = {
        parameter: _query_parameter(request[parameter], contract.contract_id)
        for parameter in contract.query_parameters
        if parameter in request
    }
    return request_target, params


def _path_parameter(value: object, contract_id: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise SourceRequestError(
            "invalid_source_path_parameter",
            "source route parameter has an invalid type",
            contract_id=contract_id,
        )
    rendered = str(value)
    if (
        _SAFE_PATH_VALUE_PATTERN.fullmatch(rendered) is None
        or rendered in {".", ".."}
        or "%" in rendered
    ):
        raise SourceRequestError(
            "invalid_source_path_parameter",
            "source route parameter is not a safe path segment",
            contract_id=contract_id,
        )
    return rendered


def _query_parameter(value: object, contract_id: str) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SourceRequestError(
                "invalid_source_query_parameter",
                "source query parameter must be finite",
                contract_id=contract_id,
            )
        return str(value)
    if isinstance(value, str):
        if not value or any(not character.isprintable() for character in value):
            raise SourceRequestError(
                "invalid_source_query_parameter",
                "source query parameter is empty or non-printable",
                contract_id=contract_id,
            )
        return value
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        if not value:
            raise SourceRequestError(
                "invalid_source_query_parameter",
                "source query parameter list is empty",
                contract_id=contract_id,
            )
        sequence = cast("Sequence[object]", value)
        return ",".join(_query_parameter(item, contract_id) for item in sequence)
    raise SourceRequestError(
        "invalid_source_query_parameter",
        "source query parameter has an invalid type",
        contract_id=contract_id,
    )


def _registered_operation(
    contract: SourceContract,
    request_target: str,
) -> EndpointOperation:
    registered = find_registered_endpoint("GET", request_target)
    if (
        registered is None
        or registered.operation.operation_id != contract.operation_id
        or registered.operation.status != "implemented"
        or registered.operation.method != "GET"
        or registered.operation.read_write_class != "read"
    ):
        raise SourceEndpointError(
            "source_endpoint_not_registered",
            "source request is not the frozen registered GET/read operation",
            contract_id=contract.contract_id,
        )
    return registered.operation


def _pagination_changes_request_scope(
    next_link: str,
    initial_params: Mapping[str, str],
) -> bool:
    returned_params = parse_qs(
        urlparse(next_link).query,
        keep_blank_values=True,
        strict_parsing=False,
    )
    return any(
        parameter not in _PAGINATION_CONTROL_QUERY_PARAMETERS
        and parameter in returned_params
        and returned_params[parameter] != [initial_value]
        for parameter, initial_value in initial_params.items()
    )


def _parse_response_object(
    contract_id: str,
    content: bytes,
) -> dict[str, SourceJsonValue]:
    try:
        parsed = parse_json_value(content)
        return _SOURCE_OBJECT_ADAPTER.validate_python(parsed, strict=True)
    except (StrictJsonError, ValidationError):
        raise SourcePayloadError(
            "invalid_source_payload",
            "registered source read did not return a valid JSON object",
            contract_id=contract_id,
        ) from None


def _validated_error_code_or_raise_access(
    contract_id: str,
    response: httpx2.Response,
) -> str | None:
    if response.status_code == _HTTP_UNAUTHORIZED:
        raise SourceAccessError(contract_id, "authentication_required")
    error_code = validated_saxo_error_code(response.content)
    if response.status_code in _ENTITLEMENT_HTTP_STATUSES or error_code in _ENTITLEMENT_ERROR_CODES:
        raise SourceEntitlementError(
            contract_id,
            response.status_code,
            error_code=error_code,
        )
    return error_code


def _source_rows(
    contract: SourceContract,
    payload: Mapping[str, SourceJsonValue],
) -> tuple[dict[str, SourceJsonValue], ...]:
    if contract.response_shape is SourceResponseShape.OBJECT:
        row = {
            key: value
            for key, value in payload.items()
            if key not in {"DataVersion", "MaxRows", "__count", "__next"}
        }
        return (row,)
    data = payload.get("Data")
    if not isinstance(data, list):
        raise SourcePayloadError(
            "invalid_source_data_envelope",
            "registered source response has an invalid Data envelope",
            contract_id=contract.contract_id,
        )
    rows: list[dict[str, SourceJsonValue]] = []
    for item in data:
        if not isinstance(item, dict):
            raise SourcePayloadError(
                "invalid_source_data_row",
                "registered source response has a non-object Data row",
                contract_id=contract.contract_id,
            )
        rows.append(item)
    return tuple(rows)


def _next_link(payload: Mapping[str, SourceJsonValue]) -> str | None:
    value = payload.get("__next")
    return value if isinstance(value, str) else None


def _data_version(payload: Mapping[str, SourceJsonValue]) -> str | int | None:
    value = payload.get("DataVersion")
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    return value


def _safe_error_code(value: str | None) -> str | None:
    if value is None or _SAFE_ERROR_CODE_PATTERN.fullmatch(value) is None:
        return None
    return value


def _retry_delay(attempt: int) -> float:
    return min(_MAX_RETRY_DELAY_SECONDS, _BASE_RETRY_DELAY_SECONDS * (2 ** (attempt - 1)))


def _retry_after_seconds(headers: httpx2.Headers) -> float | None:
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


def _rate_limit_reset_hint(headers: httpx2.Headers) -> str | None:
    value = headers.get("X-RateLimit-Reset")
    if value is None or _SAFE_RESET_HINT_PATTERN.fullmatch(value) is None:
        return None
    return value
