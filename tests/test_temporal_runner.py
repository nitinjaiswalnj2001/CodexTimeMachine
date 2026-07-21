from __future__ import annotations

import json
from pathlib import Path

import pytest

import backend.runs.runner as runner_module
from backend.runs.models import (
    CodexExecutionConfiguration,
    RunKind,
    RunSpecification,
    RunStatus,
)
from backend.runs.runner import TemporalRunError, TemporalRunner, hash_text, load_scenario
from backend.runs.workspace import RunWorkspaceError, tree_entries
from backend.temporal.integrity import sha256_file


def specification(scenario: Path, run_id: str, kind: RunKind = RunKind.BASELINE, text=None):
    return RunSpecification(
        run_id=run_id,
        scenario_path=scenario,
        run_kind=kind,
        intervention_text=text,
    )


def runs_root_for(tmp_path: Path) -> Path:
    return tmp_path.parent / f"{tmp_path.name}-ctm-runs"


def test_successful_fake_run_preserves_raw_events_and_records_evidence(
    sealed_scenario, memory_codex_factory, tmp_path
):
    sealed = sealed_scenario.parent / "sealed"
    sealed_before = tree_entries(sealed)
    runs_root = runs_root_for(tmp_path)
    manifest = TemporalRunner(
        memory_codex_factory(THREAD_ID="thread-success")
    ).run(specification(sealed_scenario, "R-001"), runs_root)
    run_directory = runs_root / "R-001"
    raw_path = run_directory / "raw_codex_events.jsonl"
    expected_raw = (
        b'{"type":"thread.started","thread_id":"thread-success"}\n'
        b'{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
        b'{"type":"future.unknown","payload":1}\n'
    )
    assert raw_path.read_bytes() == expected_raw
    assert (run_directory / "codex_stderr.log").read_bytes() == b"memory stderr: success\n"
    assert manifest.run_status is RunStatus.SUCCEEDED
    assert manifest.exit_code == 0
    assert manifest.thread_id == "thread-success"
    assert manifest.event_summary.event_count == 3
    assert manifest.event_summary.thread_started_count == 1
    assert manifest.event_summary.event_types["future.unknown"] == 1
    assert manifest.event_summary.item_types == {"agent_message": 1}
    assert manifest.event_summary.forbidden_item_types == []
    assert not manifest.event_summary.has_error_event
    assert manifest.raw_events_sha256 == sha256_file(raw_path)
    assert manifest.final_message_sha256 == sha256_file(run_directory / "final_message.txt")
    assert manifest.workspace_end_hash != manifest.workspace_start_hash
    assert tree_entries(sealed) == sealed_before


def test_nonzero_run_is_failed_and_preserves_events_stderr_and_error_summary(
    sealed_scenario, memory_codex_factory, tmp_path
):
    runs_root = runs_root_for(tmp_path)
    manifest = TemporalRunner(
        memory_codex_factory(THREAD_ID="thread-failed", MODE="failure")
    ).run(specification(sealed_scenario, "R-FAIL"), runs_root)
    run_directory = runs_root / "R-FAIL"
    assert manifest.run_status is RunStatus.FAILED
    assert manifest.exit_code == 7
    assert manifest.event_summary.has_error_event
    assert manifest.event_summary.failure_event_types == {"error": 1}
    assert manifest.event_summary.event_types == {"error": 1, "thread.started": 1}
    assert (run_directory / "raw_codex_events.jsonl").is_file()
    assert (run_directory / "codex_stderr.log").read_bytes() == b"memory stderr: failure\n"


def test_invalid_jsonl_marks_run_failed_without_rewriting_evidence(
    sealed_scenario, memory_codex_factory, tmp_path
):
    runs_root = runs_root_for(tmp_path)
    manifest = TemporalRunner(
        memory_codex_factory(THREAD_ID="thread-invalid", MODE="invalid")
    ).run(specification(sealed_scenario, "R-INVALID"), runs_root)
    raw_path = runs_root / "R-INVALID/raw_codex_events.jsonl"
    assert manifest.run_status is RunStatus.FAILED
    assert manifest.exit_code == 0
    assert manifest.event_summary is None
    assert "invalid JSONL" in manifest.event_validation_error
    assert raw_path.read_bytes().endswith(b"not-json\n")
    assert manifest.raw_events_sha256 == sha256_file(raw_path)


