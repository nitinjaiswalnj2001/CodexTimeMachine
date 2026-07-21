"""Validated domain models for temporal Codex runs."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


RUN_SCHEMA_VERSION = "2.2"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunKind(StrEnum):
    BASELINE = "BASELINE"
    REPLAY = "REPLAY"


class ReasoningEffort(StrEnum):
    MEDIUM = "medium"


class ApprovalPolicy(StrEnum):
    NEVER = "never"


class WebSearchMode(StrEnum):
    DISABLED = "disabled"


class RunStatus(StrEnum):
    PREPARED = "PREPARED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class PermissionSystem(StrEnum):
    PERMISSION_PROFILE = "permission_profile"


class ShellEnvironmentPolicy(StrictModel):
    inherit: Literal["core"] = "core"
    include_only: list[str] = Field(
        default_factory=lambda: [
            "PATH",
            "HOME",
            "USERPROFILE",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "TMPDIR",
            "COMSPEC",
            "PATHEXT",
        ]
    )
    ignore_default_excludes: Literal[False] = False
    experimental_use_profile: Literal[False] = False
    set: dict[str, str] = Field(default_factory=dict)


class CodexExecutionConfiguration(StrictModel):
    model: Literal["gpt-5.6-sol"] = "gpt-5.6-sol"
    reasoning_effort: ReasoningEffort = ReasoningEffort.MEDIUM
    approval_policy: ApprovalPolicy = ApprovalPolicy.NEVER
    web_search_mode: WebSearchMode = WebSearchMode.DISABLED
    ephemeral: Literal[True] = True
    ignore_user_config: Literal[True] = True
    ignore_rules: Literal[True] = True
    skip_git_repo_check: Literal[True] = True
    strict_config: Literal[True] = True
    timeout_seconds: int = Field(default=1800, gt=0, le=86400)
    preflight_timeout_seconds: int = Field(default=15, gt=0, le=300)
    permission_system: PermissionSystem = PermissionSystem.PERMISSION_PROFILE
    permission_profile: Literal["ctm_temporal"] = "ctm_temporal"
    network_enabled: Literal[False] = False
    shell_environment_policy: ShellEnvironmentPolicy = Field(
        default_factory=ShellEnvironmentPolicy
    )


class RunSpecification(StrictModel):
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    scenario_path: Path
    run_kind: RunKind
    execution_configuration: CodexExecutionConfiguration = Field(
        default_factory=CodexExecutionConfiguration
    )
    intervention_text: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def baseline_has_no_intervention(self) -> "RunSpecification":
        if self.run_kind is RunKind.BASELINE and self.intervention_text is not None:
            raise ValueError("BASELINE runs cannot include intervention_text")
        return self


class EventStreamSummary(StrictModel):
    event_count: int = Field(ge=0)
    event_types: dict[str, int]
    thread_id: str | None = None
    has_error_event: bool = False
    failure_event_types: dict[str, int] = Field(default_factory=dict)
    thread_started_count: int = Field(ge=0, default=0)
    item_types: dict[str, int] = Field(default_factory=dict)
    forbidden_item_types: list[str] = Field(default_factory=list)


class IsolationProbeResult(StrictModel):
    schema_version: str = "1.0"
    permission_profile: str
    platform: str
    workspace_read_succeeded: bool
    workspace_write_succeeded: bool
    outside_read_blocked: bool
    outside_write_blocked: bool
    environment_canary_absent: bool
    network_configured_disabled: bool
    network_connect_blocked: bool
    unrelated_home_read_blocked: bool
    probe_succeeded: bool
    failure_reasons: list[str]
    probe_output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class IsolationProbeCommand(StrictModel):
    schema_version: str = "1.0"
    platform: str
    resolved_codex_executable: str
    codex_version: str
    working_directory: str
    arguments: list[str]
    permission_profile: str
    permission_profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_read_paths: list[str]
    runtime_read_paths_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    probe_interpreter: str
    effective_sandbox_path: str | None = None
    managed_config_included: Literal[True] = True


class RunManifest(StrictModel):
    schema_version: str = RUN_SCHEMA_VERSION
    run_id: str
    scenario_id: str
    run_kind: RunKind
    base_snapshot_id: str
    base_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_start_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    intervention_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    effective_prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str
    reasoning_effort: ReasoningEffort
    permission_system: PermissionSystem
    permission_profile: str
    permission_profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_read_paths: list[str] = Field(default_factory=list)
    runtime_read_paths_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    probe_interpreter: str | None = None
    effective_sandbox_path: str | None = None
    network_enabled: bool
    approval_policy: ApprovalPolicy
    web_search_mode: WebSearchMode
    ephemeral: bool
    ignore_user_config: bool
    ignore_rules: bool
    skip_git_repo_check: bool
    strict_config: bool
    timeout_seconds: int
    preflight_timeout_seconds: int
    managed_config_included: Literal[True] = True
    shell_environment_policy: ShellEnvironmentPolicy
    codex_executable: str
    codex_version: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
    exit_code: int | None = None
    thread_id: str | None = None
    raw_events_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    final_message_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    workspace_end_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    run_status: RunStatus
    event_summary: EventStreamSummary | None = None
    event_validation_error: str | None = None
    failure_reason: str | None = None
    timed_out: bool = False
    workspace_end_error: str | None = None
    final_message_error: str | None = None
    isolation_probe_succeeded: bool | None = None
    isolation_probe_stdout_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    isolation_probe_stderr_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    isolation_probe_command_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    isolation_probe_result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
