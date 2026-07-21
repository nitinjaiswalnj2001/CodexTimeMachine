from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.runs.events import summarize_event_stream
from backend.runs.models import (
    ApprovalPolicy,
    PermissionSystem,
    ReasoningEffort,
    RunKind,
    RunManifest,
    RunStatus,
    ShellEnvironmentPolicy,
    WebSearchMode,
)
from backend.temporal.integrity import canonical_json_bytes, sha256_bytes, sha256_file
from backend.trajectory.classifier import classify_command
from backend.trajectory.extractor import TrajectoryExtractionError, TrajectoryExtractor, main
from backend.trajectory.models import CommandTag, ObservableEventStatus, ObservableEventType
from backend.trajectory.normalizer import (
    TrajectoryNormalizationError,
    _normalize_workspace_references,
    normalize_workspace_path,
)


FIXTURE = Path(__file__).parent / "fixtures/trajectory_run"
FIXED_TIME = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
_DEFAULT_FINAL = object()


def _fixture_events() -> list[dict]:
    return [json.loads(line) for line in (FIXTURE / "raw_codex_events.jsonl").read_text("utf-8").splitlines()]


def _write_events(path: Path, events: list[dict]) -> None:
    path.write_bytes(
        b"".join(canonical_json_bytes(event) + b"\n" for event in events)
    )


def _prepare_run(
    tmp_path: Path,
    *,
    events: list[dict] | None = None,
    final_message: str | None | object = _DEFAULT_FINAL,
    run_status: RunStatus = RunStatus.SUCCEEDED,
    manifest_thread_id: str = "thread-fixture",
) -> Path:
    run = tmp_path / "R-TRAJ"
    workspace = run / "workspace"
    workspace.mkdir(parents=True)
    for relative in ("config/retrieval.yaml", "legalrag/retrieval.py", "tests/test_retrieval.py"):
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture\n", encoding="utf-8")
    raw = run / "raw_codex_events.jsonl"
    _write_events(raw, events or _fixture_events())
    if final_message is _DEFAULT_FINAL:
        final_message = (FIXTURE / "final_message.txt").read_text("utf-8")
    final_hash = None
    if isinstance(final_message, str):
        (run / "final_message.txt").write_text(final_message, encoding="utf-8")
        final_hash = sha256_file(run / "final_message.txt")
    summary = summarize_event_stream(raw, forbidden_item_types=frozenset())
    manifest = RunManifest(
        run_id="R-TRAJ",
        scenario_id="legalrag-reranker-t001",
        run_kind=RunKind.BASELINE,
        base_snapshot_id="snapshot-fixture",
        base_snapshot_hash="1" * 64,
        workspace_start_hash="2" * 64,
        task_hash="3" * 64,
        effective_prompt_hash="4" * 64,
        model="gpt-5.6-sol",
        reasoning_effort=ReasoningEffort.MEDIUM,
        permission_system=PermissionSystem.PERMISSION_PROFILE,
        permission_profile="ctm_temporal",
        permission_profile_hash="5" * 64,
        runtime_read_paths=["/opt/codex-release"],
        runtime_read_paths_hash="6" * 64,
        probe_interpreter="/usr/bin/python3",
        effective_sandbox_path="/usr/bin:/bin",
        network_enabled=False,
        approval_policy=ApprovalPolicy.NEVER,
        web_search_mode=WebSearchMode.DISABLED,
        ephemeral=True,
        ignore_user_config=True,
        ignore_rules=True,
        skip_git_repo_check=True,
        strict_config=True,
        timeout_seconds=1800,
        preflight_timeout_seconds=15,
        shell_environment_policy=ShellEnvironmentPolicy(),
        codex_executable="/opt/codex-release/bin/codex",
        codex_version="codex-cli 0.144.5",
        started_at=FIXED_TIME,
        completed_at=FIXED_TIME,
        exit_code=0,
        thread_id=manifest_thread_id,
        raw_events_sha256=sha256_file(raw),
        final_message_sha256=final_hash,
        workspace_end_hash="7" * 64,
        run_status=run_status,
        event_summary=summary,
        isolation_probe_succeeded=True,
    )
    (run / "run_manifest.json").write_bytes(
        canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"
    )
    return run


def _extract(run: Path, name: str = "trajectory"):
    return TrajectoryExtractor().extract(
        run, run / name, extracted_at=FIXED_TIME
    )


