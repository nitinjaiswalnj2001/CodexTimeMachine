"""Deterministic minimum-clue and evidence-reference validation."""

from __future__ import annotations

import re

from backend.assessment.models import TemporalBlindSpotAssessment
from backend.evaluation.models import EvaluationContext

from .models import GeneratedIntervention, InterventionStatus, LeakageCategory


class InterventionValidationError(RuntimeError):
    pass


MAX_CLUE_WORDS = 60
MAX_CLUE_SENTENCES = 2
CONSTRAINTS = [
    "maximum 2 sentences", "maximum 60 words", "one investigative action",
    "no future result", "no preferred solution", "no future metrics",
    "no assessment internals", "no replay-control language",
]

_METRICS = re.compile(r"\b(?:mrr|hit\s*@?\s*\d+|precision|recall|ndcg|f1|latency)\b|\b\d+(?:\.\d+)?%", re.IGNORECASE)
_SOLUTIONS = re.compile(r"\b(?:bm25|semantic retrieval|hybrid retrieval|cross[- ]encoder|reranker)\b", re.IGNORECASE)
_ASSESSMENT = re.compile(r"\b(?:blind[ -]spot|missed|partially_satisfied|satisfied|insufficient_evidence|evaluator verdict)\b", re.IGNORECASE)
_FUTURE = re.compile(r"\b(?:future evidence|future benchmark|future result|known outcome|synthetic benchmark|benchmark showed|results showed|correct answer|was wrong)\b", re.IGNORECASE)
_REPLAY = re.compile(r"\b(?:replay prompt|replay agent|ghost clue|ghost engineer|intervention text|counterfactual replay)\b", re.IGNORECASE)
_CODE = re.compile(r"```|`[^`]+`|\b(?:def|class|import|SELECT|function)\s+[A-Za-z_]", re.IGNORECASE)
_SHELL = re.compile(r"(?:^|\s)(?:pytest|python(?:3)?|git|rg|grep|sed|cat|curl|wget|powershell|bash|sh)\s", re.IGNORECASE)
_ACTIONS = re.compile(r"\b(?:verify|test|evaluate|inspect|compare|implement|use|choose|select|run|check|measure|benchmark)\b", re.IGNORECASE)


def clue_word_count(clue: str | None) -> int:
    return len(re.findall(r"\b[\w@'-]+\b", clue or ""))


def _fail(category: LeakageCategory, message: str) -> None:
    raise InterventionValidationError(f"{category}: {message}")


def validate_generated_intervention(generated: GeneratedIntervention,
                                    assessment: TemporalBlindSpotAssessment,
                                    context: EvaluationContext) -> list[str]:
    targets = {target.target_id: target for target in assessment.target_assessments}
    events = {ref.event_id for target in assessment.target_assessments for ref in target.observed_past_evidence}
    evidence = {ref.evidence_id for target in assessment.target_assessments for ref in target.known_future_evidence}
    valid_references = {"overall_finding", *(f"target:{value}" for value in targets),
                        *(f"event:{value}" for value in events), *(f"evidence:{value}" for value in evidence)}
    unknown = set(generated.supporting_assessment_references) - valid_references
    if unknown:
        raise InterventionValidationError(f"unknown supporting assessment references: {sorted(unknown)}")
    if generated.status is InterventionStatus.NO_INTERVENTION:
        return ["Lexical checks cannot prove complete semantic non-leakage."]
    if generated.target_id not in targets:
        raise InterventionValidationError(f"unknown intervention target: {generated.target_id}")
    clue = generated.clue or ""
    if clue_word_count(clue) > MAX_CLUE_WORDS:
        raise InterventionValidationError("clue exceeds 60 words")
    sentences = [value for value in re.split(r"[.!?]+", clue) if value.strip()]
    if len(sentences) > MAX_CLUE_SENTENCES:
        raise InterventionValidationError("clue exceeds two sentences")
    if _CODE.search(clue):
        raise InterventionValidationError("clue contains code")
    if _SHELL.search(clue):
        raise InterventionValidationError("clue contains a shell command")
    if len(_ACTIONS.findall(clue)) > 1:
        raise InterventionValidationError("clue contains more than one investigative action")
    if _METRICS.search(clue) or re.search(r"\b\d+\.\d+\b", clue):
        _fail(LeakageCategory.METRIC_LEAK, "clue contains metric language or values")
    if _SOLUTIONS.search(clue):
        _fail(LeakageCategory.SOLUTION_LEAK, "clue names a candidate technical solution")
    if _ASSESSMENT.search(clue):
        _fail(LeakageCategory.ASSESSMENT_LEAK, "clue exposes assessment status or terminology")
    if _FUTURE.search(clue):
        _fail(LeakageCategory.FUTURE_RESULT_LEAK, "clue exposes future-result language")
    if _REPLAY.search(clue):
        _fail(LeakageCategory.REPLAY_CONTROL_LEAK, "clue contains replay-control language")
    identities = [*targets, *evidence, *events, assessment.assessment_id, assessment.context_id]
    if any(value and value.casefold() in clue.casefold() for value in identities):
        _fail(LeakageCategory.IDENTITY_LEAK, "clue contains evaluator-only identifiers")
    if context.known_future_outcome.casefold() in clue.casefold():
        _fail(LeakageCategory.FUTURE_RESULT_LEAK, "clue reproduces the known future outcome")
    return ["Lexical checks are conservative and cannot prove complete semantic non-leakage."]
