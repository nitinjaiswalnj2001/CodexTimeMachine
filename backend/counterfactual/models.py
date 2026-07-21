"""Validated deterministic Phase 9 target-coverage models."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from backend.assessment.models import TargetVerdict
from backend.divergence.models import DimensionStatus
from backend.evaluation.models import EvaluationTargetCategory

COUNTERFACTUAL_POLICY_VERSION = "1.0"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReplayCoverageStatus(StrEnum):
    OBSERVED = "OBSERVED"
    PARTIALLY_OBSERVED = "PARTIALLY_OBSERVED"
    NOT_OBSERVED = "NOT_OBSERVED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ShiftStatus(StrEnum):
    TARGET_COVERAGE_INCREASED = "TARGET_COVERAGE_INCREASED"
    TARGET_COVERAGE_UNCHANGED = "TARGET_COVERAGE_UNCHANGED"
    TARGET_COVERAGE_DECREASED = "TARGET_COVERAGE_DECREASED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"

class TotalActivityStatus(StrEnum):
    LESS_TOTAL_ACTIVITY = "LESS_TOTAL_ACTIVITY"
    MORE_TOTAL_ACTIVITY = "MORE_TOTAL_ACTIVITY"
    UNCHANGED_TOTAL_ACTIVITY = "UNCHANGED_TOTAL_ACTIVITY"
    DIFFERENT_TOTAL_ACTIVITY = "DIFFERENT_TOTAL_ACTIVITY"
    INSUFFICIENT_ACTIVITY_EVIDENCE = "INSUFFICIENT_ACTIVITY_EVIDENCE"

class TargetCoverageRelationship(StrEnum):
    INCREASED_TARGET_COVERAGE = "INCREASED_TARGET_COVERAGE"
    UNCHANGED_TARGET_COVERAGE = "UNCHANGED_TARGET_COVERAGE"
    DECREASED_TARGET_COVERAGE = "DECREASED_TARGET_COVERAGE"
    MIXED_TARGET_COVERAGE = "MIXED_TARGET_COVERAGE"
    INSUFFICIENT_TARGET_EVIDENCE = "INSUFFICIENT_TARGET_EVIDENCE"

class TargetShiftDirection(StrEnum):
    INCREASED = "INCREASED"
    UNCHANGED = "UNCHANGED"
    DECREASED = "DECREASED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"

class CoverageProof(StrictModel):
    policy_id: str
    observed_stages: list[str]
    missing_stages: list[str]
    fixture_sequence: int | None = None
    evaluation_sequence: int | None = None
    summary_sequence: int | None = None
    decision_sequence: int | None = None
    successful_command_event_ids: list[str] = Field(default_factory=list)

class TargetLevelShift(StrictModel):
    target_id: str
    baseline_level: int | None = Field(default=None, ge=0, le=2)
    replay_level: int | None = Field(default=None, ge=0, le=2)
    direction: TargetShiftDirection


class ActivityVolumeContext(StrictModel):
    total_activity_status: TotalActivityStatus
    target_coverage_relationship: TargetCoverageRelationship
    evaluation_breadth_status: DimensionStatus
    evidence_gathering_status: DimensionStatus
    statement: str
    supporting_baseline_event_ids: list[str]
    supporting_replay_event_ids: list[str]
    limitations: list[str]


class CounterfactualTargetCoverage(StrictModel):
    target_id: str
    category: EvaluationTargetCategory
    baseline_verdict: TargetVerdict
    replay_coverage_status: ReplayCoverageStatus
    baseline_evidence: list[str]
    replay_evidence: list[str]
    coverage_statement: str
    remaining_uncertainty: list[str]
    confidence: float = Field(ge=0, le=1)
    limitations: list[str]
    coverage_proof: CoverageProof


class CounterfactualShift(StrictModel):
    status: ShiftStatus
    statement: str
    supporting_target_ids: list[str]
    supporting_baseline_event_ids: list[str]
    supporting_replay_event_ids: list[str]
    supporting_difference_ids: list[str]
    confidence: float = Field(ge=0, le=1)
    limitations: list[str]
    target_level_shifts: list[TargetLevelShift]


class CounterfactualCoverageAssessment(StrictModel):
    schema_version: str = "1.1"
    policy_version: str = COUNTERFACTUAL_POLICY_VERSION
    coverage_id: str = Field(pattern=r"^cov-[0-9a-f]{24}$")
    run_id: str
    replay_id: str
    scenario_id: str
    context_id: str
    assessment_id: str
    intervention_id: str
    divergence_id: str
    target_coverages: list[CounterfactualTargetCoverage]
    shift: CounterfactualShift
    activity_volume_context: ActivityVolumeContext
    limitations: list[str]
    warnings: list[str]
    created_at: datetime
    coverage_hash: str


class CounterfactualManifest(StrictModel):
    schema_version: str = "1.1"
    policy_version: str = COUNTERFACTUAL_POLICY_VERSION
    coverage_id: str
    run_id: str
    replay_id: str
    scenario_id: str
    context_id: str
    assessment_id: str
    intervention_id: str
    divergence_id: str
    input_file_hashes: dict[str, str]
    output_file_hashes: dict[str, str]
    target_count: int
    coverage_status_counts: dict[str, int]
    shift_status: ShiftStatus
    coverage_hash: str
    created_at: datetime
