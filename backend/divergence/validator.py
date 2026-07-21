"""Authoritative semantic validation for published Phase 8 divergence evidence."""
from __future__ import annotations

from pathlib import Path

from backend.temporal.integrity import canonical_json_bytes, sha256_bytes, sha256_file

from .alignment import align_events
from .classifier import dimensions, first_investigative_divergence, first_replay_evaluation_divergence, first_structural_divergence, outcome
from .loader import DivergenceInputError, load_inputs
from .models import DivergenceManifest, ObservableHistoryDivergence
from .normalizer import comparison_events


class DivergenceArtifactValidationError(ValueError):
    """Raised when published Phase 8 evidence is not its deterministic artifact."""


def canonical_divergence_hash(value: ObservableHistoryDivergence) -> str:
    return sha256_bytes(canonical_json_bytes(value.model_dump(mode="json", exclude={"divergence_hash", "created_at"})))


def validate_divergence_artifact(run_directory, replay_directory, divergence_directory):
    """Validate sources, alignment, classifications, hashes, and published manifest."""
    try:
        source = load_inputs(run_directory, replay_directory)
    except Exception as exc:
        raise DivergenceArtifactValidationError(f"Phase 8 source lineage validation failed: {exc}") from exc
    directory = Path(divergence_directory).resolve()
    try:
        value = ObservableHistoryDivergence.model_validate_json((directory / "history_divergence.json").read_text("utf-8"))
        manifest = DivergenceManifest.model_validate_json((directory / "divergence_manifest.json").read_text("utf-8"))
    except Exception as exc:
        raise DivergenceArtifactValidationError(f"invalid Phase 8 artifact: {exc}") from exc
    baseline = comparison_events(source.baseline.events)
    replay = comparison_events(source.replay.events)
    differences, summary = align_events(baseline, replay)
    expected_dimensions = dimensions(baseline, replay)
    expected_structural = first_structural_divergence(differences)
    expected_investigative = first_investigative_divergence(differences, baseline, replay)
    expected_replay_evaluation = first_replay_evaluation_divergence(differences, replay)
    expected_outcome = outcome(differences, baseline, replay, expected_dimensions)
    checks = [
        (value.normalized_baseline_events == baseline, "normalized baseline events mismatch"),
        (value.normalized_replay_events == replay, "normalized replay events mismatch"),
        (value.event_differences == differences, "event differences mismatch"),
        (value.alignment == summary, "alignment summary mismatch"),
        (value.first_structural_divergence == expected_structural, "first structural divergence mismatch"),
        (value.first_investigative_divergence == expected_investigative, "first investigative divergence mismatch"),
        (value.first_replay_evaluation_divergence == expected_replay_evaluation, "first replay evaluation divergence mismatch"),
        (value.behavioral_dimensions == expected_dimensions, "behavioral dimensions mismatch"),
        (value.observable_outcome == expected_outcome, "observable outcome mismatch"),
        (value.divergence_hash == canonical_divergence_hash(value), "canonical divergence hash mismatch"),
        (manifest.divergence_id == value.divergence_id and manifest.divergence_hash == value.divergence_hash, "manifest divergence identity mismatch"),
        (manifest.baseline_event_count == summary.baseline_event_count and manifest.replay_event_count == summary.replay_event_count, "manifest event count mismatch"),
        (manifest.matched_count == summary.matched_count and manifest.baseline_only_count == summary.baseline_only_count and manifest.replay_only_count == summary.replay_only_count and manifest.modified_count == summary.modified_count and manifest.reordered_count == summary.reordered_count and manifest.expanded_count == summary.expanded_count and manifest.contracted_count == summary.contracted_count, "manifest alignment counts mismatch"),
    ]
    if manifest.replay_directory_relative_path and Path(replay_directory).resolve().relative_to(source.run_directory).as_posix() != manifest.replay_directory_relative_path:
        checks.append((False, "replay directory lineage mismatch"))
    for name, expected in manifest.output_file_hashes.items():
        checks.append(((directory / name).is_file() and sha256_file(directory / name) == expected, f"output hash mismatch: {name}"))
    for valid, message in checks:
        if not valid:
            raise DivergenceArtifactValidationError(message)
    return source, value, manifest
