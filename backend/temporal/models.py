"""Validated domain models for temporal scenarios and sealed snapshots."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = "1.1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CutoffKind(StrEnum):
    FIXTURE_REVISION = "FIXTURE_REVISION"
    GIT_REF = "GIT_REF"
    TIMESTAMP = "TIMESTAMP"


class TemporalCutoff(StrictModel):
    kind: CutoffKind
    value: str = Field(min_length=1)

    @model_validator(mode="after")
    def implemented_kind_only(self) -> "TemporalCutoff":
        if self.kind is not CutoffKind.FIXTURE_REVISION:
            raise ValueError(f"cutoff kind {self.kind} is reserved but not implemented")
        return self


class NetworkPolicy(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"


class ScenarioType(StrEnum):
    CONTROLLED_FIXTURE = "controlled_fixture"


class AuditConfiguration(StrictModel):
    future_canary_token: str | None = None

    @field_validator("future_canary_token")
    @classmethod
    def nonempty_canary(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("future canary token must not be empty")
        return value


class TemporalScenario(StrictModel):
    schema_version: str = SCHEMA_VERSION
    scenario_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    scenario_type: ScenarioType
    cutoff: TemporalCutoff
    task: str = Field(min_length=1)
    network_policy: NetworkPolicy
    assets_manifest: str = "assets.yaml"
    output_directory: str = "sealed_snapshot"
    audit: AuditConfiguration = Field(default_factory=AuditConfiguration)


class AssetKind(StrEnum):
    SOURCE = "SOURCE"
    TEST = "TEST"
    DOCUMENT = "DOCUMENT"
    METRIC = "METRIC"
    EXPERIMENT_RESULT = "EXPERIMENT_RESULT"
    CONFIG = "CONFIG"
    DEVELOPMENT_NOTE = "DEVELOPMENT_NOTE"
    SESSION_ARTIFACT = "SESSION_ARTIFACT"
    SERVICE_SNAPSHOT = "SERVICE_SNAPSHOT"
    MODEL_RESOURCE = "MODEL_RESOURCE"


class VisibilityScope(StrEnum):
    PAST_CODEX = "PAST_CODEX"
    GHOST_ONLY = "GHOST_ONLY"
    EVALUATOR_ONLY = "EVALUATOR_ONLY"


class AvailabilityStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    LOCKED_FUTURE = "LOCKED_FUTURE"
    EXCLUDED = "EXCLUDED"


class ScenarioAssetAvailability(StrictModel):
    status: AvailabilityStatus
    reason: str = Field(min_length=1)


class TemporalAsset(StrictModel):
    asset_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    logical_path: str = Field(min_length=1)
    asset_kind: AssetKind
    source_path: str = Field(min_length=1)
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    availability_basis: str = Field(min_length=1)
    visibility_scope: VisibilityScope
    availability: ScenarioAssetAvailability
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssetAvailabilityManifest(StrictModel):
    schema_version: str = SCHEMA_VERSION
    scenario_id: str
    assets: list[TemporalAsset]

    @model_validator(mode="after")
    def unique_asset_ids(self) -> "AssetAvailabilityManifest":
        ids = [asset.asset_id for asset in self.assets]
        if len(ids) != len(set(ids)):
            raise ValueError("asset_id values must be unique")
        return self


class MaterializedAsset(StrictModel):
    asset_id: str
    logical_path: str
    asset_kind: AssetKind
    source_path: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    availability_basis: str
    visibility_scope: VisibilityScope
    availability_status: AvailabilityStatus
    availability_reason: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class SnapshotManifest(StrictModel):
    schema_version: str = SCHEMA_VERSION
    snapshot_id: str
    scenario_id: str
    scenario_type: ScenarioType
    cutoff: TemporalCutoff
    task: str
    network_policy: NetworkPolicy
    network_policy_enforced: bool = False
    asset_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    boundary_control_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    boundary_report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_root_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    materialized_assets: list[MaterializedAsset]


class BoundaryClassification(StrEnum):
    MATERIALIZED = "MATERIALIZED"
    LOCKED_FUTURE = "LOCKED_FUTURE"
    EXCLUDED = "EXCLUDED"
    NOT_VISIBLE_TO_PAST = "NOT_VISIBLE_TO_PAST"


class BoundaryEntry(StrictModel):
    asset_id: str
    logical_path: str
    status: BoundaryClassification
    reason: str


class BoundaryControlEntry(StrictModel):
    """Canonical control-plane classification for one declared asset."""

    asset_id: str
    logical_path: str
    availability_status: AvailabilityStatus
    visibility_scope: VisibilityScope
    classification: BoundaryClassification
    availability_reason: str
    classification_reason: str
    asset_kind: AssetKind
    availability_basis: str


class BoundaryControl(StrictModel):
    schema_version: str = SCHEMA_VERSION
    scenario_id: str
    entries: list[BoundaryControlEntry]

    @model_validator(mode="after")
    def unique_asset_ids(self) -> "BoundaryControl":
        asset_ids = [entry.asset_id for entry in self.entries]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("boundary control asset_id values must be unique")
        return self


class BoundarySummary(StrictModel):
    total: int
    materialized: int
    locked_future: int
    excluded: int
    not_visible_to_past: int


class BoundaryReport(StrictModel):
    schema_version: str = SCHEMA_VERSION
    scenario_id: str
    entries: list[BoundaryEntry]
    summary: BoundarySummary