def test_two_independent_runs_share_base_hash_but_capture_distinct_threads(
    sealed_scenario, memory_codex_factory, tmp_path
):
    runs_root = runs_root_for(tmp_path)
    first = TemporalRunner(memory_codex_factory(THREAD_ID="thread-one")).run(
        specification(sealed_scenario, "R-001"), runs_root
    )
    second = TemporalRunner(memory_codex_factory(THREAD_ID="thread-two")).run(
        specification(sealed_scenario, "R-002"), runs_root
    )
    assert first.base_snapshot_hash == second.base_snapshot_hash
    assert first.workspace_start_hash == second.workspace_start_hash
    assert first.thread_id == "thread-one"
    assert second.thread_id == "thread-two"


def test_baseline_prompt_is_exact_scenario_task(sealed_scenario, memory_codex_factory, tmp_path):
    runs_root = runs_root_for(tmp_path)
    manifest = TemporalRunner(memory_codex_factory()).run(
        specification(sealed_scenario, "R-BASE"), runs_root
    )
    task = load_scenario(sealed_scenario).task
    received = (runs_root / "R-BASE/workspace/received_prompt.txt").read_text("utf-8")
    assert received == task
    assert manifest.task_hash == hash_text(task)
    assert manifest.effective_prompt_hash == hash_text(task)
    assert manifest.intervention_hash is None


def test_replay_prompt_uses_neutral_guidance_and_hashes_intervention(
    sealed_scenario, memory_codex_factory, tmp_path
):
    runs_root = runs_root_for(tmp_path)
    intervention = "Inspect the ranking configuration before recommending changes."
    manifest = TemporalRunner(memory_codex_factory()).run(
        specification(sealed_scenario, "R-REPLAY", RunKind.REPLAY, intervention),
        runs_root,
    )
    task = load_scenario(sealed_scenario).task
    expected = f"{task}\n\nADDITIONAL EVALUATION GUIDANCE\n\n{intervention}"
    received = (runs_root / "R-REPLAY/workspace/received_prompt.txt").read_text("utf-8")
    assert received == expected
    assert manifest.intervention_hash == hash_text(intervention)
    assert manifest.effective_prompt_hash == hash_text(expected)
    assert "Ghost" not in received
    assert "future clue" not in received


def test_run_manifest_records_fixed_isolation_and_uses_atomic_valid_json(
    sealed_scenario, memory_codex_factory, tmp_path
):
    runs_root = runs_root_for(tmp_path)
    manifest = TemporalRunner(memory_codex_factory()).run(
        specification(sealed_scenario, "R-MANIFEST"), runs_root
    )
    run_directory = runs_root / "R-MANIFEST"
    persisted = json.loads((run_directory / "run_manifest.json").read_text("utf-8"))
    assert persisted["run_status"] == "SUCCEEDED"
    assert persisted["model"] == "gpt-5.6-sol"
    assert persisted["reasoning_effort"] == "medium"
    assert persisted["permission_system"] == "permission_profile"
    assert persisted["permission_profile"] == "ctm_temporal"
    assert persisted["network_enabled"] is False
    assert persisted["isolation_probe_succeeded"] is True
    assert persisted["isolation_probe_result_sha256"]
    assert persisted["isolation_probe_command_sha256"]
    assert persisted["isolation_probe_stdout_sha256"]
    assert persisted["isolation_probe_stderr_sha256"]
    assert persisted["approval_policy"] == "never"
    assert persisted["web_search_mode"] == "disabled"
    assert persisted["ephemeral"] is True
    assert persisted["ignore_user_config"] is True
    assert persisted["ignore_rules"] is True
    assert persisted["skip_git_repo_check"] is True
    assert persisted["strict_config"] is True
    assert persisted["timeout_seconds"] == 1800
    assert persisted["base_snapshot_hash"] == manifest.base_snapshot_hash
    assert not list(run_directory.glob(".run_manifest.json.tmp-*"))


def test_future_outcome_and_control_plane_metadata_never_enter_run_workspace(
    sealed_scenario, memory_codex_factory, tmp_path
):
    runs_root = runs_root_for(tmp_path)
    TemporalRunner(memory_codex_factory()).run(
        specification(sealed_scenario, "R-CLEAN"), runs_root
    )
    workspace = runs_root / "R-CLEAN/workspace"
    names = {path.name for path in workspace.rglob("*")}
    assert "future_outcome.yaml" not in names
    assert "boundary_control.json" not in names
    assert "boundary_report.json" not in names
    assert "manifest.json" not in names
    assert all(
        b"TM_FUTURE_CANARY_9F41B7" not in path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file()
    )


