from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal

import anyio
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.client.transports import FastMCPTransport
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from saxo_bank_mcp.config import SimAuthSettings
from saxo_bank_mcp.live_mode import LiveReadSettingsError
from saxo_bank_mcp.live_oauth_settings import resolve_live_oauth_settings
from saxo_bank_mcp.live_precheck_ledger_models import SafeLedgerReport
from saxo_bank_mcp.live_precheck_proof_artifact import ArtifactContext, artifact_payload
from saxo_bank_mcp.live_precheck_proof_audit import (
    source_provenance,
)
from saxo_bank_mcp.live_precheck_proof_models import (
    BuySell,
    ExecutionAborted,
    ExecutionOutcome,
    ProofOrder,
)
from saxo_bank_mcp.live_precheck_proof_publication import write_scanned_artifact
from saxo_bank_mcp.live_precheck_proof_state import (
    execute_proof,
)
from saxo_bank_mcp.live_token_refresh import refresh_live_token_if_needed
from saxo_bank_mcp.request_ledger import (
    RequestLedgerAlreadyActiveError,
    RequestLedgerEvent,
    capture_request_ledger,
)
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.token_cache import load_token_cache
from saxo_bank_mcp.transport_boundary import (
    TransportBoundaryCapture,
    capture_transport_boundary,
)

PROOF_TIMEOUT_SECONDS: Final = 60
REFRESH_MARGIN_SECONDS: Final = 120
CLOCK_SKEW_SECONDS: Final = 30
TOKEN_VALIDITY_LOWER_BOUND_SECONDS: Final = (
    PROOF_TIMEOUT_SECONDS + REFRESH_MARGIN_SECONDS + CLOCK_SKEW_SECONDS
)
_TOKEN_VALIDITY: Final = timedelta(seconds=TOKEN_VALIDITY_LOWER_BOUND_SECONDS)
_EVIDENCE_BOUNDARY_ERRORS: Final[tuple[type[Exception], ...]] = (Exception,)
_REPO_ROOT: Final = Path(__file__).resolve().parents[2]
_DRIVER: Final = (
    "FastMCP protocol client with exposed session ledger and out-of-process "
    "transport-boundary capture"
)
_LEDGER_TOOL: Final = "saxo_get_safe_request_ledger"


class ProofOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    allow_live: Literal[True]
    out: Path
    uic: int = Field(gt=0)
    asset_type: str = Field(min_length=1)
    amount: float = Field(gt=0, allow_inf_nan=False)
    buy_sell: BuySell
    account_position: int | None = Field(default=None, ge=1)

    def order(self) -> ProofOrder:
        return ProofOrder(
            uic=self.uic,
            asset_type=self.asset_type,
            amount=self.amount,
            buy_sell=self.buy_sell,
            account_position=self.account_position,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a no-purchase LIVE precheck proof.")
    parser.add_argument("--allow-live", action="store_true", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--uic", type=_positive_int, required=True)
    parser.add_argument("--asset-type", type=_present_text, required=True)
    parser.add_argument("--amount", type=_positive_float, required=True)
    parser.add_argument("--buy-sell", choices=("Buy", "Sell"), required=True)
    parser.add_argument("--account-position", type=_positive_int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    options = ProofOptions.model_validate(vars(build_parser().parse_args(argv)))
    try:
        return anyio.run(_run, options)
    except _EVIDENCE_BOUNDARY_ERRORS:
        provenance = source_provenance(_REPO_ROOT)
        payload = artifact_payload(
            ExecutionAborted(None, "proof_runner", "unexpected_internal_error"),
            ArtifactContext(
                events=[],
                provenance=provenance,
                driver=_DRIVER,
                token_validity_lower_bound_seconds=TOKEN_VALIDITY_LOWER_BOUND_SECONDS,
                exposed_ledger=None,
                transport_capture=None,
            ),
        )
        write_scanned_artifact(options.out, payload)
        return 1


async def _run(options: ProofOptions) -> int:
    provenance = source_provenance(_REPO_ROOT)
    outcome: ExecutionOutcome = ExecutionAborted(
        None,
        "proof_runner",
        "unexpected_internal_error",
    )
    events: list[RequestLedgerEvent] = []
    exposed_ledger: SafeLedgerReport | None = None
    transport_capture: TransportBoundaryCapture | None = None
    try:
        with capture_transport_boundary() as boundary:
            with capture_request_ledger() as ledger:
                try:
                    settings = resolve_live_oauth_settings()
                    if await _token_ready(settings):
                        with anyio.fail_after(PROOF_TIMEOUT_SECONDS):
                            async with Client(mcp) as client:
                                await client.call_tool(_LEDGER_TOOL, {"clear": True})
                                try:
                                    outcome = await execute_proof(client, options.order())
                                finally:
                                    exposed_ledger = await _read_exposed_ledger(client)
                    else:
                        outcome = ExecutionAborted(
                            None,
                            "authentication",
                            "live_token_not_ready",
                        )
                except LiveReadSettingsError:
                    outcome = ExecutionAborted(
                        None,
                        "authentication",
                        "live_settings_invalid",
                    )
                except _EVIDENCE_BOUNDARY_ERRORS:
                    outcome = ExecutionAborted(
                        None,
                        "proof_runner",
                        "unexpected_internal_error",
                    )
                events = ledger.events
            transport_capture = boundary
    except RequestLedgerAlreadyActiveError:
        outcome = ExecutionAborted(
            None,
            "proof_runner",
            "concurrent_request_ledger",
        )
    payload = artifact_payload(
        outcome,
        ArtifactContext(
            events=events,
            provenance=provenance,
            driver=_DRIVER,
            token_validity_lower_bound_seconds=TOKEN_VALIDITY_LOWER_BOUND_SECONDS,
            exposed_ledger=exposed_ledger,
            transport_capture=transport_capture,
        ),
    )
    scan_clean = write_scanned_artifact(options.out, payload)
    return 0 if payload["status"] == "completed" and scan_clean else 1


async def _token_ready(settings: SimAuthSettings) -> bool:
    refresh = await refresh_live_token_if_needed(
        settings,
        minimum_validity=_TOKEN_VALIDITY,
    )
    if refresh.status not in {"fresh", "refreshed"}:
        return False
    token = load_token_cache(settings.cache_path)
    return (
        token is not None
        and token.environment == "LIVE"
        and token.expires_at > datetime.now(UTC) + _TOKEN_VALIDITY
    )


async def _read_exposed_ledger(
    client: Client[FastMCPTransport],
) -> SafeLedgerReport | None:
    result = await client.call_tool(_LEDGER_TOOL, {}, raise_on_error=False)
    return accepted_exposed_ledger(result)


def accepted_exposed_ledger(result: CallToolResult) -> SafeLedgerReport | None:
    if result.is_error:
        return None
    try:
        return SafeLedgerReport.model_validate(result.structured_content)
    except ValidationError:
        return None


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0 or not float("-inf") < parsed < float("inf"):
        raise argparse.ArgumentTypeError("value must be a finite positive number")
    return parsed


def _present_text(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("value must not be empty")
    return value


if __name__ == "__main__":
    sys.exit(main())
