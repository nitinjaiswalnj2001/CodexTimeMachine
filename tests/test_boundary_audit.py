from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from backend.temporal.audit import BoundaryAuditError, BoundaryAuditor
from backend.temporal.integrity import canonical_json_bytes, sha256_bytes
from backend.temporal.models import BoundaryControl, BoundaryReport, SnapshotManifest
from backend.temporal.snapshot import _stable_root_inputs, main, TemporalSnapshotBuilder


CANARY = "TM_FUTURE_CANARY_9F41B7"


def make_snapshot(manifest_factory, asset_factory, assets=None, output=None):
    scenario = manifest_factory(assets or [asset_factory()])
    output = output or scenario.parent / "sealed"
    TemporalSnapshotBuilder().build(scenario, output)
    return scenario, output


def load_artifacts(output: Path) -> tuple[dict, dict, dict]:
    return tuple(
        json.loads((output / name).read_text("utf-8"))
        for name in ("manifest.json", "boundary_control.json", "boundary_report.json")
    )


def write_artifacts(output: Path, manifest: dict, control: dict, report: dict) -> None:
    for name, data in (
        ("manifest.json", manifest),
        ("boundary_control.json", control),
        ("boundary_report.json", report),
    ):
        (output / name).write_text(json.dumps(data), encoding="utf-8")


def rehash_artifacts(output: Path, manifest: dict, control: dict, report: dict) -> None:
    manifest["asset_manifest_hash"] = sha256_bytes(
        canonical_json_bytes(manifest["materialized_assets"])
    )
    manifest["boundary_control_hash"] = sha256_bytes(canonical_json_bytes(control))
    manifest["boundary_report_hash"] = sha256_bytes(canonical_json_bytes(report))
    validated = SnapshotManifest.model_validate(manifest)
    manifest["snapshot_root_hash"] = sha256_bytes(
        canonical_json_bytes(_stable_root_inputs(validated))
    )
    write_artifacts(output, manifest, control, report)


def test_canary_leak_audit_passes_for_valid_snapshot(manifest_factory, asset_factory):
    _, output = make_snapshot(
        manifest_factory,
        asset_factory,
        [asset_factory(), asset_factory(status="LOCKED_FUTURE", content=CANARY)],
    )
    assert BoundaryAuditor(CANARY).audit(output).passed


def test_audit_detects_unmanifested_file(manifest_factory, asset_factory):
    _, output = make_snapshot(manifest_factory, asset_factory)
    (output / "repo/surprise.txt").write_text("not declared", encoding="utf-8")
    with pytest.raises(BoundaryAuditError, match="unmanifested files"):
        BoundaryAuditor(CANARY).audit(output)


def test_audit_detects_tampering_with_materialized_file(manifest_factory, asset_factory):
    _, output = make_snapshot(manifest_factory, asset_factory)
    (output / "repo/src/example.py").write_text("tampered", encoding="utf-8")
    with pytest.raises(BoundaryAuditError, match="hash mismatch"):
        BoundaryAuditor(CANARY).audit(output)


def test_audit_rejects_git_anywhere_in_snapshot(manifest_factory, asset_factory):
    _, output = make_snapshot(manifest_factory, asset_factory)
    git_dir = output / "repo/nested/.git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text("secret", encoding="utf-8")
    with pytest.raises(BoundaryAuditError, match=r"\.git content found"):
        BoundaryAuditor(CANARY).audit(output)


def test_boundary_report_tampering_is_detected(manifest_factory, asset_factory):
    assets = [asset_factory(), asset_factory(asset_id="future", status="LOCKED_FUTURE")]
    _, output = make_snapshot(manifest_factory, asset_factory, assets)
    manifest, control, report = load_artifacts(output)
    next(entry for entry in report["entries"] if entry["asset_id"] == "future")["status"] = "MATERIALIZED"
    write_artifacts(output, manifest, control, report)
    with pytest.raises(BoundaryAuditError, match="boundary report hash mismatch"):
        BoundaryAuditor(CANARY).audit(output)


def test_boundary_control_tampering_is_detected(manifest_factory, asset_factory):
    _, output = make_snapshot(manifest_factory, asset_factory)
    manifest, control, report = load_artifacts(output)
    control["entries"][0]["classification"] = "LOCKED_FUTURE"
    write_artifacts(output, manifest, control, report)
    with pytest.raises(BoundaryAuditError, match="boundary control hash mismatch"):
        BoundaryAuditor(CANARY).audit(output)


@pytest.mark.parametrize("field", ["boundary_control_hash", "boundary_report_hash"])
def test_boundary_artifact_hash_tampering_is_detected(manifest_factory, asset_factory, field):
    _, output = make_snapshot(manifest_factory, asset_factory)
    manifest, control, report = load_artifacts(output)
    manifest[field] = "0" * 64
    write_artifacts(output, manifest, control, report)
    with pytest.raises(BoundaryAuditError, match=field.replace("_", " ")):
        BoundaryAuditor(CANARY).audit(output)