def test_preflight_failure_records_failed_manifest_and_never_executes(
    sealed_scenario, memory_codex_factory, tmp_path
):
    runs_root = runs_root_for(tmp_path)
    runner = TemporalRunner(memory_codex_factory(PREFLIGHT_ERROR="missing --ignore-rules"))
    with pytest.raises(TemporalRunError, match="--ignore-rules"):
        runner.run(specification(sealed_scenario, "R-PREFLIGHT"), runs_root)
    run_directory = runs_root / "R-PREFLIGHT"
    persisted = json.loads((run_directory / "run_manifest.json").read_text("utf-8"))
    assert persisted["run_status"] == "FAILED"
    assert not (run_directory / "raw_codex_events.jsonl").exists()
    assert not (run_directory / "codex_stderr.log").exists()


def test_preflight_timeout_is_terminal_and_starts_neither_probe_nor_exec(
    sealed_scenario, memory_codex_factory, tmp_path
):
    adapter = memory_codex_factory(PREFLIGHT_ERROR="Codex preflight version timed out")
    runs_root = runs_root_for(tmp_path)
    with pytest.raises(TemporalRunError, match="timed out"):
        TemporalRunner(adapter).run(
            specification(sealed_scenario, "R-PREFLIGHT-TIMEOUT"), runs_root
        )
    persisted = json.loads(
        (runs_root / "R-PREFLIGHT-TIMEOUT/run_manifest.json").read_text("utf-8")
    )
    assert persisted["run_status"] == "FAILED"
    assert persisted["completed_at"] is not None
    assert adapter.probe_called is False
    assert adapter.execute_called is False


def test_preflight_failure_remains_terminal_when_workspace_end_hash_fails(
    sealed_scenario, memory_codex_factory, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        runner_module,
        "compute_workspace_tree_hash",
        lambda _workspace: (_ for _ in ()).throw(OSError("cannot inspect")),
    )
    adapter = memory_codex_factory(PREFLIGHT_ERROR="unsupported capability")
    runs_root = runs_root_for(tmp_path)
    with pytest.raises(TemporalRunError, match="unsupported capability"):
        TemporalRunner(adapter).run(
            specification(sealed_scenario, "R-PREFLIGHT-HASH-FAIL"), runs_root
        )
    persisted = json.loads(
        (runs_root / "R-PREFLIGHT-HASH-FAIL/run_manifest.json").read_text("utf-8")
    )
    assert persisted["run_status"] == "FAILED"
    assert persisted["workspace_end_hash"] is None
    assert "cannot inspect" in persisted["workspace_end_error"]


@pytest.mark.parametrize(
    ("mode", "failure_type"),
    [
        ("turn_failed", "turn.failed"),
        ("item_failed", "item.failed"),
        ("error_zero", "error"),
    ],
)
def test_explicit_failure_events_fail_even_with_zero_exit(
    sealed_scenario, memory_codex_factory, tmp_path, mode, failure_type
):
    manifest = TemporalRunner(memory_codex_factory(MODE=mode)).run(
        specification(sealed_scenario, f"R-{mode}"), runs_root_for(tmp_path)
    )
    assert manifest.exit_code == 0
    assert manifest.run_status is RunStatus.FAILED
    assert manifest.event_summary.failure_event_types == {failure_type: 1}
    assert failure_type in manifest.failure_reason


@pytest.mark.parametrize(
    ("mode", "expected_count", "reason"),
    [
        ("no_thread", 0, "expected exactly one"),
        ("missing_thread_id", 1, "thread ID is missing"),
        ("multiple_threads", 2, "expected exactly one"),
    ],
)
def test_fresh_thread_evidence_is_required(
    sealed_scenario, memory_codex_factory, tmp_path, mode, expected_count, reason
):
    manifest = TemporalRunner(memory_codex_factory(MODE=mode)).run(
        specification(sealed_scenario, f"R-{mode}"), runs_root_for(tmp_path)
    )
    assert manifest.run_status is RunStatus.FAILED
    assert manifest.event_summary.thread_started_count == expected_count
    assert reason in manifest.failure_reason


@pytest.mark.parametrize(
    ("mode", "item_type"),
    [("web_search", "web_search"), ("mcp", "mcp_tool_call")],
)
def test_forbidden_external_context_items_fail_the_run(
    sealed_scenario, memory_codex_factory, tmp_path, mode, item_type
):
    manifest = TemporalRunner(memory_codex_factory(MODE=mode)).run(
        specification(sealed_scenario, f"R-{mode}"), runs_root_for(tmp_path)
    )
    assert manifest.run_status is RunStatus.FAILED
    assert manifest.event_summary.item_types == {item_type: 1}
    assert manifest.event_summary.forbidden_item_types == [item_type]
    assert item_type in manifest.failure_reason


