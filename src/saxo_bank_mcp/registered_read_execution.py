from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self

import httpx2

from saxo_bank_mcp.endpoint_registry import EndpointOperation
from saxo_bank_mcp.read_tool_execution import (
    LiveTokenLoader,
    execution_context,
    read_headers,
)
from saxo_bank_mcp.read_tool_results import network_error
from saxo_bank_mcp.read_tool_types import (
    ReadExecutionContext,
    ReadExecutionResult,
    ReadToolResult,
)


class RegisteredReadClient(Protocol):
    async def __aenter__(self) -> Self: ...  # noqa: D105

    async def __aexit__(  # noqa: D105
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> httpx2.Response: ...


type RegisteredReadClientFactory = Callable[..., RegisteredReadClient]


@dataclass(frozen=True, slots=True)
class RegisteredReadResponse:
    """Raw response retained only inside the registered server/provider boundary."""

    context: ReadExecutionContext
    response: httpx2.Response


type RegisteredReadOutcome = RegisteredReadResponse | ReadToolResult


async def execute_registered_get(
    operation: EndpointOperation,
    request_target: str,
    params: Mapping[str, str],
    *,
    client_factory: RegisteredReadClientFactory,
    live_token_loader: LiveTokenLoader,
) -> RegisteredReadOutcome:
    """Execute one registry-bound GET and retain its body only inside the server."""
    context_or_result: ReadExecutionResult = await execution_context(
        operation,
        live_token_loader=live_token_loader,
    )
    if not isinstance(context_or_result, ReadExecutionContext):
        return {**context_or_result, "live_write": False}
    context = context_or_result
    try:
        async with client_factory(base_url=context.rest_base_url) as client:
            response = await client.get(
                request_target.lstrip("/"),
                params=dict(params),
                headers=read_headers(context.token),
            )
    except httpx2.HTTPError as error:
        return network_error(operation, context.environment, type(error).__name__)
    return RegisteredReadResponse(context=context, response=response)


__all__ = (
    "RegisteredReadClient",
    "RegisteredReadClientFactory",
    "RegisteredReadOutcome",
    "RegisteredReadResponse",
    "execute_registered_get",
)
