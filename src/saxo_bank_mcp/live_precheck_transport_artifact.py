from __future__ import annotations

from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.transport_boundary import TransportBoundaryCapture

_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def transport_capture_payload(
    capture: TransportBoundaryCapture | None,
) -> dict[str, JsonValue] | None:
    if capture is None:
        return None
    return {
        "collector_process": "separate_process",
        "collector_credentials_inherited": False,
        "collector_complete": capture.collector_complete,
        "collector_exit_code": capture.collector_exit_code,
        "transport_layer": "httpx_async_base_transport",
        "safe_fields_only": True,
        "events": _JSON_VALUE_ADAPTER.validate_python(
            [event.model_dump(mode="json") for event in capture.events],
        ),
    }
