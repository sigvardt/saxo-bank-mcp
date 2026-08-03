"""Process-scoped opaque selectors for AccountKey and OrderId agent usability.

Agents never receive raw AccountKey/OrderId in redacted tool output. They receive
HMAC-bound selectors that resolve only inside this process against the current token
generation, Saxo environment, and matching account context. Order selectors also carry
a short explicit expiry and are one-time for a successful write-preview creation.
Selectors are not portable across processes, token generations, environments, or accounts.
"""

from __future__ import annotations

import base64
import hmac
import secrets
import threading
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, cast
from uuid import UUID

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.auth import SaxoTokenSet

ACCOUNT_SELECTOR_PREFIX: Final = "proc-acct-"
ORDER_SELECTOR_PREFIX: Final = "proc-ord-"
_SAFE_ACCOUNT_FIELD: Final = "SafeAccountSelector"
_SAFE_ORDER_FIELD: Final = "SafeOrderSelector"
_PROCESS_SECRET: Final = secrets.token_bytes(32)
_LOCK = threading.Lock()
# Short explicit TTL: long enough for place → readback → cancel preview, not forever.
ORDER_SELECTOR_TTL_SECONDS: Final = 15 * 60


@dataclass(frozen=True, slots=True)
class AccountRow:
    account_key: str
    account_id: str = ""
    client_key: str = ""
    active: bool = True
    currency: str = ""
    account_type: str = ""


@dataclass(slots=True)
class OrderSelectorBinding:
    token_generation: str
    environment: str
    account_key: str
    order_id: str
    expires_at: datetime
    consumed: bool = False


_ORDER_BINDINGS: dict[str, OrderSelectorBinding] = {}


@dataclass(frozen=True, slots=True)
class BoundAccountSelector:
    """Private account material retained only for this token generation and process."""

    token_generation: str
    account_key: str
    client_key: str
    account_alias: str
    currency: str


_ACCOUNT_BINDINGS: dict[str, BoundAccountSelector] = {}


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


def resolve_bound_account_selector(
    token: SaxoTokenSet,
    selector: str,
) -> BoundAccountSelector | None:
    """Resolve only a selector issued from an observed account row in this process."""
    if not is_account_selector(selector):
        return None
    with _LOCK:
        binding = _ACCOUNT_BINDINGS.get(selector)
    if binding is None or not hmac.compare_digest(
        binding.token_generation,
        token_generation(token),
    ):
        return None
    return binding


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


def bind_order_selector(  # noqa: PLR0913
    token: SaxoTokenSet,
    *,
    environment: str,
    account_key: str,
    order_id: str,
    now: datetime | None = None,
    ttl_seconds: int = ORDER_SELECTOR_TTL_SECONDS,
) -> str:
    """Bind an order ID to a process-scoped selector for the current auth context."""
    cleaned_order = order_id.strip()
    cleaned_account = account_key.strip()
    cleaned_env = environment.strip().upper()
    if not cleaned_order or not cleaned_account or not cleaned_env:
        message = "order selector bind requires order_id, account_key, and environment"
        raise ValueError(message)
    generation = token_generation(token)
    current = now if now is not None else datetime.now(tz=UTC)
    expires_at = current + timedelta(seconds=ttl_seconds)
    material = b"\0".join(
        (
            generation.encode(),
            cleaned_env.encode(),
            cleaned_account.encode(),
            cleaned_order.encode(),
        )
    )
    digest = hmac.digest(_PROCESS_SECRET, material, "sha256")[:18]
    encoded = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    selector = f"{ORDER_SELECTOR_PREFIX}{encoded}"
    binding = OrderSelectorBinding(
        token_generation=generation,
        environment=cleaned_env,
        account_key=cleaned_account,
        order_id=cleaned_order,
        expires_at=expires_at,
        consumed=False,
    )
    with _LOCK:
        existing = _ORDER_BINDINGS.get(selector)
        if existing is not None:
            same_target = (
                existing.token_generation == generation
                and existing.environment == cleaned_env
                and existing.account_key == cleaned_account
                and existing.order_id == cleaned_order
            )
            if same_target:
                if existing.consumed:
                    # One-time selector stays consumed; never rearm via rebind.
                    return selector
                # Refresh expiry for an unused binding of the same exact target.
                existing.expires_at = expires_at
                return selector
            # Rare hash collision on a different target: rehash with a counter.
            counter = 0
            while selector in _ORDER_BINDINGS:
                counter += 1
                material = b"\0".join(
                    (
                        generation.encode(),
                        cleaned_env.encode(),
                        cleaned_account.encode(),
                        cleaned_order.encode(),
                        str(counter).encode(),
                    )
                )
                digest = hmac.digest(_PROCESS_SECRET, material, "sha256")[:18]
                encoded = base64.urlsafe_b64encode(digest).decode().rstrip("=")
                selector = f"{ORDER_SELECTOR_PREFIX}{encoded}"
        _ORDER_BINDINGS[selector] = binding
        return selector


