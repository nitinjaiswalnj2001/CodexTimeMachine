"""Typed models for grounded known-future evaluation contexts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.runs.models import RunKind
from backend.trajectory.models import NormalizedEvent


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProvenanceType(StrEnum):
    CONTROLLED_SYNTHETIC = "CONTROLLED_SYNTHETIC"
    ORGANIC_HISTORY = "ORGANIC_HISTORY"


class TemporalRelation(StrEnum):
    AFTER_CUTOFF = "AFTER_CUTOFF"


class EvidenceKind(StrEnum):
    EXPERIMENT_RESULT = "EXPERIMENT_RESULT"
    BENCHMARK_RESULT = "BENCHMARK_RESULT"
    INCIDENT = "INCIDENT"
    REVERSAL = "REVERSAL"
    LATENCY_RESULT = "LATENCY_RESULT"
    QUALITY_RESULT = "QUALITY_RESULT"
    TEST_RESULT = "TEST_RESULT"
    DEVELOPMENT_NOTE = "DEVELOPMENT_NOTE"
    OTHER = "OTHER"


class EvaluationTargetCategory(StrEnum):
    MISSING_EXPERIMENT = "MISSING_EXPERIMENT"
    MISSING_QUESTION = "MISSING_QUESTION"
    UNTESTED_ASSUMPTION = "UNTESTED_ASSUMPTION"
    IGNORED_CONSTRAINT = "IGNORED_CONSTRAINT"
    INSUFFICIENT_EVALUATION = "INSUFFICIENT_EVALUATION"
    DECISION_REVERSAL = "DECISION_REVERSAL"


class FutureEvidenceItem(StrictModel):
    evidence_id: str = Field(min_length=1)
    evidence_kind: EvidenceKind
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: str = Field(min_length=1)
    observed_after_cutoff: Literal[True]
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationTarget(StrictModel):
    target_id: str = Field(min_length=1)
    category: EvaluationTargetCategory
    description: str = Field(min_length=1)
    observable_success_condition: str = Field(min_length=1)
    related_evidence_ids: list[str] = Field(min_length=1)


class KnownFutureOutcomePacket(StrictModel):
    schema_version: str = "1.0"
    outcome_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    base_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_type: ProvenanceType
    fixture_notice: str | None = None
    provenance: str | None = None
    temporal_relation: TemporalRelation
    decision_under_evaluation: str = Field(min_length=1)
    known_future_outcome: str = Field(min_length=1)
    evidence_items: list[FutureEvidenceItem] = Field(min_length=1)
    evaluation_targets: list[EvaluationTarget] = Field(min_length=1)
    packet_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_semantics(self) -> "KnownFutureOutcomePacket":
        if self.provenance_type is ProvenanceType.CONTROLLED_SYNTHETIC:
            notice = (self.fixture_notice or "").casefold()
            if "synthetic" not in notice:
                raise ValueError("CONTROLLED_SYNTHETIC requires a synthetic fixture_notice")
            if "organic" in notice and "not organic" not in notice:
                raise ValueError("synthetic packet ambiguously claims organic history")
        elif not (self.provenance or "").strip():
            raise ValueError("ORGANIC_HISTORY requires non-empty provenance")
        evidence_ids = [item.evidence_id for item in self.evidence_items]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("duplicate evidence IDs")
        paths = [item.relative_path.casefold() for item in self.evidence_items]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate case-insensitive evidence paths")
        target_ids = [target.target_id for target in self.evaluation_targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("duplicate evaluation target IDs")
        known = set(evidence_ids)
        for target in self.evaluation_targets:
            missing = set(target.related_evidence_ids) - known
            if missing:
                raise ValueError(f"unknown related evidence IDs: {sorted(missing)}")
        return self


class BoundaryValidation(StrictModel):
    expected_workspace_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    actual_workspace_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    workspace_integrity_succeeded: bool
    packet_outside_workspace: bool
    all_evidence_outside_workspace: bool
    no_evidence_paths_in_workspace: bool
    no_evidence_hashes_in_workspace: bool
    workspace_file_count_checked: int = Field(ge=0)
    validation_succeeded: bool
    warnings: list[str]
    limitation: str


class EvaluationContext(StrictModel):
    schema_version: str = "1.0"
    context_id: str = Field(pattern=r"^ctx-[0-9a-f]{24}$")
    run_id: str
    scenario_id: str
    run_kind: RunKind
    base_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    thread_id: str
    trajectory_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome_id: str
    outcome_packet_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_type: ProvenanceType
    fixture_notice: str | None
    decision_under_evaluation: str
    known_future_outcome: str
    past_observable_evidence: list[NormalizedEvent]
    known_future_evidence: list[FutureEvidenceItem]
    evaluation_targets: list[EvaluationTarget]
    boundary_validation: BoundaryValidation
    warnings: list[str]
    created_at: datetime
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvaluationManifest(StrictModel):
    schema_version: str = "1.0"
    context_id: str
    run_id: str
    scenario_id: str
    trajectory_hash: str
    outcome_packet_hash: str
    context_hash: str
    input_file_hashes: dict[str, str]
    output_file_hashes: dict[str, str]
    future_evidence_file_hashes: dict[str, str]
    event_count: int
    evidence_item_count: int
    evaluation_target_count: int
    warnings_count: int
    created_at: datetime
