"""Shared value-free ingestion receipts, independent of capture domains."""

from pydantic import BaseModel, ConfigDict, Field


class IngestionFingerprints(BaseModel):
    """Value-free fingerprints retained for every authenticated ingestion."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, revalidate_instances="always"
    )
    raw_pages_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    normalized_rows_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    entitlements_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    correction_state_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
