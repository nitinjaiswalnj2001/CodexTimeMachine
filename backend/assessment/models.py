"""Validated Phase 5 assessment domain models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.evaluation.models import EvaluationTargetCategory
from backend.trajectory.models import ObservableEventType


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TargetVerdict(StrEnum):
    SATISFIED = "SATISFIED"
    PARTIALLY_SATISFIED = "PARTIALLY_SATISFIED"
    MISSED = "MISSED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class OverallStatus(StrEnum):
    BLIND_SPOT_IDENTIFIED = "BLIND_SPOT_IDENTIFIED"
    NO_BLIND_SPOT_IDENTIFIED = "NO_BLIND_SPOT_IDENTIFIED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class PastEvidenceReference(StrictModel):
    event_id: str
    sequence: int = Field(gt=0)
    event_type: ObservableEventType
    relevance: str = Field(min_length=1)


class FutureEvidenceReference(StrictModel):
    evidence_id: str
    relevance: str = Field(min_length=1)


class TargetAssessment(StrictModel):
    target_id: str
    category: EvaluationTargetCategory
    verdict: TargetVerdict
    summary: str = Field(min_length=1)
    observed_past_evidence: list[PastEvidenceReference]
    known_future_evidence: list[FutureEvidenceReference]
    missing_investigation: str | None = None
    confidence: float = Field(ge=0, le=1)
    limitations: list[str]


class OverallFinding(StrictModel):
    status: OverallStatus
    blind_spot_category: EvaluationTargetCategory | None = None
    statement: str = Field(min_length=1)
    supporting_target_ids: list[str]
    supporting_past_event_ids: list[str]
    supporting_future_evidence_ids: list[str]
    confidence: float = Field(ge=0, le=1)
    limitations: list[str]


class EvaluatorOutput(StrictModel):
    target_assessments: list[TargetAssessment]
    overall_finding: OverallFinding
    limitations: list[str]


class EvaluatorMetadata(StrictModel):
    provider: str
    model: str
    reasoning_effort: str
    thread_id: str
    exit_code: int
    web_search: Literal["disabled"] = "disabled"
    network_enabled: Literal[False] = False
    tool_policy: Literal["none"] = "none"
    provider_version: str | None = None


class TemporalBlindSpotAssessment(StrictModel):
    schema_version: str = "1.0"
    assessment_id: str = Field(pattern=r"^asm-[0-9a-f]{24}$")
    run_id: str
    scenario_id: str
    thread_id: str
    context_id: str
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_under_evaluation: str
    target_assessments: list[TargetAssessment]
    overall_finding: OverallFinding
    limitations: list[str]
    evaluator_metadata: EvaluatorMetadata
    created_at: datetime
    assessment_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class AssessmentManifest(StrictModel):
    schema_version: str = "1.0"
    assessment_id: str
    run_id: str
    scenario_id: str
    context_id: str
    context_hash: str
    evaluator_provider: str
    evaluator_model: str
    reasoning_effort: str
    evaluator_thread_id: str
    evaluator_input_hash: str
    raw_evaluator_response_hash: str
    raw_evaluator_events_hash: str | None = None
    evaluator_stderr_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    assessment_hash: str
    target_count: int
    verdict_counts: dict[str, int]
    warning_count: int
    input_file_hashes: dict[str, str]
    output_file_hashes: dict[str, str]
    created_at: datetime


class AssessmentFailureStage(StrEnum):
    PROVIDER_PREFLIGHT = "PROVIDER_PREFLIGHT"
    PROVIDER_EXECUTION = "PROVIDER_EXECUTION"
    PROVIDER_RESULT_VALIDATION = "PROVIDER_RESULT_VALIDATION"
    STRUCTURED_OUTPUT_PARSING = "STRUCTURED_OUTPUT_PARSING"
    GROUNDING_VALIDATION = "GROUNDING_VALIDATION"
    PUBLICATION = "PUBLICATION"


class AssessmentFailureManifest(StrictModel):
    schema_version: str = "1.0"
    assessment_status: Literal["FAILED"] = "FAILED"
    attempt_id: str
    run_id: str
    scenario_id: str
    context_id: str
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_provider: str
    evaluator_model: str
    reasoning_effort: str
    evaluator_thread_ids: list[str]
    exit_code: int | None = None
    failure_stage: AssessmentFailureStage
    failure_type: str
    failure_message: str
    evaluator_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_evaluator_response_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    raw_evaluator_events_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evaluator_stderr_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    available_artifact_hashes: dict[str, str]


class EvaluatorInput(StrictModel):
    schema_version: str = "1.0"
    identity: dict[str, str]
    decision_under_evaluation: str
    past_observable_evidence: list[dict[str, Any]]
    known_future_outcome: str
    known_future_evidence: list[dict[str, Any]]
    evaluation_targets: list[dict[str, Any]]
    boundary_summary: dict[str, Any]
    evaluator_instructions: list[str]
    required_output_schema: dict[str, Any]
