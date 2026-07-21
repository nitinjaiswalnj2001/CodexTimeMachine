"""Validated Phase 7 replay models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.runs.models import ApprovalPolicy, IsolationProbeResult, WebSearchMode


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReplayKind(StrEnum):
    COUNTERFACTUAL_WITH_MINIMUM_CLUE = "COUNTERFACTUAL_WITH_MINIMUM_CLUE"


class ReplayStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ReplayExecutionMode(StrEnum):
    DETERMINISTIC_FAKE = "DETERMINISTIC_FAKE"
    LIVE_MODEL = "LIVE_MODEL"


class SandboxPlatform(StrEnum):
    FAKE = "fake"
    LINUX = "linux"
    WINDOWS = "windows"
    MACOS = "macos"


class SandboxBackend(StrEnum):
    FAKE_ISOLATION = "fake-isolation"
    CODEX_LINUX_SANDBOX = "codex-linux-sandbox"
    ELEVATED_WINDOWS_SANDBOX = "elevated-windows-sandbox"
    CODEX_MACOS_SANDBOX = "codex-macos-sandbox"
    UNAVAILABLE = "unavailable"


class ReplayFailureStage(StrEnum):
    SOURCE_VALIDATION = "SOURCE_VALIDATION"
    WORKSPACE_MATERIALIZATION = "WORKSPACE_MATERIALIZATION"
    ISOLATION_PROBE = "ISOLATION_PROBE"
    PROVIDER_PREFLIGHT = "PROVIDER_PREFLIGHT"
    PROVIDER_EXECUTION = "PROVIDER_EXECUTION"
    PROVIDER_RESULT_VALIDATION = "PROVIDER_RESULT_VALIDATION"
    TRAJECTORY_EXTRACTION = "TRAJECTORY_EXTRACTION"
    PUBLICATION = "PUBLICATION"


class ReplayIdentity(StrictModel):
    schema_version: str = "1.0"
    replay_id: str = Field(pattern=r"^replay-[0-9a-f]{24}$")
    baseline_run_id: str
    scenario_id: str
    base_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_thread_id: str
    intervention_id: str
    intervention_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    replay_kind: Literal[ReplayKind.COUNTERFACTUAL_WITH_MINIMUM_CLUE] = ReplayKind.COUNTERFACTUAL_WITH_MINIMUM_CLUE
    created_at: datetime


class ReplayConfiguration(StrictModel):
    requested_model: str | None = None
    requested_reasoning_effort: str | None = None
    provider: Literal["fake", "codex"] = "fake"


class ReplayProviderResult(StrictModel):
    exit_code: int
    thread_ids: list[str]
    raw_event_bytes: bytes
    final_response_bytes: bytes | None
    stderr_bytes: bytes = b""
    provider_version: str
    timing_metadata: dict[str, Any] = Field(default_factory=dict)


class ReplayManifest(StrictModel):
    schema_version: str = "1.3"
    replay_id: str
    status: ReplayStatus
    baseline_run_id: str
    scenario_id: str
    replay_kind: ReplayKind
    base_snapshot_hash: str
    baseline_workspace_start_hash: str
    replay_workspace_start_hash: str
    replay_workspace_end_hash: str
    baseline_thread_id: str
    replay_thread_id: str
    intervention_id: str
    intervention_hash: str
    replay_prompt_hash: str
    provider: str
    provider_version: str
    execution_mode: ReplayExecutionMode
    live_model_invoked: bool
    requested_model: str | None
    effective_model: str
    reasoning_effort: str
    permission_profile: str
    permission_profile_hash: str
    runtime_read_paths: list[str]
    runtime_read_paths_hash: str
    sandbox_platform: SandboxPlatform
    sandbox_backend: SandboxBackend
    effective_sandbox_path: str | None
    network_enabled: bool
    approval_policy: ApprovalPolicy
    web_search_mode: WebSearchMode
    isolation_result: IsolationProbeResult
    raw_event_count: int
    normalized_event_count: int
    replay_trajectory_hash: str
    input_file_hashes: dict[str, str]
    output_file_hashes: dict[str, str]
    created_at: datetime
    warnings: list[str]

    @model_validator(mode="after")
    def validate_execution_metadata(self) -> "ReplayManifest":
        if self.execution_mode is ReplayExecutionMode.DETERMINISTIC_FAKE:
            if (self.provider != "fake" or self.live_model_invoked
                    or self.requested_model is not None
                    or self.effective_model != "deterministic-fake-replay"):
                raise ValueError("deterministic fake replay metadata is inconsistent")
        elif (self.provider != "codex" or not self.live_model_invoked
              or not self.requested_model or not self.effective_model):
            raise ValueError("live replay metadata is inconsistent")
        if self.sandbox_backend is SandboxBackend.FAKE_ISOLATION:
            if self.sandbox_platform is not SandboxPlatform.FAKE:
                raise ValueError("fake isolation backend requires fake platform")
        elif self.sandbox_platform is SandboxPlatform.FAKE:
            raise ValueError("fake platform requires fake isolation backend")
        return self


class ReplayFailureManifest(StrictModel):
    schema_version: str = "1.0"
    attempt_id: str
    baseline_run_id: str | None = None
    replay_id: str | None = None
    stage: ReplayFailureStage
    reason: str
    created_at: datetime
    artifact_hashes: dict[str, str] = Field(default_factory=dict)
