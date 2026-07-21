from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from backend.runs.codex_cli import (
    CodexCLIAdapter,
    CodexPreflightError,
    CodexPreflightResult,
    PERMISSION_PROFILE_DEFINITION,
    build_permission_profile_definition,
    derive_runtime_read_paths,
    parse_codex_semantic_version,
    permission_profile_hash,
)
from backend.runs.models import CodexExecutionConfiguration


def test_preflight_and_command_enforce_fixed_experiment_configuration(
    fake_codex_factory, tmp_path
):
    adapter = fake_codex_factory(FAKE_THREAD_ID="thread-command")
    preflight = adapter.preflight()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    final_message = tmp_path / "final_message.txt"
    command = adapter.build_command(
        preflight,
        CodexExecutionConfiguration(),
        workspace,
        final_message,
        "exact task",
    )
    assert isinstance(command, list)
    assert command[command.index("exec")] == "exec"
    for flag in (
        "--json",
        "--strict-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ask-for-approval",
        "--ignore-user-config",
        "--ignore-rules",
        "--model",
        "--cd",
        "--output-last-message",
    ):
        assert flag in command
    assert "--sandbox" not in command
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert command.index("--ask-for-approval") < command.index("exec")
    assert command[command.index("--model") + 1] == "gpt-5.6-sol"
    assert command[command.index("--cd") + 1] == str(workspace.resolve())
    assert command[command.index("--output-last-message") + 1] == str(final_message.resolve())
    assert 'model_reasoning_effort="medium"' in command
    assert 'web_search="disabled"' in command
    assert not any("sandbox_workspace_write.network_access" in arg for arg in command)
    assert 'default_permissions="ctm_temporal"' in command
    filesystem_config = next(
        arg for arg in command if arg.startswith("permissions.ctm_temporal.filesystem=")
    )
    assert '":root"="deny"' in filesystem_config
    assert '":minimal"="read"' in filesystem_config
    assert '":workspace_roots"={"."="write"}' in filesystem_config
    assert "permissions.ctm_temporal.network.enabled=false" in command
    assert not any('filesystem={":root"="read"' in arg for arg in command)
    assert 'personality="none"' in command
    assert "project_doc_max_bytes=0" in command
    assert "features.hooks=false" in command
    assert "features.memories=false" in command
    assert "features.remote_plugin=false" in command
    assert "features.shell_snapshot=false" in command
    assert "features.multi_agent=false" in command
    assert "features.goals=false" in command
    assert "features.skill_mcp_dependency_install=false" in command
    assert "tools.web_search=false" in command
    assert "allow_login_shell=false" in command
    assert 'shell_environment_policy.inherit="core"' in command
    include_only = next(arg for arg in command if arg.startswith("shell_environment_policy.include_only="))
    assert "OPENAI_API_KEY" not in include_only
    assert "PATH" in include_only
    assert "shell_environment_policy.ignore_default_excludes=false" in command
    assert "shell_environment_policy.experimental_use_profile=false" in command
    assert "--search" not in command
    assert "--yolo" not in command
    assert "danger-full-access" not in command
    assert "resume" not in command
    assert command[-1] == "exact task"


def test_sandbox_command_omits_exec_only_strict_config(fake_codex_factory, tmp_path):
    adapter = fake_codex_factory()
    preflight = adapter.preflight()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command = adapter.build_sandbox_command(
        preflight,
        CodexExecutionConfiguration(),
        workspace,
        workspace / "probe.py",
        tmp_path / "outside",
        tmp_path / "home_canary.txt",
        workspace / "probe_raw.json",
        "127.0.0.1",
        12345,
    )
    assert "--strict-config" not in command
    assert command[command.index("sandbox") + 1] == "--permission-profile"


@pytest.mark.parametrize("platform", ["linux", "windows", "macos"])
def test_sandbox_command_never_uses_platform_as_a_positional_subcommand(tmp_path, platform):
    adapter = CodexCLIAdapter(sys.executable)
    workspace = tmp_path / platform
    workspace.mkdir()
    preflight = CodexPreflightResult(
        executable=sys.executable,
        version="codex 0.144.2",
        help_text="",
        version_tuple=(0, 144, 2),
        sandbox_platform=platform,
    )
    command = adapter.build_sandbox_command(
        preflight,
        CodexExecutionConfiguration(),
        workspace,
        workspace / "probe.py",
        tmp_path / "outside",
        tmp_path / "home_canary.txt",
        workspace / "probe_raw.json",
        "127.0.0.1",
        12345,
    )
    sandbox_index = command.index("sandbox")
    assert command[sandbox_index + 1] == "--permission-profile"
    assert platform not in command[sandbox_index + 1 :]


