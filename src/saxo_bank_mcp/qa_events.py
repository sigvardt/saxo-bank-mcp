from __future__ import annotations

from saxo_bank_mcp._evidence import JsonValue, now_utc
from saxo_bank_mcp.loop_manifest import current_git_state


def base_event(command: str, status: str, detail: str) -> dict[str, JsonValue]:
    return {
        "checked_at": now_utc(),
        "command": command,
        "status": status,
        "detail": detail,
        "driver": "loop_harness",
        "git": current_git_state().model_dump(mode="json"),
    }
