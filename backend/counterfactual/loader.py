"""Fail-closed loading of the accepted Phase 1–8 lineage."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.assessment.models import AssessmentManifest, TemporalBlindSpotAssessment
from backend.divergence.models import DivergenceManifest, ObservableHistoryDivergence
from backend.evaluation.models import EvaluationContext, EvaluationManifest
from backend.intervention.models import GhostIntervention, InterventionManifest, ReplayIntervention
from backend.replay.loader import load_replay_inputs
from backend.replay.runner import validate_replay_artifacts
from backend.runs.models import RunManifest, RunStatus
from backend.temporal.integrity import canonical_json_bytes, sha256_bytes, sha256_file
from backend.trajectory.models import ObservableTrajectory, TrajectoryManifest
from backend.divergence.receipt import PhaseReceipt
from backend.divergence.validator import validate_divergence_artifact

from .validator import canonical_divergence_hash


class CounterfactualInputError(RuntimeError):
    """Raised when accepted source evidence no longer validates."""


@dataclass(frozen=True)
class Inputs:
    run: Path
    replay_dir: Path
    divergence_dir: Path
    assessment_dir: Path
    context: EvaluationContext
    assessment: TemporalBlindSpotAssessment
    intervention: GhostIntervention
    payload: ReplayIntervention
    replay: object
    trajectory: ObservableTrajectory
    divergence: ObservableHistoryDivergence
    hashes: dict[str, str]


def _require(path: Path, expected: str, label: str) -> None:
    if not path.is_file():
        raise CounterfactualInputError(f"missing source: {label}")
    if sha256_file(path) != expected:
        raise CounterfactualInputError(f"source hash mismatch: {label}")


def _manifest_outputs(directory: Path, manifest: object, label: str) -> None:
    for name, expected in manifest.output_file_hashes.items():
        _require(directory / name, expected, f"{label}/{name}")


def _record_existing(hashes: dict[str, str], root: Path, label: str, names: tuple[str, ...]) -> None:
    for name in names:
        path = root / name
        if path.is_file():
            hashes[f"{label}/{name}"] = sha256_file(path)


def _lineage_hashes(
    run: Path,
    assessment_dir: Path,
    replay_dir: Path,
    divergence_dir: Path,
) -> dict[str, str]:
    """Record every existing evidence file trusted by Phase 9 with stable keys."""
    hashes: dict[str, str] = {}
    _record_existing(hashes, run, "run", (
        "run_manifest.json", "raw_codex_events.jsonl", "final_message.txt",
        "isolation_probe.json", "isolation_probe_command.json",
    ))
    _record_existing(hashes, run / "trajectory", "trajectory", (
        "trajectory.json", "trajectory.md", "trajectory_manifest.json",
    ))
    _record_existing(hashes, run / "evaluation", "evaluation", (
        "evaluation_context.json", "evaluation_context.md", "evaluation_manifest.json",
    ))
    _record_existing(hashes, assessment_dir, "assessment_source", (
        "blind_spot_assessment.json", "blind_spot_assessment.md", "assessment_manifest.json",
    ))
    _record_existing(hashes, run / "intervention", "intervention", (
        "ghost_intervention.json", "ghost_intervention.md", "replay_intervention.json",
        "intervention_manifest.json",
    ))
    _record_existing(hashes, replay_dir, "replay", (
        "replay_prompt.txt", "raw_replay_events.jsonl", "final_replay_message.txt",
        "replay_stderr.log", "isolation_probe.json", "isolation_probe_command.json",
        "replay_trajectory.json", "replay_trajectory.md", "replay_trajectory_manifest.json",
        "replay_manifest.json",
    ))
    _record_existing(hashes, divergence_dir, "divergence", (
        "history_divergence.json", "history_divergence.md", "divergence_manifest.json",
        "alignment_debug.json",
    ))
    receipt = run / "phase_receipts" / "phase-8.json"
    if receipt.is_file():
        hashes["phase_receipts/phase-8.json"] = sha256_file(receipt)
    return hashes


def _add_manifested_outputs(hashes: dict[str, str], root: Path, label: str, manifest: object) -> None:
    """Record every artifact the accepted parent manifest declares, including optional evidence."""
    for name, expected in manifest.output_file_hashes.items():
        path = root / name
        _require(path, expected, f"{label}/{name}")
        hashes[f"{label}/{name}"] = sha256_file(path)


def load_inputs(run_directory: str | Path, replay_directory: str | Path, divergence_directory: str | Path) -> Inputs:
    run = Path(run_directory).resolve()
    replay_dir = Path(replay_directory).resolve()
    divergence_dir = Path(divergence_directory).resolve()
    evaluation_dir = run / "evaluation"
    intervention_dir = run / "intervention"
    try:
        run_manifest = RunManifest.model_validate_json((run / "run_manifest.json").read_text("utf-8"))
        context = EvaluationContext.model_validate_json((evaluation_dir / "evaluation_context.json").read_text("utf-8"))
        evaluation_manifest = EvaluationManifest.model_validate_json((evaluation_dir / "evaluation_manifest.json").read_text("utf-8"))
        intervention_manifest = InterventionManifest.model_validate_json((intervention_dir / "intervention_manifest.json").read_text("utf-8"))
        intervention = GhostIntervention.model_validate_json((intervention_dir / "ghost_intervention.json").read_text("utf-8"))
        payload = ReplayIntervention.model_validate_json((intervention_dir / "replay_intervention.json").read_text("utf-8"))
        assessment_dir = run.joinpath(*intervention_manifest.assessment_source_directory.split("/")).resolve()
        if not assessment_dir.is_relative_to(run):
            raise CounterfactualInputError("assessment source escapes run directory")
        assessment = TemporalBlindSpotAssessment.model_validate_json((assessment_dir / "blind_spot_assessment.json").read_text("utf-8"))
        assessment_manifest = AssessmentManifest.model_validate_json((assessment_dir / "assessment_manifest.json").read_text("utf-8"))
        replay = validate_replay_artifacts(replay_dir)
        trajectory = ObservableTrajectory.model_validate_json((replay_dir / "replay_trajectory.json").read_text("utf-8"))
        replay_trajectory_manifest = TrajectoryManifest.model_validate_json((replay_dir / "replay_trajectory_manifest.json").read_text("utf-8"))
        divergence = ObservableHistoryDivergence.model_validate_json((divergence_dir / "history_divergence.json").read_text("utf-8"))
        divergence_manifest = DivergenceManifest.model_validate_json((divergence_dir / "divergence_manifest.json").read_text("utf-8"))
        receipt = PhaseReceipt.model_validate_json((run / "phase_receipts" / "phase-8.json").read_text("utf-8"))
    except Exception as exc:
        raise CounterfactualInputError(f"invalid counterfactual source: {exc}") from exc

    _manifest_outputs(evaluation_dir, evaluation_manifest, "evaluation")
    _manifest_outputs(assessment_dir, assessment_manifest, "assessment")
    _manifest_outputs(intervention_dir, intervention_manifest, "intervention")
    _manifest_outputs(divergence_dir, divergence_manifest, "divergence")
    _require(intervention_dir / "replay_intervention.json", intervention_manifest.replay_intervention_hash, "replay_intervention.json")
    try:
        validate_divergence_artifact(run, replay_dir, divergence_dir)
        load_replay_inputs(run, intervention_dir)
    except Exception as exc:
        raise CounterfactualInputError(f"replay input lineage validation failed: {exc}") from exc

    recomputed_divergence_hash = canonical_divergence_hash(divergence)
    expected_receipt_path = divergence_dir.relative_to(run).as_posix()
    if receipt.output_directory_relative_path.startswith(("/", "\\")) or ":" in receipt.output_directory_relative_path or ".." in Path(receipt.output_directory_relative_path).parts:
        raise CounterfactualInputError("unsafe Phase 8 receipt output path")
    checks = [
        (run_manifest.run_status is RunStatus.SUCCEEDED, "baseline run is not SUCCEEDED"),
        (context.boundary_validation.validation_succeeded, "evaluation boundary validation failed"),
        (context.context_id == assessment.context_id == intervention_manifest.context_id, "context ID mismatch"),
        (context.context_hash == assessment.context_hash == assessment_manifest.context_hash, "context hash mismatch"),
        (assessment.assessment_id == intervention_manifest.assessment_id == assessment_manifest.assessment_id, "assessment ID mismatch"),
        (assessment.assessment_hash == intervention_manifest.assessment_hash == assessment_manifest.assessment_hash, "assessment hash mismatch"),
        (intervention.intervention_id == intervention_manifest.intervention_id == payload.intervention_id == replay.intervention_id == divergence.intervention_id, "intervention ID mismatch"),
        (intervention.intervention_hash == intervention_manifest.intervention_hash == payload.intervention_hash == replay.intervention_hash, "intervention hash mismatch"),
        (run_manifest.run_id == context.run_id == assessment.run_id == intervention.run_id == replay.baseline_run_id == divergence.baseline_run_id, "run ID mismatch"),
        (run_manifest.scenario_id == context.scenario_id == assessment.scenario_id == intervention.scenario_id == replay.scenario_id == divergence.scenario_id, "scenario mismatch"),
        (run_manifest.base_snapshot_hash == context.base_snapshot_hash == replay.base_snapshot_hash == divergence.base_snapshot_hash, "snapshot mismatch"),
        (replay.replay_id == trajectory.run_id == divergence.replay_id, "replay ID mismatch"),
        (trajectory.trajectory_hash == replay.replay_trajectory_hash == divergence.replay_trajectory_hash, "replay trajectory mismatch"),
        (replay_trajectory_manifest.trajectory_hash == trajectory.trajectory_hash, "replay trajectory manifest mismatch"),
        (recomputed_divergence_hash == divergence.divergence_hash == divergence_manifest.divergence_hash, "canonical divergence hash mismatch"),
        (receipt.artifact_id == divergence.divergence_id, "Phase 8 receipt artifact ID mismatch"),
        (receipt.artifact_hash == divergence.divergence_hash, "Phase 8 receipt artifact hash mismatch"),
        (receipt.manifest_sha256 == sha256_file(divergence_dir / "divergence_manifest.json"), "Phase 8 receipt manifest hash mismatch"),
        (receipt.output_directory_relative_path == expected_receipt_path, "Phase 8 receipt output path mismatch"),
        (receipt.source_lineage_hash == sha256_bytes(canonical_json_bytes(divergence_manifest.input_file_hashes)), "Phase 8 receipt source lineage hash mismatch"),
        (divergence_manifest.baseline_event_count >= 0 and divergence_manifest.replay_event_count >= 0, "invalid divergence counts"),
    ]
    for valid, message in checks:
        if not valid:
            raise CounterfactualInputError(message)
    hashes = _lineage_hashes(run, assessment_dir, replay_dir, divergence_dir)
    _add_manifested_outputs(hashes, evaluation_dir, "evaluation", evaluation_manifest)
    _add_manifested_outputs(hashes, assessment_dir, "assessment_source", assessment_manifest)
    _add_manifested_outputs(hashes, intervention_dir, "intervention", intervention_manifest)
    _add_manifested_outputs(hashes, divergence_dir, "divergence", divergence_manifest)
    # Replay validation has already checked its manifest; record every declared output.
    _add_manifested_outputs(hashes, replay_dir, "replay", replay)
    return Inputs(
        run, replay_dir, divergence_dir, assessment_dir, context, assessment, intervention,
        payload, replay, trajectory, divergence,
        hashes,
    )
