from __future__ import annotations

import logging
import socket
import time
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

import httpx2

from saxo_bank_mcp.request_ledger import (
    record_request_attempt,
    record_request_completed,
    request_ledger_active,
)
from saxo_bank_mcp.transport_boundary import wrap_transport_for_boundary_capture

_LOGGER: Final = logging.getLogger(__name__)
_SENSITIVE_DEPENDENCY_LOGGER_NAMES: Final = (
    "httpx2",
    "httpcore2",
    "hpack",
    "h2",
    "websockets",
)
_LIMITS: Final = httpx2.Limits(
    max_connections=200,
    max_keepalive_connections=40,
    keepalive_expiry=30.0,
)
_TIMEOUT: Final = httpx2.Timeout(connect=5.0, read=30.0, write=10.0, pool=10.0)
_SOCKET_OPTIONS: Final[list[tuple[int, int, int]]] = [
    (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
]


class NetworkTransportForbiddenError(RuntimeError):
    pass


class NetworkTransportSentinel:
    __slots__ = ("constructed",)

    def __init__(self) -> None:
        """Initialize an unused transport marker."""
        self.constructed = False


_TRANSPORT_SENTINEL: Final[ContextVar[NetworkTransportSentinel | None]] = ContextVar(
    "saxo_network_transport_sentinel",
    default=None,
)


def _cap_sensitive_dependency_log_levels() -> None:
    for logger_name in _SENSITIVE_DEPENDENCY_LOGGER_NAMES:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


_cap_sensitive_dependency_log_levels()


@contextmanager
def forbid_network_transport() -> Generator[NetworkTransportSentinel]:
    sentinel = NetworkTransportSentinel()
    token = _TRANSPORT_SENTINEL.set(sentinel)
    try:
        yield sentinel
    finally:
        _TRANSPORT_SENTINEL.reset(token)


async def _log_request(request: httpx2.Request) -> None:
    record_request_attempt(request)
    request.extensions["request_start"] = time.perf_counter()


async def _log_response(response: httpx2.Response) -> None:
    record_request_completed(response)
    started = response.request.extensions.get("request_start")
    if isinstance(started, float):
        elapsed = time.perf_counter() - started
        host = response.request.url.host or "<unknown-host>"
        _LOGGER.info(
            "HTTP %s %s -> %d (%.3fs, %s)",
            response.request.method,
            host,
            response.status_code,
            elapsed,
            response.http_version,
        )


def create_async_client(
    *,
    base_url: str = "",
    transport: httpx2.AsyncBaseTransport | None = None,
    retries: int | None = None,
) -> httpx2.AsyncClient:
    sentinel = _TRANSPORT_SENTINEL.get()
    if sentinel is not None:
        sentinel.constructed = True
        raise NetworkTransportForbiddenError("network transport construction was forbidden")
    effective_transport = (
        httpx2.AsyncHTTPTransport(
            http2=True,
            retries=(0 if request_ledger_active() else 3) if retries is None else retries,
            limits=_LIMITS,
            socket_options=_SOCKET_OPTIONS,
        )
        if transport is None
        else transport
    )
    return httpx2.AsyncClient(
        transport=wrap_transport_for_boundary_capture(effective_transport),
        timeout=_TIMEOUT,
        base_url=base_url,
        event_hooks={"request": [_log_request], "response": [_log_response]},
        follow_redirects=False,
    )
