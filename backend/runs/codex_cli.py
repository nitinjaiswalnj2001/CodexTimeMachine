"""Explicit, fail-closed adapter for fresh non-resumed Codex CLI execution."""

from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
import sys
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from backend.temporal.integrity import canonical_json_bytes, sha256_bytes

from .models import CodexExecutionConfiguration


class CodexPreflightError(RuntimeError):
    """Raised when the local Codex CLI cannot enforce the run configuration."""


@dataclass(frozen=True)
class CodexPreflightResult:
    executable: str
    version: str | None
    help_text: str
    version_tuple: tuple[int, int, int]
    sandbox_platform: str
    runtime_read_paths: tuple[str, ...] = ()
    runtime_read_paths_hash: str = ""
    probe_interpreter: str = ""
    effective_sandbox_path: str | None = None


@dataclass(frozen=True)
class CodexProcessResult:
    exit_code: int | None
    timed_out: bool = False
    failure_reason: str | None = None


ROOT_CAPABILITY_GROUPS: tuple[tuple[str, ...], ...] = (
    ("exec",),
    ("--ask-for-approval",),
)

EXEC_CAPABILITY_GROUPS: tuple[tuple[str, ...], ...] = (
    ("--json",),
    ("--ephemeral",),
    ("--skip-git-repo-check",),
    ("--ignore-user-config",),
    ("--ignore-rules",),
    ("--model",),
    ("--config", "-c"),
    ("--cd", "-C"),
    ("--output-last-message",),
    ("--strict-config",),
)

SANDBOX_CAPABILITY_GROUPS: tuple[tuple[str, ...], ...] = (
    ("--permission-profile",),
    ("--include-managed-config",),
    ("--config", "-c"),
    ("--cd", "-C"),
)
MINIMUM_PERMISSION_PROFILE_VERSION = (0, 138, 0)
PERMISSION_PROFILE_NAME = "ctm_temporal"
PERMISSION_PROFILE_DEFINITION = {
    "default_permissions": PERMISSION_PROFILE_NAME,
    "permissions": {
        PERMISSION_PROFILE_NAME: {
            "description": "Codex Time Machine temporally bounded project workspace.",
            "filesystem": {
                ":root": "deny",
                ":minimal": "read",
                ":workspace_roots": {".": "write"},
            },
            "network": {"enabled": False},
        }
    },
}


def _normalized_runtime_read_paths(runtime_read_paths: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({str(Path(path).resolve()) for path in runtime_read_paths}))


def build_permission_profile_definition(runtime_read_paths: Sequence[str]) -> dict:
    """Build the precise profile declared in an execution artifact."""
    definition = deepcopy(PERMISSION_PROFILE_DEFINITION)
    filesystem = definition["permissions"][PERMISSION_PROFILE_NAME]["filesystem"]
    for runtime_path in _normalized_runtime_read_paths(runtime_read_paths):
        filesystem[runtime_path] = "read"
    return definition


def permission_profile_hash(runtime_read_paths: Sequence[str]) -> str:
    return sha256_bytes(
        canonical_json_bytes(build_permission_profile_definition(runtime_read_paths))
    )


PERMISSION_PROFILE_HASH = permission_profile_hash(())
LINUX_SANDBOX_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def parse_codex_semantic_version(version_text: str) -> tuple[int, int, int]:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", version_text)
    if match is None:
        raise CodexPreflightError(
            "Codex CLI version could not be established; permission profiles require 0.138.0 or newer"
        )
    return tuple(int(part) for part in match.groups())


def sandbox_platform_subcommand() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _is_unsafe_runtime_root(path: Path) -> bool:
    home = Path.home().resolve()
    return path in {Path(path.anchor), home, home / ".codex"}


def derive_runtime_read_paths(resolved_executable: str | Path) -> tuple[str, ...]:
    """Return only the release subtree required to execute a resolved Codex binary."""
    executable = Path(resolved_executable).resolve()
    if not executable.is_file():
        raise CodexPreflightError(f"resolved Codex executable is not a regular file: {executable}")
    if executable.stem == "codex" and executable.parent.name == "bin":
        release_root = executable.parent.parent
        if (
            release_root.parent.name == "releases"
            and release_root.parent.parent.name == "standalone"
        ):
            return _normalized_runtime_read_paths((str(release_root),))
    fallback_root = executable.parent
    if _is_unsafe_runtime_root(fallback_root):
        raise CodexPreflightError(
            f"cannot derive a narrow safe Codex runtime read path from: {executable}"
        )
    return _normalized_runtime_read_paths((str(fallback_root),))


