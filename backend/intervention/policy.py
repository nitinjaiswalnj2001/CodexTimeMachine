"""Deterministic minimum-future-clue policy."""

from __future__ import annotations

from backend.assessment.models import OverallStatus, TargetVerdict, TemporalBlindSpotAssessment
from backend.evaluation.models import EvaluationContext, EvaluationTargetCategory

from .models import GeneratedIntervention, InterventionStatus, InterventionType


class DeterministicInterventionPolicy:
    name = "policy"
    version = "1.0.0"

    def generate(self, assessment: TemporalBlindSpotAssessment,
                 context: EvaluationContext) -> GeneratedIntervention:
        if assessment.overall_finding.status is not OverallStatus.BLIND_SPOT_IDENTIFIED:
            return GeneratedIntervention(status=InterventionStatus.NO_INTERVENTION,
                reason="The accepted assessment does not identify a sufficiently grounded blind spot.")
        candidates = [target for target in assessment.target_assessments
                      if target.verdict in {TargetVerdict.MISSED, TargetVerdict.PARTIALLY_SATISFIED}]
        if not candidates:
            return GeneratedIntervention(status=InterventionStatus.NO_INTERVENTION,
                reason="No grounded missed or partially satisfied target supports a safe clue.")
        target = candidates[0]
        if target.category in {EvaluationTargetCategory.MISSING_EXPERIMENT,
                               EvaluationTargetCategory.INSUFFICIENT_EVALUATION}:
            clue = ("Before selecting a production retrieval default, verify the recommendation "
                    "on a representative labeled query set covering multiple query types.")
            intervention_type = InterventionType.EVALUATION_REQUEST
        elif target.category is EvaluationTargetCategory.UNTESTED_ASSUMPTION:
            clue = "Before committing to the decision, verify the key assumption with a focused test."
            intervention_type = InterventionType.QUESTION
        elif target.verdict is TargetVerdict.PARTIALLY_SATISFIED:
            clue = "Before committing to the decision, complete the observed evaluation across a representative set of cases."
            intervention_type = InterventionType.EVALUATION_REQUEST
        elif target.category is EvaluationTargetCategory.MISSING_QUESTION:
            clue = "Before committing to the decision, check whether the evaluation covers the relevant operating conditions."
            intervention_type = InterventionType.QUESTION
        elif target.category is EvaluationTargetCategory.IGNORED_CONSTRAINT:
            clue = "Before committing to the decision, verify the recommendation against the relevant operating constraint."
            intervention_type = InterventionType.CONSTRAINT_REMINDER
        else:
            return GeneratedIntervention(status=InterventionStatus.NO_INTERVENTION,
                reason="The assessment category cannot be converted into a safe non-leaking clue by policy.")
        references = [f"target:{target.target_id}", "overall_finding"]
        references.extend(f"event:{ref.event_id}" for ref in target.observed_past_evidence)
        references.extend(f"evidence:{ref.evidence_id}" for ref in target.known_future_evidence)
        return GeneratedIntervention(status=InterventionStatus.INTERVENTION_GENERATED,
            target_id=target.target_id, intervention_type=intervention_type, clue=clue,
            rationale=("The clue names only the missing investigation dimension and withholds the "
                       "future result, preferred solution, metrics, and evaluator verdict."),
            supporting_assessment_references=references)