def test_correlates_started_and_completed_items_without_double_counting(tmp_path):
    trajectory = _extract(_prepare_run(tmp_path))
    progress = next(event for event in trajectory.events if event.source_item_id == "msg-progress")
    assert progress.source_event_indexes == [3, 4]
    assert progress.evidence.raw_line_indexes == [3, 4]
    assert len([event for event in trajectory.events if event.source_item_id == "msg-progress"]) == 1


def test_started_only_item_is_incomplete(tmp_path):
    events = _fixture_events()
    events.insert(-1, {"type": "item.started", "item": {"id": "unfinished", "type": "command_execution", "command": "rg TODO"}})
    trajectory = _extract(_prepare_run(tmp_path, events=events))
    event = next(event for event in trajectory.events if event.source_item_id == "unfinished")
    assert event.status is ObservableEventStatus.INCOMPLETE


def test_completed_only_item_emits_warning(tmp_path):
    events = _fixture_events()
    events.insert(-1, {"type": "item.completed", "item": {"id": "orphan", "type": "command_execution", "command": "git status", "exit_code": 0, "status": "completed"}})
    trajectory = _extract(_prepare_run(tmp_path, events=events))
    assert any("orphan completed without" in warning for warning in trajectory.warnings)
    assert any(event.source_item_id == "orphan" for event in trajectory.events)


def test_conflicting_item_types_fail_closed(tmp_path):
    events = _fixture_events()
    events[3]["item"]["type"] = "command_execution"
    with pytest.raises(TrajectoryNormalizationError, match="conflicting item types"):
        _extract(_prepare_run(tmp_path, events=events))


def test_successful_and_failed_commands_are_retained(tmp_path):
    trajectory = _extract(_prepare_run(tmp_path))
    commands = {event.source_item_id: event for event in trajectory.events if event.event_type is ObservableEventType.COMMAND_EXECUTED}
    assert commands["cmd-success"].status is ObservableEventStatus.SUCCEEDED
    assert commands["cmd-failed"].status is ObservableEventStatus.FAILED
    assert commands["cmd-failed"].exit_code == 127
    assert CommandTag.TEST_EXECUTION in commands["cmd-failed"].command_tags


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("pytest -q", {CommandTag.TEST_EXECUTION}),
        ("python -m unittest", {CommandTag.TEST_EXECUTION}),
        ("git status", {CommandTag.GIT_INSPECTION}),
        ("git log -1", {CommandTag.GIT_INSPECTION}),
        ("cat src/app.py", {CommandTag.FILE_INSPECTION}),
        ("sed -n 1,20p app.py", {CommandTag.FILE_INSPECTION}),
        ("rg TODO", {CommandTag.FILE_INSPECTION}),
        ("find . -type f", {CommandTag.REPOSITORY_INSPECTION}),
        ("python -m compileall src", {CommandTag.COMPILATION}),
        ("custom-tool --flag", {CommandTag.OTHER}),
    ],
)
def test_command_tagging_is_conservative(command, expected):
    assert expected.issubset(set(classify_command(command)))


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/bin/bash -c 'pytest -q'", {CommandTag.TEST_EXECUTION}),
        ('bash -c "python -m pytest -q"', {CommandTag.TEST_EXECUTION}),
        ("/bin/sh -c 'python3 -m unittest'", {CommandTag.TEST_EXECUTION}),
        ('/bin/bash -c "python3 -m compileall src"', {CommandTag.COMPILATION}),
        ("bash -c 'git status'", {CommandTag.GIT_INSPECTION}),
        ('bash -c "cat src/app.py"', {CommandTag.FILE_INSPECTION}),
        (
            "python3 -m compileall src && python3 -m unittest",
            {CommandTag.COMPILATION, CommandTag.TEST_EXECUTION},
        ),
    ],
)
def test_shell_wrapped_commands_receive_conservative_inner_tags(command, expected):
    assert expected.issubset(set(classify_command(command)))


@pytest.mark.parametrize("command", ["echo pytest", 'printf "git status"', "somepytesttool"])
def test_command_data_does_not_create_false_positive_tags(command):
    assert classify_command(command) == [CommandTag.OTHER]


