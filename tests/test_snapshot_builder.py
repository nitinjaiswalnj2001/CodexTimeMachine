from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.temporal.audit import BoundaryAuditor
from backend.temporal.models import BoundaryReport
from backend.temporal.snapshot import SnapshotBuildError, TemporalSnapshotBuilder


def build(scenario: Path, output: Path | None = None):
    destination = output or scenario.parent / "sealed"
    manifest = TemporalSnapshotBuilder().build(scenario, destination)
    return manifest, destination


def test_only_available_past_asset_is_materialized_and_all_others_are_absent(
    manifest_factory, asset_factory
):
    assets = [
        asset_factory(asset_id="past", logical_path="src/past.py"),
        asset_factory(asset_id="future", logical_path="future.txt", status="LOCKED_FUTURE"),
        asset_factory(asset_id="ghost", logical_path="ghost.txt", visibility="GHOST_ONLY"),
        asset_factory(asset_id="evaluator", logical_path="eval.txt", visibility="EVALUATOR_ONLY"),
        asset_factory(asset_id="excluded", logical_path="excluded.txt", status="EXCLUDED"),
    ]
    scenario = manifest_factory(assets)
    _, output = build(scenario)

    assert (output / "repo/src/past.py").read_text("utf-8") == "past evidence\n"
    for name in ("future.txt", "ghost.txt", "eval.txt", "excluded.txt"):
        assert not (output / "repo" / name).exists()
    report = BoundaryReport.model_validate_json((output / "boundary_report.json").read_text("utf-8"))
    assert report.summary.model_dump() == {
        "total": 5,
        "materialized": 1,
        "locked_future": 1,
        "excluded": 1,
        "not_visible_to_past": 2,
    }


@pytest.mark.parametrize("logical_path", [".git/config", "src/.git/objects/x", "SRC/.GIT/HEAD"])
def test_git_logical_paths_are_rejected(manifest_factory, asset_factory, logical_path):
    scenario = manifest_factory([asset_factory(logical_path=logical_path)])
    with pytest.raises(SnapshotBuildError, match=r"\.git"):
        build(scenario)


def test_git_source_paths_are_rejected(manifest_factory, asset_factory, tmp_path):
    git_file = tmp_path / ".git" / "config"
    git_file.parent.mkdir()
    git_file.write_text("secret", encoding="utf-8")
    scenario = manifest_factory([asset_factory(source_path=".git/config")])
    with pytest.raises(SnapshotBuildError, match=r"\.git"):
        build(scenario)


@pytest.mark.parametrize("logical_path", ["../future.txt", "foo/../../future.txt", r"foo\..\future.txt"])
def test_path_traversal_is_rejected(manifest_factory, asset_factory, logical_path):
    scenario = manifest_factory([asset_factory(logical_path=logical_path)])
    with pytest.raises(SnapshotBuildError, match="unsafe logical path"):
        build(scenario)


@pytest.mark.parametrize("logical_path", ["/future.txt", r"C:\future.txt", r"\\server\share\x"])
def test_absolute_logical_paths_are_rejected(manifest_factory, asset_factory, logical_path):
    scenario = manifest_factory([asset_factory(logical_path=logical_path)])
    with pytest.raises(SnapshotBuildError, match="absolute logical path"):
        build(scenario)


def test_case_insensitive_logical_destination_collision_is_rejected(manifest_factory, asset_factory):
    scenario = manifest_factory(
        [asset_factory(logical_path="src/File.py"), asset_factory(logical_path="SRC/file.py")]
    )
    with pytest.raises(SnapshotBuildError, match="logical path collision"):
        build(scenario)


def test_file_directory_destination_collision_is_rejected(manifest_factory, asset_factory):
    scenario = manifest_factory(
        [asset_factory(logical_path="src"), asset_factory(logical_path="src/file.py")]
    )
    with pytest.raises(SnapshotBuildError, match="logical path collision"):
        build(scenario)


def test_missing_source_asset_fails_build(manifest_factory, asset_factory):
    scenario = manifest_factory([asset_factory(source_path="missing.txt")])
    with pytest.raises(SnapshotBuildError, match="does not exist"):
        build(scenario)


def test_declared_materialized_hash_is_verified(manifest_factory, asset_factory):
    asset = asset_factory()
    asset["content_sha256"] = "0" * 64
    scenario = manifest_factory([asset])
    with pytest.raises(SnapshotBuildError, match="declared content hash mismatch"):
        build(scenario)


def test_repeated_builds_have_identical_deterministic_hashes(manifest_factory, asset_factory, tmp_path):
    scenario = manifest_factory([asset_factory()])
    first, _ = build(scenario, tmp_path / "one")
    second, _ = build(scenario, tmp_path / "two")
    assert first.snapshot_root_hash == second.snapshot_root_hash
    assert first.asset_manifest_hash == second.asset_manifest_hash


def test_changing_available_file_changes_snapshot_root_hash(manifest_factory, asset_factory, tmp_path):
    asset = asset_factory()
    scenario = manifest_factory([asset])
    first, _ = build(scenario, tmp_path / "one")
    (tmp_path / asset["source_path"]).write_text("changed past evidence", encoding="utf-8")
    second, _ = build(scenario, tmp_path / "two")
    assert first.snapshot_root_hash != second.snapshot_root_hash


def test_changing_only_future_file_does_not_change_past_hash(manifest_factory, asset_factory, tmp_path):
    past = asset_factory()
    future = asset_factory(status="LOCKED_FUTURE", content="future one")
    scenario = manifest_factory([past, future])
    first, _ = build(scenario, tmp_path / "one")
    (tmp_path / future["source_path"]).write_text("future two", encoding="utf-8")
    second, _ = build(scenario, tmp_path / "two")
    assert first.snapshot_root_hash == second.snapshot_root_hash


def test_created_at_is_not_an_input_to_deterministic_hash(manifest_factory, asset_factory, tmp_path):
    scenario = manifest_factory([asset_factory()])
    first, _ = build(scenario, tmp_path / "one")
    manifest_path = tmp_path / "one/manifest.json"
    raw = json.loads(manifest_path.read_text("utf-8"))
    raw["created_at"] = "2099-01-01T00:00:00Z"
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    assert BoundaryAuditor().audit(tmp_path / "one").passed
    assert first.snapshot_root_hash == raw["snapshot_root_hash"]


def test_failed_build_preserves_previous_valid_snapshot(manifest_factory, asset_factory, tmp_path):
    output = tmp_path / "sealed"
    valid_scenario = manifest_factory([asset_factory(content="known good")])
    valid, _ = build(valid_scenario, output)
    before = (output / "manifest.json").read_bytes()
    invalid_scenario = manifest_factory([asset_factory(source_path="missing.txt")])
    with pytest.raises(SnapshotBuildError):
        build(invalid_scenario, output)
    assert (output / "manifest.json").read_bytes() == before
    assert json.loads(before)["snapshot_root_hash"] == valid.snapshot_root_hash
