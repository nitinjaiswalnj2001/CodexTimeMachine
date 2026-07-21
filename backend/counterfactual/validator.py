"""Canonical and cross-reference validation for Phase 9 evidence."""
from __future__ import annotations

from pathlib import Path
from backend.temporal.integrity import canonical_json_bytes, sha256_bytes, sha256_file
from backend.divergence.models import BehavioralDimension, ObservableHistoryDivergence
from .models import COUNTERFACTUAL_POLICY_VERSION, CounterfactualCoverageAssessment, ReplayCoverageStatus, ShiftStatus


class CounterfactualValidationError(ValueError):
    pass


FORBIDDEN_CLAIM_TERMS = (
    "quality score", "improvement score", "production ready", "statistical significance",
    "retrieval quality improved", "caused the change", "correct recommendation",
)


def canonical_divergence_hash(value: ObservableHistoryDivergence) -> str:
    return sha256_bytes(canonical_json_bytes(value.model_dump(
        mode="json", exclude={"divergence_hash", "created_at"}
    )))


def canonical_coverage_hash(value: CounterfactualCoverageAssessment) -> str:
    return sha256_bytes(canonical_json_bytes(value.model_dump(
        mode="json", exclude={"coverage_hash", "created_at"}
    )))


def validate_generated_assessment(value, context, assessment, trajectory, divergence) -> None:
    target_ids = {target.target_id for target in context.evaluation_targets}
    coverage_ids = [coverage.target_id for coverage in value.target_coverages]
    if len(coverage_ids) != len(set(coverage_ids)) or set(coverage_ids) != target_ids:
        raise CounterfactualValidationError("coverage must contain every evaluation target exactly once")
    baseline_ids = {event.event_id for event in context.past_observable_evidence}
    replay_ids = {event.event_id for event in trajectory.events}
    difference_ids = {difference.difference_id for difference in divergence.event_differences}
    for coverage in value.target_coverages:
        if not set(coverage.baseline_evidence) <= baseline_ids:
            raise CounterfactualValidationError("coverage has unknown baseline event reference")
        if not set(coverage.replay_evidence) <= replay_ids:
            raise CounterfactualValidationError("coverage has unknown replay event reference")
        if coverage.replay_coverage_status is ReplayCoverageStatus.OBSERVED:
            if not coverage.replay_evidence or not coverage.coverage_proof.successful_command_event_ids:
                raise CounterfactualValidationError("OBSERVED coverage requires replay and successful-command evidence")
            proof = coverage.coverage_proof
            if not {"fixture", "categories", "successful_evaluation", "summary", "decision", "ordered"} <= set(proof.observed_stages):
                raise CounterfactualValidationError("OBSERVED coverage lacks required policy stages")
            if not (proof.fixture_sequence < proof.evaluation_sequence < proof.summary_sequence <= proof.decision_sequence):
                raise CounterfactualValidationError("OBSERVED coverage has invalid stage ordering")
        if coverage.replay_coverage_status is ReplayCoverageStatus.PARTIALLY_OBSERVED and not coverage.coverage_proof.observed_stages:
            raise CounterfactualValidationError("PARTIALLY_OBSERVED coverage requires an observed stage")
        if coverage.replay_coverage_status is ReplayCoverageStatus.INSUFFICIENT_EVIDENCE and not coverage.remaining_uncertainty:
            raise CounterfactualValidationError("INSUFFICIENT_EVIDENCE coverage requires an explanation")
    from .coverage import target_coverages, shift as recompute_shift, activity_volume_context
    recomputed = {item.target_id: item for item in target_coverages(context, assessment, trajectory)}
    for coverage in value.target_coverages:
        expected = recomputed[coverage.target_id]
        if coverage.replay_coverage_status != expected.replay_coverage_status or coverage.replay_evidence != expected.replay_evidence or coverage.coverage_proof != expected.coverage_proof:
            raise CounterfactualValidationError("coverage proof does not match recomputed replay evidence")
    shift = value.shift
    if not set(shift.supporting_target_ids) <= target_ids:
        raise CounterfactualValidationError("shift has unknown target reference")
    if not set(shift.supporting_baseline_event_ids) <= baseline_ids:
        raise CounterfactualValidationError("shift has unknown baseline event reference")
    if not set(shift.supporting_replay_event_ids) <= replay_ids:
        raise CounterfactualValidationError("shift has unknown replay event reference")
    if not set(shift.supporting_difference_ids) <= difference_ids:
        raise CounterfactualValidationError("shift has unknown divergence reference")
    activity = value.activity_volume_context
    if not set(activity.supporting_baseline_event_ids) <= baseline_ids:
        raise CounterfactualValidationError("activity context has unknown baseline event reference")
    if not set(activity.supporting_replay_event_ids) <= replay_ids:
        raise CounterfactualValidationError("activity context has unknown replay event reference")
    for text in [value.shift.statement, *(coverage.coverage_statement for coverage in value.target_coverages)]:
        lowered = text.casefold()
        if any(term in lowered for term in FORBIDDEN_CLAIM_TERMS):
            raise CounterfactualValidationError("generated coverage contains forbidden quality, correctness, or causality claim")
    if value.shift.status is ShiftStatus.TARGET_COVERAGE_INCREASED:
        if not any(coverage.replay_coverage_status in {ReplayCoverageStatus.PARTIALLY_OBSERVED, ReplayCoverageStatus.OBSERVED} for coverage in value.target_coverages):
            raise CounterfactualValidationError("increased shift lacks increased observable coverage")
        if not shift.supporting_replay_event_ids:
            raise CounterfactualValidationError("increased shift lacks supporting replay evidence")
    dimensions = {dimension.dimension: dimension for dimension in divergence.behavioral_dimensions}
    if activity.evaluation_breadth_status != dimensions[BehavioralDimension.EVALUATION_BREADTH].status:
        raise CounterfactualValidationError("activity context evaluation breadth disagrees with divergence")
    if activity.evidence_gathering_status != dimensions[BehavioralDimension.EVIDENCE_GATHERING].status:
        raise CounterfactualValidationError("activity context evidence gathering disagrees with divergence")
    expected_shift = recompute_shift(value.target_coverages, divergence)
    if shift.target_level_shifts != expected_shift.target_level_shifts or shift.status != expected_shift.status:
        raise CounterfactualValidationError("stored target-level or aggregate shift does not match recomputation")
    expected_activity = activity_volume_context(divergence, value.target_coverages, shift)
    if (activity.total_activity_status != expected_activity.total_activity_status
            or activity.target_coverage_relationship != expected_activity.target_coverage_relationship
            or activity.statement != expected_activity.statement):
        raise CounterfactualValidationError("activity relationship does not match recomputation")
    if value.policy_version != COUNTERFACTUAL_POLICY_VERSION:
        raise CounterfactualValidationError("unsupported counterfactual policy version")
    identity={"policy_version": COUNTERFACTUAL_POLICY_VERSION,"run":value.run_id,"replay":value.replay_id,"context":value.context_id,"assessment":value.assessment_id,"intervention":value.intervention_id,"divergence":value.divergence_id}
    expected_id="cov-"+sha256_bytes(canonical_json_bytes(identity))[:24]
    if value.coverage_id != expected_id or value.coverage_hash != canonical_coverage_hash(value):
        raise CounterfactualValidationError("coverage identity or canonical hash mismatch")