def test_unknown_unrelated_item_type_remains_accepted(
    sealed_scenario, memory_codex_factory, tmp_path
):
    manifest = TemporalRunner(memory_codex_factory(MODE="unknown_item")).run(
        specification(sealed_scenario, "R-UNKNOWN-ITEM"), runs_root_for(tmp_path)
    )
    assert manifest.run_status is RunStatus.SUCCEEDED
    assert manifest.event_summary.item_types == {"novel_local_item": 1}
    assert manifest.event_summary.forbidden_item_types == []


def test_timeout_preserves_partial_evidence_and_writes_terminal_manifest(
    sealed_scenario, memory_codex_factory, tmp_path
):
    spec = RunSpecification(
        run_id="R-TIMEOUT",
        scenario_path=sealed_scenario,
        run_kind=RunKind.BASELINE,
        execution_configuration=CodexExecutionConfiguration(timeout_seconds=1),
    )
    runs_root = runs_root_for(tmp_path)
    manifest = TemporalRunner(memory_codex_factory(MODE="timeout")).run(spec, runs_root)
    run_directory = runs_root / "R-TIMEOUT"
    assert manifest.run_status is RunStatus.FAILED
    assert manifest.timed_out is True
    assert manifest.completed_at is not None
    assert "timed out" in manifest.failure_reason
    assert (run_directory / "raw_codex_events.jsonl").is_file()
    assert (run_directory / "codex_stderr.log").is_file()
    persisted = json.loads((run_directory / "run_manifest.json").read_text("utf-8"))
    assert persisted["run_status"] == "FAILED"
    assert persisted["completed_at"] is not None


def test_post_run_symlink_inspection_failure_is_terminal_and_preserves_evidence(
    sealed_scenario, memory_codex_factory, tmp_path, monkeypatch
):
    def reject_symlink(_workspace):
        raise RunWorkspaceError("symbolic links are forbidden: post-run-link")

    monkeypatch.setattr(runner_module, "compute_workspace_tree_hash", reject_symlink)
    runs_root = runs_root_for(tmp_path)
    manifest = TemporalRunner(memory_codex_factory(MODE="symlink")).run(
        specification(sealed_scenario, "R-SYMLINK"), runs_root
    )
    assert manifest.run_status is RunStatus.FAILED
    assert manifest.completed_at is not None
    assert manifest.workspace_end_hash is None
    assert "symbolic links are forbidden" in manifest.workspace_end_error
    assert (runs_root / "R-SYMLINK/raw_codex_events.jsonl").is_file()
    assert (runs_root / "R-SYMLINK/codex_stderr.log").is_file()
    assert (runs_root / "R-SYMLINK/final_message.txt").is_file()


def test_unhashable_workspace_state_writes_terminal_failed_manifest(
    sealed_scenario, memory_codex_factory, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        runner_module,
        "compute_workspace_tree_hash",
        lambda _workspace: (_ for _ in ()).throw(OSError("unreadable workspace file")),
    )
    runs_root = runs_root_for(tmp_path)
    manifest = TemporalRunner(memory_codex_factory()).run(
        specification(sealed_scenario, "R-UNREADABLE"), runs_root
    )
    assert manifest.run_status is RunStatus.FAILED
    assert manifest.completed_at is not None
    assert manifest.workspace_end_hash is None
    assert "unreadable workspace file" in manifest.workspace_end_error


def test_final_message_hash_failure_still_writes_terminal_manifest(
    sealed_scenario, memory_codex_factory, tmp_path, monkeypatch
):
    real_hash = runner_module.sha256_file

    def fail_final(path):
        if Path(path).name == "final_message.txt":
            raise OSError("unreadable final message")
        return real_hash(path)

    monkeypatch.setattr(runner_module, "sha256_file", fail_final)
    manifest = TemporalRunner(memory_codex_factory()).run(
        specification(sealed_scenario, "R-FINAL-HASH"), runs_root_for(tmp_path)
    )
    assert manifest.run_status is RunStatus.FAILED
    assert manifest.completed_at is not None
    assert manifest.final_message_sha256 is None
    assert "unreadable final message" in manifest.final_message_error


def test_complete_subprocess_fake_cli_end_to_end(
    sealed_scenario, fake_codex_factory, tmp_path
):
    manifest = TemporalRunner(
        fake_codex_factory(FAKE_THREAD_ID="thread-subprocess-e2e")
    ).run(
        specification(sealed_scenario, "R-SUBPROCESS-E2E"), runs_root_for(tmp_path)
    )
    assert manifest.run_status is RunStatus.SUCCEEDED
    assert manifest.thread_id == "thread-subprocess-e2e"
    assert manifest.isolation_probe_succeeded is True
