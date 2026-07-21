from __future__ import annotations

import copy
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from backend.evaluation.context_builder import EvaluationContextBuilder, EvaluationContextError, main
from backend.evaluation.integrity import EvaluationIntegrityError
from backend.evaluation.loader import EvaluationPacketError, load_outcome_packet, packet_hash
from backend.evaluation.models import KnownFutureOutcomePacket, ProvenanceType
from backend.temporal.integrity import canonical_json_bytes, sha256_file
from backend.runs.workspace import compute_workspace_tree_hash
from backend.trajectory.extractor import TrajectoryExtractor
from backend.trajectory.models import ObservableTrajectory, TrajectoryManifest
from test_trajectory_extractor import FIXED_TIME, _prepare_run


def _packet_data(base_hash: str = "1" * 64) -> dict:
    return {
        "schema_version": "1.0", "outcome_id": "outcome-1", "scenario_id": "legalrag-reranker-t001",
        "base_snapshot_hash": base_hash, "provenance_type": "CONTROLLED_SYNTHETIC",
        "fixture_notice": "All values are synthetic and this is not organic project history.",
        "provenance": "Controlled fixture.", "temporal_relation": "AFTER_CUTOFF",
        "decision_under_evaluation": "Choose a retrieval default.",
        "known_future_outcome": "Evidence was insufficient for a production default.",
        "evidence_items": [{"evidence_id": "e1", "evidence_kind": "BENCHMARK_RESULT",
            "relative_path": "evidence/result.json", "sha256": "0" * 64,
            "summary": "Synthetic representative evaluation.", "observed_after_cutoff": True,
            "metadata": {"synthetic": True}}],
        "evaluation_targets": [{"target_id": "t1", "category": "INSUFFICIENT_EVALUATION",
            "description": "Run a representative labeled evaluation.",
            "observable_success_condition": "Representative labeled queries are evaluated.",
            "related_evidence_ids": ["e1"]}], "packet_hash": "0" * 64,
    }


def _write_packet(root: Path, data: dict | None = None, content: bytes = b'{"synthetic":true}\n') -> Path:
    root.mkdir(parents=True, exist_ok=True)
    evidence = root / "evidence/result.json"
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_bytes(content)
    payload = copy.deepcopy(data or _packet_data())
    payload["evidence_items"][0]["sha256"] = sha256_file(evidence)
    candidate = KnownFutureOutcomePacket.model_validate(payload)
    payload["packet_hash"] = packet_hash(candidate)
    path = root / "outcome.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


_COMPUTE_WORKSPACE_END_HASH = object()


def _accepted_inputs(tmp_path: Path, *, workspace_end_hash: str | None | object = _COMPUTE_WORKSPACE_END_HASH):
    run = _prepare_run(tmp_path / "run")
    manifest_path = run / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["workspace_end_hash"] = (
        compute_workspace_tree_hash(run / "workspace")
        if workspace_end_hash is _COMPUTE_WORKSPACE_END_HASH
        else workspace_end_hash
    )
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
    trajectory_dir = run / "trajectory"
    TrajectoryExtractor().extract(run, trajectory_dir, extracted_at=FIXED_TIME)
    packet = _write_packet(tmp_path / "packet")
    return run, trajectory_dir, packet


def _build(tmp_path: Path, name="evaluation"):
    run, trajectory, packet = _accepted_inputs(tmp_path)
    context = EvaluationContextBuilder().build(run, trajectory, packet, run / name, created_at=FIXED_TIME)
    return context, run, trajectory, packet


def test_valid_controlled_synthetic_packet_loads(tmp_path):
    packet, _ = load_outcome_packet(_write_packet(tmp_path / "packet"))
    assert packet.provenance_type is ProvenanceType.CONTROLLED_SYNTHETIC


def test_valid_organic_history_packet_loads(tmp_path):
    data = _packet_data(); data["provenance_type"] = "ORGANIC_HISTORY"; data["fixture_notice"] = None
    data["provenance"] = "Collected from archived release records with source identifiers."
    packet, _ = load_outcome_packet(_write_packet(tmp_path / "packet", data))
    assert packet.provenance_type is ProvenanceType.ORGANIC_HISTORY


@pytest.mark.parametrize("mutation", ["missing_type", "unknown_type", "synthetic_notice", "organic_provenance", "temporal_relation"])
def test_invalid_provenance_or_temporal_semantics_fail(tmp_path, mutation):
    data = _packet_data()
    if mutation == "missing_type": data.pop("provenance_type")
    elif mutation == "unknown_type": data["provenance_type"] = "AMBIGUOUS"
    elif mutation == "synthetic_notice": data["fixture_notice"] = None
    elif mutation == "organic_provenance": data.update(provenance_type="ORGANIC_HISTORY", provenance="")
    else: data["temporal_relation"] = "AT_CUTOFF"
    with pytest.raises((ValidationError, EvaluationPacketError)):
        _write_packet(tmp_path / "packet", data)


