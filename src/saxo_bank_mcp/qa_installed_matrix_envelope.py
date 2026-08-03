"""Strict candidate-bound envelope for one installed SIM matrix child result."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from saxo_bank_mcp.qa_sim_tool_matrix_models import SimToolMatrixReceipt

_ANALYSIS_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


def matrix_receipt_sha256(matrix: SimToolMatrixReceipt) -> str:
    """Hash one strict matrix receipt using its canonical JSON representation."""
    payload = json.dumps(
        matrix.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


class InstalledMatrixEnvelope(BaseModel):
    """Bind a child matrix to the exact candidate and ordered request."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    schema_version: Literal["1"] = "1"
    candidate_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    analysis_kinds: tuple[str, ...] = Field(min_length=1)
    matrix_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    matrix: SimToolMatrixReceipt

    @model_validator(mode="after")
    def _validate_binding(self) -> Self:
        if any(_ANALYSIS_KIND_PATTERN.fullmatch(kind) is None for kind in self.analysis_kinds):
            raise ValueError("installed matrix analysis kind is invalid")
        if len(self.analysis_kinds) != len(set(self.analysis_kinds)):
            raise ValueError("installed matrix analysis kinds must be unique")
        if self.matrix_sha256 != matrix_receipt_sha256(self.matrix):
            raise ValueError("installed matrix digest mismatch")
        return self
