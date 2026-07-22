from __future__ import annotations

import sys

import anyio

from saxo_bank_mcp.config import SimAuthSettings
from saxo_bank_mcp.live_mode import LiveReadSettingsError
from saxo_bank_mcp.live_oauth_settings import resolve_live_oauth_settings
from saxo_bank_mcp.live_token_refresh import keep_live_token_fresh


async def _run(settings: SimAuthSettings) -> None:
    try:
        await keep_live_token_fresh(settings)
    except anyio.get_cancelled_exc_class():
        return


def main() -> int:
    try:
        settings = resolve_live_oauth_settings()
    except LiveReadSettingsError as error:
        sys.stderr.write(
            "LIVE session keeper cannot start: configure LIVE OAuth settings "
            f"(reason: {error.code}).\n",
        )
        return 2

    try:
        anyio.run(_run, settings)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
