"""Process-scoped opaque selectors for AccountKey and OrderId agent usability.

Agents never receive raw AccountKey/OrderId in redacted tool output. They receive
HMAC-bound selectors that resolve only inside this process against the current token
generation. Selectors are not portable across processes or token generations.
"""

from __future__ import annotations

import base64
import hmac
import secrets
import threading
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Final, cast

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.auth import SaxoTokenSet

ACCOUNT_SELECTOR_PREFIX: Final = "proc-acct-"
ORDER_SELECTOR_PREFIX: Final = "proc-ord-"
_SAFE_ACCOUNT_FIELD: Final = "SafeAccountSelector"
_SAFE_ORDER_FIELD: Final = "SafeOrderSelector"
_PROCESS_SECRET: Final = secrets.token_bytes(32)
_LOCK = threading.Lock()
_ORDER_BINDINGS: dict[str, str] = {}
_ORDER_BY_ID: dict[str, str] = {}


@dataclass(frozen=True, slots=True)
class AccountRow:
    account_key: str
    account_id: str = ""
    active: bool = True
    currency: str = ""
    account_type: str = ""


def is_account_selector(value: str) -> bool:
    return value.startswith(ACCOUNT_SELECTOR_PREFIX)


def is_order_selector(value: str) -> bool:
    return value.startswith(ORDER_SELECTOR_PREFIX)


def token_generation(token: SaxoTokenSet) -> str:
    return token.code_verifier or token.access_token


def account_selector_for(token: SaxoTokenSet, account_key: str) -> str:
    message = b"\0".join((token_generation(token).encode(), account_key.encode()))
    digest = hmac.digest(_PROCESS_SECRET, message, "sha256")[:18]
    encoded = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return f"{ACCOUNT_SELECTOR_PREFIX}{encoded}"


def resolve_account_selector(
    token: SaxoTokenSet,
    accounts: Sequence[AccountRow],
    selector: str,
) -> str | None:
    if not is_account_selector(selector):
        return None
    matches = [
        account.account_key
        for account in accounts
        if hmac.compare_digest(account_selector_for(token, account.account_key), selector)
    ]
    return matches[0] if len(matches) == 1 else None


def resolve_account_key_input(
    token: SaxoTokenSet,
    accounts: Sequence[AccountRow],
    value: str,
) -> tuple[str | None, str]:
    """Return (resolved_account_key, denial_reason). Empty denial means success."""
    stripped = value.strip()
    if not stripped:
        return None, "account_key_missing"
    if is_account_selector(stripped):
        resolved = resolve_account_selector(token, accounts, stripped)
        if resolved is None:
            return None, "account_selector_invalid"
        return resolved, ""
    # Raw account keys remain valid for controlled probes and allowlisted automation.
    return stripped, ""


def bind_order_selector(order_id: str) -> str:
    cleaned = order_id.strip()
    with _LOCK:
        existing = _ORDER_BY_ID.get(cleaned)
        if existing is not None:
            return existing
        digest = hmac.digest(_PROCESS_SECRET, cleaned.encode(), "sha256")[:18]
        encoded = base64.urlsafe_b64encode(digest).decode().rstrip("=")
        selector = f"{ORDER_SELECTOR_PREFIX}{encoded}"
        # Avoid rare collisions by rehashing with a counter suffix material.
        counter = 0
        while selector in _ORDER_BINDINGS and _ORDER_BINDINGS[selector] != cleaned:
            counter += 1
            material = f"{cleaned}:{counter}".encode()
            digest = hmac.digest(_PROCESS_SECRET, material, "sha256")[:18]
            encoded = base64.urlsafe_b64encode(digest).decode().rstrip("=")
            selector = f"{ORDER_SELECTOR_PREFIX}{encoded}"
        _ORDER_BINDINGS[selector] = cleaned
        _ORDER_BY_ID[cleaned] = selector
        return selector


def resolve_order_selector(selector: str) -> str | None:
    if not is_order_selector(selector):
        return None
    with _LOCK:
        return _ORDER_BINDINGS.get(selector)


def resolve_order_id_input(value: str) -> tuple[str | None, str]:
    stripped = value.strip()
    if not stripped:
        return None, "order_id_missing"
    if is_order_selector(stripped):
        resolved = resolve_order_selector(stripped)
        if resolved is None:
            return None, "order_selector_invalid"
        return resolved, ""
    return stripped, ""


def inject_account_selectors(
    payload: JsonValue,
    token: SaxoTokenSet,
) -> JsonValue:
    """Copy payload, adding SafeAccountSelector beside each AccountKey before redaction."""
    return _inject_accounts(payload, token)


def resolve_order_body_accounts(
    order_body: Mapping[str, JsonValue],
    token: SaxoTokenSet,
    accounts: Sequence[AccountRow],
) -> tuple[dict[str, JsonValue] | None, str]:
    """Return a body copy with AccountKey resolved from selector when present."""
    body = dict(order_body)
    selector_or_key = body.get("AccountKey")
    if not isinstance(selector_or_key, str) or not selector_or_key.strip():
        safe = body.get(_SAFE_ACCOUNT_FIELD)
        if isinstance(safe, str) and safe.strip():
            selector_or_key = safe
        else:
            return None, "account_key_missing"
    resolved, reason = resolve_account_key_input(token, accounts, str(selector_or_key))
    if resolved is None:
        return None, reason or "account_selector_invalid"
    body["AccountKey"] = resolved
    body.pop(_SAFE_ACCOUNT_FIELD, None)
    return body, ""