def resolve_order_id_input(  # noqa: PLR0911
    value: str,
    *,
    token: SaxoTokenSet,
    environment: str,
    account_key: str,
    now: datetime | None = None,
) -> tuple[str | None, str]:
    """Validate an order id or selector without consuming it.

    Returns (resolved_order_id, denial_reason). Empty denial means success.
    Denial reasons never include the submitted selector or raw order id.
    """
    stripped = value.strip()
    if not stripped:
        return None, "order_id_missing"
    if not is_order_selector(stripped):
        # Raw order ids remain valid for controlled probes and allowlisted automation.
        return stripped, ""
    current = now if now is not None else datetime.now(tz=UTC)
    generation = token_generation(token)
    cleaned_env = environment.strip().upper()
    cleaned_account = account_key.strip()
    with _LOCK:
        binding = _ORDER_BINDINGS.get(stripped)
        if binding is None:
            return None, "order_selector_unknown"
        if binding.consumed:
            return None, "order_selector_consumed"
        if binding.expires_at <= current:
            return None, "order_selector_expired"
        if not hmac.compare_digest(binding.token_generation, generation):
            return None, "order_selector_token_mismatch"
        if binding.environment != cleaned_env:
            return None, "order_selector_environment_mismatch"
        if binding.account_key != cleaned_account:
            return None, "order_selector_account_mismatch"
        return binding.order_id, ""


def consume_order_selector(selector: str, *, now: datetime | None = None) -> str:
    """Mark one selector consumed. Prefer consume_order_selectors for multi-select atomicity."""
    return consume_order_selectors((selector,), now=now)


def consume_order_selectors(
    selectors: Sequence[str],
    *,
    now: datetime | None = None,
) -> str:
    """Atomically consume all selectors after one successful write preview.

    All-or-nothing under the process lock: if any selector is missing, expired, or
    already consumed, none of the selectors in this batch are modified.
    Returns empty string on success, or a safe denial reason. Never echoes values.
    """
    unique: list[str] = []
    seen: set[str] = set()
    for selector in selectors:
        stripped = selector.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        unique.append(stripped)
    if not unique:
        return "order_id_missing"
    current = now if now is not None else datetime.now(tz=UTC)
    with _LOCK:
        for stripped in unique:
            if not is_order_selector(stripped):
                return "order_selector_unknown"
            binding = _ORDER_BINDINGS.get(stripped)
            if binding is None:
                return "order_selector_unknown"
            if binding.consumed:
                return "order_selector_consumed"
            # Reject under the same lock, before any mutation, so expiry races fail closed.
            if binding.expires_at <= current:
                return "order_selector_expired"
        for stripped in unique:
            binding = _ORDER_BINDINGS[stripped]
            binding.consumed = True
        return ""


def inject_account_selectors(
    payload: JsonValue,
    token: SaxoTokenSet,
) -> JsonValue:
    """Copy payload, adding SafeAccountSelector only on account-row objects."""
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


def resolve_request_body_selectors(  # noqa: PLR0913
    request_body: Mapping[str, JsonValue],
    token: SaxoTokenSet,
    accounts: Sequence[AccountRow],
    *,
    environment: str,
    account_key_context: str,
    now: datetime | None = None,
) -> tuple[dict[str, JsonValue] | None, str, tuple[str, ...]]:
    """Resolve account and order selectors inside a write-preview request body.

    Order selectors are validated but not consumed. Returns
    (body, denial_reason, order_selectors_pending_consume).
    """
    body = dict(request_body)
    pending: list[str] = []
    account_value = body.get("AccountKey")
    resolved_account = account_key_context.strip()
    if isinstance(account_value, str) and account_value.strip():
        resolved_account_key, reason = resolve_account_key_input(
            token,
            accounts,
            account_value,
        )
        if resolved_account_key is None:
            return None, reason or "account_selector_invalid", ()
        body["AccountKey"] = resolved_account_key
        resolved_account = resolved_account_key
    if not resolved_account:
        return None, "account_key_missing", ()
    for key in ("OrderId", "OrderIds", "MultiLegOrderId"):
        if key not in body:
            continue
        resolved_value, reason, used = _resolve_order_field(
            body[key],
            token=token,
            environment=environment,
            account_key=resolved_account,
            now=now,
        )
        if reason:
            return None, reason, ()
        body[key] = resolved_value
        pending.extend(used)
    return body, "", tuple(pending)


