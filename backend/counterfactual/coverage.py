"""Target-specific, observable-only deterministic coverage policies."""
from __future__ import annotations

from backend.assessment.models import TargetVerdict
from backend.divergence.models import BehavioralDimension, DimensionStatus
from backend.evaluation.models import EvaluationTargetCategory
from backend.trajectory.models import ObservableEventStatus, ObservableEventType

from .models import ActivityVolumeContext, CounterfactualShift, CounterfactualTargetCoverage, CoverageProof, ReplayCoverageStatus, ShiftStatus, TargetCoverageRelationship, TargetLevelShift, TargetShiftDirection, TotalActivityStatus


# ``t1`` is the stable identifier in the already accepted sanitized controlled lineage;
# the descriptive identifier is used by the LegalRAG packet.  Both are explicit registry
# keys, never a category-wide fallback.
REPRESENTATIVE_EVALUATION_TARGETS = frozenset({"representative-labeled-evaluation", "t1"})
REPRESENTATIVE_CATEGORIES = (
    "lexical overlap", "paraphrased", "synonym-heavy", "long-form legal", "exact-term",
)
COVERAGE_LIMITATION = (
    "Target coverage describes whether a required investigation was observably attempted. "
    "It does not establish the quality, validity or correctness of that investigation."
)


def _event_text(event: object) -> str:
    return " ".join(
        part for part in (event.summary, event.command, event.output_preview) if part
    ).casefold()


def _baseline_assessment(target: object, assessment: object) -> object:
    return next(item for item in assessment.target_assessments if item.target_id == target.target_id)


def _unique_event_ids(*groups: list[object]) -> list[str]:
    return list(dict.fromkeys(event.event_id for group in groups for event in group))


def _partial_status(observed_parts: int) -> ReplayCoverageStatus:
    return ReplayCoverageStatus.PARTIALLY_OBSERVED if observed_parts else ReplayCoverageStatus.NOT_OBSERVED


def _representative_evaluation_policy(target: object, assessment: object, trajectory: object) -> CounterfactualTargetCoverage:
    """Require the declared representative evaluation stages in observable order."""
    baseline = _baseline_assessment(target, assessment)
    events = trajectory.events
    fixtures = [
        event for event in events
        if any("representative_retrieval" in path.casefold() for path in event.workspace_relative_paths)
    ]
    category_events = [
        event for event in events
        if all(category in _event_text(event) for category in REPRESENTATIVE_CATEGORIES)
    ]
    relevant_commands = [
        event for event in events
        if event.event_type is ObservableEventType.COMMAND_EXECUTED
        and event.command
        and ("representative" in event.command.casefold() or "test_representative" in event.command.casefold())
    ]
    successful_commands = [
        event for event in relevant_commands
        if event.status is ObservableEventStatus.SUCCEEDED
        and event.exit_code == 0
        and bool(event.output_preview)
    ]
    summaries = [
        event for event in events
        if event.event_type is not ObservableEventType.COMMAND_EXECUTED
        if "representative" in _event_text(event)
        and ("evaluated" in _event_text(event) or "evaluation" in _event_text(event))
    ]
    decisions = [
        event for event in events
        if "default" in _event_text(event)
        and any(term in _event_text(event) for term in ("unchanged", "recommend", "decision", "defer"))
    ]
    fixture_sequence = min((event.sequence for event in fixtures), default=None)
    category_sequence = min((event.sequence for event in category_events), default=None)
    command_sequence = min((event.sequence for event in successful_commands), default=None)
    summary_sequence = min((event.sequence for event in summaries), default=None)
    decision_sequence = min((event.sequence for event in decisions), default=None)
    ordered = (
        all(sequence is not None for sequence in (
            fixture_sequence, category_sequence, command_sequence, summary_sequence, decision_sequence,
        ))
        and fixture_sequence < command_sequence
        and category_sequence <= command_sequence
        and command_sequence < summary_sequence <= decision_sequence
    )
    stages = (bool(fixtures), bool(category_events), bool(successful_commands), bool(summaries), bool(decisions), ordered)
    status = ReplayCoverageStatus.OBSERVED if all(stages) else _partial_status(sum(stages))
    statement = (
        "Observable representative-query preparation, multiple categories, successful evaluation execution, "
        "post-evaluation evidence, and a later production decision were present."
        if status is ReplayCoverageStatus.OBSERVED
        else "Only part of the required ordered representative labeled evaluation was observable."
    )
    return CounterfactualTargetCoverage(
        target_id=target.target_id, category=target.category, baseline_verdict=baseline.verdict,
        replay_coverage_status=status,
        baseline_evidence=[item.event_id for item in baseline.observed_past_evidence],
        replay_evidence=_unique_event_ids(fixtures, category_events, relevant_commands, summaries, decisions),
        coverage_statement=statement,
        remaining_uncertainty=["A failed, incomplete, or unobserved evaluation cannot establish full target coverage."],
        confidence=0.9 if status is ReplayCoverageStatus.OBSERVED else 0.6,
        limitations=[COVERAGE_LIMITATION],
        coverage_proof=CoverageProof(policy_id="representative-labeled-evaluation-v1", observed_stages=[name for name,present in zip(("fixture","categories","successful_evaluation","summary","decision","ordered"),stages) if present], missing_stages=[name for name,present in zip(("fixture","categories","successful_evaluation","summary","decision","ordered"),stages) if not present], fixture_sequence=fixture_sequence,evaluation_sequence=command_sequence,summary_sequence=summary_sequence,decision_sequence=decision_sequence,successful_command_event_ids=[event.event_id for event in successful_commands]),
    )