def test_realistic_fake_sandbox_rejects_strict_config_but_corrected_command_succeeds(
    fake_codex_factory, tmp_path
):
    adapter = fake_codex_factory()
    preflight = adapter.preflight()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    corrected = adapter.build_sandbox_command(
        preflight,
        CodexExecutionConfiguration(),
        workspace,
        workspace / "probe.py",
        tmp_path / "outside",
        tmp_path / "home_canary.txt",
        workspace / "probe_raw.json",
        "127.0.0.1",
        12345,
    )
    rejected = [*corrected[:1], "--strict-config", *corrected[1:]]
    rejected_result = adapter.execute_sandbox_probe(
        rejected, workspace, "TM_TEST", "value"
    )
    assert rejected_result.returncode != 0
    corrected_result = adapter.execute_sandbox_probe(
        corrected, workspace, "TM_TEST", "value"
    )
    assert corrected_result.returncode == 0
    sandbox_index = corrected.index("sandbox")
    platform_reintroduced = [
        *corrected[: sandbox_index + 1],
        "linux",
        *corrected[sandbox_index + 1 :],
    ]
    platform_result = adapter.execute_sandbox_probe(
        platform_reintroduced, workspace, "TM_TEST", "value"
    )
    assert platform_result.returncode != 0


def test_missing_codex_executable_fails_closed():
    with pytest.raises(CodexPreflightError, match="not found"):
        CodexCLIAdapter("codex-that-does-not-exist-9f41b7").preflight()


def test_missing_required_capability_fails_without_dropping_flag(fake_codex_factory):
    adapter = fake_codex_factory(FAKE_MISSING_FLAG="--ignore-rules")
    with pytest.raises(CodexPreflightError, match="--ignore-rules"):
        adapter.preflight()


def test_strict_config_is_a_required_preflight_capability(fake_codex_factory):
    with pytest.raises(CodexPreflightError, match="--strict-config"):
        fake_codex_factory(FAKE_MISSING_FLAG="--strict-config").preflight()


def test_sandbox_does_not_need_exec_only_strict_config(fake_codex_factory):
    assert fake_codex_factory().preflight().sandbox_platform


def test_permission_profile_definition_has_only_minimal_and_active_workspace_access():
    profile = PERMISSION_PROFILE_DEFINITION["permissions"]["ctm_temporal"]
    assert profile["filesystem"] == {
        ":root": "deny",
        ":minimal": "read",
        ":workspace_roots": {".": "write"},
    }
    assert profile["filesystem"][":root"] == "deny"
    assert profile["network"] == {"enabled": False}


def test_standalone_codex_runtime_grant_is_only_the_release_directory(tmp_path):
    home = tmp_path / "home"
    release = (
        home
        / ".codex/packages/standalone/releases/0.144.5-x86_64-unknown-linux-musl"
    )
    executable = release / "bin/codex"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")

    runtime_paths = derive_runtime_read_paths(executable)
    assert runtime_paths == (str(release.resolve()),)
    filesystem = build_permission_profile_definition(runtime_paths)["permissions"][
        "ctm_temporal"
    ]["filesystem"]
    assert filesystem[str(release.resolve())] == "read"
    assert str((home / ".codex").resolve()) not in filesystem
    assert str(home.resolve()) not in filesystem
    assert str(tmp_path.resolve()) not in filesystem
    assert filesystem[":workspace_roots"] == {".": "write"}


