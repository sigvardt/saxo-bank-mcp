from __future__ import annotations

import re
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_REQUIREMENTS_RESOURCE: Final = "_analytics_vision_requirements/vision_coverage_requirements.json"
_REQUIREMENTS_SOURCE_PATH: Final = (
    Path(__file__).resolve().parents[2] / "data" / "analytics" / "vision_coverage_requirements.json"
)
_SAFE_NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_VERSION_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FIELD_PATH_PATTERN: Final = re.compile(
    r"^[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*$",
)


class VisionRequirementsError(RuntimeError):
    """Raised when the independent analytics-vision requirements are invalid."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class RequiredArtifactOwner(_StrictModel):
    template_id: str
    analysis_kind: str

    @model_validator(mode="after")
    def _validate_binding(self) -> Self:
        _require_safe_name(self.template_id, "artifact template identifier")
        _require_safe_name(self.analysis_kind, "artifact analysis kind")
        return self


class RequiredSourceContract(_StrictModel):
    contract_id: str
    field_paths: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_binding(self) -> Self:
        _require_safe_name(self.contract_id, "source contract identifier")
        _require_unique(self.field_paths, "source field paths")
        if any(_FIELD_PATH_PATTERN.fullmatch(path) is None for path in self.field_paths):
            raise ValueError("source field path is invalid")
        return self


class VisionCoverageRequirements(_StrictModel):
    """Independent immutable inventory of the analytics vision's required surfaces."""

    schema_version: Literal["1"]
    requirements_version: str
    required_metric_ids: tuple[str, ...] = Field(min_length=1)
    required_analysis_kinds: tuple[str, ...] = Field(min_length=1)
    required_artifact_owners: tuple[RequiredArtifactOwner, ...] = Field(min_length=1)
    required_source_contracts: tuple[RequiredSourceContract, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_requirements(self) -> Self:
        _require_version(self.requirements_version, "requirements version")
        for values, label in (
            (self.required_metric_ids, "required metric identifiers"),
            (self.required_analysis_kinds, "required analysis kinds"),
        ):
            _require_unique(values, label)
            for value in values:
                _require_safe_name(value, label)
        _require_unique(
            (owner.template_id for owner in self.required_artifact_owners),
            "required artifact owners",
        )
        _require_unique(
            (source.contract_id for source in self.required_source_contracts),
            "required source contracts",
        )
        if any(
            owner.analysis_kind not in self.required_analysis_kinds
            for owner in self.required_artifact_owners
        ):
            raise ValueError("required artifact owner is not a required analysis kind")
        return self


def load_vision_coverage_requirements(
    path: Path | None = None,
) -> VisionCoverageRequirements:
    """Load the source-controlled vision inventory independently of runtime catalogs."""
    try:
        return VisionCoverageRequirements.model_validate_json(
            _read_requirements(path),
            strict=True,
        )
    except ValidationError as error:
        raise VisionRequirementsError("analytics vision requirements are invalid") from error


def _read_requirements(path: Path | None) -> str:
    if path is not None:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as error:
            raise VisionRequirementsError(
                "analytics vision requirements cannot be read",
            ) from error
    if _REQUIREMENTS_SOURCE_PATH.is_file():
        return _REQUIREMENTS_SOURCE_PATH.read_text(encoding="utf-8")
    try:
        return files("saxo_bank_mcp").joinpath(_REQUIREMENTS_RESOURCE).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError) as error:
        raise VisionRequirementsError(
            "analytics vision requirements cannot be read",
        ) from error


def _require_safe_name(value: str, label: str) -> None:
    if _SAFE_NAME_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _require_version(value: str, label: str) -> None:
    if _VERSION_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _require_unique(values: Iterable[str], label: str) -> None:
    material = tuple(values)
    if len(set(material)) != len(material):
        raise ValueError(f"{label} must be unique")
