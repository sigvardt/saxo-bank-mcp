"""Strict, value-free proof receipts for deterministic analytics artifacts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictReceipt(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class ArtifactParityReceipt(_StrictReceipt):
    """Prove that a renderer consumed, but did not recalculate, verified semantics."""

    template_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    analysis_kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    structured_semantics_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_semantics_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    structured_value_count: int = Field(ge=0)
    rendered_value_count: int = Field(ge=0)
    sampled_point_count: int = Field(ge=0)
    state: Literal["passed", "failed", "refused"]
    reason_code: str = Field(default="passed", pattern=r"^[a-z][a-z0-9_]{0,127}$")

    @model_validator(mode="after")
    def _validate_pass_claim(self) -> Self:
        if self.state == "passed" and (
            self.structured_semantics_sha256 != self.artifact_semantics_sha256
            or self.structured_value_count != self.rendered_value_count
            or self.sampled_point_count > self.rendered_value_count
        ):
            raise ValueError("artifact fingerprint or value parity does not support a pass")
        if self.state == "passed" and self.reason_code != "passed":
            raise ValueError("passed artifact parity must use the passed reason")
        if self.state != "passed" and self.reason_code == "passed":
            raise ValueError("failed artifact parity requires a named reason")
        return self


class ArtifactVisualIntegrityReceipt(_StrictReceipt):
    """Record actual headless visual checks without retaining artifact content or paths."""

    template_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")
    formats: tuple[Literal["png", "html", "pdf"], ...] = Field(min_length=1)
    desktop_width: int = Field(ge=768, le=4096)
    mobile_width: int = Field(ge=320, le=767)
    png_pixel_check_passed: bool
    text_clipping_detected: bool
    label_overlap_detected: bool
    html_mobile_readable: bool
    privacy_footer_present: bool
    provenance_stamp_present: bool
    state: Literal["passed", "failed", "refused"]
    reason_code: str = Field(default="passed", pattern=r"^[a-z][a-z0-9_]{0,127}$")

    @model_validator(mode="after")
    def _validate_visual_claim(self) -> Self:
        if len(set(self.formats)) != len(self.formats):
            raise ValueError("visual formats must be unique")
        checks_pass = (
            ("png" not in self.formats or self.png_pixel_check_passed)
            and not self.text_clipping_detected
            and not self.label_overlap_detected
            and ("html" not in self.formats or self.html_mobile_readable)
            and self.privacy_footer_present
            and self.provenance_stamp_present
        )
        if self.state == "passed" and not checks_pass:
            raise ValueError("visual integrity checks do not support a pass")
        if self.state == "passed" and self.reason_code != "passed":
            raise ValueError("passed visual integrity must use the passed reason")
        if self.state != "passed" and self.reason_code == "passed":
            raise ValueError("non-passing visual integrity requires a named reason")
        return self


def artifact_evidence_errors(
    parity: Sequence[ArtifactParityReceipt],
    visual: Sequence[ArtifactVisualIntegrityReceipt],
    *,
    expected_template_ids: Sequence[str],
) -> tuple[str, ...]:
    """Return deterministic coverage errors for one final artifact evidence set."""
    expected = tuple(expected_template_ids)
    parity_ids = tuple(item.template_id for item in parity)
    visual_ids = tuple(item.template_id for item in visual)
    errors: list[str] = []
    if len(expected) != len(set(expected)):
        errors.append("expected_artifact_templates_not_unique")
    if len(parity_ids) != len(set(parity_ids)):
        errors.append("artifact_parity_receipts_not_unique")
    if len(visual_ids) != len(set(visual_ids)):
        errors.append("artifact_visual_receipts_not_unique")
    if set(parity_ids) != set(expected):
        errors.append("artifact_parity_coverage_mismatch")
    if set(visual_ids) != set(expected):
        errors.append("artifact_visual_coverage_mismatch")
    if any(item.state != "passed" for item in parity):
        errors.append("artifact_parity_not_passed")
    if any(item.state != "passed" for item in visual):
        errors.append("artifact_visual_integrity_not_passed")
    return tuple(errors)