@pytest.mark.parametrize("bad_path", ["/etc/passwd", "C:\\future.txt", "\\\\server\\share\\x", "../future.txt", "a/../../x"])
def test_unsafe_evidence_paths_fail(tmp_path, bad_path):
    data = _packet_data(); data["evidence_items"][0]["relative_path"] = bad_path
    path = _write_packet(tmp_path / "packet", data)
    with pytest.raises(EvaluationPacketError): load_outcome_packet(path)


def test_symlink_evidence_fails(tmp_path):
    root = tmp_path / "packet"; path = _write_packet(root); target = root / "evidence/result.json"
    outside = tmp_path / "outside.json"; outside.write_text("outside")
    target.unlink()
    try: target.symlink_to(outside)
    except OSError: pytest.skip("symlink unavailable")
    with pytest.raises(EvaluationPacketError, match="symlink"): load_outcome_packet(path)


@pytest.mark.parametrize("mutation", ["duplicate_evidence", "path_collision", "duplicate_target", "unknown_reference"])
def test_duplicate_or_dangling_packet_identifiers_fail(tmp_path, mutation):
    data = _packet_data()
    if mutation == "duplicate_evidence": data["evidence_items"].append(copy.deepcopy(data["evidence_items"][0]))
    elif mutation == "path_collision":
        extra = copy.deepcopy(data["evidence_items"][0]); extra.update(evidence_id="e2", relative_path="EVIDENCE/RESULT.JSON"); data["evidence_items"].append(extra)
    elif mutation == "duplicate_target": data["evaluation_targets"].append(copy.deepcopy(data["evaluation_targets"][0]))
    else: data["evaluation_targets"][0]["related_evidence_ids"] = ["missing"]
    with pytest.raises(ValidationError): _write_packet(tmp_path / "packet", data)


def test_missing_file_and_hash_mismatch_fail(tmp_path):
    path = _write_packet(tmp_path / "missing"); (path.parent / "evidence/result.json").unlink()
    with pytest.raises(EvaluationPacketError, match="missing"): load_outcome_packet(path)
    path = _write_packet(tmp_path / "mismatch"); (path.parent / "evidence/result.json").write_text("changed")
    with pytest.raises(EvaluationPacketError, match="SHA-256"): load_outcome_packet(path)


@pytest.mark.parametrize("mismatch", ["scenario", "snapshot", "run", "thread", "failed", "raw", "trajectory_hash", "trajectory_output", "event_count"])
def test_cross_phase_integrity_mismatches_fail(tmp_path, mismatch):
    run, trajectory_dir, packet = _accepted_inputs(tmp_path)
    if mismatch == "scenario":
        data = yaml.safe_load(packet.read_text()); data["scenario_id"] = "other"; packet = _write_packet(tmp_path / "packet2", data)
    elif mismatch == "snapshot":
        data = yaml.safe_load(packet.read_text()); data["base_snapshot_hash"] = "9" * 64; packet = _write_packet(tmp_path / "packet2", data)
    elif mismatch in {"run", "thread", "trajectory_hash", "event_count"}:
        path = trajectory_dir / "trajectory.json"; data = json.loads(path.read_text())
        if mismatch == "run": data["run_id"] = "other"
        elif mismatch == "thread": data["thread_id"] = "other"
        elif mismatch == "trajectory_hash": data["trajectory_hash"] = "9" * 64
        else: data["event_count"] += 1
        path.write_bytes(canonical_json_bytes(data) + b"\n")
    elif mismatch == "failed":
        path = run / "run_manifest.json"; data = json.loads(path.read_text()); data["run_status"] = "FAILED"; path.write_bytes(canonical_json_bytes(data)+b"\n")
    elif mismatch == "raw": (run / "raw_codex_events.jsonl").write_bytes(b"tampered\n")
    else: (trajectory_dir / "trajectory.json").write_bytes((trajectory_dir / "trajectory.json").read_bytes()+b" ")
    with pytest.raises((EvaluationContextError, EvaluationIntegrityError)):
        EvaluationContextBuilder().build(run, trajectory_dir, packet, run / "evaluation", created_at=FIXED_TIME)


def test_packet_inside_workspace_fails(tmp_path):
    run, trajectory, _ = _accepted_inputs(tmp_path)
    packet = _write_packet(run / "workspace/future-packet")
    with pytest.raises(EvaluationIntegrityError): EvaluationContextBuilder().build(run, trajectory, packet, run / "evaluation", created_at=FIXED_TIME)


