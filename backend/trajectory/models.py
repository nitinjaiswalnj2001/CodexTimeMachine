"""Validated Phase 3 observable-trajectory domain models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.runs.models import RunKind


TRAJECTORY_SCHEMA_VERSION = "1.0"
TRAJECTORY_MANIFEST_SCHEMA_VERSION = "1.0"
EXTRACTOR_VERSION = "1.0.0"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ObservableEventType(StrEnum):
    THREAD_STARTED = "THREAD_STARTED"
    TURN_STARTED = "TURN_STARTED"
    AGENT_MESSAGE = "AGENT_MESSAGE"
    COMMAND_EXECUTED = "COMMAND_EXECUTED"
    FILE_CREATED = "FILE_CREATED"
    FILE_UPDATED = "FILE_UPDATED"
    FILE_DELETED = "FILE_DELETED"
    TURN_COMPLETED = "TURN_COMPLETED"


class ObservableEventStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INCOMPLETE = "INCOMPLETE"


class CommandTag(StrEnum):
    REPOSITORY_INSPECTION = "repository_inspection"
    FILE_INSPECTION = "file_inspection"
    GIT_INSPECTION = "git_inspection"
    TEST_EXECUTION = "test_execution"
    COMPILATION = "compilation"
    EVALUATION = "evaluation"
    OTHER = "other"


class MessageClassification(StrEnum):
    PROGRESS_UPDATE = "progress_update"
    FINAL_RESPONSE = "final_response"
    OTHER = "other"


class EvidenceReference(StrictModel):
    raw_line_indexes: list[int] = Field(min_length=1)
    source_item_id: str | None = None
    source_raw_event_types: list[str] = Field(min_length=1)
    source_fragments_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class NormalizedEvent(StrictModel):
    event_id: str = Field(pattern=r"^evt-[0-9a-f]{24}$")
    sequence: int = Field(gt=0)
    event_type: ObservableEventType
    status: ObservableEventStatus
    source_event_indexes: list[int] = Field(min_length=1)
    source_item_id: str | None = None
    source_item_type: str | None = None
    timestamp: datetime | None = None
    summary: str
    evidence: EvidenceReference
    workspace_relative_paths: list[str] = Field(default_factory=list)
    command: str | None = None
    exit_code: int | None = None
    command_tags: list[CommandTag] = Field(default_factory=list)
    output_preview: str | None = None
    output_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    redactions_applied: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ObservableTrajectory(StrictModel):
    schema_version: str = TRAJECTORY_SCHEMA_VERSION
    run_id: str
    scenario_id: str
    thread_id: str
    run_kind: RunKind
    source_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_raw_events_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    extracted_at: datetime
    extractor_version: str = EXTRACTOR_VERSION
    event_count: int = Field(ge=0)
    events: list[NormalizedEvent]
    warnings: list[str]
    trajectory_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class TrajectoryManifest(StrictModel):
    schema_version: str = TRAJECTORY_MANIFEST_SCHEMA_VERSION
    run_id: str
    extractor_version: str = EXTRACTOR_VERSION
    extracted_at: datetime
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_raw_events_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_final_message_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    trajectory_json_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trajectory_markdown_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trajectory_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_count: int = Field(ge=0)
    warnings_count: int = Field(ge=0)
