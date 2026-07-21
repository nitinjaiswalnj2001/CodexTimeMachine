"""Load and verify accepted Phase 4 evaluation evidence."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from backend.evaluation.models import EvaluationContext, EvaluationManifest
from backend.temporal.integrity import canonical_json_bytes, sha256_bytes, sha256_file


class AssessmentInputError(RuntimeError):
    pass


def context_hash(context: EvaluationContext) -> str:
    return sha256_bytes(canonical_json_bytes(context.model_dump(mode="json", exclude={"context_hash", "created_at"})))


def load_evaluation_context(directory: str | Path) -> tuple[EvaluationContext, EvaluationManifest]:
    root = Path(directory).resolve()
    context_path = root / "evaluation_context.json"
    markdown_path = root / "evaluation_context.md"
    manifest_path = root / "evaluation_manifest.json"
    try:
        context = EvaluationContext.model_validate_json(context_path.read_text("utf-8"))
        manifest = EvaluationManifest.model_validate_json(manifest_path.read_text("utf-8"))
    except (OSError, ValueError, ValidationError) as exc:
        raise AssessmentInputError(f"invalid Phase 4 evaluation input: {exc}") from exc
    checks = (
        (context_hash(context) == context.context_hash, "evaluation context hash mismatch"),
        (manifest.context_hash == context.context_hash, "evaluation manifest context hash mismatch"),
        (manifest.context_id == context.context_id, "evaluation context ID mismatch"),
        (manifest.run_id == context.run_id, "evaluation run ID mismatch"),
        (manifest.scenario_id == context.scenario_id, "evaluation scenario mismatch"),
        (manifest.output_file_hashes.get("evaluation_context.json") == sha256_file(context_path), "evaluation context output hash mismatch"),
        (manifest.output_file_hashes.get("evaluation_context.md") == sha256_file(markdown_path), "evaluation Markdown output hash mismatch"),
        (manifest.event_count == len(context.past_observable_evidence), "evaluation event-count mismatch"),
        (manifest.evidence_item_count == len(context.known_future_evidence), "evaluation evidence-count mismatch"),
        (manifest.evaluation_target_count == len(context.evaluation_targets), "evaluation target-count mismatch"),
        (context.boundary_validation.validation_succeeded, "Phase 4 boundary validation did not succeed"),
        (context.boundary_validation.workspace_integrity_succeeded, "Phase 4 workspace integrity did not succeed"),
    )
    for valid, message in checks:
        if not valid:
            raise AssessmentInputError(message)
    return context, manifest