def validate_counterfactual_manifest(assessment, manifest, stage_directory, accepted_input_hashes) -> None:
    stage=Path(stage_directory)
    if manifest.policy_version != COUNTERFACTUAL_POLICY_VERSION or manifest.coverage_id != assessment.coverage_id or manifest.coverage_hash != assessment.coverage_hash:
        raise CounterfactualValidationError("counterfactual manifest identity mismatch")
    if manifest.input_file_hashes != accepted_input_hashes:
        raise CounterfactualValidationError("counterfactual manifest input hashes mismatch")
    expected_names={"counterfactual_coverage.json","counterfactual_coverage.md"}
    if set(manifest.output_file_hashes) != expected_names:
        raise CounterfactualValidationError("counterfactual manifest output set mismatch")
    for name, expected in manifest.output_file_hashes.items():
        if not (stage/name).is_file() or sha256_file(stage/name) != expected:
            raise CounterfactualValidationError(f"counterfactual output hash mismatch: {name}")
    if manifest.target_count != len(assessment.target_coverages) or manifest.shift_status != assessment.shift.status:
        raise CounterfactualValidationError("counterfactual manifest count or shift mismatch")
    expected_counts={status.value:sum(item.replay_coverage_status is status for item in assessment.target_coverages) for status in ReplayCoverageStatus}
    if manifest.coverage_status_counts != expected_counts:
        raise CounterfactualValidationError("counterfactual manifest status counts mismatch")
    parsed=CounterfactualCoverageAssessment.model_validate_json((stage/"counterfactual_coverage.json").read_text("utf-8"))
    if parsed != assessment or canonical_coverage_hash(parsed) != parsed.coverage_hash:
        raise CounterfactualValidationError("staged counterfactual JSON mismatch")
