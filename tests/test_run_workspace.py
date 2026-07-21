from __future__ import annotations

from pathlib import Path

import pytest

from backend.runs.workspace import (
    RunWorkspaceBuilder,
    RunWorkspaceError,
    compute_workspace_tree_hash,
    tree_entries,
)
from backend.temporal.audit import BoundaryAuditError
from backend.temporal.snapshot import TemporalSnapshotBuilder


CANARY = "TM_FUTURE_CANARY_9F41B7"


def test_base_is_audited_before_run_directory_creation(sealed_scenario, tmp_path):
    sealed = sealed_scenario.parent / "sealed"
    (sealed / "repo/src/example.py").write_text("tampered", encoding="utf-8")
    run_directory = tmp_path / "runs/R-001"
    with pytest.raises(BoundaryAuditError, match="hash mismatch"):
        RunWorkspaceBuilder(CANARY).prepare(sealed, run_directory)
    assert not run_directory.exists()


def test_fresh_workspace_contains_only_materialized_project_evidence(sealed_scenario, tmp_path):
    sealed = sealed_scenario.parent / "sealed"
    prepared = RunWorkspaceBuilder(CANARY).prepare(sealed, tmp_path / "runs/R-001")
    files = [entry["logical_path"] for entry in tree_entries(prepared.workspace)]
    assert files == ["src/example.py"]
    assert not (prepared.workspace / "manifest.json").exists()
    assert not (prepared.workspace / "boundary_control.json").exists()
    assert not (prepared.workspace / "boundary_report.json").exists()
    assert not any(path.name.casefold() == ".git" for path in prepared.workspace.rglob("*"))
    assert all(CANARY.encode() not in path.read_bytes() for path in prepared.workspace.rglob("*") if path.is_file())


def test_repeated_fresh_workspaces_have_identical_start_hashes(sealed_scenario, tmp_path):
    sealed = sealed_scenario.parent / "sealed"
    builder = RunWorkspaceBuilder(CANARY)
    first = builder.prepare(sealed, tmp_path / "runs/R-001")
    second = builder.prepare(sealed, tmp_path / "runs/R-002")
    assert first.workspace_start_hash == second.workspace_start_hash


def test_initial_file_tampering_changes_tree_hash_and_fails_verification(sealed_scenario, tmp_path):
    sealed = sealed_scenario.parent / "sealed"
    builder = RunWorkspaceBuilder(CANARY)
    prepared = builder.prepare(sealed, tmp_path / "runs/R-001")
    before = prepared.workspace_start_hash
    (prepared.workspace / "src/example.py").write_text("changed", encoding="utf-8")
    assert compute_workspace_tree_hash(prepared.workspace) != before
    with pytest.raises(RunWorkspaceError, match="hashes differ"):
        builder.verify_starting_workspace(prepared.workspace, prepared.snapshot_manifest)


def test_unmanifested_starting_file_fails_verification(sealed_scenario, tmp_path):
    sealed = sealed_scenario.parent / "sealed"
    builder = RunWorkspaceBuilder(CANARY)
    prepared = builder.prepare(sealed, tmp_path / "runs/R-001")
    (prepared.workspace / "extra.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(RunWorkspaceError, match="extra"):
        builder.verify_starting_workspace(prepared.workspace, prepared.snapshot_manifest)


def test_run_directory_inside_sealed_snapshot_is_rejected(sealed_scenario):
    sealed = sealed_scenario.parent / "sealed"
    with pytest.raises(RunWorkspaceError, match="outside sealed snapshot"):
        RunWorkspaceBuilder(CANARY).prepare(sealed, sealed / "runs/R-001")


def test_sealed_repo_itself_cannot_be_a_run_workspace(sealed_scenario):
    sealed = sealed_scenario.parent / "sealed"
    with pytest.raises(RunWorkspaceError, match="cannot be used as a run workspace"):
        RunWorkspaceBuilder(CANARY).prepare(sealed, sealed / "repo")


def test_workspace_symlink_presence_is_rejected(sealed_scenario, tmp_path, monkeypatch):
    sealed = sealed_scenario.parent / "sealed"
    prepared = RunWorkspaceBuilder(CANARY).prepare(sealed, tmp_path / "runs/R-001")
    original = Path.is_symlink

    def pretend_symlink(path: Path) -> bool:
        if path.name == "example.py":
            return True
        return original(path)

    monkeypatch.setattr(Path, "is_symlink", pretend_symlink)
    with pytest.raises(RunWorkspaceError, match="symbolic links"):
        tree_entries(prepared.workspace)


def test_sealed_snapshot_remains_byte_identical_after_workspace_mutation(sealed_scenario, tmp_path):
    sealed = sealed_scenario.parent / "sealed"
    before = tree_entries(sealed)
    prepared = RunWorkspaceBuilder(CANARY).prepare(sealed, tmp_path / "runs/R-001")
    (prepared.workspace / "src/example.py").write_text("run mutation", encoding="utf-8")
    (prepared.workspace / "generated.log").write_text("run state", encoding="utf-8")
    assert tree_entries(sealed) == before


def test_controlled_fixture_future_outcome_never_enters_workspace(tmp_path):
    scenario = Path("backend/scenarios/legalrag_reranker_t001/scenario.yaml")
    sealed = tmp_path / "controlled-sealed"
    TemporalSnapshotBuilder().build(scenario, sealed)
    prepared = RunWorkspaceBuilder(CANARY).prepare(sealed, tmp_path / "runs/R-CONTROLLED")
    files = [entry["logical_path"] for entry in tree_entries(prepared.workspace)]
    assert "evaluation/future_outcome.yaml" not in files
    assert "evidence/reranker_experiment.json" not in files
    assert all(
        CANARY.encode() not in path.read_bytes()
        for path in prepared.workspace.rglob("*")
        if path.is_file()
    )


@pytest.mark.parametrize("logical_path", [".codex/config.toml", "nested/.CoDeX/hooks/config.json"])
def test_project_local_codex_control_paths_are_rejected(
    manifest_factory, asset_factory, tmp_path, logical_path
):
    scenario = manifest_factory([asset_factory(logical_path=logical_path)], output_name="sealed")
    sealed = tmp_path / "sealed"
    TemporalSnapshotBuilder().build(scenario, sealed)
    run_directory = tmp_path / "runs/R-CODEX-CONTROL"
    with pytest.raises(RunWorkspaceError, match=r"\.codex is forbidden"):
        RunWorkspaceBuilder(CANARY).prepare(sealed, run_directory)
    assert not run_directory.exists()
