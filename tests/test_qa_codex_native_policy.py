from __future__ import annotations

import pytest
from pydantic import ValidationError

from saxo_bank_mcp.qa_codex_native_policy import (
    CODEX_NATIVE_POLICY,
    CodexNativeProofPolicy,
)


def test_codex_native_policy_requires_one_real_codex_run() -> None:
    policy = CODEX_NATIVE_POLICY

    assert policy.policy_id == "codex_native_v1"
    assert policy.required_harnesses == ("codex",)
    assert policy.model_run_quorum == 1
    assert policy.require_real_model_calls is True
    assert policy.allow_skipped_cases is False


def test_codex_native_policy_is_strict_and_frozen() -> None:
    with pytest.raises(ValidationError):
        CodexNativeProofPolicy.model_validate(
            {
                "policy_id": "codex_native_v1",
                "required_harnesses": ["codex", "claude"],
                "model_run_quorum": 2,
            },
        )

    assert CODEX_NATIVE_POLICY.model_config.get("frozen") is True