def test_declared_path_or_hash_in_workspace_fails(tmp_path):
    run, trajectory, packet = _accepted_inputs(tmp_path / "path")
    target = run / "workspace/evidence/result.json"; target.parent.mkdir(); target.write_text("different")
    with pytest.raises(EvaluationIntegrityError, match="workspace hash"): EvaluationContextBuilder().build(run, trajectory, packet, run / "evaluation", created_at=FIXED_TIME)
    run, trajectory, packet = _accepted_inputs(tmp_path / "hash")
    (run / "workspace/copied-future.json").write_bytes((packet.parent / "evidence/result.json").read_bytes())
    with pytest.raises(EvaluationIntegrityError, match="workspace hash"): EvaluationContextBuilder().build(run, trajectory, packet, run / "evaluation", created_at=FIXED_TIME)


def test_valid_context_separates_past_future_and_records_boundary(tmp_path):
    context, run, _, _ = _build(tmp_path)
    assert context.boundary_validation.validation_succeeded
    assert context.boundary_validation.packet_outside_workspace
    assert context.boundary_validation.all_evidence_outside_workspace
    assert context.boundary_validation.no_evidence_paths_in_workspace
    assert context.boundary_validation.no_evidence_hashes_in_workspace
    assert context.boundary_validation.workspace_integrity_succeeded
    assert context.boundary_validation.expected_workspace_hash == context.boundary_validation.actual_workspace_hash
    assert "semantically equivalent" in context.boundary_validation.limitation
    payload = json.loads((run / "evaluation/evaluation_context.json").read_text())
    assert "past_observable_evidence" in payload and "known_future_evidence" in payload
    forbidden = {"score", "passed", "failed", "blind_spot", "recommended_intervention", "ghost_clue", "replay_prompt"}
    assert forbidden.isdisjoint(payload)
    markdown = (run / "evaluation/evaluation_context.md").read_text()
    assert "## Past Observable Trajectory" in markdown and "## Known Future Outcome" in markdown
    assert "No agent score has been produced" in markdown


def test_packet_context_and_fixed_outputs_are_deterministic(tmp_path):
    run, trajectory, packet = _accepted_inputs(tmp_path)
    builder = EvaluationContextBuilder()
    first = builder.build(run, trajectory, packet, run / "a", created_at=FIXED_TIME)
    second = builder.build(run, trajectory, packet, run / "b", created_at=FIXED_TIME)
    assert first.context_id == second.context_id and first.context_hash == second.context_hash
    assert (run / "a/evaluation_context.json").read_bytes() == (run / "b/evaluation_context.json").read_bytes()
    assert (run / "a/evaluation_context.md").read_bytes() == (run / "b/evaluation_context.md").read_bytes()
    assert load_outcome_packet(packet)[0].packet_hash == first.outcome_packet_hash


def test_existing_output_and_source_evidence_are_immutable(tmp_path):
    run, trajectory, packet = _accepted_inputs(tmp_path); raw = (run / "raw_codex_events.jsonl").read_bytes(); traj = (trajectory / "trajectory.json").read_bytes(); future = (packet.parent / "evidence/result.json").read_bytes()
    builder = EvaluationContextBuilder(); builder.build(run, trajectory, packet, run / "evaluation", created_at=FIXED_TIME)
    with pytest.raises(EvaluationContextError, match="--overwrite"): builder.build(run, trajectory, packet, run / "evaluation", created_at=FIXED_TIME)
    assert (run / "raw_codex_events.jsonl").read_bytes() == raw
    assert (trajectory / "trajectory.json").read_bytes() == traj
    assert (packet.parent / "evidence/result.json").read_bytes() == future


@pytest.mark.parametrize("mutation", ["missing", "file", "added", "removed", "modified"])
def test_workspace_integrity_failures_fail_closed(tmp_path, mutation):
    run, trajectory, packet = _accepted_inputs(tmp_path)
    workspace = run / "workspace"
    if mutation == "missing":
        shutil.rmtree(workspace)
    elif mutation == "file":
        shutil.rmtree(workspace); workspace.write_text("not a workspace")
    elif mutation == "added":
        (workspace / "new.txt").write_text("new")
    elif mutation == "removed":
        next(path for path in workspace.rglob("*") if path.is_file()).unlink()
    else:
        next(path for path in workspace.rglob("*") if path.is_file()).write_text("tampered")
    with pytest.raises(EvaluationIntegrityError):
        EvaluationContextBuilder().build(run, trajectory, packet, run / "evaluation", created_at=FIXED_TIME)