def select_probe_interpreter(platform: str) -> str:
    if platform == "linux":
        for candidate in (Path("/usr/bin/python3"), Path("/usr/local/bin/python3"), Path("/bin/python3")):
            if candidate.exists() and candidate.is_file():
                resolved = candidate.resolve()
                if not any(part == ".venv" for part in resolved.parts):
                    return str(resolved)
        raise CodexPreflightError(
            "no safe system python3 interpreter is available for the Linux isolation probe"
        )
    interpreter = Path(sys.executable).resolve()
    if not interpreter.is_file():
        raise CodexPreflightError("no safe probe interpreter is available")
    return str(interpreter)


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _filesystem_override(runtime_read_paths: Sequence[str]) -> str:
    entries = ['":root"="deny"', '":minimal"="read"']
    entries.extend(
        f"{_toml_string(path)}=\"read\""
        for path in _normalized_runtime_read_paths(runtime_read_paths)
    )
    entries.append('":workspace_roots"={"."="write"}')
    return "permissions.ctm_temporal.filesystem={" + ",".join(entries) + "}"


def _config_args(
    configuration: CodexExecutionConfiguration,
    runtime_read_paths: Sequence[str],
    effective_sandbox_path: str | None,
) -> list[str]:
    include_only = json.dumps(configuration.shell_environment_policy.include_only)
    overrides = [
        f'default_permissions="{configuration.permission_profile}"',
        'permissions.ctm_temporal.description="Codex Time Machine temporally bounded project workspace."',
        _filesystem_override(runtime_read_paths),
        "permissions.ctm_temporal.network.enabled=false",
        f'model_reasoning_effort="{configuration.reasoning_effort}"',
        f'web_search="{configuration.web_search_mode}"',
        "tools.web_search=false",
        'personality="none"',
        "project_doc_max_bytes=0",
        "features.hooks=false",
        "features.memories=false",
        "features.remote_plugin=false",
        "features.shell_snapshot=false",
        "features.multi_agent=false",
        "features.goals=false",
        "features.skill_mcp_dependency_install=false",
        "allow_login_shell=false",
        f'shell_environment_policy.inherit="{configuration.shell_environment_policy.inherit}"',
        f"shell_environment_policy.include_only={include_only}",
        "shell_environment_policy.ignore_default_excludes=false",
        "shell_environment_policy.experimental_use_profile=false",
    ]
    if effective_sandbox_path is not None:
        overrides.append(
            f"shell_environment_policy.set.PATH={_toml_string(effective_sandbox_path)}"
        )
    return [part for override in overrides for part in ("-c", override)]


