"""Provider boundary for fresh blind-spot evaluator executions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .models import EvaluatorInput


@dataclass(frozen=True)
class EvaluationConfiguration:
    model: str = "gpt-5.6-sol"
    reasoning_effort: str = "high"
    timeout_seconds: int = 1800


@dataclass(frozen=True)
class ProviderResult:
    raw_response: bytes
    thread_ids: tuple[str, ...]
    exit_code: int | None
    raw_events: bytes | None = None
    stderr: bytes = b""
    provider_version: str | None = None


class BlindSpotEvaluatorProvider(Protocol):
    name: str

    def evaluate(
        self,
        evaluator_input: EvaluatorInput,
        prompt: str,
        configuration: EvaluationConfiguration,
        working_directory: Path,
    ) -> ProviderResult: ...


class FakeEvaluatorProvider:
    name = "fake"

    def __init__(self, response: str | bytes, *, thread_ids: tuple[str, ...] = ("evaluator-thread-1",),
                 exit_code: int | None = 0, raw_events: bytes | None = None, stderr: bytes = b"") -> None:
        self.response = response.encode() if isinstance(response, str) else response
        self.thread_ids = thread_ids
        self.exit_code = exit_code
        self.raw_events = raw_events
        self.stderr = stderr

    def evaluate(self, evaluator_input: EvaluatorInput, prompt: str,
                 configuration: EvaluationConfiguration, working_directory: Path) -> ProviderResult:
        return ProviderResult(self.response, self.thread_ids, self.exit_code, self.raw_events, self.stderr, "deterministic-fake-1")