def test_runtime_read_grant_is_bound_into_exec_and_probe_profile_configuration(tmp_path):
    release = tmp_path / "release"
    runtime_path = str(release.resolve())
    preflight = CodexPreflightResult(
        executable=sys.executable,
        version="codex 0.144.5",
        help_text="",
        version_tuple=(0, 144, 5),
        sandbox_platform="linux",
        runtime_read_paths=(runtime_path,),
        runtime_read_paths_hash="a" * 64,
        probe_interpreter="/usr/bin/python3",
        effective_sandbox_path="/usr/local/bin:/usr/bin:/bin",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = CodexCLIAdapter(sys.executable)
    exec_command = adapter.build_command(
        preflight, CodexExecutionConfiguration(), workspace, tmp_path / "final.txt", "task"
    )
    probe_command = adapter.build_sandbox_command(
        preflight,
        CodexExecutionConfiguration(),
        workspace,
        workspace / "probe.py",
        tmp_path / "outside",
        tmp_path / "home-canary.txt",
        workspace / "probe-result.json",
        "127.0.0.1",
        1,
    )
    for command in (exec_command, probe_command):
        filesystem = next(
            argument
            for argument in command
            if argument.startswith("permissions.ctm_temporal.filesystem=")
        )
        assert f'{runtime_path!r}'.replace("'", '"') in filesystem
        assert 'shell_environment_policy.set.PATH="/usr/local/bin:/usr/bin:/bin"' in command
    separator_index = probe_command.index("--")
    sandboxed_command = probe_command[separator_index + 1 :]
    assert sandboxed_command[0] == preflight.probe_interpreter
    assert sandboxed_command[0] == "/usr/bin/python3"
    assert ".venv" not in sandboxed_command[0]


def test_runtime_read_path_changes_permission_profile_identity(tmp_path):
    first = str((tmp_path / "release-a").resolve())
    second = str((tmp_path / "release-b").resolve())
    assert permission_profile_hash((first,)) != permission_profile_hash((second,))


@pytest.mark.parametrize("version", ["codex-cli 0.137.9", "not-a-version"])
def test_old_or_unparseable_codex_version_fails_closed(fake_codex_factory, version):
    with pytest.raises(CodexPreflightError, match="0.138.0|could not be established"):
        fake_codex_factory(FAKE_VERSION=version).preflight()


def test_semantic_version_parser_accepts_supported_version():
    assert parse_codex_semantic_version("codex-cli 0.138.0-beta.1") == (0, 138, 0)


def test_missing_sandbox_support_fails_closed(fake_codex_factory):
    with pytest.raises(CodexPreflightError, match="sandbox"):
        fake_codex_factory(FAKE_NO_SANDBOX="1").preflight()


def test_missing_permission_profile_support_fails_closed(fake_codex_factory):
    with pytest.raises(CodexPreflightError, match="--permission-profile"):
        fake_codex_factory(FAKE_MISSING_SANDBOX_FLAG="--permission-profile").preflight()


def test_missing_managed_config_support_fails_closed(fake_codex_factory):
    with pytest.raises(CodexPreflightError, match="--include-managed-config"):
        fake_codex_factory(
            FAKE_MISSING_SANDBOX_FLAG="--include-managed-config"
        ).preflight()


@pytest.mark.parametrize(
    ("timed_command", "error_category"),
    [
        (("--version",), "version"),
        (("--help",), "root help"),
        (("exec", "--help"), "exec help"),
        (("sandbox", "--help"), "sandbox help"),
    ],
)
def test_each_preflight_command_is_time_bounded(
    monkeypatch, timed_command, error_category
):
    adapter = CodexCLIAdapter(sys.executable)

    def fake_run(command, **kwargs):
        normalized = tuple(command[1:])
        matches = (
            normalized == timed_command
        )
        if matches:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if normalized == ("--version",):
            return subprocess.CompletedProcess(command, 0, b"codex 0.138.0", b"")
        flags = b"exec sandbox --json --ephemeral --skip-git-repo-check --ask-for-approval --ignore-user-config --ignore-rules --model --config -c --cd -C --output-last-message --strict-config --permission-profile --include-managed-config"
        return subprocess.CompletedProcess(command, 0, flags, b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(CodexPreflightError, match=error_category):
        adapter.preflight(timeout_seconds=3)


def test_unsupported_version_fails_before_help_commands(monkeypatch):
    adapter = CodexCLIAdapter(sys.executable)
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, b"codex 0.137.9", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(CodexPreflightError, match="0.138.0"):
        adapter.preflight()
    assert len(calls) == 1
    assert calls[0][-1] == "--version"


def test_preflight_uses_only_generic_sandbox_help(monkeypatch):
    adapter = CodexCLIAdapter(sys.executable)
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        normalized = tuple(command[1:])
        if normalized == ("--version",):
            output = b"codex 0.144.2"
        elif normalized == ("sandbox", "--help"):
            output = b"--permission-profile --include-managed-config --config -c --cd -C"
        elif normalized == ("exec", "--help"):
            output = b"--json --ephemeral --skip-git-repo-check --ignore-user-config --ignore-rules --model --config -c --cd -C --output-last-message --strict-config"
        else:
            output = b"exec --ask-for-approval"
        return subprocess.CompletedProcess(command, 0, output, b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter.preflight()
    assert [call[1:] for call in calls] == [
        ["--version"],
        ["--help"],
        ["exec", "--help"],
        ["sandbox", "--help"],
    ]


def test_execute_uses_argument_list_and_never_shell_true(tmp_path, monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = CodexCLIAdapter()
    result = adapter.execute(
        ["codex", "exec", "--json", "task"],
        workspace,
        tmp_path / "events.jsonl",
        tmp_path / "stderr.log",
        10,
    )
    assert result.exit_code == 0
    assert not result.timed_out
    assert isinstance(captured["command"], list)
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == 10


def test_timeout_preserves_known_partial_stream_bytes(tmp_path, monkeypatch):
    raw = tmp_path / "events.jsonl"
    stderr = tmp_path / "stderr.log"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def fake_run(_command, **kwargs):
        kwargs["stdout"].write(b'{"type":"thread.started","thread_id":"partial"}\n')
        kwargs["stdout"].flush()
        kwargs["stderr"].write(b"partial stderr\n")
        kwargs["stderr"].flush()
        raise subprocess.TimeoutExpired("codex", 0.01)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = CodexCLIAdapter().execute(
        ["codex", "exec"], workspace, raw, stderr, timeout_seconds=1
    )
    assert result.timed_out is True
    assert raw.read_bytes() == b'{"type":"thread.started","thread_id":"partial"}\n'
    assert stderr.read_bytes() == b"partial stderr\n"