def _unregistered_policy(target: object, assessment: object) -> CounterfactualTargetCoverage:
    """Conservative category fallback: never infer coverage without target-specific rules."""
    baseline = _baseline_assessment(target, assessment)
    requirements = {
        EvaluationTargetCategory.MISSING_EXPERIMENT: "observable setup, successful execution, and result evidence",
        EvaluationTargetCategory.MISSING_QUESTION: "a target-relevant observable question before the decision",
        EvaluationTargetCategory.UNTESTED_ASSUMPTION: "an observable assumption test and result before the decision",
        EvaluationTargetCategory.IGNORED_CONSTRAINT: "observable acknowledgement and action on the target constraint",
        EvaluationTargetCategory.INSUFFICIENT_EVALUATION: "a registered target-specific evaluation policy",
        EvaluationTargetCategory.DECISION_REVERSAL: "prior and later observable decisions with intervening investigation",
    }
    return CounterfactualTargetCoverage(
        target_id=target.target_id, category=target.category, baseline_verdict=baseline.verdict,
        replay_coverage_status=ReplayCoverageStatus.INSUFFICIENT_EVIDENCE,
        baseline_evidence=[item.event_id for item in baseline.observed_past_evidence], replay_evidence=[],
        coverage_statement="No deterministic coverage policy is registered for this target.",
        remaining_uncertainty=[f"This category would require {requirements[target.category]}."],
        confidence=0.2, limitations=[COVERAGE_LIMITATION],
        coverage_proof=CoverageProof(policy_id="unregistered",observed_stages=[],missing_stages=["registered_target_policy"]),
    )


def target_coverages(context: object, assessment: object, trajectory: object) -> list[CounterfactualTargetCoverage]:
    coverages = []
    for target in context.evaluation_targets:
        compatible = target.category in {
            EvaluationTargetCategory.MISSING_EXPERIMENT,
            EvaluationTargetCategory.INSUFFICIENT_EVALUATION,
        }
        if target.target_id in REPRESENTATIVE_EVALUATION_TARGETS and compatible:
            coverages.append(_representative_evaluation_policy(target, assessment, trajectory))
        else:
            coverages.append(_unregistered_policy(target, assessment))
    return coverages


def activity_volume_context(divergence: object, coverages: list[CounterfactualTargetCoverage], value_shift: CounterfactualShift | None = None) -> ActivityVolumeContext:
    dimensions = {item.dimension: item for item in divergence.behavioral_dimensions}
    evaluation = dimensions[BehavioralDimension.EVALUATION_BREADTH]
    evidence = dimensions[BehavioralDimension.EVIDENCE_GATHERING]
    if evaluation.status is DimensionStatus.LESS_OBSERVED_IN_REPLAY and evidence.status is DimensionStatus.LESS_OBSERVED_IN_REPLAY:
        total=TotalActivityStatus.LESS_TOTAL_ACTIVITY
    elif evaluation.status is DimensionStatus.MORE_OBSERVED_IN_REPLAY and evidence.status is DimensionStatus.MORE_OBSERVED_IN_REPLAY:
        total=TotalActivityStatus.MORE_TOTAL_ACTIVITY
    elif evaluation.status is DimensionStatus.UNCHANGED and evidence.status is DimensionStatus.UNCHANGED:
        total=TotalActivityStatus.UNCHANGED_TOTAL_ACTIVITY
    elif DimensionStatus.INSUFFICIENT_EVIDENCE in {evaluation.status,evidence.status}:
        total=TotalActivityStatus.INSUFFICIENT_ACTIVITY_EVIDENCE
    else:
        total=TotalActivityStatus.DIFFERENT_TOTAL_ACTIVITY
    target={ShiftStatus.TARGET_COVERAGE_INCREASED:TargetCoverageRelationship.INCREASED_TARGET_COVERAGE,ShiftStatus.TARGET_COVERAGE_UNCHANGED:TargetCoverageRelationship.UNCHANGED_TARGET_COVERAGE,ShiftStatus.TARGET_COVERAGE_DECREASED:TargetCoverageRelationship.DECREASED_TARGET_COVERAGE}.get(value_shift.status if value_shift else ShiftStatus.INSUFFICIENT_EVIDENCE,TargetCoverageRelationship.INSUFFICIENT_TARGET_EVIDENCE)
    if total is TotalActivityStatus.LESS_TOTAL_ACTIVITY and target is TargetCoverageRelationship.INCREASED_TARGET_COVERAGE:
        statement = (
            "The replay contained less total observed evaluation and evidence-gathering activity than the baseline, "
            "while increasing coverage of the specific representative-evaluation target."
        )
    else:
        statement = f"Total observed activity is {total.value}; target coverage is {target.value}."
    return ActivityVolumeContext(
        total_activity_status=total,target_coverage_relationship=target,evaluation_breadth_status=evaluation.status, evidence_gathering_status=evidence.status,
        statement=statement,
        supporting_baseline_event_ids=list(dict.fromkeys(evaluation.baseline_evidence + evidence.baseline_evidence)),
        supporting_replay_event_ids=list(dict.fromkeys(evaluation.replay_evidence + evidence.replay_evidence)),
        limitations=[COVERAGE_LIMITATION],
    )