def test_materialized_asset_conflicting_with_control_is_rejected(manifest_factory, asset_factory):
    _, output = make_snapshot(manifest_factory, asset_factory)
    manifest, control, report = load_artifacts(output)
    control["entries"][0]["classification"] = "LOCKED_FUTURE"
    report["entries"][0]["status"] = "LOCKED_FUTURE"
    report["summary"].update(materialized=0, locked_future=1)
    rehash_artifacts(output, manifest, control, report)
    with pytest.raises(BoundaryAuditError, match="boundary classification inconsistent"):
        BoundaryAuditor(CANARY).audit(output)


def _sync_report_entry(control_entry: dict, report_entry: dict) -> None:
    report_entry["status"] = control_entry["classification"]
    report_entry["reason"] = control_entry["classification_reason"]


@pytest.mark.parametrize(
    ("status", "visibility", "incorrect_classification"),
    [
        ("AVAILABLE", "GHOST_ONLY", "LOCKED_FUTURE"),
        ("AVAILABLE", "EVALUATOR_ONLY", "MATERIALIZED"),
        ("LOCKED_FUTURE", "PAST_CODEX", "NOT_VISIBLE_TO_PAST"),
        ("EXCLUDED", "PAST_CODEX", "MATERIALIZED"),
    ],
)
def test_hash_consistent_impossible_control_classification_is_rejected(
    manifest_factory, asset_factory, status, visibility, incorrect_classification
):
    assets = [
        asset_factory(asset_id="past"),
        asset_factory(
            asset_id="target",
            logical_path="evidence/target.txt",
            status=status,
            visibility=visibility,
        ),
    ]
    _, output = make_snapshot(manifest_factory, asset_factory, assets)
    manifest, control, report = load_artifacts(output)
    control_entry = next(entry for entry in control["entries"] if entry["asset_id"] == "target")
    report_entry = next(entry for entry in report["entries"] if entry["asset_id"] == "target")
    control_entry["classification"] = incorrect_classification
    control_entry["classification_reason"] = "hash-consistent but impossible"
    _sync_report_entry(control_entry, report_entry)
    rehash_artifacts(output, manifest, control, report)
    with pytest.raises(BoundaryAuditError, match="boundary classification inconsistent for asset target"):
        BoundaryAuditor(CANARY).audit(output)


@pytest.mark.parametrize(
    "field",
    ["materialized", "total", "locked_future", "excluded", "not_visible_to_past"],
)
def test_hash_consistent_report_summary_mismatch_is_rejected(
    manifest_factory, asset_factory, field
):
    assets = [
        asset_factory(asset_id="past"),
        asset_factory(asset_id="future", status="LOCKED_FUTURE"),
        asset_factory(asset_id="excluded", status="EXCLUDED"),
        asset_factory(asset_id="ghost", visibility="GHOST_ONLY"),
    ]
    _, output = make_snapshot(manifest_factory, asset_factory, assets)
    manifest, control, report = load_artifacts(output)
    report["summary"][field] += 1
    rehash_artifacts(output, manifest, control, report)
    with pytest.raises(BoundaryAuditError, match="boundary report summary is inconsistent"):
        BoundaryAuditor(CANARY).audit(output)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("asset_kind", "CONFIG"),
        ("availability_basis", "tampered provenance basis"),
        ("availability_reason", "tampered raw availability reason"),
    ],
)
def test_hash_consistent_materialized_provenance_conflict_is_rejected(
    manifest_factory, asset_factory, field, value
):
    _, output = make_snapshot(manifest_factory, asset_factory)
    manifest, control, report = load_artifacts(output)
    manifest["materialized_assets"][0][field] = value
    rehash_artifacts(output, manifest, control, report)
    with pytest.raises(BoundaryAuditError, match="materialized asset provenance conflicts"):
        BoundaryAuditor(CANARY).audit(output)


def test_hash_consistent_derived_reason_tampering_is_rejected(manifest_factory, asset_factory):
    _, output = make_snapshot(
        manifest_factory, asset_factory, [asset_factory(visibility="GHOST_ONLY")]
    )
    manifest, control, report = load_artifacts(output)
    control["entries"][0]["classification_reason"] = "tampered derived reason"
    _sync_report_entry(control["entries"][0], report["entries"][0])
    rehash_artifacts(output, manifest, control, report)
    with pytest.raises(BoundaryAuditError, match="boundary classification reason inconsistent"):
        BoundaryAuditor(CANARY).audit(output)


def test_materialized_control_asset_missing_from_manifest_is_rejected(manifest_factory, asset_factory):
    _, output = make_snapshot(manifest_factory, asset_factory)
    manifest, control, report = load_artifacts(output)
    manifest["materialized_assets"] = []
    rehash_artifacts(output, manifest, control, report)
    with pytest.raises(BoundaryAuditError, match="materialized control asset missing"):
        BoundaryAuditor(CANARY).audit(output)


