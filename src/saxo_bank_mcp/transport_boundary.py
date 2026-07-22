from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

import httpx2
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from saxo_bank_mcp.request_ledger import safe_query_names
from saxo_bank_mcp.strict_json import StrictJsonError, parse_json_value

type BoundaryPhase = Literal["attempted", "completed"]
type BoundaryHostRole = Literal["gateway", "oauth", "other"]

_SAFE_METHODS: Final = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
_GATEWAY_HOSTS: Final = frozenset({"gateway.saxobank.com"})
_OAUTH_HOSTS: Final = frozenset({"live.logonvalidation.net", "sim.logonvalidation.net"})
_SAFE_PATH_SEGMENTS: Final = frozenset(
    {
        "accounts",
        "balances",
        "details",
        "disclaimers",
        "instruments",
        "me",
        "messages",
        "openapi",
        "orders",
        "port",
        "positions",
        "precheck",
        "ref",
        "token",
        "trade",
        "v1",
        "v2",
    },
)
_REDACTED: Final = "{redacted}"
_CAPTURE_FD: Final[ContextVar[int | None]] = ContextVar(
    "saxo_transport_boundary_capture_fd",
    default=None,
)
_COLLECTOR: Final = Path(__file__).with_name("transport_audit_collector.py")
_EVENTS_ADAPTER: Final = TypeAdapter(list["TransportBoundaryEvent"])


class TransportBoundaryEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    timestamp: str = Field(min_length=1)
    phase: BoundaryPhase
    host_role: BoundaryHostRole
    method: str = Field(min_length=1)
    path: str = Field(min_length=1)
    query_names: tuple[str, ...] = Field(strict=False)
    query_present: bool
    status: int | None


class TransportBoundaryCapture:
    __slots__ = ("collector_complete", "collector_exit_code", "events")

    def __init__(self) -> None:
        """Initialize an incomplete collector result."""
        self.events: list[TransportBoundaryEvent] = []
        self.collector_complete = False
        self.collector_exit_code: int | None = None


@contextmanager
def capture_transport_boundary() -> Generator[TransportBoundaryCapture]:
    event_read, event_write = os.pipe()
    result_read, result_write = os.pipe()
    process = subprocess.Popen(
        [sys.executable, str(_COLLECTOR), str(event_read), str(result_write)],
        close_fds=True,
        env={"PYTHONIOENCODING": "utf-8"},
        pass_fds=(event_read, result_write),
    )
    os.close(event_read)
    os.close(result_write)
    capture = TransportBoundaryCapture()
    token = _CAPTURE_FD.set(event_write)
    try:
        yield capture
    finally:
        _CAPTURE_FD.reset(token)
        os.close(event_write)
        raw = _read_all(result_read)
        os.close(result_read)
        try:
            capture.collector_exit_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            capture.collector_exit_code = process.wait(timeout=5)
        try:
            capture.events = parse_transport_boundary_events(raw)
        except (StrictJsonError, ValidationError):
            capture.events = []
        capture.collector_complete = capture.collector_exit_code == 0 and bool(raw)


def parse_transport_boundary_events(raw: bytes) -> list[TransportBoundaryEvent]:
    return _EVENTS_ADAPTER.validate_python(parse_json_value(raw))


def wrap_transport_for_boundary_capture(
    transport: httpx2.AsyncBaseTransport,
) -> httpx2.AsyncBaseTransport:
    return _BoundaryTransport(transport) if _CAPTURE_FD.get() is not None else transport


class _BoundaryTransport(httpx2.AsyncBaseTransport):
    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        _emit(request, phase="attempted", status=None)
        response = await self._inner.handle_async_request(request)
        _emit(request, phase="completed", status=response.status_code)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def _emit(request: httpx2.Request, *, phase: BoundaryPhase, status: int | None) -> None:
    descriptor = _CAPTURE_FD.get()
    if descriptor is None:
        return
    event = {
        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "phase": phase,
        "host_role": _host_role(request.url.host),
        "method": request.method if request.method in _SAFE_METHODS else _REDACTED,
        "path": _safe_path(request.url.path),
        "query_names": safe_query_names(request),
        "query_present": bool(request.url.query),
        "status": status,
    }
    os.write(descriptor, (json.dumps(event, separators=(",", ":")) + "\n").encode())


def _host_role(host: str | None) -> BoundaryHostRole:
    if host in _GATEWAY_HOSTS:
        return "gateway"
    if host in _OAUTH_HOSTS:
        return "oauth"
    return "other"


def _safe_path(path: str) -> str:
    segments = (
        segment if segment in _SAFE_PATH_SEGMENTS else _REDACTED
        for segment in path.split("/")
        if segment
    )
    sanitized = "/".join(segments)
    return f"/{sanitized}" if sanitized else "/"
def _read_all(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 65536):
        chunks.append(chunk)
    return b"".join(chunks)
