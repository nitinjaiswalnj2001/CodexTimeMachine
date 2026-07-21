"""Validated Ghost Engineer intervention models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.evaluation.models import EvaluationTargetCategory


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InterventionStatus(StrEnum):
    INTERVENTION_GENERATED = "INTERVENTION_GENERATED"
    NO_INTERVENTION = "NO_INTERVENTION"


class InterventionType(StrEnum):
    QUESTION = "QUESTION"
    OBSERVATION = "OBSERVATION"
    CONSTRAINT_REMINDER = "CONSTRAINT_REMINDER"
    EVALUATION_REQUEST = "EVALUATION_REQUEST"


class LeakageCategory(StrEnum):
    FUTURE_RESULT_LEAK = "FUTURE_RESULT_LEAK"
    SOLUTION_LEAK = "SOLUTION_LEAK"
    METRIC_LEAK = "METRIC_LEAK"
    ASSESSMENT_LEAK = "ASSESSMENT_LEAK"
    IDENTITY_LEAK = "IDENTITY_LEAK"
    REPLAY_CONTROL_LEAK = "REPLAY_CONTROL_LEAK"


class GeneratedIntervention(StrictModel):
    status: InterventionStatus
    target_id: str | None = None
    intervention_type: InterventionType | None = None
    clue: str | None = None
    reason: str | None = None
    rationale: str | None = None
    supporting_assessment_references: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status_shape(self) -> "GeneratedIntervention":
        if self.status is InterventionStatus.INTERVENTION_GENERATED:
            if not self.target_id or not self.intervention_type or not self.clue or not self.rationale:
                raise ValueError("generated intervention requires target, type, clue, and rationale")
            if self.reason is not None:
                raise ValueError("generated intervention must not contain a no-intervention reason")
        else:
            if self.clue is not None or self.intervention_type is not None:
                raise ValueError("NO_INTERVENTION requires a null clue and type")
            if not (self.reason or "").strip():
                raise ValueError("NO_INTERVENTION requires a reason")
        return self


class GhostIntervention(StrictModel):
    schema_version: str = "1.0"
    intervention_id: str = Field(pattern=r"^int-[0-9a-f]{24}$")
    status: InterventionStatus
    run_id: str
    scenario_id: str
    context_id: str
    assessment_id: str
    assessment_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_id: str | None = None
    blind_spot_category: EvaluationTargetCategory | None = None
    intervention_type: InterventionType | None = None
    clue: str | None = None
    reason: str | None = None
    rationale: str | None = None
    supporting_assessment_references: list[str]
    constraints: list[str]
    warnings: list[str]
    created_at: datetime
    intervention_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReplayIntervention(StrictModel):
    schema_version: str = "1.0"
    intervention_id: str
    intervention_hash: str
    clue: str | None


class InterventionManifest(StrictModel):
    schema_version: str = "1.1"
    intervention_id: str
    status: InterventionStatus
    run_id: str
    scenario_id: str
    context_id: str
    assessment_id: str
    assessment_hash: str
    assessment_source_directory: str
    generator_type: str
    generator_version: str
    intervention_hash: str
    replay_intervention_hash: str
    input_file_hashes: dict[str, str]
    output_file_hashes: dict[str, str]
    warning_count: int
    created_at: datetime

    @field_validator("assessment_source_directory")
    @classmethod
    def validate_assessment_source_directory(cls, value: str) -> str:
        windows = PureWindowsPath(value)
        parts = value.replace("\\", "/").split("/")
        if (not value or windows.is_absolute() or windows.drive
                or value.startswith(("/", "\\"))
                or any(part in {"", ".", ".."} for part in parts)):
            raise ValueError("assessment_source_directory must be a safe run-relative path")
        return "/".join(parts)