@pytest.mark.parametrize(
    ("status", "visibility", "classification"),
    [
        ("LOCKED_FUTURE", "PAST_CODEX", "LOCKED_FUTURE"),
        ("AVAILABLE", "GHOST_ONLY", "NOT_VISIBLE_TO_PAST"),
    ],
)
def test_non_past_control_asset_in_manifest_is_rejected(
    manifest_factory, asset_factory, status, visibility, classification
):
    assets = [
        asset_factory(asset_id="past", logical_path="src/past.py"),
        asset_factory(
            asset_id="future",
            logical_path="evidence/future.txt",
            status=status,
            visibility=visibility,
        ),
    ]
    _, output = make_snapshot(manifest_factory, asset_factory, assets)
    manifest, control, report = load_artifacts(output)
    injected = copy.deepcopy(manifest["materialized_assets"][0])
    injected.update(
        asset_id="future",
        logical_path="evidence/future.txt",
        availability_status=status,
        visibility_scope=visibility,
    )
    manifest["materialized_assets"].append(injected)
    rehash_artifacts(output, manifest, control, report)
    with pytest.raises(BoundaryAuditError, match="non-materialized control asset appears"):
        BoundaryAuditor(CANARY).audit(output)


def test_audit_detects_manifest_asset_hash_tampering(manifest_factory, asset_factory):
    _, output = make_snapshot(manifest_factory, asset_factory)
    manifest, control, report = load_artifacts(output)
    manifest["materialized_assets"][0]["availability_reason"] = "tampered provenance"
    write_artifacts(output, manifest, control, report)
    with pytest.raises(BoundaryAuditError, match="asset manifest hash mismatch"):
        BoundaryAuditor(CANARY).audit(output)


def test_boundary_change_changes_snapshot_hash_and_materialization(manifest_factory, asset_factory, tmp_path):
    past = asset_factory(asset_id="past", logical_path="src/past.py")
    future = asset_factory(asset_id="future", logical_path="evidence/future.txt", status="LOCKED_FUTURE")
    scenario = manifest_factory([past, future])
    first = TemporalSnapshotBuilder().build(scenario, tmp_path / "one")
    assets_path = scenario.parent / "assets.yaml"
    assets = yaml.safe_load(assets_path.read_text("utf-8"))
    assets["assets"][1]["availability"]["status"] = "AVAILABLE"
    assets_path.write_text(yaml.safe_dump(assets, sort_keys=False), encoding="utf-8")
    second = TemporalSnapshotBuilder().build(scenario, tmp_path / "two")
    assert first.snapshot_root_hash != second.snapshot_root_hash
    assert (tmp_path / "two/repo/evidence/future.txt").is_file()


def test_staging_canary_failure_preserves_existing_snapshot(manifest_factory, asset_factory, tmp_path):
    asset = asset_factory(content="known good")
    scenario = manifest_factory([asset])
    output = tmp_path / "sealed"
    TemporalSnapshotBuilder().build(scenario, output)
    before = (output / "manifest.json").read_bytes()
    (tmp_path / asset["source_path"]).write_text(CANARY, encoding="utf-8")
    with pytest.raises(BoundaryAuditError, match="future canary found"):
        TemporalSnapshotBuilder().build(scenario, output)
    assert (output / "manifest.json").read_bytes() == before


def test_cli_canary_failure_is_controlled_and_preserves_output(
    manifest_factory, asset_factory, tmp_path, capsys
):
    asset = asset_factory(content="known good")
    scenario = manifest_factory([asset])
    output = tmp_path / "sealed"
    TemporalSnapshotBuilder().build(scenario, output)
    before = (output / "manifest.json").read_bytes()
    (tmp_path / asset["source_path"]).write_text(CANARY, encoding="utf-8")
    assert main([str(scenario), "--output", str(output)]) == 1
    captured = capsys.readouterr()
    assert "TEMPORAL SNAPSHOT CREATED" not in captured.out
    assert "ERROR: future canary found" in captured.err
    assert "Traceback" not in captured.err
    assert (output / "manifest.json").read_bytes() == before


def test_controlled_scenario_declares_boundary_only_future_canary(tmp_path):
    scenario = Path("backend/scenarios/legalrag_reranker_t001/scenario.yaml")
    future_outcome = scenario.parent / "future_outcome.yaml"
    text = future_outcome.read_text("utf-8")
    assert CANARY in text
    assert "expected_recommendation" not in text
    assert "not Temporal Eval ground truth" in text
    output = tmp_path / "controlled-snapshot"
    TemporalSnapshotBuilder().build(scenario, output)
    assert BoundaryAuditor(CANARY).audit(output).passed
    assert all(
        CANARY.encode() not in path.read_bytes()
        for path in (output / "repo").rglob("*")
        if path.is_file()
    )
