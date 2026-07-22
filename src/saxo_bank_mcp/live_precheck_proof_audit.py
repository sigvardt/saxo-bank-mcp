from __future__ import annotations

import hashlib
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Final

from saxo_bank_mcp.live_precheck_ledger_models import SafeLedgerReport
from saxo_bank_mcp.request_ledger import RequestLedgerEvent
from saxo_bank_mcp.transport_boundary import TransportBoundaryEvent

_SOURCE_PATHS: Final = (
    "README.md",
    "data",
    "docs",
    "pyproject.toml",
    "scripts",
    "src",
    "tests",
    "uv.lock",
)
_GATEWAY_REQUESTS: Final = (
    ("GET", "/openapi/port/v1/accounts/me", ()),
    ("GET", "/openapi/port/v1/accounts/me", ()),
    (
        "GET",
        "/openapi/ref/v1/instruments/details/{redacted}/{redacted}",
        (),
    ),
    ("GET", "/openapi/port/v1/orders/me", ()),
    ("GET", "/openapi/port/v1/orders/me", ()),
    ("GET", "/openapi/port/v1/positions/me", ()),
    ("GET", "/openapi/port/v1/positions/me", ()),
    ("GET", "/openapi/port/v1/balances/me", ()),
    ("GET", "/openapi/port/v1/balances/me", ()),
    ("GET", "/openapi/trade/v1/messages", ()),
    ("GET", "/openapi/trade/v1/messages", ()),
    ("POST", "/openapi/trade/v2/orders/precheck", ()),
)


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    git_head: str
    dirty_source_sha256: dict[str, str]
    complete: bool


def source_provenance(root: Path) -> SourceProvenance:
    git = which("git")
    if git is None:
        return SourceProvenance("unavailable", {}, complete=False)
    try:
        head = _run_git(git, root, ("rev-parse", "HEAD")).decode("ascii").strip()
        tracked = _nul_paths(
            _run_git(
                git,
                root,
                ("diff", "--name-only", "--no-renames", "-z", "HEAD", "--", *_SOURCE_PATHS),
            ),
        )
        untracked = _nul_paths(
            _run_git(
                git,
                root,
                ("ls-files", "--others", "--exclude-standard", "-z", "--", *_SOURCE_PATHS),
            ),
        )
    except (OSError, UnicodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return SourceProvenance("unavailable", {}, complete=False)

    hashes: dict[str, str] = {}
    complete = bool(head)
    resolved_root = root.resolve()
    for relative in sorted({*tracked, *untracked}):
        candidate = root / relative
        try:
            is_source_file = candidate.is_file() and candidate.resolve().is_relative_to(
                resolved_root,
            )
        except OSError:
            is_source_file = False
        if not is_source_file:
            complete = False
            continue
        try:
            hashes[relative] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            complete = False
    return SourceProvenance(head or "unavailable", hashes, complete=complete)


def ledger_allows_proof(events: list[RequestLedgerEvent]) -> bool:
    if any(event.query_present != bool(event.query_names) for event in events):
        return False
    attempted: Counter[tuple[str, str, str, tuple[str, ...]]] = Counter(
        (event.host_role, event.method, event.path, event.query_names)
        for event in events
        if event.phase == "attempted"
    )
    completed: Counter[tuple[str, str, str, tuple[str, ...]]] = Counter(
        (event.host_role, event.method, event.path, event.query_names)
        for event in events
        if event.phase == "completed"
    )
    gateway: Counter[tuple[str, str, tuple[str, ...]]] = Counter(
        (method, path, query_names)
        for role, method, path, query_names in attempted.elements()
        if role == "gateway"
    )
    oauth = [
        (method, path, query_names)
        for role, method, path, query_names in attempted.elements()
        if role == "oauth"
    ]
    unexpected_roles = {role for role, _, _, _ in attempted if role not in {"gateway", "oauth"}}
    return (
        attempted == completed
        and gateway == Counter(_GATEWAY_REQUESTS)
        and all(request == ("POST", "/token", ()) for request in oauth)
        and len(oauth) <= 1
        and not unexpected_roles
    )


def exposed_ledger_events(report: SafeLedgerReport) -> list[RequestLedgerEvent]:
    return [
        RequestLedgerEvent(
            timestamp=event.timestamp,
            phase=event.phase,
            host_role=event.host_role,
            method=event.method,
            path=event.path,
            query_names=event.query_names,
            query_present=event.query_present,
            status=event.status,
        )
        for event in report.events
    ]


def exposed_ledger_allows_proof(report: SafeLedgerReport) -> bool:
    return (
        report.ledger_complete
        and report.events_evicted == 0
        and report.negative_proof_available
        and report.only_precheck_gateway_non_get
        and report.unsafe_gateway_request_detected is False
        and report.order_placement_endpoint_called is False
        and ledger_allows_proof(exposed_ledger_events(report))
    )


def request_ledgers_match(
    outer_events: list[RequestLedgerEvent],
    exposed_report: SafeLedgerReport,
) -> bool:
    outer = [_event_identity(event) for event in outer_events if event.host_role != "oauth"]
    exposed = [_event_identity(event) for event in exposed_ledger_events(exposed_report)]
    return outer == exposed


def transport_boundary_allows_proof(events: list[TransportBoundaryEvent]) -> bool:
    return ledger_allows_proof([_request_event(event) for event in events])


def transport_boundary_matches(
    boundary_events: list[TransportBoundaryEvent],
    outer_events: list[RequestLedgerEvent],
    exposed_report: SafeLedgerReport,
) -> bool:
    boundary = [_event_identity(_request_event(event)) for event in boundary_events]
    outer = [_event_identity(event) for event in outer_events]
    exposed = [_event_identity(event) for event in exposed_ledger_events(exposed_report)]
    boundary_without_oauth = [
        _event_identity(_request_event(event))
        for event in boundary_events
        if event.host_role != "oauth"
    ]
    return boundary == outer and boundary_without_oauth == exposed


def _request_event(event: TransportBoundaryEvent) -> RequestLedgerEvent:
    return RequestLedgerEvent(
        timestamp=event.timestamp,
        phase=event.phase,
        host_role=event.host_role,
        method=event.method,
        path=event.path,
        query_names=event.query_names,
        query_present=event.query_present,
        status=event.status,
    )


def _event_identity(
    event: RequestLedgerEvent,
) -> tuple[str, str, str, str, tuple[str, ...], bool, int | None]:
    return (
        event.phase,
        event.host_role,
        event.method,
        event.path,
        event.query_names,
        event.query_present,
        event.status,
    )


def _run_git(git: str, root: Path, arguments: tuple[str, ...]) -> bytes:
    return subprocess.run(
        [git, *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=5,
    ).stdout


def _nul_paths(raw: bytes) -> tuple[str, ...]:
    return tuple(part.decode("utf-8") for part in raw.split(b"\0") if part)