def test_module_help_does_not_emit_runpy_runtime_warning():
    result = subprocess.run(
        [sys.executable, "-m", "backend.trajectory.extractor", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "RuntimeWarning" not in result.stderr


def test_multifile_change_expands_in_declared_order_with_shared_batch(tmp_path):
    trajectory = _extract(_prepare_run(tmp_path))
    changes = [event for event in trajectory.events if event.source_item_id == "files-1"]
    assert [event.event_type for event in changes] == [
        ObservableEventType.FILE_UPDATED,
        ObservableEventType.FILE_UPDATED,
        ObservableEventType.FILE_CREATED,
    ]
    assert [event.workspace_relative_paths[0] for event in changes] == [
        "config/retrieval.yaml",
        "legalrag/retrieval.py",
        "tests/test_retrieval.py",
    ]
    assert len({event.metadata["batch_id"] for event in changes}) == 1
    assert all(event.source_event_indexes == [9, 10] for event in changes)


def test_delete_change_maps_to_file_deleted(tmp_path):
    events = _fixture_events()
    events[8]["item"]["changes"] = [{"path": "old.txt", "kind": "delete"}]
    events[9]["item"]["changes"] = [{"path": "old.txt", "kind": "delete"}]
    trajectory = _extract(_prepare_run(tmp_path, events=events))
    event = next(event for event in trajectory.events if event.source_item_id == "files-1")
    assert event.event_type is ObservableEventType.FILE_DELETED


def test_unknown_file_change_kind_fails_closed(tmp_path):
    events = _fixture_events()
    events[8]["item"]["changes"][0]["kind"] = "rename"
    events[9]["item"]["changes"][0]["kind"] = "rename"
    with pytest.raises(TrajectoryNormalizationError, match="unknown file change kind"):
        _extract(_prepare_run(tmp_path, events=events))


@pytest.mark.parametrize(
    "raw_path",
    [
        "../future.txt",
        "foo/../../future.txt",
        "/etc/passwd",
        "C:\\outside\\future.txt",
        "/tmp/.ctm_runs/OTHER/workspace/future.txt",
    ],
)
def test_escaping_or_external_workspace_paths_are_rejected(tmp_path, raw_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(TrajectoryNormalizationError):
        normalize_workspace_path(raw_path, workspace, "R-TRAJ")


@pytest.mark.parametrize(
    ("raw_path", "expected"),
    [
        ("src/app.py", "src/app.py"),
        ("/home/user/project/.ctm_runs/R-TRAJ/workspace/src/app.py", "src/app.py"),
        ("C:\\project\\.ctm_runs\\R-TRAJ\\workspace\\src\\app.py", "src/app.py"),
    ],
)
def test_posix_and_windows_workspace_paths_normalize_safely(tmp_path, raw_path, expected):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert normalize_workspace_path(raw_path, workspace, "R-TRAJ") == expected


def test_symlink_escape_is_rejected_when_supported(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    try:
        (workspace / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(TrajectoryNormalizationError, match="resolves outside"):
        normalize_workspace_path("link/file.txt", workspace, "R-TRAJ")


def test_markdown_contains_only_normalized_workspace_paths_and_disclaimer(tmp_path):
    run = _prepare_run(tmp_path)
    _extract(run)
    markdown = (run / "trajectory/trajectory.md").read_text("utf-8")
    assert "/home/example" not in markdown
    assert "config/retrieval.yaml" in markdown
    assert "does not reconstruct hidden chain-of-thought" in markdown


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "/home/example/project/.ctm_runs/R-TRAJ/workspace",
            ".",
        ),
        (
            "/home/example/project/.ctm_runs/R-TRAJ/workspace/legalrag/retrieval.py",
            "./legalrag/retrieval.py",
        ),
        (
            "/home/example/project/.ctm_runs/R-TRAJ",
            "<RUN_DIR>",
        ),
        (
            "/home/example/project/.ctm_runs/R-TRAJ/isolation_probe.json",
            "<RUN_DIR>/isolation_probe.json",
        ),
        (
            r"C:\Users\Example\project\.ctm_runs\R-TRAJ\workspace\src\app.py",
            "./src/app.py",
        ),
        (
            r"D:\projects\project\.ctm_runs\R-TRAJ\codex_stderr.log",
            "<RUN_DIR>/codex_stderr.log",
        ),
    ],
)
def test_visible_workspace_and_run_directory_paths_are_normalized(tmp_path, value, expected):
    workspace = tmp_path / ".ctm_runs/R-TRAJ/workspace"
    workspace.mkdir(parents=True)
    assert _normalize_workspace_references(value, workspace, "R-TRAJ") == expected


def test_git_error_run_directory_is_normalized_without_machine_prefix(tmp_path):
    workspace = tmp_path / ".ctm_runs/R-SMOKE-WSL-001/workspace"
    workspace.mkdir(parents=True)
    raw = (
        "fatal: not a git repository (or any parent up to mount point "
        "/home/example/project/.ctm_runs/R-SMOKE-WSL-001)"
    )
    normalized = _normalize_workspace_references(raw, workspace, "R-SMOKE-WSL-001")
    assert "/home/example" not in normalized
    assert normalized.endswith("<RUN_DIR>)")


def test_visible_path_normalization_preserves_raw_output_hash_and_evidence(tmp_path):
    raw_output = (
        "fatal: not a git repository (or any parent up to mount point "
        "/home/example/project/.ctm_runs/R-TRAJ)\n"
    )
    events = _fixture_events()
    events[7]["item"]["aggregated_output"] = raw_output
    run = _prepare_run(tmp_path, events=events)
    trajectory = _extract(run)
    command = next(event for event in trajectory.events if event.source_item_id == "cmd-failed")
    assert command.output_sha256 == sha256_bytes(raw_output.encode("utf-8"))
    assert command.evidence.source_fragments_sha256 == sha256_bytes(
        canonical_json_bytes([events[6], events[7]])
    )
    assert "/home/example" not in (command.output_preview or "")
    assert "<RUN_DIR>" in (command.output_preview or "")
    assert command.redactions_applied is False
    assert (run / "raw_codex_events.jsonl").read_bytes().find(
        b"/home/example/project/.ctm_runs/R-TRAJ"
    ) >= 0
    json_output = (run / "trajectory/trajectory.json").read_text("utf-8")
    markdown_output = (run / "trajectory/trajectory.md").read_text("utf-8")
    assert "/home/example" not in json_output
    assert "/home/example" not in markdown_output
    assert "<RUN_DIR>" in json_output
    assert "<RUN_DIR>" in markdown_output


def test_visible_path_normalization_changes_trajectory_identity(tmp_path):
    first = _extract(_prepare_run(tmp_path / "first"))
    events = _fixture_events()
    events[7]["item"]["aggregated_output"] = (
        "fatal: not a git repository (or any parent up to mount point "
        "/home/example/project/.ctm_runs/R-TRAJ)\n"
    )
    second = _extract(_prepare_run(tmp_path / "second", events=events))
    assert first.trajectory_hash != second.trajectory_hash


def test_every_event_has_compact_raw_evidence(tmp_path):
    trajectory = _extract(_prepare_run(tmp_path))
    assert all(event.evidence.raw_line_indexes for event in trajectory.events)
    assert all(len(event.evidence.source_fragments_sha256) == 64 for event in trajectory.events)


def test_evidence_hash_changes_with_source_fragment(tmp_path):
    first_run = _prepare_run(tmp_path / "first")
    first = _extract(first_run)
    events = _fixture_events()
    events[5]["item"]["aggregated_output"] = "changed output\n"
    second_run = _prepare_run(tmp_path / "second", events=events)
    second = _extract(second_run)
    first_hash = next(event for event in first.events if event.source_item_id == "cmd-success").evidence.source_fragments_sha256
    second_hash = next(event for event in second.events if event.source_item_id == "cmd-success").evidence.source_fragments_sha256
    assert first_hash != second_hash


def test_raw_hash_mismatch_fails_closed(tmp_path):
    run = _prepare_run(tmp_path)
    with (run / "raw_codex_events.jsonl").open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(TrajectoryExtractionError, match="SHA-256"):
        _extract(run)


def test_manifest_thread_mismatch_fails_closed(tmp_path):
    with pytest.raises(TrajectoryExtractionError, match="thread ID"):
        _extract(_prepare_run(tmp_path, manifest_thread_id="different-thread"))


def test_multiple_thread_started_events_fail_closed(tmp_path):
    events = _fixture_events()
    events.insert(1, {"type": "thread.started", "thread_id": "thread-two"})
    with pytest.raises(TrajectoryExtractionError, match="exactly one"):
        _extract(_prepare_run(tmp_path, events=events))


def test_failed_run_cannot_produce_accepted_trajectory(tmp_path):
    with pytest.raises(TrajectoryExtractionError, match="SUCCEEDED"):
        _extract(_prepare_run(tmp_path, run_status=RunStatus.FAILED))


def test_final_agent_message_must_match_final_message_file(tmp_path):
    with pytest.raises(TrajectoryNormalizationError, match="does not match"):
        _extract(_prepare_run(tmp_path, final_message="different\n"))


def test_deterministic_input_produces_deterministic_json_hash_and_event_ids(tmp_path):
    run = _prepare_run(tmp_path)
    first = _extract(run, "first")
    second = _extract(run, "second")
    assert (run / "first/trajectory.json").read_bytes() == (run / "second/trajectory.json").read_bytes()
    assert first.trajectory_hash == second.trajectory_hash
    assert [event.event_id for event in first.events] == [event.event_id for event in second.events]


def test_extraction_timestamp_is_excluded_from_trajectory_identity(tmp_path):
    run = _prepare_run(tmp_path)
    first = TrajectoryExtractor().extract(run, run / "first", extracted_at=FIXED_TIME)
    later = TrajectoryExtractor().extract(
        run,
        run / "later",
        extracted_at=datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
    )
    assert first.extracted_at != later.extracted_at
    assert first.trajectory_hash == later.trajectory_hash


def test_trajectory_manifest_hashes_published_outputs(tmp_path):
    run = _prepare_run(tmp_path)
    trajectory = _extract(run)
    output = run / "trajectory"
    manifest = json.loads((output / "trajectory_manifest.json").read_text("utf-8"))
    assert manifest["trajectory_json_sha256"] == sha256_file(output / "trajectory.json")
    assert manifest["trajectory_markdown_sha256"] == sha256_file(output / "trajectory.md")
    assert manifest["trajectory_hash"] == trajectory.trajectory_hash
    assert manifest["event_count"] == trajectory.event_count


def test_existing_output_is_not_silently_overwritten(tmp_path):
    run = _prepare_run(tmp_path)
    _extract(run)
    before = (run / "trajectory/trajectory.json").read_bytes()
    with pytest.raises(TrajectoryExtractionError, match="--overwrite"):
        _extract(run)
    assert (run / "trajectory/trajectory.json").read_bytes() == before


def test_secret_like_values_are_redacted_but_raw_evidence_is_unchanged(tmp_path):
    events = _fixture_events()
    secret = "sk-abcdefghijklmnop"
    for index in (2, 3):
        events[index]["item"]["text"] = f"Authorization: Bearer {secret}"
    run = _prepare_run(tmp_path, events=events)
    raw_before = (run / "raw_codex_events.jsonl").read_bytes()
    _extract(run)
    normalized = (run / "trajectory/trajectory.json").read_text("utf-8")
    assert secret not in normalized
    assert "[REDACTED]" in normalized
    assert (run / "raw_codex_events.jsonl").read_bytes() == raw_before


def test_cli_prints_compact_summary(tmp_path, capsys):
    run = _prepare_run(tmp_path)
    assert main([str(run), "--fixed-extracted-at", FIXED_TIME.isoformat()]) == 0
    output = capsys.readouterr().out
    assert "TRAJECTORY EXTRACTION" in output
    assert "R-TRAJ" in output
    assert "Trajectory hash" in output


def test_output_inside_workspace_is_rejected(tmp_path):
    run = _prepare_run(tmp_path)
    with pytest.raises(TrajectoryExtractionError, match="outside"):
        TrajectoryExtractor().extract(run, run / "workspace/trajectory")


@pytest.mark.skipif(
    not os.environ.get("CTM_REAL_EVIDENCE_DIR"),
    reason="CTM_REAL_EVIDENCE_DIR was not supplied",
)
def test_optional_real_evidence_bundle_extracts(tmp_path):
    source = Path(os.environ["CTM_REAL_EVIDENCE_DIR"]).resolve()
    run = tmp_path / source.name
    shutil.copytree(source, run)
    trajectory = TrajectoryExtractor().extract(run, extracted_at=FIXED_TIME)
    assert trajectory.event_count > 0
    assert trajectory.thread_id
