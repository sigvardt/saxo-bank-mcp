from __future__ import annotations

import json
import sys

import anyio

from saxo_bank_mcp.config import (
    SaxoEnvironment,
    SaxoRuntimeConfig,
    SimAuthSettings,
    SimAuthSettingsError,
    resolve_sim_auth_settings,
)
from saxo_bank_mcp.sim_token_refresh import (
    SimRefreshOutcome,
    refresh_sim_token_if_needed,
)


async def _run(settings: SimAuthSettings) -> SimRefreshOutcome:
    return await refresh_sim_token_if_needed(settings)


def _emit(status: str, *, network_call_made: bool | None) -> None:
    payload = {
        "environment": "SIM",
        "network_call_made": network_call_made,
        "status": status,
    }
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def main() -> int:
    try:
        runtime = SaxoRuntimeConfig.from_env()
    except (OSError, ValueError):
        _emit("sim_environment_required", network_call_made=False)
        return 2

    if (
        runtime.requested_environment != SaxoEnvironment.SIM
        or runtime.effective_read_environment() != "SIM"
        or runtime.live_reads_enabled
    ):
        _emit("sim_environment_required", network_call_made=False)
        return 2

    try:
        settings = resolve_sim_auth_settings(
            require_redirect=False,
            allow_pending_redirect=False,
        )
    except SimAuthSettingsError:
        _emit("settings_unavailable", network_call_made=False)
        return 2

    try:
        outcome = anyio.run(_run, settings)
    except Exception:  # noqa: BLE001 - launcher must never print exception or secret details
        _emit("keeper_failed", network_call_made=None)
        return 1

    _emit(outcome.status, network_call_made=outcome.network_call_made)
    return 0 if outcome.status in {"fresh", "refreshed"} else 1


if __name__ == "__main__":
    sys.exit(main())
