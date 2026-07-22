from __future__ import annotations

from typing import Final, TypedDict

import anyio
import mcp.types as mt
from fastmcp import Context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from pydantic import TypeAdapter

from saxo_bank_mcp.request_ledger import (
    RequestLedgerEvent,
    RequestLedgerEventJson,
    capture_scoped_request_ledger,
    safe_request_events,
)

SAFE_REQUEST_LEDGER_TOOL_DESCRIPTION: Final = (
    "Returns or clears a safe log of outbound HTTP requests made by MCP tools in the current "
    "MCP session. It stores time, phase, host role, method, sanitized path, query parameter "
    "names without values, and status. It never stores headers, bodies, tokens, account "
    "identifier values, instrument identifier values, or balances. Clear it before a task "
    "that needs complete negative proof; if retention overflow occurs, negative proof is denied."
)
_STATE_KEY: Final = "saxo.safe_request_ledger.v2"
_MAX_EVENTS: Final = 500
_PRECHECK_PATH: Final = "/openapi/trade/v2/orders/precheck"
_PLACEMENT_PATHS: Final = frozenset(
    {
        "/openapi/trade/v2/orders",
        "/openapi/trade/v2/orders/multileg",
    },
)
_EVENTS_ADAPTER: Final[TypeAdapter[list[RequestLedgerEventJson]]] = TypeAdapter(
    list[RequestLedgerEventJson],
)
_STATE_LOCK: Final = anyio.Lock()


class _LedgerState(TypedDict):
    events: list[RequestLedgerEventJson]
    events_evicted: int


_STATE_ADAPTER: Final[TypeAdapter[_LedgerState]] = TypeAdapter(_LedgerState)


class _SafeRequestLedgerMiddleware(Middleware):
    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        fastmcp_context = context.fastmcp_context
        if fastmcp_context is None:
            return await call_next(context)
        with capture_scoped_request_ledger() as ledger:
            try:
                return await call_next(context)
            finally:
                await _append_session_events(fastmcp_context, ledger.events)


SAFE_REQUEST_LEDGER_MIDDLEWARE: Final[Middleware] = _SafeRequestLedgerMiddleware()


async def saxo_get_safe_request_ledger(
    *,
    clear: bool = False,
    ctx: Context,
) -> ToolResult:
    if clear:
        await ctx.delete_state(_STATE_KEY)
        state = _LedgerState(events=[], events_evicted=0)
        status = "cleared"
    else:
        state = await _session_state(ctx)
        status = "passed"
    attempted = [event for event in state["events"] if event["phase"] == "attempted"]
    gateway_non_get = [
        event for event in attempted if event["host_role"] == "gateway" and event["method"] != "GET"
    ]
    gateway_post_paths = [event["path"] for event in gateway_non_get if event["method"] == "POST"]
    observed_unsafe = any(
        event["method"] != "POST" or event["path"] != _PRECHECK_PATH for event in gateway_non_get
    )
    ledger_complete = state["events_evicted"] == 0
    placement_observed = any(
        event["method"] == "POST" and event["path"] in _PLACEMENT_PATHS
        for event in gateway_non_get
    )
    order_placement_called: bool | None = placement_observed
    if not ledger_complete and not placement_observed:
        order_placement_called = None
    unsafe_detected: bool | None = observed_unsafe
    if not ledger_complete and not observed_unsafe:
        unsafe_detected = None
    payload = {
        "status": status,
        "tool_name": "saxo_get_safe_request_ledger",
        "scope": "current_mcp_session",
        "retention_limit_events": _MAX_EVENTS,
        "safe_fields_only": True,
        "ledger_complete": ledger_complete,
        "events_evicted": state["events_evicted"],
        "negative_proof_available": ledger_complete,
        "request_count": len(attempted),
        "non_get_request_count": len(
            [event for event in attempted if event["method"] != "GET"],
        ),
        "gateway_post_paths": gateway_post_paths,
        "only_precheck_gateway_non_get": ledger_complete
        and bool(gateway_non_get)
        and not observed_unsafe,
        "unsafe_gateway_request_detected": unsafe_detected,
        "order_placement_endpoint_called": order_placement_called,
        "events": state["events"],
        "does_not_verify": [
            "requests made outside the current MCP session",
            "Saxo account changes made by another client",
            "successful order execution",
        ],
    }
    return ToolResult(structured_content=payload, is_error=False)


async def _append_session_events(ctx: Context, events: list[RequestLedgerEvent]) -> None:
    if not events:
        return
    async with _STATE_LOCK:
        stored = await _session_state(ctx)
        combined = [*stored["events"], *safe_request_events(events)]
        events_evicted = max(0, len(combined) - _MAX_EVENTS)
        state = _LedgerState(
            events=combined[-_MAX_EVENTS:],
            events_evicted=stored["events_evicted"] + events_evicted,
        )
        await ctx.set_state(_STATE_KEY, state)


async def _session_state(ctx: Context) -> _LedgerState:
    raw = await ctx.get_state(_STATE_KEY)
    if raw is None:
        return _LedgerState(events=[], events_evicted=0)
    parsed = _STATE_ADAPTER.validate_python(raw)
    return _LedgerState(
        events=_EVENTS_ADAPTER.validate_python(parsed["events"]),
        events_evicted=parsed["events_evicted"],
    )