BASELINE_LEVEL = {TargetVerdict.MISSED: 0, TargetVerdict.PARTIALLY_SATISFIED: 1, TargetVerdict.SATISFIED: 2}
REPLAY_LEVEL = {ReplayCoverageStatus.NOT_OBSERVED: 0, ReplayCoverageStatus.PARTIALLY_OBSERVED: 1, ReplayCoverageStatus.OBSERVED: 2}


def shift(coverages: list[CounterfactualTargetCoverage], divergence: object) -> CounterfactualShift:
    known = [item for item in coverages if item.baseline_verdict in BASELINE_LEVEL and item.replay_coverage_status in REPLAY_LEVEL]
    if len(known) != len(coverages):
        return CounterfactualShift(
            status=ShiftStatus.INSUFFICIENT_EVIDENCE,
            statement="Coverage levels could not be compared deterministically for every evaluation target.",
            supporting_target_ids=[], supporting_baseline_event_ids=[], supporting_replay_event_ids=[],
            supporting_difference_ids=[], confidence=0.3, limitations=[COVERAGE_LIMITATION], target_level_shifts=[TargetLevelShift(target_id=item.target_id,baseline_level=BASELINE_LEVEL.get(item.baseline_verdict),replay_level=REPLAY_LEVEL.get(item.replay_coverage_status),direction=TargetShiftDirection.INSUFFICIENT_EVIDENCE) for item in coverages],
        )
    deltas = {item.target_id: REPLAY_LEVEL[item.replay_coverage_status] - BASELINE_LEVEL[item.baseline_verdict] for item in known}
    directions=[TargetLevelShift(target_id=item.target_id,baseline_level=BASELINE_LEVEL[item.baseline_verdict],replay_level=REPLAY_LEVEL[item.replay_coverage_status],direction=TargetShiftDirection.INCREASED if deltas[item.target_id]>0 else TargetShiftDirection.DECREASED if deltas[item.target_id]<0 else TargetShiftDirection.UNCHANGED) for item in known]
    if any(delta > 0 for delta in deltas.values()) and any(delta < 0 for delta in deltas.values()):
        return CounterfactualShift(status=ShiftStatus.INSUFFICIENT_EVIDENCE,statement="Target-level coverage changes are mixed and cannot be summarized as a single direction.",supporting_target_ids=[item.target_id for item in known],supporting_baseline_event_ids=[event for item in known for event in item.baseline_evidence],supporting_replay_event_ids=[event for item in known for event in item.replay_evidence],supporting_difference_ids=[],confidence=.7,limitations=[COVERAGE_LIMITATION],target_level_shifts=directions)
    if any(delta > 0 for delta in deltas.values()):
        status = ShiftStatus.TARGET_COVERAGE_INCREASED
        selected = [item for item in known if deltas[item.target_id] > 0]
    elif any(delta < 0 for delta in deltas.values()):
        status = ShiftStatus.TARGET_COVERAGE_DECREASED
        selected = [item for item in known if deltas[item.target_id] < 0]
    else:
        status = ShiftStatus.TARGET_COVERAGE_UNCHANGED
        selected = known
    baseline_ids = list(dict.fromkeys(event_id for item in selected for event_id in item.baseline_evidence))
    replay_ids = list(dict.fromkeys(event_id for item in selected for event_id in item.replay_evidence))
    difference_ids = [
        difference.difference_id for difference in divergence.event_differences
        if set(difference.replay_event_ids).intersection(replay_ids)
    ]
    wording = {
        ShiftStatus.TARGET_COVERAGE_INCREASED: "Observable target coverage increased from the baseline verdict to the replay coverage status.",
        ShiftStatus.TARGET_COVERAGE_DECREASED: "Observable target coverage decreased relative to the baseline verdict.",
        ShiftStatus.TARGET_COVERAGE_UNCHANGED: "Observable target coverage was unchanged relative to the baseline verdict.",
    }
    return CounterfactualShift(
        status=status, statement=wording[status], supporting_target_ids=[item.target_id for item in selected],
        supporting_baseline_event_ids=baseline_ids, supporting_replay_event_ids=replay_ids,
        supporting_difference_ids=difference_ids, confidence=0.85,
        limitations=[COVERAGE_LIMITATION, "This does not establish technical correctness, performance improvement, or causality."], target_level_shifts=directions,
    )
