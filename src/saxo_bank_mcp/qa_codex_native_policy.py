"""Explicit policy for the Codex-only analytics proof path."""

from __future__ import annotations

from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

type HarnessPolicy = Literal["dual_v1", "codex_native_v1"]


class CodexNativeProofPolicy(BaseModel):
    """Frozen model quorum that may activate analytics proof profiles."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    policy_id: Literal["codex_native_v1"] = "codex_native_v1"
    required_harnesses: tuple[Literal["codex"], ...] = ("codex",)
    model_run_quorum: Literal[1] = 1
    require_real_model_calls: Literal[True] = True
    allow_skipped_cases: Literal[False] = False

    @model_validator(mode="after")
    def _require_exact_harness_set(self) -> Self:
        if self.required_harnesses != ("codex",):
            raise ValueError("codex_native_harness_set_invalid")
        return self


CODEX_NATIVE_POLICY: Final = CodexNativeProofPolicy()
