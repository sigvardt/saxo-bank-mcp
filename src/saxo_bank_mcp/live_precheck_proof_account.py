from __future__ import annotations

from dataclasses import dataclass

from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from pydantic import ValidationError

from saxo_bank_mcp.live_precheck_proof_models import (
    AccountCountJson,
    AccountListing,
    ExecutionAborted,
)

_ACCOUNT_TOOL = "saxo_list_live_accounts"


@dataclass(frozen=True, slots=True)
class ProofAccount:
    counts: AccountCountJson
    account_id: str
    account_ref: str
    position: int


@dataclass(frozen=True, slots=True)
class _SelectedAccount:
    account_id: str
    account_ref: str
    position: int


async def proof_account(
    client: Client[FastMCPTransport],
    account_position: int | None,
) -> ProofAccount | ExecutionAborted:
    accounts_result = await client.call_tool(_ACCOUNT_TOOL, {}, raise_on_error=False)
    if accounts_result.is_error:
        return ExecutionAborted(None, "account_listing", "invalid_account_response")
    try:
        listing = AccountListing.model_validate(accounts_result.structured_content)
    except ValidationError:
        return ExecutionAborted(None, "account_listing", "invalid_account_response")
    account_counts: AccountCountJson = {
        "active": listing.active_account_count,
        "total": listing.account_count,
    }
    selected = _selected_account(listing, account_position)
    if selected is None:
        return ExecutionAborted(
            account_counts,
            "account_selection",
            "account_selection_required",
        )
    return ProofAccount(
        account_counts,
        selected.account_id,
        selected.account_ref,
        selected.position,
    )


def _selected_account(
    listing: AccountListing,
    position: int | None,
) -> _SelectedAccount | None:
    active = tuple(account for account in listing.accounts if account.active)
    counts_match = listing.account_count == len(
        listing.accounts
    ) and listing.active_account_count == len(active)
    if not counts_match:
        return None
    if position is None:
        selected_position = 1 if len(active) == 1 else None
    else:
        selected_position = position if position <= len(active) else None
    if selected_position is None:
        return None
    selected = active[selected_position - 1]
    return _SelectedAccount(
        selected.account_id,
        selected.account_ref,
        selected_position,
    )
