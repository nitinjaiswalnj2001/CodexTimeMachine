"""Fresh, isolated Codex CLI provider for reference assessments."""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

from backend.runs.codex_cli import CodexCLIAdapter, _filesystem_override

from .models import EvaluatorInput
from .provider import EvaluationConfiguration, ProviderResult


class CodexEvaluatorProviderError(RuntimeError):
    pass


class CodexEvaluatorProvider:
    name = "codex"

    def __init__(self, executable: str = "codex") -> None:
        self.adapter = CodexCLIAdapter(executable)

    def evaluate(self, evaluator_input: EvaluatorInput, prompt: str,
                 configuration: EvaluationConfiguration, working_directory: Path) -> ProviderResult:
        try:
            preflight = self.adapter.preflight()
        except Exception as exc:
            raise CodexEvaluatorProviderError(f"provider preflight failed: {exc}") from exc
        response_path = working_directory / f".provider-last-message-{uuid.uuid4().hex}.txt"
        overrides = [
            'default_permissions="ctm_evaluator"',
            'permissions.ctm_evaluator.description="Codex Time Machine evidence-only evaluator."',
            _filesystem_override(preflight.runtime_read_paths).replace("permissions.ctm_temporal", "permissions.ctm_evaluator"),
            "permissions.ctm_evaluator.network.enabled=false",
            f'model_reasoning_effort="{configuration.reasoning_effort}"',
            'web_search="disabled"', "tools.web_search=false", 'personality="none"',
            "project_doc_max_bytes=0", "features.hooks=false", "features.memories=false",
            "features.remote_plugin=false", "features.multi_agent=false", "features.goals=false",
            "features.shell_snapshot=false", "features.skill_mcp_dependency_install=false",
            'shell_environment_policy.inherit="core"',
            'shell_environment_policy.include_only=["PATH","HOME","USERPROFILE","SYSTEMROOT","WINDIR","TEMP","TMP","TMPDIR","COMSPEC","PATHEXT"]',
            "shell_environment_policy.ignore_default_excludes=false",
            "shell_environment_policy.experimental_use_profile=false", "allow_login_shell=false",
        ]
        config_args = [part for override in overrides for part in ("-c", override)]
        command = [preflight.executable, "--ask-for-approval", "never", "exec", "--json",
                   "--strict-config", "--ephemeral", "--skip-git-repo-check",
                   "--ignore-user-config", "--ignore-rules", "--model", configuration.model,
                   *config_args, "--cd", str(working_directory.resolve()),
                   "--output-last-message", str(response_path.resolve()), prompt]
        try:
            process = subprocess.run(command, cwd=working_directory, capture_output=True,
                                     check=False, shell=False, timeout=configuration.timeout_seconds)
            raw_events, stderr, exit_code = process.stdout, process.stderr, process.returncode
        except subprocess.TimeoutExpired as exc:
            raw_events = exc.stdout or b""; stderr = exc.stderr or b""; exit_code = None
        except OSError as exc:
            raise CodexEvaluatorProviderError(f"fresh evaluator process failed: {exc}") from exc
        thread_ids: list[str] = []
        for line in raw_events.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # Preserve raw stdout for runner-owned evidence publication;
                # the runner performs the fail-closed JSONL validation.
                continue
            if event.get("type") == "thread.started":
                value = event.get("thread_id") or event.get("threadId")
                if isinstance(value, str) and value.strip():
                    thread_ids.append(value.strip())
                else:
                    thread_ids.append("")
        try:
            response = response_path.read_bytes() if response_path.is_file() else b""
        finally:
            if response_path.exists(): response_path.unlink()
        return ProviderResult(response, tuple(thread_ids), exit_code, raw_events, stderr, preflight.version)