def test_workspace_symlink_fails_closed(tmp_path):
    run, trajectory, packet = _accepted_inputs(tmp_path / "symlink")
    workspace = run / "workspace"; target = tmp_path / "target"; workspace.rename(target)
    try:
        workspace.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink unavailable")
    with pytest.raises(EvaluationIntegrityError, match="symbolic link"):
        EvaluationContextBuilder().build(run, trajectory, packet, run / "evaluation", created_at=FIXED_TIME)


def test_missing_workspace_end_hash_fails_closed(tmp_path):
    run, trajectory, packet = _accepted_inputs(tmp_path, workspace_end_hash=None)
    with pytest.raises(EvaluationIntegrityError, match="workspace_end_hash"):
        EvaluationContextBuilder().build(run, trajectory, packet, run / "evaluation", created_at=FIXED_TIME)


@pytest.mark.parametrize("location", ["trajectory", "trajectory/nested", "packet", "packet/evidence/nested", "run_manifest.json", "raw_codex_events.jsonl", "final_message.txt", "evidence/result.json"])
def test_output_cannot_overlap_protected_source_evidence(tmp_path, location):
    run, trajectory, packet = _accepted_inputs(tmp_path)
    sources = {
        "trajectory": trajectory,
        "trajectory/nested": trajectory / "nested",
        "packet": packet.parent,
        "packet/evidence/nested": packet.parent / "evidence" / "nested",
        "run_manifest.json": run / "run_manifest.json",
        "raw_codex_events.jsonl": run / "raw_codex_events.jsonl",
        "final_message.txt": run / "final_message.txt",
        "evidence/result.json": packet.parent / "evidence" / "result.json",
    }
    protected = [run / "run_manifest.json", run / "raw_codex_events.jsonl", run / "final_message.txt", trajectory / "trajectory.json", trajectory / "trajectory.md", trajectory / "trajectory_manifest.json", packet, packet.parent / "evidence" / "result.json"]
    before = {path: path.read_bytes() for path in protected}
    with pytest.raises(EvaluationContextError, match="protected"):
        EvaluationContextBuilder().build(run, trajectory, packet, sources[location], created_at=FIXED_TIME, overwrite=True)
    assert {path: path.read_bytes() for path in protected} == before


def test_default_sibling_and_overwrite_unprotected_outputs_work(tmp_path):
    run, trajectory, packet = _accepted_inputs(tmp_path)
    builder = EvaluationContextBuilder()
    builder.build(run, trajectory, packet, created_at=FIXED_TIME)
    builder.build(run, trajectory, packet, run / "evaluation", created_at=FIXED_TIME, overwrite=True)
    sibling = tmp_path / "sibling-evaluation"
    builder.build(run, trajectory, packet, sibling, created_at=FIXED_TIME)
    assert (run / "evaluation/evaluation_context.json").is_file() and (sibling / "evaluation_context.json").is_file()


def test_legalrag_fixture_is_explicitly_synthetic():
    path = Path("backend/evaluations/legalrag_reranker_t001/outcome.yaml")
    packet, _ = load_outcome_packet(path)
    combined = (packet.fixture_notice + " " + packet.known_future_outcome).casefold()
    assert packet.provenance_type is ProvenanceType.CONTROLLED_SYNTHETIC
    assert "synthetic" in combined and "not organic" in combined
    assert "real legalrag" not in combined
    evidence = path.parent / "evidence/representative_retrieval_eval.json"
    payload = json.loads(evidence.read_text())
    assert payload["values_are_synthetic"] is True
    assert "synthetic" in payload["fixture_notice"].casefold()
    assert len(payload["query_groups"]) >= 5
    strategies = payload["strategies"]
    definitions = payload["metric_definitions"]
    for group in payload["query_groups"]:
        assert group["synthetic_cases"] > 0
        assert {entry["strategy"] for entry in group["strategy_results"]} == set(strategies)
        for entry in group["strategy_results"]:
            for metric, value in entry["metrics"].items():
                assert isinstance(value, (int, float))
                assert definitions[metric]["minimum"] <= value <= definitions[metric]["maximum"]
    aggregate = {entry["strategy"]: entry["metrics"] for entry in payload["aggregate_results"]["strategy_results"]}
    for strategy in strategies:
        for metric in definitions:
            values = [entry["metrics"][metric] for group in payload["query_groups"] for entry in group["strategy_results"] if entry["strategy"] == strategy]
            assert aggregate[strategy][metric] == pytest.approx(sum(values) / len(values), abs=0.0005)
    assert "insufficient" in payload["synthetic_conclusion"].casefold()
    assert "organic" in payload["limitations"].casefold() and "real legalrag" in payload["limitations"].casefold()
    assert packet.evidence_items[0].sha256 == sha256_file(evidence)
    assert packet.packet_hash == packet_hash(packet)


def test_cli_help_and_build(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