class CodexCLIAdapter:
    def __init__(
        self,
        executable: str = "codex",
        executable_prefix_args: Sequence[str] = (),
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.executable = executable
        self.executable_prefix_args = tuple(executable_prefix_args)
        self.environment = dict(environment) if environment is not None else None

    def resolve_executable(self) -> str:
        resolved = shutil.which(self.executable)
        if resolved is None:
            raise CodexPreflightError(f"Codex executable not found: {self.executable}")
        return str(Path(resolved).resolve())

    def _base_command(self, resolved: str) -> list[str]:
        return [resolved, *self.executable_prefix_args]

    def _environment(self) -> dict[str, str] | None:
        if self.environment is None:
            return None
        return {**os.environ, **self.environment}

    def _preflight_command(
        self,
        command: Sequence[str],
        category: str,
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                list(command),
                capture_output=True,
                check=False,
                shell=False,
                env=self._environment(),
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise CodexPreflightError(
                f"Codex preflight {category} timed out after {timeout_seconds} seconds"
            ) from exc
        except OSError as exc:
            raise CodexPreflightError(
                f"Codex preflight {category} could not execute: {exc}"
            ) from exc

    def preflight(self, timeout_seconds: int = 15) -> CodexPreflightResult:
        resolved = self.resolve_executable()
        platform = sandbox_platform_subcommand()
        runtime_read_paths = derive_runtime_read_paths(resolved)
        probe_interpreter = select_probe_interpreter(platform)
        effective_sandbox_path = LINUX_SANDBOX_PATH if platform == "linux" else None
        base = self._base_command(resolved)
        version_process = self._preflight_command(
            [*base, "--version"], "version", timeout_seconds
        )
        if version_process.returncode != 0:
            raise CodexPreflightError(
                f"Codex --version failed with exit code {version_process.returncode}"
            )
        version_text = (version_process.stdout + version_process.stderr).decode(
            "utf-8", errors="replace"
        ).strip()
        version_tuple = parse_codex_semantic_version(version_text)
        if version_tuple < MINIMUM_PERMISSION_PROFILE_VERSION:
            raise CodexPreflightError(
                "Codex CLI 0.138.0 or newer is required for permission-profile temporal runs; "
                f"found {version_text}"
            )

        root_help_process = self._preflight_command(
            [*base, "--help"], "root help", timeout_seconds
        )
        if root_help_process.returncode != 0:
            raise CodexPreflightError(
                f"Codex --help failed with exit code {root_help_process.returncode}"
            )
        help_process = self._preflight_command(
            [*base, "exec", "--help"], "exec help", timeout_seconds
        )
        if help_process.returncode != 0:
            raise CodexPreflightError(
                f"Codex exec --help failed with exit code {help_process.returncode}"
            )
        sandbox_help_process = self._preflight_command(
            [*base, "sandbox", "--help"], "sandbox help", timeout_seconds
        )
        if sandbox_help_process.returncode != 0:
            raise CodexPreflightError(
                "Codex sandbox command is unavailable for temporal runs"
            )
        root_help_text = (root_help_process.stdout + root_help_process.stderr).decode(
            "utf-8", errors="replace"
        )
        exec_help_text = (help_process.stdout + help_process.stderr).decode(
            "utf-8", errors="replace"
        )
        missing_root = [
            " / ".join(group)
            for group in ROOT_CAPABILITY_GROUPS
            if not any(capability in root_help_text for capability in group)
        ]
        if missing_root:
            raise CodexPreflightError(
                "Codex CLI lacks required global capabilities: " + ", ".join(missing_root)
            )
        missing_exec = [
            " / ".join(group)
            for group in EXEC_CAPABILITY_GROUPS
            if not any(capability in exec_help_text for capability in group)
        ]
        if missing_exec:
            raise CodexPreflightError(
                "Codex exec lacks required capabilities: " + ", ".join(missing_exec)
            )
        sandbox_help_text = (sandbox_help_process.stdout + sandbox_help_process.stderr).decode(
            "utf-8", errors="replace"
        )
        missing_sandbox = [
            " / ".join(group)
            for group in SANDBOX_CAPABILITY_GROUPS
            if not any(capability in sandbox_help_text for capability in group)
        ]
        if missing_sandbox:
            raise CodexPreflightError(
                "Codex sandbox lacks required capabilities: " + ", ".join(missing_sandbox)
            )
        return CodexPreflightResult(
            resolved,
            version_text or None,
            root_help_text + "\n" + exec_help_text + "\n" + sandbox_help_text,
            version_tuple,
            platform,
            runtime_read_paths,
            sha256_bytes(canonical_json_bytes(list(runtime_read_paths))),
            probe_interpreter,
            effective_sandbox_path,
        )

    def build_command(
        self,
        preflight: CodexPreflightResult,
        configuration: CodexExecutionConfiguration,
        workspace: str | Path,
        final_message_path: str | Path,
        effective_prompt: str,
    ) -> list[str]:
        return [
            *self._base_command(preflight.executable),
            "--ask-for-approval",
            str(configuration.approval_policy),
            "exec",
            "--json",
            "--strict-config",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--model",
            configuration.model,
            *_config_args(
                configuration,
                preflight.runtime_read_paths,
                preflight.effective_sandbox_path,
            ),
            "--cd",
            str(Path(workspace).resolve()),
            "--output-last-message",
            str(Path(final_message_path).resolve()),
            effective_prompt,
        ]

    def build_sandbox_command(
        self,
        preflight: CodexPreflightResult,
        configuration: CodexExecutionConfiguration,
        workspace: str | Path,
        probe_script: str | Path,
        outside_directory: str | Path,
        unrelated_home_canary: str | Path,
        result_path: str | Path,
        network_host: str,
        network_port: int,
    ) -> list[str]:
        return [
            *self._base_command(preflight.executable),
            "sandbox",
            "--permission-profile",
            configuration.permission_profile,
            "--include-managed-config",
            *_config_args(
                configuration,
                preflight.runtime_read_paths,
                preflight.effective_sandbox_path,
            ),
            "--cd",
            str(Path(workspace).resolve()),
            "--",
            preflight.probe_interpreter,
            str(Path(probe_script).resolve()),
            str(Path(outside_directory).resolve()),
            str(Path(unrelated_home_canary).resolve()),
            str(Path(result_path).resolve()),
            network_host,
            str(network_port),
        ]

    def execute_sandbox_probe(
        self,
        command: Sequence[str],
        workspace: str | Path,
        environment_canary_name: str,
        environment_canary_value: str,
        timeout_seconds: int = 30,
    ) -> subprocess.CompletedProcess[bytes]:
        environment = self._environment() or dict(os.environ)
        environment[environment_canary_name] = environment_canary_value
        try:
            return subprocess.run(
                list(command),
                cwd=Path(workspace),
                capture_output=True,
                check=False,
                shell=False,
                env=environment,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise CodexPreflightError("Codex sandbox isolation probe timed out") from exc

    def execute(
        self,
        command: Sequence[str],
        workspace: str | Path,
        raw_events_path: str | Path,
        stderr_path: str | Path,
        timeout_seconds: int,
    ) -> CodexProcessResult:
        with Path(raw_events_path).open("wb") as stdout_stream, Path(stderr_path).open(
            "wb"
        ) as stderr_stream:
            try:
                process = subprocess.run(
                    list(command),
                    cwd=Path(workspace),
                    stdout=stdout_stream,
                    stderr=stderr_stream,
                    check=False,
                    shell=False,
                    env=self._environment(),
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                return CodexProcessResult(
                    exit_code=None,
                    timed_out=True,
                    failure_reason=f"Codex process timed out after {timeout_seconds} seconds",
                )
        return CodexProcessResult(exit_code=process.returncode)
