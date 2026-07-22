from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from saxo_bank_mcp.auth import SaxoTokenSet, TokenEnvironment
from saxo_bank_mcp.endpoint_registry import RegisteredEndpoint

type ReadLeaf = str | int | bool | None
type ReadResponseMode = Literal["redacted_body", "fingerprint_only"]
type ReadObject = dict[str, ReadLeaf]
type ReadToolValue = (
    ReadLeaf | list[str] | ReadObject | dict[str, int] | dict[str, bool] | list[ReadObject]
)
type ReadToolResult = dict[str, ReadToolValue]
type EndpointPreflightResult = RegisteredEndpoint | ReadToolResult

REGISTERED_CALL_TOOL_DESCRIPTION: Final = (
    "Calls registered Saxo OpenAPI GET/read operations only in SIM or explicitly enabled "
    "LIVE read mode. It denies unregistered, arbitrary-host, and write-class operations "
    "before any network call. It never performs LIVE writes."
    " Set response_mode=fingerprint_only when an agent only needs change detection and should "
    "not receive response values such as balances."
)
READ_DOES_NOT_VERIFY: Final[tuple[str, ...]] = (
    "Saxo connectivity",
    "credentials/session",
    "account access",
    "catalog completeness/freshness vs live Saxo",
    "trading/order readiness",
    "instrument/account suitability",
    "real-money approval",
    "live write readiness",
)
READINESS_PREREQUISITES: Final[tuple[str, ...]] = (
    "valid Saxo session",
    "required account entitlements",
    "instrument/account suitability checks",
    "one exact-action human chat approval for LIVE write-class tools",
)


@dataclass(frozen=True, slots=True)
class ReadExecutionContext:
    environment: TokenEnvironment
    rest_base_url: str
    token: SaxoTokenSet | None


type ReadExecutionResult = ReadExecutionContext | ReadToolResult
