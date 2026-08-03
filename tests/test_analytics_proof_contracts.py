"""Measured installed-test receipts for exact offline analytics proof contracts."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from functools import wraps
from importlib import import_module
from pathlib import Path
from typing import Literal, cast

import pytest
from pydantic import BaseModel

from saxo_bank_mcp.qa_analytics_evidence import (
    EXACT_OFFLINE_PROOF_SUPPORT,
    ProofExecutionKind,
    build_proof_execution_contracts,
    exact_analysis_measurement_node_id,
)

_CONTRACTS = build_proof_execution_contracts()
ANALYSIS_PROOF_CASES = tuple(
    (contract.analysis_kind, case.kind)
    for contract in _CONTRACTS
    for case in contract.cases
    if case.applicability == "required"
    and case.kind not in {"agent_use", "artifact_parity", "executable_sim", "visual_integrity"}
)


def _analysis_measurement_target(
    analysis_kind: str,
    case_kind: ProofExecutionKind,
) -> tuple[str, str]:
    """Route one exact supported proof pair to its measured domain assertion."""
    if (analysis_kind, case_kind) not in EXACT_OFFLINE_PROOF_SUPPORT:
        raise AssertionError("unsupported exact proof measurement")
    node_id = exact_analysis_measurement_node_id(analysis_kind, case_kind)
    module_id, function_name = node_id.split("::", maxsplit=1)
    return module_id.removeprefix("tests."), function_name


@dataclass(frozen=True, slots=True)
class ExactAnalysisProofMeasurement:
    analysis_kind: str
    case_kind: ProofExecutionKind
    requirement_code: str
    measurement_state: Literal["passed", "unavailable"]
    operation_kind: str
    operation_id: str
    executed_test_node_id: str
    observed_result_count: int
    observed_result_types: tuple[str, ...]
    observed_result_sha256: str
    observed_value_count: int
    observed_output_sha256: str
    executed_case_count: int
    failed_case_count: int
    comparison_count: int
    unexplained_difference_count: int
    mutation_count: int
    mutation_killed_count: int
    independent_path_observed: bool
    recovery_observed: bool
    publication_scan_passed: bool


@dataclass(frozen=True, slots=True)
class _ExecutedAnalysisAssertion:
    test_node_id: str
    observed_result_count: int
    observed_result_types: tuple[str, ...]
    observed_result_sha256: str
    observed_value_count: int


def _golden_fixture() -> object:
    fixture = import_module("test_analytics_metrics").golden
    factory = getattr(fixture, "__wrapped__", None)
    if not callable(factory):
        raise TypeError("golden proof fixture is unavailable")
    return factory()


_CAPTURE_EXCLUDED_MODULES = frozenset(
    {
        "saxo_bank_mcp.analytics_instrument_identity",
        "saxo_bank_mcp.analytics_models",
        "saxo_bank_mcp.analytics_source_contracts",
    },
)

type _ObservedJsonValue = (
    str | int | float | bool | None | list[_ObservedJsonValue] | dict[str, _ObservedJsonValue]
)


def _observed_json_value(value: object) -> _ObservedJsonValue:  # noqa: PLR0911
    if isinstance(value, BaseModel):
        return {
            "result_type": type(value).__qualname__,
            "value": _observed_json_value(
                cast("dict[str, object]", value.model_dump(mode="json")),
            ),
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "result_type": type(value).__qualname__,
            "value": repr(value),
        }
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        return {
            str(key): _observed_json_value(item)
            for key, item in sorted(mapping.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_observed_json_value(item) for item in cast("Sequence[object]", value)]
    if isinstance(value, Enum):
        return _observed_json_value(value.value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return {"result_type": type(value).__qualname__}


def _observed_value_count(value: object) -> int:
    if isinstance(value, Mapping):
        return sum(
            _observed_value_count(item) for item in cast("Mapping[object, object]", value).values()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return sum(_observed_value_count(item) for item in cast("Sequence[object]", value))
    return 1


def _invoke_measurement(  # noqa: C901
    module_name: str,
    function_name: str,
) -> _ExecutedAnalysisAssertion:
    module = import_module(module_name)
    function = getattr(module, function_name)
    signature = inspect.signature(function)
    observed_results: list[object] = []
    with ExitStack() as stack:
        patcher = stack.enter_context(pytest.MonkeyPatch.context())
        for name, candidate in tuple(vars(module).items()):
            if not inspect.isfunction(candidate):
                continue
            origin = candidate.__module__
            if (
                not origin.startswith("saxo_bank_mcp.analytics_")
                or origin in _CAPTURE_EXCLUDED_MODULES
            ):
                continue
            if inspect.iscoroutinefunction(candidate):
                async_candidate = cast("Callable[..., Awaitable[object]]", candidate)

                @wraps(async_candidate)
                async def async_capture(
                    *args: object,
                    __candidate: Callable[..., Awaitable[object]] = async_candidate,
                    **kwargs: object,
                ) -> object:
                    result = await __candidate(*args, **kwargs)
                    observed_results.append(result)
                    return result

                patcher.setattr(module, name, async_capture)
            else:

                @wraps(candidate)
                def capture(
                    *args: object,
                    __candidate: Callable[..., object] = candidate,
                    **kwargs: object,
                ) -> object:
                    result = __candidate(*args, **kwargs)
                    observed_results.append(result)
                    return result

                patcher.setattr(module, name, capture)
        arguments: dict[str, object] = {}
        for name in signature.parameters:
            if name == "golden":
                arguments[name] = _golden_fixture()
            elif name == "monkeypatch":
                arguments[name] = patcher
            elif name == "tmp_path":
                temporary = stack.enter_context(
                    tempfile.TemporaryDirectory(
                        prefix="proof-measurement-",
                        dir=Path(os.environ["TMPDIR"]),
                    ),
                )
                arguments[name] = Path(temporary)
            else:
                raise AssertionError(f"unsupported proof measurement fixture: {name}")
        observed = function(**arguments)
        if inspect.isawaitable(observed):
            asyncio.run(_await_measurement(observed))
    if not observed_results:
        raise AssertionError("analysis proof measurement observed no typed domain result")
    result_values = tuple(_observed_json_value(item) for item in observed_results)
    observed_result_sha256 = hashlib.sha256(
        json.dumps(result_values, separators=(",", ":"), sort_keys=True).encode(),
    ).hexdigest()
    return _ExecutedAnalysisAssertion(
        test_node_id=f"tests.{module_name}::{function_name}",
        observed_result_count=len(observed_results),
        observed_result_types=tuple(sorted({type(item).__qualname__ for item in observed_results})),
        observed_result_sha256=observed_result_sha256,
        observed_value_count=_observed_value_count(result_values),
    )


async def _await_measurement(observed: Awaitable[object]) -> None:
    await observed


def _execute_exact_proof_measurement(
    analysis_kind: str,
    case_kind: ProofExecutionKind,
) -> ExactAnalysisProofMeasurement:
    """Observe the exact analysis result without inventing unsupported proof semantics."""
    contract = next(item for item in _CONTRACTS if item.analysis_kind == analysis_kind)
    case = next(item for item in contract.cases if item.kind == case_kind)
    target = _analysis_measurement_target(analysis_kind, case_kind)
    assert f"tests.{target[0]}::{target[1]}" == exact_analysis_measurement_node_id(
        analysis_kind,
        case_kind,
    )
    primary = _invoke_measurement(*target)
    return ExactAnalysisProofMeasurement(
        analysis_kind=analysis_kind,
        case_kind=case_kind,
        requirement_code=case.requirement_code,
        measurement_state="passed",
        operation_kind="property_assertion",
        operation_id=(f"{analysis_kind}_{case_kind}_{primary.observed_result_sha256[:12]}"),
        executed_test_node_id=primary.test_node_id,
        observed_result_count=primary.observed_result_count,
        observed_result_types=primary.observed_result_types,
        observed_result_sha256=primary.observed_result_sha256,
        observed_value_count=primary.observed_value_count,
        observed_output_sha256=primary.observed_result_sha256,
        executed_case_count=primary.observed_result_count,
        failed_case_count=0,
        comparison_count=0,
        unexplained_difference_count=0,
        mutation_count=0,
        mutation_killed_count=0,
        independent_path_observed=False,
        recovery_observed=False,
        publication_scan_passed=False,
    )


@pytest.mark.parametrize(
    ("analysis_kind", "case_kind"),
    ANALYSIS_PROOF_CASES,
    ids=str,
)
def test_analysis_proof_contract(
    analysis_kind: str,
    case_kind: ProofExecutionKind,
    record_property: Callable[[str, object], None],
) -> None:
    """Emit the typed result observation and keep unmeasured proof semantics unavailable."""
    measurement = _execute_exact_proof_measurement(analysis_kind, case_kind)
    assert measurement.observed_result_count > 0
    assert measurement.observed_result_types
    assert measurement.observed_value_count > 0
    assert measurement.observed_output_sha256 == measurement.observed_result_sha256
    assert measurement.executed_case_count == measurement.observed_result_count
    assert measurement.measurement_state == "passed"
    assert measurement.operation_kind == "property_assertion"
    assert measurement.comparison_count == 0
    assert measurement.mutation_count == measurement.mutation_killed_count == 0
    assert not measurement.independent_path_observed
    assert not measurement.recovery_observed
    assert not measurement.publication_scan_passed
    record_property(
        "saxo_analytics_proof_receipt_v1",
        json.dumps(
            {
                "receipt_kind": "analysis_case",
                "analysis_kind": measurement.analysis_kind,
                "case_kind": measurement.case_kind,
                "requirement_code": measurement.requirement_code,
                "measurement_state": measurement.measurement_state,
                "operation_kind": measurement.operation_kind,
                "operation_id": measurement.operation_id,
                "executed_test_node_id": measurement.executed_test_node_id,
                "observed_result_count": measurement.observed_result_count,
                "observed_result_types": measurement.observed_result_types,
                "observed_result_sha256": measurement.observed_result_sha256,
                "observed_value_count": measurement.observed_value_count,
                "observed_output_sha256": measurement.observed_output_sha256,
                "executed_case_count": measurement.executed_case_count,
                "failed_case_count": measurement.failed_case_count,
                "comparison_count": measurement.comparison_count,
                "unexplained_difference_count": measurement.unexplained_difference_count,
                "mutation_count": measurement.mutation_count,
                "mutation_killed_count": measurement.mutation_killed_count,
                "independent_path_observed": measurement.independent_path_observed,
                "recovery_observed": measurement.recovery_observed,
                "publication_scan_passed": measurement.publication_scan_passed,
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
    )
