from __future__ import annotations

from dataclasses import dataclass

from saxo_bank_mcp.live_precheck_response_models import (
    LivePrecheckResponse,
    PrecheckOrderResult,
)


@dataclass(frozen=True, slots=True)
class PrecheckResponseSummary:
    error_code: str
    disclaimer_count: int
    root_result_ok: bool
    child_result_count: int
    all_results_ok: bool
    disclaimer_object_present: bool
    error_object_present: bool


def summarize_precheck_response(parsed: LivePrecheckResponse) -> PrecheckResponseSummary:
    items = _response_items(parsed)
    return PrecheckResponseSummary(
        error_code=_error_code(items),
        disclaimer_count=_disclaimer_count(items),
        root_result_ok=parsed.precheck_result == "Ok",
        child_result_count=len(parsed.orders),
        all_results_ok=all(item.precheck_result == "Ok" for item in items),
        disclaimer_object_present=_disclaimer_object_present(items),
        error_object_present=any(item.error_info is not None for item in items),
    )


def _response_items(parsed: LivePrecheckResponse) -> tuple[PrecheckOrderResult, ...]:
    return (parsed, *parsed.orders)


def _error_code(items: tuple[PrecheckOrderResult, ...]) -> str:
    for item in items:
        if item.error_info is not None:
            return item.error_info.error_code
    return ""


def _disclaimer_count(items: tuple[PrecheckOrderResult, ...]) -> int:
    count = 0
    for item in items:
        if item.pretrade_disclaimers is not None:
            count += len(item.pretrade_disclaimers.disclaimer_tokens)
        if item.error_info is not None and item.error_info.pretrade_disclaimers is not None:
            count += len(item.error_info.pretrade_disclaimers.disclaimer_tokens)
    return count


def _disclaimer_object_present(items: tuple[PrecheckOrderResult, ...]) -> bool:
    return any(
        item.pretrade_disclaimers is not None
        or (item.error_info is not None and item.error_info.pretrade_disclaimers is not None)
        for item in items
    )
