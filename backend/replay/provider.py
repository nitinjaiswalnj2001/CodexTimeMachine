"""Replay execution provider boundary."""
from __future__ import annotations
from pathlib import Path
from typing import Protocol
from backend.runs.models import CodexExecutionConfiguration
from .models import ReplayExecutionMode
from .models import ReplayProviderResult

class ReplayProvider(Protocol):
    name: str
    version: str
    execution_mode: ReplayExecutionMode
    def execute(self, prompt: str, workspace: Path, configuration: CodexExecutionConfiguration | None,
                raw_events_path: Path, final_message_path: Path, stderr_path: Path,
                *, preflight: object | None = None) -> ReplayProviderResult: ...
