from __future__ import annotations

import logging
import socket
import time
from typing import Final

import httpx2

_LOGGER: Final = logging.getLogger(__name__)
_LIMITS: Final = httpx2.Limits(
    max_connections=200,
    max_keepalive_connections=40,
    keepalive_expiry=30.0,
)
_TIMEOUT: Final = httpx2.Timeout(connect=5.0, read=30.0, write=10.0, pool=10.0)
_SOCKET_OPTIONS: Final[list[tuple[int, int, int]]] = [
    (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
]


async def _log_request(request: httpx2.Request) -> None:
    request.extensions["request_start"] = time.perf_counter()


async def _log_response(response: httpx2.Response) -> None:
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
) -> httpx2.AsyncClient:
    effective_transport = (
        httpx2.AsyncHTTPTransport(
            http2=True,
            retries=3,
            limits=_LIMITS,
            socket_options=_SOCKET_OPTIONS,
        )
        if transport is None
        else transport
    )
    return httpx2.AsyncClient(
        transport=effective_transport,
        timeout=_TIMEOUT,
        base_url=base_url,
        event_hooks={"request": [_log_request], "response": [_log_response]},
        follow_redirects=False,
    )
