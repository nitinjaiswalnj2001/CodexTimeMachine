"""Deterministic provider boundary for intervention generation."""

from __future__ import annotations

from typing import Protocol

from backend.assessment.models import TemporalBlindSpotAssessment
from backend.evaluation.models import EvaluationContext

from .models import GeneratedIntervention


class InterventionProvider(Protocol):
    name: str
    version: str
    def generate(self, assessment: TemporalBlindSpotAssessment,
                 context: EvaluationContext) -> GeneratedIntervention: ...


class FakeInterventionProvider:
    name = "fake"
    version = "1.0.0"

    def __init__(self, result: GeneratedIntervention) -> None:
        self.result = result

    def generate(self, assessment: TemporalBlindSpotAssessment,
                 context: EvaluationContext) -> GeneratedIntervention:
        return self.result.model_copy(deep=True)
