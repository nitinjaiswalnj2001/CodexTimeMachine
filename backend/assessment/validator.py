"""Strict assessment parsing and evidence-grounding validation."""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from backend.evaluation.models import EvaluationContext

from .models import EvaluatorOutput, OverallStatus, TargetVerdict


class AssessmentValidationError(RuntimeError):
    pass


_FENCE = re.compile(r"\A\s*```(?:json)?\s*\n(?P<body>\{.*\})\s*\n```\s*\Z", re.DOTALL | re.IGNORECASE)
_INSTRUCTION = re.compile(r"(?:^|[.!?]\s+)(run|try|use|inspect|tell|ask|provide)\b", re.IGNORECASE)
_FORBIDDEN_GUIDANCE = ("replay prompt", "ghost clue", "minimum future clue", "replay agent", "intervention text")


def parse_evaluator_output(raw: bytes | str) -> EvaluatorOutput:
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    match = _FENCE.fullmatch(text)
    if match:
        text = match.group("body")
    try:
        value = json.loads(text)
        return EvaluatorOutput.model_validate(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise AssessmentValidationError(f"invalid structured evaluator output: {exc}") from exc


def _reject_guidance(text: str | None) -> None:
    if not text:
        return
    lowered = text.casefold()
    if any(term in lowered for term in _FORBIDDEN_GUIDANCE) or _INSTRUCTION.search(text):
        raise AssessmentValidationError("assessment contains instruction-like replay guidance")


def validate_grounding(output: EvaluatorOutput, context: EvaluationContext) -> list[str]:
    targets = {target.target_id: target for target in context.evaluation_targets}
    events = {event.event_id: event for event in context.past_observable_evidence}
    evidence = {item.evidence_id: item for item in context.known_future_evidence}
    assessed_ids = [item.target_id for item in output.target_assessments]
    if len(assessed_ids) != len(set(assessed_ids)):
        raise AssessmentValidationError("duplicate target assessment")
    if set(assessed_ids) != set(targets):
        missing = sorted(set(targets) - set(assessed_ids)); unknown = sorted(set(assessed_ids) - set(targets))
        raise AssessmentValidationError(f"target assessments do not match context; missing={missing}, unknown={unknown}")
    for item in output.target_assessments:
        target = targets[item.target_id]
        if item.category != target.category:
            raise AssessmentValidationError(f"target category mismatch: {item.target_id}")
        past_ids: set[str] = set()
        for ref in item.observed_past_evidence:
            event = events.get(ref.event_id)
            if event is None:
                raise AssessmentValidationError(f"unknown past event reference: {ref.event_id}")
            if ref.sequence != event.sequence or ref.event_type != event.event_type:
                raise AssessmentValidationError(f"past event provenance mismatch: {ref.event_id}")
            past_ids.add(ref.event_id)
        future_ids = {ref.evidence_id for ref in item.known_future_evidence}
        unknown_future = future_ids - set(evidence)
        if unknown_future:
            raise AssessmentValidationError(f"unknown future evidence reference: {sorted(unknown_future)}")
        if item.verdict in {TargetVerdict.MISSED, TargetVerdict.PARTIALLY_SATISFIED}:
            if not past_ids or not future_ids:
                raise AssessmentValidationError(f"{item.verdict} target requires past and future evidence: {item.target_id}")
            if not (item.missing_investigation or "").strip():
                raise AssessmentValidationError(f"{item.verdict} target requires missing_investigation: {item.target_id}")
            if not item.limitations:
                raise AssessmentValidationError(f"{item.verdict} target requires limitations: {item.target_id}")
        elif item.verdict is TargetVerdict.SATISFIED and not past_ids:
            raise AssessmentValidationError(f"SATISFIED target requires observable success evidence: {item.target_id}")
        elif item.verdict is TargetVerdict.INSUFFICIENT_EVIDENCE and not item.limitations:
            raise AssessmentValidationError(f"INSUFFICIENT_EVIDENCE target requires an explanation: {item.target_id}")
        _reject_guidance(item.missing_investigation)
        _reject_guidance(item.summary)

    overall = output.overall_finding
    unknown_targets = set(overall.supporting_target_ids) - set(targets)
    unknown_events = set(overall.supporting_past_event_ids) - set(events)
    unknown_evidence = set(overall.supporting_future_evidence_ids) - set(evidence)
    if unknown_targets or unknown_events or unknown_evidence:
        raise AssessmentValidationError("overall finding contains unknown evidence references")
    if overall.status is OverallStatus.BLIND_SPOT_IDENTIFIED:
        supporting = [item for item in output.target_assessments if item.target_id in overall.supporting_target_ids]
        if not any(item.verdict in {TargetVerdict.MISSED, TargetVerdict.PARTIALLY_SATISFIED} for item in supporting):
            raise AssessmentValidationError("BLIND_SPOT_IDENTIFIED requires a missed or partial target")
        if not overall.supporting_past_event_ids or not overall.supporting_future_evidence_ids:
            raise AssessmentValidationError("BLIND_SPOT_IDENTIFIED requires past and future evidence")
        if not any((item.missing_investigation or "").strip() for item in supporting):
            raise AssessmentValidationError("blind-spot statement lacks an absent or insufficient investigation")
    _reject_guidance(overall.statement)
    return ["Natural-language entailment is conservatively reference-validated; full semantic proof is not claimed."]
