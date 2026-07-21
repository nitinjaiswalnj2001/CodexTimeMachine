"""Cross-phase integrity loading for intervention generation."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from backend.assessment.loader import AssessmentInputError, load_evaluation_context
from backend.assessment.models import AssessmentManifest, OverallStatus, TargetVerdict, TemporalBlindSpotAssessment
from backend.temporal.integrity import canonical_json_bytes, sha256_bytes, sha256_file


class InterventionInputError(RuntimeError):
    pass


def assessment_hash(assessment: TemporalBlindSpotAssessment) -> str:
    return sha256_bytes(canonical_json_bytes(assessment.model_dump(
        mode="json", exclude={"assessment_hash", "created_at", "evaluator_metadata"}
    )))


def load_intervention_inputs(assessment_directory: str | Path):
    assessment_dir = Path(assessment_directory).resolve()
    run_dir = assessment_dir.parent
    assessment_path = assessment_dir / "blind_spot_assessment.json"
    manifest_path = assessment_dir / "assessment_manifest.json"
    evaluation_dir = run_dir / "evaluation"
    try:
        assessment = TemporalBlindSpotAssessment.model_validate_json(assessment_path.read_text("utf-8"))
        manifest = AssessmentManifest.model_validate_json(manifest_path.read_text("utf-8"))
        context, evaluation_manifest = load_evaluation_context(evaluation_dir)
    except (OSError, ValueError, ValidationError, AssessmentInputError) as exc:
        raise InterventionInputError(f"invalid intervention input: {exc}") from exc
    checks = (
        (assessment_hash(assessment) == assessment.assessment_hash, "assessment canonical hash mismatch"),
        (manifest.assessment_hash == assessment.assessment_hash, "assessment manifest hash mismatch"),
        (manifest.output_file_hashes.get("blind_spot_assessment.json") == sha256_file(assessment_path), "assessment output hash mismatch"),
        (manifest.assessment_id == assessment.assessment_id, "assessment ID mismatch"),
        (assessment.context_id == context.context_id == manifest.context_id, "context ID mismatch"),
        (assessment.context_hash == context.context_hash == manifest.context_hash, "context hash mismatch"),
        (assessment.run_id == context.run_id == manifest.run_id, "run ID mismatch"),
        (assessment.scenario_id == context.scenario_id == manifest.scenario_id, "scenario mismatch"),
    )
    for valid, message in checks:
        if not valid:
            raise InterventionInputError(message)
    for name, expected in manifest.output_file_hashes.items():
        source = assessment_dir / name
        if not source.is_file() or sha256_file(source) != expected:
            raise InterventionInputError(f"assessment manifested output hash mismatch: {name}")
    context_targets = {target.target_id for target in context.evaluation_targets}
    assessed = {target.target_id for target in assessment.target_assessments}
    if assessed != context_targets:
        raise InterventionInputError("assessment targets do not match evaluation context")
    if assessment.overall_finding.status is OverallStatus.BLIND_SPOT_IDENTIFIED:
        if not any(target.verdict in {TargetVerdict.MISSED, TargetVerdict.PARTIALLY_SATISFIED}
                   for target in assessment.target_assessments):
            raise InterventionInputError("identified blind spot lacks a grounded missed or partial target")
    return assessment, manifest, context, evaluation_manifest
