from __future__ import annotations

from dataclasses import dataclass

from fastmcp.tools import ToolResult

from saxo_bank_mcp.streaming import (
    SUBSCRIPTION_CLEANUP_PATH,
    StreamingValidationError,
    build_streaming_connect_url,
    local_subscription_count,
    remove_local_context,
    streaming_docs_evidence,
    validate_context_id,
)
from saxo_bank_mcp.streaming_tool_payloads import (
    base_payload,
    cached_token,
    denied,
    live_streaming_denial,
    tool_result,
    validate_price_subscription_request,
)
from saxo_bank_mcp.streaming_transport import (
    create_price_subscriptions,
    delete_root_subscription,
    receive_stream_frame,
)


@dataclass(frozen=True, slots=True)
class StreamingSubscriptionInput:
    context_id: str
    reference_id: str
    uics: list[int]
    asset_type: str
    wait_seconds: float
    last_message_id: int | None = None


async def execute_streaming_price_subscription(
    input_value: StreamingSubscriptionInput,
) -> ToolResult:
    environment_denial = live_streaming_denial("saxo_create_streaming_price_subscription")
    if environment_denial is not None:
        return tool_result(environment_denial)
    request = validate_price_subscription_request(
        input_value.context_id,
        input_value.reference_id,
        input_value.uics,
        input_value.asset_type,
    )
    if isinstance(request, dict):
        return tool_result(request)
    connect_evidence = streaming_docs_evidence(
        build_streaming_connect_url(
            str(request.context),
            last_message_id=input_value.last_message_id,
        ),
    )
    token_or_payload = cached_token("saxo_create_streaming_price_subscription")
    if isinstance(token_or_payload, dict):
        return tool_result({**token_or_payload, **connect_evidence})

    snapshots = await create_price_subscriptions(
        token_or_payload,
        str(request.context),
        str(request.reference),
        list(request.uics),
        request.asset_type,
    )
    if snapshots.get("status") != "snapshot_recorded":
        return tool_result(
            {
                **base_payload(
                    "saxo_create_streaming_price_subscription",
                    str(snapshots.get("status", "failed")),
                ),
                **connect_evidence,
                **snapshots,
            },
        )
    frame = await receive_stream_frame(
        token_or_payload,
        str(request.context),
        input_value.wait_seconds,
        last_message_id=input_value.last_message_id,
    )
    frame_recorded = frame.get("websocket_frame_recorded") is True
    data_message_observed = frame.get("data_message_observed") is True
    if frame_recorded and data_message_observed:
        status = "completed"
    elif frame_recorded:
        status = "control_only_no_data"
    else:
        status = "incomplete_no_frame"
    return tool_result(
        {
            **base_payload("saxo_create_streaming_price_subscription", status),
            **connect_evidence,
            **snapshots,
            **frame,
            "status": status,
            "network_call_made": True,
            "authorization_header_used": True,
            "subscription_snapshot_recorded": True,
            "streaming_completion_claim_allowed": status == "completed",
            "remote_subscription_may_remain": True,
            "cleanup_required": True,
            "cleanup_tool_name": "saxo_cleanup_streaming_subscriptions",
            "cleanup_warning": (
                "A REST subscription snapshot was recorded; call cleanup when the stream is "
                "done or when no usable data frame arrives."
            ),
        },
    )

async def execute_streaming_cleanup(context_id: str) -> ToolResult:
    environment_denial = live_streaming_denial("saxo_cleanup_streaming_subscriptions")
    if environment_denial is not None:
        return tool_result(environment_denial)
    try:
        context = validate_context_id(context_id)
    except StreamingValidationError as error:
        return tool_result(
            denied(
                "saxo_cleanup_streaming_subscriptions",
                error.code,
                context_id_validated=False,
            ),
        )
    before = local_subscription_count(str(context))
    removed = remove_local_context(str(context))
    after = local_subscription_count(str(context))
    token_or_payload = cached_token("saxo_cleanup_streaming_subscriptions")
    if isinstance(token_or_payload, dict):
        return tool_result(
            {
                **token_or_payload,
                "status": "auth_required",
                "local_registry_before_count": before,
                "local_registry_after_count": after,
                "local_removed_reference_ids": [
                    str(subscription.reference_id) for subscription in removed
                ],
                "local_open_records_after": [],
                "local_open_records_left": after > 0,
                "cleanup_endpoint": SUBSCRIPTION_CLEANUP_PATH,
                "cleanup_attempted": False,
                "cleanup_status": "auth_required",
                "remote_cleanup_accepted": False,
                "remote_cleanup_confirmed": False,
                "remote_cleanup_status_known": False,
                "remote_cleanup_acceptance_status_known": False,
                "remote_subscription_may_remain": True,
                "remote_cleanup_claim_allowed": False,
                "remote_cleanup_acceptance_claim_allowed": False,
                "any_subscription_may_remain": True,
                "open_subscription_left": after > 0,
                "open_subscription_left_scope": "local_registry_only",
            },
        )
    remote = await delete_root_subscription(token_or_payload, str(context))
    remote_accepted = remote.get("cleanup_status") == "accepted"
    status = "completed" if remote_accepted else "cleanup_remote_failed"
    return tool_result(
        {
            **base_payload("saxo_cleanup_streaming_subscriptions", status),
            "local_registry_before_count": before,
            "local_registry_after_count": after,
            "local_removed_reference_ids": [
                str(subscription.reference_id) for subscription in removed
            ],
            "local_open_records_after": [],
            "local_open_records_left": after > 0,
            "cleanup_endpoint": SUBSCRIPTION_CLEANUP_PATH,
            "cleanup_attempted": True,
            "remote_cleanup_accepted": remote_accepted,
            "remote_cleanup_confirmed": False,
            "remote_cleanup_status_known": False,
            "remote_cleanup_acceptance_status_known": remote_accepted,
            "remote_cleanup_status_scope": "delete_request_acceptance_only",
            "remote_subscription_may_remain": True,
            "remote_cleanup_claim_allowed": False,
            "remote_cleanup_acceptance_claim_allowed": remote_accepted,
            "any_subscription_may_remain": True,
            "open_subscription_left": after > 0,
            "open_subscription_left_scope": "local_registry_only",
            **remote,
        },
    )
