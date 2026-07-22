from __future__ import annotations

from collections.abc import Mapping, Sequence

from saxo_bank_mcp._evidence import JsonValue

_RESERVED_PRECHECK_KEYS = frozenset(
    {
        "allreturnedresultsexplicitlyok",
        "disclaimercount",
        "disclaimerobjectpresent",
        "errorinfo",
        "errorobjectpresent",
        "multilegorderid",
        "orderid",
        "orderidentifierpresent",
        "orderids",
        "orders",
        "precheckrequestaccepted",
        "precheckresult",
        "pretradedisclaimers",
        "requiresdisclaimerreview",
        "rootresultexplicitlyok",
    },
)
_ALLOWED_ROOT_PRECHECK_KEYS = frozenset(
    {
        "allreturnedresultsexplicitlyok",
        "disclaimercount",
        "disclaimerobjectpresent",
        "errorobjectpresent",
        "orderidentifierpresent",
        "precheckrequestaccepted",
        "precheckresult",
        "requiresdisclaimerreview",
        "rootresultexplicitlyok",
    },
)


def contains_reserved_precheck_key(value: JsonValue) -> bool:
    return _contains_reserved_precheck_key(value, at_root=True)


def _contains_reserved_precheck_key(value: JsonValue, *, at_root: bool) -> bool:
    if isinstance(value, Mapping):
        reserved = any(
            normalized in _RESERVED_PRECHECK_KEYS
            and (not at_root or normalized not in _ALLOWED_ROOT_PRECHECK_KEYS)
            for normalized in (_normalized_key(key) for key in value)
        )
        return reserved or any(
            _contains_reserved_precheck_key(item, at_root=False) for item in value.values()
        )
    if isinstance(value, Sequence) and not isinstance(value, str):
        return any(_contains_reserved_precheck_key(item, at_root=False) for item in value)
    return False


def _normalized_key(key: str) -> str:
    return "".join(character.lower() for character in key if character.isalnum())