def public_order_selectors(
    order_ids: Sequence[str],
    *,
    token: SaxoTokenSet,
    environment: str,
    account_key: str,
    now: datetime | None = None,
) -> list[str]:
    return [
        bind_order_selector(
            token,
            environment=environment,
            account_key=account_key,
            order_id=order_id,
            now=now,
        )
        for order_id in order_ids
        if order_id.strip()
    ]


def clear_process_scoped_selector_state_for_tests() -> None:
    """Test-only reset of in-process order bindings."""
    with _LOCK:
        _ACCOUNT_BINDINGS.clear()
        _ORDER_BINDINGS.clear()


def force_expire_order_selector_for_tests(
    selector: str,
    *,
    now: datetime | None = None,
) -> None:
    """Test-only: set a bound selector's expiry in the past under the process lock."""
    current = now if now is not None else datetime.now(tz=UTC)
    stripped = selector.strip()
    with _LOCK:
        binding = _ORDER_BINDINGS.get(stripped)
        if binding is None:
            return
        binding.expires_at = current - timedelta(seconds=1)


def _looks_like_account_row(mapping: Mapping[str, JsonValue]) -> bool:
    """Return True for port account rows, not order rows that only carry AccountKey."""
    account_key = mapping.get("AccountKey")
    if not isinstance(account_key, str) or not account_key.strip():
        return False
    account_id = mapping.get("AccountId")
    if isinstance(account_id, str) and account_id.strip():
        return True
    currency = mapping.get("Currency")
    account_type = mapping.get("AccountType")
    return (
        isinstance(currency, str)
        and bool(currency.strip())
        and isinstance(account_type, str)
        and bool(account_type.strip())
    )


def _inject_accounts(value: JsonValue, token: SaxoTokenSet) -> JsonValue:
    if isinstance(value, Mapping):
        mapping = cast("Mapping[str, JsonValue]", value)
        out: dict[str, JsonValue] = {}
        for key, child in mapping.items():
            out[key] = _inject_accounts(child, token)
        if _looks_like_account_row(mapping):
            account_key = mapping.get("AccountKey")
            if isinstance(account_key, str) and account_key.strip():
                selector = account_selector_for(token, account_key.strip())
                out[_SAFE_ACCOUNT_FIELD] = selector
                client_key = mapping.get("ClientKey")
                currency = mapping.get("Currency")
                if isinstance(client_key, str) and client_key.strip():
                    alias_bytes = hmac.digest(
                        _PROCESS_SECRET,
                        b"\0".join(
                            (
                                b"analytics-account-alias-v1",
                                token_generation(token).encode(),
                                account_key.strip().encode(),
                                client_key.strip().encode(),
                            ),
                        ),
                        "sha256",
                    )[:16]
                    with _LOCK:
                        _ACCOUNT_BINDINGS[selector] = BoundAccountSelector(
                            token_generation=token_generation(token),
                            account_key=account_key.strip(),
                            client_key=client_key.strip(),
                            account_alias=f"aa_{UUID(bytes=alias_bytes, version=4).hex}",
                            currency=(
                                currency.strip().upper()
                                if isinstance(currency, str) and currency.strip()
                                else ""
                            ),
                        )
        return out
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_inject_accounts(child, token) for child in value]
    return value


def _resolve_order_field(
    value: JsonValue,
    *,
    token: SaxoTokenSet,
    environment: str,
    account_key: str,
    now: datetime | None,
) -> tuple[JsonValue, str, tuple[str, ...]]:
    if isinstance(value, str):
        pending: tuple[str, ...] = (value,) if is_order_selector(value) else ()
        resolved, reason = resolve_order_id_input(
            value,
            token=token,
            environment=environment,
            account_key=account_key,
            now=now,
        )
        if resolved is None:
            return None, reason, ()
        return resolved, "", pending
    if isinstance(value, Sequence) and not isinstance(value, str):
        resolved_items: list[JsonValue] = []
        pending_list: list[str] = []
        for item in value:
            if not isinstance(item, str):
                return None, "order_selector_invalid", ()
            if is_order_selector(item):
                pending_list.append(item)
            resolved, reason = resolve_order_id_input(
                item,
                token=token,
                environment=environment,
                account_key=account_key,
                now=now,
            )
            if resolved is None:
                return None, reason, ()
            resolved_items.append(resolved)
        return resolved_items, "", tuple(pending_list)
    return None, "order_selector_invalid", ()


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