def resolve_request_body_selectors(
    request_body: Mapping[str, JsonValue],
    token: SaxoTokenSet,
    accounts: Sequence[AccountRow],
) -> tuple[dict[str, JsonValue] | None, str]:
    """Resolve account and order selectors inside a write-preview request body."""
    body = dict(request_body)
    account_value = body.get("AccountKey")
    if isinstance(account_value, str) and account_value.strip():
        resolved_account, reason = resolve_account_key_input(token, accounts, account_value)
        if resolved_account is None:
            return None, reason or "account_selector_invalid"
        body["AccountKey"] = resolved_account
    for key in ("OrderId", "OrderIds", "MultiLegOrderId"):
        if key not in body:
            continue
        resolved_value, reason = _resolve_order_field(body[key])
        if reason:
            return None, reason
        body[key] = resolved_value
    return body, ""


def public_order_selectors(order_ids: Sequence[str]) -> list[str]:
    return [bind_order_selector(order_id) for order_id in order_ids if order_id.strip()]


def clear_process_scoped_selector_state_for_tests() -> None:
    """Test-only reset of in-process order bindings."""
    with _LOCK:
        _ORDER_BINDINGS.clear()
        _ORDER_BY_ID.clear()


def _inject_accounts(value: JsonValue, token: SaxoTokenSet) -> JsonValue:
    if isinstance(value, Mapping):
        mapping = cast("Mapping[str, JsonValue]", value)
        out: dict[str, JsonValue] = {}
        account_key = mapping.get("AccountKey")
        for key, child in mapping.items():
            out[key] = _inject_accounts(child, token)
        if isinstance(account_key, str) and account_key.strip():
            out[_SAFE_ACCOUNT_FIELD] = account_selector_for(token, account_key.strip())
        return out
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_inject_accounts(child, token) for child in value]
    return value


def _resolve_order_field(value: JsonValue) -> tuple[JsonValue, str]:
    if isinstance(value, str):
        resolved, reason = resolve_order_id_input(value)
        if resolved is None:
            return None, reason
        return resolved, ""
    if isinstance(value, Sequence) and not isinstance(value, str):
        resolved_items: list[JsonValue] = []
        for item in value:
            if not isinstance(item, str):
                return None, "order_selector_invalid"
            resolved, reason = resolve_order_id_input(item)
            if resolved is None:
                return None, reason
            resolved_items.append(resolved)
        return resolved_items, ""
    return None, "order_selector_invalid"


def accounts_from_port_payload(payload: Mapping[str, JsonValue]) -> tuple[AccountRow, ...]:
    data = payload.get("Data")
    rows: list[AccountRow] = []
    if isinstance(data, Sequence) and not isinstance(data, str):
        for item in data:
            if not isinstance(item, Mapping):
                continue
            row = cast("Mapping[str, JsonValue]", item)
            account_key = row.get("AccountKey")
            if not isinstance(account_key, str) or not account_key.strip():
                continue
            account_id = row.get("AccountId")
            currency = row.get("Currency")
            account_type = row.get("AccountType")
            active = row.get("Active")
            rows.append(
                AccountRow(
                    account_key=account_key.strip(),
                    account_id=account_id.strip()
                    if isinstance(account_id, str) and account_id.strip()
                    else "",
                    active=active is not False,
                    currency=currency.strip()
                    if isinstance(currency, str) and currency.strip()
                    else "",
                    account_type=account_type.strip()
                    if isinstance(account_type, str) and account_type.strip()
                    else "",
                ),
            )
    return tuple(rows)


def mutate_mapping_account_key(
    body: MutableMapping[str, JsonValue],
    *,
    account_key: str,
) -> None:
    body["AccountKey"] = account_key


async def fetch_account_rows_for_token(
    token: SaxoTokenSet,
    *,
    rest_base_url: str,
) -> tuple[AccountRow, ...]:
    """Fetch /port/v1/accounts/me and parse account rows for selector resolution."""
    import httpx2  # noqa: PLC0415

    from saxo_bank_mcp.http_client import create_async_client  # noqa: PLC0415
    from saxo_bank_mcp.strict_json import StrictJsonError, parse_json_value  # noqa: PLC0415

    try:
        async with create_async_client(base_url=rest_base_url) as client:
            response = await client.get(
                "port/v1/accounts/me",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token.access_token}",
                },
            )
    except httpx2.HTTPError:
        return ()
    if not 200 <= response.status_code < 300:  # noqa: PLR2004
        return ()
    try:
        parsed = parse_json_value(response.content)
    except StrictJsonError:
        return ()
    if not isinstance(parsed, Mapping):
        return ()
    return accounts_from_port_payload(cast("Mapping[str, JsonValue]", parsed))
