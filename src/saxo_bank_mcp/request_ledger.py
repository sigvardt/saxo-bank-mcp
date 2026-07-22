from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, TypedDict

import httpx2

type RequestPhase = Literal["attempted", "completed"]
type HostRole = Literal["gateway", "oauth", "other"]

_SAFE_METHODS: Final = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
_GATEWAY_HOSTS: Final = frozenset({"gateway.saxobank.com"})
_OAUTH_HOSTS: Final = frozenset({"live.logonvalidation.net", "sim.logonvalidation.net"})
_SAFE_PATH_SEGMENTS: Final = frozenset(
    {
        "accounts",
        "audit",
        "authorize",
        "balances",
        "capabilities",
        "chart",
        "charts",
        "cs",
        "defaults",
        "details",
        "disclaimers",
        "dm",
        "infoprices",
        "instruments",
        "me",
        "messages",
        "multileg",
        "netpositions",
        "openapi",
        "orderactivities",
        "orders",
        "port",
        "positions",
        "precheck",
        "ref",
        "root",
        "sessions",
        "sim",
        "token",
        "trade",
        "v1",
        "v2",
    }
)
_REDACTED_PATH_SEGMENT: Final = "{redacted}"
_SAFE_QUERY_NAMES: Final = frozenset(
    {
        "$skip",
        "$top",
        "AccountGroupKey",
        "AccountKey",
        "AssetType",
        "ClientKey",
        "DisclaimerTokens",
        "EntitlementFieldSet",
        "OptionRootId",
        "OptionsStrategyType",
        "Status",
        "Uic",
        "contextId",
        "messageid",
    },
)
_ACTIVE_ERROR_MESSAGE: Final = "a request ledger is already active"


@dataclass(frozen=True, slots=True)
class RequestLedgerEvent:
    timestamp: str
    phase: RequestPhase
    host_role: HostRole
    method: str
    path: str
    query_names: tuple[str, ...]
    query_present: bool
    status: int | None


class RequestLedgerEventJson(TypedDict):
    timestamp: str
    phase: RequestPhase
    host_role: HostRole
    method: str
    path: str
    query_names: list[str]
    query_present: bool
    status: int | None


class RequestLedgerAlreadyActiveError(RuntimeError):
    pass


class _EventBuffer:
    __slots__ = ("_events", "_lock")

    def __init__(self) -> None:
        self._events: list[RequestLedgerEvent] = []
        self._lock = threading.Lock()

    def append(self, event: RequestLedgerEvent) -> None:
        with self._lock:
            self._events.append(event)

    def snapshot(self) -> list[RequestLedgerEvent]:
        with self._lock:
            return list(self._events)


@dataclass(frozen=True, slots=True)
class RequestLedgerCapture:
    _buffer: _EventBuffer

    @property
    def events(self) -> list[RequestLedgerEvent]:
        return self._buffer.snapshot()


class _LedgerRegistry:
    __slots__ = ("_active", "_lock")

    def __init__(self) -> None:
        self._active: _EventBuffer | None = None
        self._lock = threading.Lock()

    def activate(self) -> _EventBuffer:
        with self._lock:
            if self._active is not None:
                raise RequestLedgerAlreadyActiveError(_ACTIVE_ERROR_MESSAGE)
            active = _EventBuffer()
            self._active = active
            return active

    def deactivate(self, active: _EventBuffer) -> None:
        with self._lock:
            if self._active is active:
                self._active = None

    def is_active(self) -> bool:
        with self._lock:
            return self._active is not None

    def append(self, event: RequestLedgerEvent) -> None:
        with self._lock:
            if self._active is not None:
                self._active.append(event)


_REGISTRY: Final = _LedgerRegistry()
_SCOPED_BUFFER: Final[ContextVar[_EventBuffer | None]] = ContextVar(
    "saxo_scoped_request_ledger",
    default=None,
)


@contextmanager
def capture_request_ledger() -> Generator[RequestLedgerCapture]:
    active = _REGISTRY.activate()
    try:
        yield RequestLedgerCapture(active)
    finally:
        _REGISTRY.deactivate(active)


def request_ledger_active() -> bool:
    return _REGISTRY.is_active()


@contextmanager
def capture_scoped_request_ledger() -> Generator[RequestLedgerCapture]:
    if _SCOPED_BUFFER.get() is not None:
        raise RequestLedgerAlreadyActiveError(_ACTIVE_ERROR_MESSAGE)
    active = _EventBuffer()
    token = _SCOPED_BUFFER.set(active)
    try:
        yield RequestLedgerCapture(active)
    finally:
        _SCOPED_BUFFER.reset(token)


def safe_request_events(events: list[RequestLedgerEvent]) -> list[RequestLedgerEventJson]:
    return [
        {
            "timestamp": event.timestamp,
            "phase": event.phase,
            "host_role": event.host_role,
            "method": event.method,
            "path": event.path,
            "query_names": list(event.query_names),
            "query_present": event.query_present,
            "status": event.status,
        }
        for event in events
    ]


def record_request_attempt(request: httpx2.Request) -> None:
    _record(request, phase="attempted", status=None)


def record_request_completed(response: httpx2.Response) -> None:
    _record(response.request, phase="completed", status=response.status_code)


def _record(request: httpx2.Request, *, phase: RequestPhase, status: int | None) -> None:
    host_role = _host_role(request.url.host)
    method = request.method if request.method in _SAFE_METHODS else "{redacted}"
    event = RequestLedgerEvent(
        timestamp=datetime.now(UTC).isoformat(timespec="milliseconds"),
        phase=phase,
        host_role=host_role,
        method=method,
        path=_safe_path(request.url.path),
        query_names=safe_query_names(request),
        query_present=bool(request.url.query),
        status=status,
    )
    _REGISTRY.append(event)
    scoped = _SCOPED_BUFFER.get()
    if scoped is not None:
        scoped.append(event)


def _host_role(host: str | None) -> HostRole:
    if host in _GATEWAY_HOSTS:
        return "gateway"
    if host in _OAUTH_HOSTS:
        return "oauth"
    return "other"


def _safe_path(path: str) -> str:
    segments = (
        segment if segment in _SAFE_PATH_SEGMENTS else _REDACTED_PATH_SEGMENT
        for segment in path.split("/")
        if segment
    )
    sanitized = "/".join(segments)
    return f"/{sanitized}" if sanitized else "/"


def safe_query_names(request: httpx2.Request) -> tuple[str, ...]:
    names = {
        name if name in _SAFE_QUERY_NAMES else _REDACTED_PATH_SEGMENT
        for name in request.url.params
    }
    return tuple(sorted(names))
