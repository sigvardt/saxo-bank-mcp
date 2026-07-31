# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import json.encoder
from collections.abc import Callable
from types import ModuleType
from typing import cast

import pytest

import saxo_bank_mcp.qa_analytics_source_matrix as matrix_module
from saxo_bank_mcp._evidence import JsonValue

_ORIGINLESS_SOURCE = """POLICY = {"allow": False}
class GateMeta(type):
    gate = {"allow": False}
    def __call__(cls):
        return cls.gate["allow"]
class Gate(metaclass=GateMeta):
    policy = {"allow": False}
    @classmethod
    def decision(cls):
        return cls.policy["allow"]
def decision():
    return POLICY["allow"]
"""


def _module_projection(module: ModuleType) -> list[dict[str, JsonValue]]:
    return matrix_module._module_execution_projection(module)  # noqa: SLF001


def _originless_module() -> ModuleType:
    module = ModuleType("round7_originless")
    exec(  # noqa: S102 - synthetic originless module is the regression surface
        _ORIGINLESS_SOURCE,
        module.__dict__,
    )
    del module.__dict__["GateMeta"]
    return module


def test_originless_module_projection_tracks_referenced_globals_and_class_state() -> None:
    module = _originless_module()
    decision = cast("Callable[[], bool]", module.__dict__["decision"])
    gate = cast("type", module.__dict__["Gate"])
    policy = cast("dict[str, bool]", module.__dict__["POLICY"])
    gate_policy = cast("dict[str, bool]", vars(gate)["policy"])
    metaclass_gate = cast("dict[str, bool]", vars(type(gate))["gate"])
    gate_decision = cast("Callable[[], bool]", type.__getattribute__(gate, "decision"))
    baseline = _module_projection(module)
    assert _module_projection(_originless_module()) == baseline

    policy["allow"] = True
    assert decision() is True
    global_changed = _module_projection(module) != baseline
    policy["allow"] = False

    gate_policy["allow"] = True
    assert gate_decision() is True
    class_changed = _module_projection(module) != baseline
    gate_policy["allow"] = False

    metaclass_gate["allow"] = True
    assert gate() is True
    metaclass_changed = _module_projection(module) != baseline

    assert [global_changed, class_changed, metaclass_changed] == [True, True, True]
    serialized = json.dumps(_module_projection(module), sort_keys=True)
    assert '"allow"' not in serialized
    assert "identity_sha256" not in serialized


def test_stdlib_projection_tracks_noncallable_global_that_changes_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoder = json.JSONEncoder(indent=0)
    before_behavior = encoder.encode(float("inf"))
    before_projection = _module_projection(json.encoder)

    monkeypatch.setattr(json.encoder, "INFINITY", 0.0)

    after_behavior = encoder.encode(float("inf"))
    after_projection = _module_projection(json.encoder)
    assert (before_behavior, after_behavior) == ("Infinity", "inf")
    assert after_projection != before_projection
    assert '"Infinity"' not in json.dumps(after_projection, sort_keys=True)
