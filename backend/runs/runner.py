"""Temporal run lifecycle: audited workspace, fresh Codex exec, raw evidence."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml
from pydantic import ValidationError

from backend.temporal.integrity import canonical_json_bytes, sha256_bytes, sha256_file
from backend.temporal.models import TemporalScenario

from .codex_cli import (
    PERMISSION_PROFILE_HASH,
    CodexCLIAdapter,
    CodexPreflightError,
    CodexProcessResult,
    permission_profile_hash,
)
from .events import (
    DEFAULT_FORBIDDEN_ITEM_TYPES,
    EventStreamError,
    summarize_event_stream,
)
from .models import (
    CodexExecutionConfiguration,
    RunKind,
    RunManifest,
    RunSpecification,
    RunStatus,
)
from .isolation import IsolationProbeRunner
from .workspace import RunWorkspaceBuilder, RunWorkspaceError, compute_workspace_tree_hash


class TemporalRunError(RuntimeError):
    """Raised for controlled failures before a Codex process can complete."""


def hash_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def build_effective_prompt(task: str, intervention_text: str | None) -> str:
    if intervention_text is None:
        return task
    return f"{task}\n\nADDITIONAL EVALUATION GUIDANCE\n\n{intervention_text}"


def atomic_write_manifest(path: str | Path, manifest: RunManifest) -> None:
    """Replace a manifest only after a complete canonical JSON file is durable."""
    path = Path(path)
    temp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temp.write_bytes(canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def finalize_pre_execution_failure(
    manifest: RunManifest,
    manifest_path: Path,
    workspace: Path,
    reason: str,
    **updates: object,
) -> RunManifest:
    """Persist FAILED even when inspecting the prepared workspace also fails."""
    end_hash: str | None = None
    end_error: str | None = None
    try:
        end_hash = compute_workspace_tree_hash(workspace)
    except (RunWorkspaceError, OSError) as exc:
        end_error = f"workspace end inspection failed: {exc}"
    failed = manifest.model_copy(
        update={
            "completed_at": datetime.now(timezone.utc),
            "workspace_end_hash": end_hash,
            "workspace_end_error": end_error,
            "run_status": RunStatus.FAILED,
            "failure_reason": reason,
            **updates,
        }
    )
    atomic_write_manifest(manifest_path, failed)
    return failed


def load_scenario(path: str | Path) -> TemporalScenario:
    path = Path(path).resolve()
    try:
        return TemporalScenario.model_validate(yaml.safe_load(path.read_text("utf-8")))
    except (OSError, yaml.YAMLError, ValidationError, ValueError) as exc:
        raise TemporalRunError(f"invalid scenario manifest: {exc}") from exc


def default_runs_root(scenario_path: str | Path) -> Path:
    scenario_path = Path(scenario_path).resolve()
    for candidate in scenario_path.parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate / ".ctm_runs"
    return scenario_path.parent.parent / ".ctm_runs"


def _validate_scenario_run_separation(scenario_directory: Path, runs_root: Path) -> None:
    scenario_directory = scenario_directory.resolve()
    runs_root = runs_root.resolve()
    if (
        runs_root == scenario_directory
        or runs_root.is_relative_to(scenario_directory)
        or scenario_directory.is_relative_to(runs_root)
    ):
        raise RunWorkspaceError(
            "scenario directory and runs root must be non-overlapping resolved paths"
        )


class TemporalRunner:
    def __init__(
        self,
        codex_cli: CodexCLIAdapter | None = None,
        forbidden_item_types: frozenset[str] | None = None,
    ) -> None:
        self.codex_cli = codex_cli or CodexCLIAdapter()
        self.forbidden_item_types = (
            forbidden_item_types
            if forbidden_item_types is not None
            else DEFAULT_FORBIDDEN_ITEM_TYPES
        )

    def run(
        self,
        specification: RunSpecification,
        runs_root: str | Path | None = None,
    ) -> RunManifest:
        scenario_path = specification.scenario_path.resolve()
        scenario = load_scenario(scenario_path)
        sealed_snapshot = (scenario_path.parent / scenario.output_directory).resolve()
        root = Path(runs_root).resolve() if runs_root else default_runs_root(scenario_path)
        _validate_scenario_run_separation(scenario_path.parent, root)
        run_directory = root / specification.run_id
        prepared = RunWorkspaceBuilder(
            scenario.audit.future_canary_token
        ).prepare(sealed_snapshot, run_directory)
        config = specification.execution_configuration
        effective_prompt = build_effective_prompt(scenario.task, specification.intervention_text)
        manifest_path = run_directory / "run_manifest.json"
        started_at = datetime.now(timezone.utc)
        manifest = RunManifest(
            run_id=specification.run_id,
            scenario_id=scenario.scenario_id,
            run_kind=specification.run_kind,
            base_snapshot_id=prepared.snapshot_manifest.snapshot_id,
            base_snapshot_hash=prepared.snapshot_manifest.snapshot_root_hash,
            workspace_start_hash=prepared.workspace_start_hash,
            task_hash=hash_text(scenario.task),
            intervention_hash=(
                hash_text(specification.intervention_text)
                if specification.intervention_text is not None
                else None
            ),
            effective_prompt_hash=hash_text(effective_prompt),
            model=config.model,
            reasoning_effort=config.reasoning_effort,
            permission_system=config.permission_system,
            permission_profile=config.permission_profile,
            permission_profile_hash=PERMISSION_PROFILE_HASH,
            runtime_read_paths=[],
            runtime_read_paths_hash=sha256_bytes(canonical_json_bytes([])),
            probe_interpreter=None,
            effective_sandbox_path=None,
            network_enabled=config.network_enabled,
            approval_policy=config.approval_policy,
            web_search_mode=config.web_search_mode,
            ephemeral=config.ephemeral,
            ignore_user_config=config.ignore_user_config,
            ignore_rules=config.ignore_rules,
            skip_git_repo_check=config.skip_git_repo_check,
            strict_config=config.strict_config,
            timeout_seconds=config.timeout_seconds,
            preflight_timeout_seconds=config.preflight_timeout_seconds,
            managed_config_included=True,
            shell_environment_policy=config.shell_environment_policy,
            codex_executable=self.codex_cli.executable,
            started_at=started_at,
            run_status=RunStatus.PREPARED,
        )
        atomic_write_manifest(manifest_path, manifest)

        try:
            preflight = self.codex_cli.preflight(config.preflight_timeout_seconds)
        except CodexPreflightError as exc:
            finalize_pre_execution_failure(
                manifest, manifest_path, prepared.workspace, str(exc)
            )
            raise TemporalRunError(str(exc)) from exc

        effective_shell_environment_policy = config.shell_environment_policy.model_copy(
            update={
                "set": (
                    {"PATH": preflight.effective_sandbox_path}
                    if preflight.effective_sandbox_path is not None
                    else {}
                )
            }
        )
        runtime_updates = {
            "permission_profile_hash": permission_profile_hash(preflight.runtime_read_paths),
            "runtime_read_paths": list(preflight.runtime_read_paths),
            "runtime_read_paths_hash": preflight.runtime_read_paths_hash,
            "probe_interpreter": preflight.probe_interpreter,
            "effective_sandbox_path": preflight.effective_sandbox_path,
            "shell_environment_policy": effective_shell_environment_policy,
        }

        try:
            probe = IsolationProbeRunner().run(
                self.codex_cli,
                preflight,
                config,
                run_directory,
            )
        except (OSError, RuntimeError) as exc:
            return finalize_pre_execution_failure(
                manifest,
                manifest_path,
                prepared.workspace,
                f"isolation probe orchestration failed: {exc}",
                codex_executable=preflight.executable,
                codex_version=preflight.version,
                isolation_probe_succeeded=False,
                **runtime_updates,
            )
        probe_hashes = {
            "isolation_probe_stdout_sha256": probe.stdout_sha256,
            "isolation_probe_stderr_sha256": probe.stderr_sha256,
            "isolation_probe_command_sha256": probe.command_sha256,
            "isolation_probe_result_sha256": probe.result_sha256,
        }
        if not probe.result.probe_succeeded:
            return finalize_pre_execution_failure(
                manifest,
                manifest_path,
                prepared.workspace,
                "; ".join(probe.result.failure_reasons),
                codex_executable=preflight.executable,
                codex_version=preflight.version,
                isolation_probe_succeeded=False,
                **runtime_updates,
                **probe_hashes,
            )

        running = manifest.model_copy(
            update={
                "codex_executable": preflight.executable,
                "codex_version": preflight.version,
                "isolation_probe_succeeded": True,
                **runtime_updates,
                **probe_hashes,
                "run_status": RunStatus.RUNNING,
            }
        )
        atomic_write_manifest(manifest_path, running)
        raw_events = run_directory / "raw_codex_events.jsonl"
        stderr_log = run_directory / "codex_stderr.log"
        final_message = run_directory / "final_message.txt"
        command = self.codex_cli.build_command(
            preflight,
            config,
            prepared.workspace,
            final_message,
            effective_prompt,
        )

        try:
            process_result = self.codex_cli.execute(
                command,
                prepared.workspace,
                raw_events,
                stderr_log,
                config.timeout_seconds,
            )
        except OSError as exc:
            process_result = CodexProcessResult(
                exit_code=None,
                failure_reason=f"Codex process could not execute: {exc}",
            )
        return self._finalize_execution(
            running,
            manifest_path,
            prepared.workspace,
            raw_events,
            final_message,
            process_result,
            self.forbidden_item_types,
        )

    @staticmethod
    def _finalize_execution(
        running: RunManifest,
        manifest_path: Path,
        workspace: Path,
        raw_events: Path,
        final_message: Path,
        process_result: CodexProcessResult,
        forbidden_item_types: frozenset[str],
    ) -> RunManifest:
        failures: list[str] = []
        if process_result.failure_reason:
            failures.append(process_result.failure_reason)
        if process_result.exit_code not in (0, None):
            failures.append(f"Codex process exited with code {process_result.exit_code}")
        if process_result.exit_code is None and not process_result.timed_out:
            failures.append("Codex process did not produce an exit code")

        raw_hash: str | None = None
        summary = None
        validation_error: str | None = None
        try:
            if raw_events.is_file():
                raw_hash = sha256_file(raw_events)
                try:
                    summary = summarize_event_stream(raw_events, forbidden_item_types)
                except EventStreamError as exc:
                    validation_error = str(exc)
                    failures.append(validation_error)
            else:
                failures.append("raw Codex event stream was not created")
        except OSError as exc:
            validation_error = f"raw event inspection failed: {exc}"
            failures.append(validation_error)

        if summary is not None:
            if summary.has_error_event:
                names = ", ".join(summary.failure_event_types)
                failures.append(f"Codex failure event emitted: {names}")
            if summary.thread_started_count != 1:
                failures.append(
                    "fresh thread requirement failed: expected exactly one "
                    f"thread.started event, found {summary.thread_started_count}"
                )
            elif not summary.thread_id:
                failures.append("fresh thread requirement failed: thread ID is missing")
            if summary.forbidden_item_types:
                failures.append(
                    "forbidden external-context item emitted: "
                    + ", ".join(summary.forbidden_item_types)
                )

        final_hash: str | None = None
        final_message_error: str | None = None
        try:
            if final_message.is_file():
                final_hash = sha256_file(final_message)
        except OSError as exc:
            final_message_error = f"final message hashing failed: {exc}"
            failures.append(final_message_error)

        end_hash: str | None = None
        workspace_end_error: str | None = None
        try:
            end_hash = compute_workspace_tree_hash(workspace)
        except (RunWorkspaceError, OSError) as exc:
            workspace_end_error = f"workspace end inspection failed: {exc}"
            failures.append(workspace_end_error)

        final_manifest = running.model_copy(
            update={
                "completed_at": datetime.now(timezone.utc),
                "exit_code": process_result.exit_code,
                "thread_id": summary.thread_id if summary else None,
                "raw_events_sha256": raw_hash,
                "final_message_sha256": final_hash,
                "workspace_end_hash": end_hash,
                "run_status": RunStatus.FAILED if failures else RunStatus.SUCCEEDED,
                "event_summary": summary,
                "event_validation_error": validation_error,
                "failure_reason": "; ".join(failures) if failures else None,
                "timed_out": process_result.timed_out,
                "workspace_end_error": workspace_end_error,
                "final_message_error": final_message_error,
            }
        )
        atomic_write_manifest(manifest_path, final_manifest)
        return final_manifest


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Codex from an audited temporal snapshot")
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--kind", choices=[kind.value for kind in RunKind], required=True)
    parser.add_argument("--intervention-text")
    parser.add_argument("--runs-root", type=Path)
    parser.add_argument("--codex-executable", default="codex")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        specification = RunSpecification(
            run_id=args.run_id,
            scenario_path=args.scenario,
            run_kind=RunKind(args.kind),
            execution_configuration=CodexExecutionConfiguration(),
            intervention_text=args.intervention_text,
        )
        manifest = TemporalRunner(CodexCLIAdapter(args.codex_executable)).run(
            specification, args.runs_root
        )
        print("TEMPORAL RUN\n")
        print(f"Run             {manifest.run_id}")
        print(f"Kind            {manifest.run_kind}")
        print(f"Scenario        {manifest.scenario_id}")
        print(f"Base snapshot   {manifest.base_snapshot_hash}")
        print(f"Workspace hash  {manifest.workspace_start_hash}")
        print(f"Model           {manifest.model}")
        print(f"Reasoning       {manifest.reasoning_effort}")
        print(f"Web search      {manifest.web_search_mode}")
        print(f"Permissions     {manifest.permission_profile}\n")
        print("CODEX RUN COMPLETE\n")
        print(f"Status          {manifest.run_status}")
        print(f"Thread          {manifest.thread_id or 'unavailable'}")
        count = manifest.event_summary.event_count if manifest.event_summary else 0
        print(f"Events          {count}")
        print(f"Exit code       {manifest.exit_code}")
        print(f"End hash        {manifest.workspace_end_hash}")
        return 0 if manifest.run_status is RunStatus.SUCCEEDED else 1
    except (TemporalRunError, RunWorkspaceError, ValidationError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
