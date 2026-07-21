from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

import backend.runs.isolation as isolation_module
from backend.runs.models import RunKind, RunSpecification, RunStatus
from backend.runs.runner import TemporalRunner, default_runs_root
from backend.runs.workspace import RunWorkspaceError
from backend.temporal.integrity import sha256_file


def _specification(scenario: Path, run_id: str) -> RunSpecification:
    return RunSpecification(
        run_id=run_id,
        scenario_path=scenario,
        run_kind=RunKind.BASELINE,
    )


def _runs_root(tmp_path: Path) -> Path:
    return tmp_path.parent / f"{tmp_path.name}-isolation-runs"


def test_successful_probe_is_recorded_and_never_enters_workspace(
    sealed_scenario, memory_codex_factory, tmp_path
):
    runs_root = _runs_root(tmp_path)
    manifest = TemporalRunner(memory_codex_factory()).run(
        _specification(sealed_scenario, "R-PROBE"), runs_root
    )
    artifact = runs_root / "R-PROBE/isolation_probe.json"
    probe = json.loads(artifact.read_text("utf-8"))
    assert manifest.run_status is RunStatus.SUCCEEDED
    assert manifest.isolation_probe_succeeded is True
    assert manifest.isolation_probe_result_sha256 == sha256_file(artifact)
    assert manifest.isolation_probe_stdout_sha256 == sha256_file(
        runs_root / "R-PROBE/isolation_probe_stdout.log"
    )
    assert manifest.isolation_probe_stderr_sha256 == sha256_file(
        runs_root / "R-PROBE/isolation_probe_stderr.log"
    )
    assert manifest.isolation_probe_command_sha256 == sha256_file(
        runs_root / "R-PROBE/isolation_probe_command.json"
    )
    assert probe["workspace_read_succeeded"] is True
    assert probe["workspace_write_succeeded"] is True
    assert probe["outside_read_blocked"] is True
    assert probe["outside_write_blocked"] is True
    assert probe["environment_canary_absent"] is True
    assert probe["unrelated_home_read_blocked"] is True
    assert probe["network_configured_disabled"] is True
    assert probe["network_connect_blocked"] is True
    command = json.loads(
        (runs_root / "R-PROBE/isolation_probe_command.json").read_text("utf-8")
    )
    assert command["managed_config_included"] is True
    assert command["runtime_read_paths"] == ["/opt/memory-codex"]
    assert command["probe_interpreter"]
    assert "--include-managed-config" in command["arguments"]
    assert "TM_FUTURE_ENV_CANARY_71C8D4" not in json.dumps(command)
    assert not list((runs_root / "R-PROBE/workspace").rglob("isolation_probe.json"))
    assert not list((runs_root / "R-PROBE/workspace").rglob("future_read_canary.txt"))


def test_linux_runtime_grants_and_effective_path_are_recorded(
    sealed_scenario, memory_codex_factory, tmp_path, monkeypatch
):
    def create_home_canary(_platform: str) -> Path:
        path = tmp_path / "home-canary"
        path.mkdir()
        return path

    monkeypatch.setattr(
        isolation_module, "_create_unrelated_home_canary_directory", create_home_canary
    )
    runs_root = _runs_root(tmp_path)
    manifest = TemporalRunner(
        memory_codex_factory(SANDBOX_PLATFORM="linux")
    ).run(_specification(sealed_scenario, "R-LINUX-PROBE"), runs_root)
    assert manifest.run_status is RunStatus.SUCCEEDED
    assert manifest.runtime_read_paths == ["/opt/memory-codex"]
    assert manifest.runtime_read_paths_hash
    assert manifest.probe_interpreter
    assert manifest.effective_sandbox_path == (
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    )
    assert manifest.shell_environment_policy.set == {
        "PATH": manifest.effective_sandbox_path
    }


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("outside_read_leak", "outside read blocked"),
        ("outside_write_leak", "outside write blocked"),
        ("environment_leak", "environment canary absent"),
        ("home_read_leak", "unrelated home read blocked"),
        ("network_leak", "loopback network connection blocked"),
        ("network_lying_leak", "loopback network connection blocked"),
    ],
)
def test_probe_leak_fails_closed_and_prevents_model_execution(
    sealed_scenario, memory_codex_factory, tmp_path, mode, reason
):
    runs_root = _runs_root(tmp_path)
    adapter = memory_codex_factory(PROBE_MODE=mode)
    manifest = TemporalRunner(adapter).run(
        _specification(sealed_scenario, f"R-{mode}"), runs_root
    )
    assert manifest.run_status is RunStatus.FAILED
    assert manifest.completed_at is not None
    assert manifest.isolation_probe_succeeded is False
    assert reason in manifest.failure_reason
    assert adapter.execute_called is False
    assert not (runs_root / f"R-{mode}/raw_codex_events.jsonl").exists()


def test_network_configuration_intent_alone_is_not_probe_success(
    sealed_scenario, memory_codex_factory, tmp_path
):
    manifest = TemporalRunner(memory_codex_factory(PROBE_MODE="network_leak")).run(
        _specification(sealed_scenario, "R-NETWORK-INTENT"), _runs_root(tmp_path)
    )
    probe = json.loads(
        (_runs_root(tmp_path) / "R-NETWORK-INTENT/isolation_probe.json").read_text("utf-8")
    )
    assert probe["network_configured_disabled"] is True
    assert probe["network_connect_blocked"] is False
    assert probe["probe_succeeded"] is False
    assert manifest.run_status is RunStatus.FAILED


def test_host_loopback_observation_overrides_a_lying_probe_claim(
    sealed_scenario, memory_codex_factory, tmp_path
):
    runs_root = _runs_root(tmp_path)
    manifest = TemporalRunner(memory_codex_factory(PROBE_MODE="network_lying_leak")).run(
        _specification(sealed_scenario, "R-NETWORK-LYING"), runs_root
    )
    probe = json.loads(
        (runs_root / "R-NETWORK-LYING/isolation_probe.json").read_text("utf-8")
    )
    assert probe["network_connect_blocked"] is False
    assert probe["probe_succeeded"] is False
    assert manifest.run_status is RunStatus.FAILED


def test_listener_observation_wait_returns_immediately_after_fast_connection():
    with isolation_module._LoopbackListener() as listener:
        connection = socket.create_connection((listener.host, listener.port), timeout=1)
        connection.close()
        started = time.monotonic()
        assert listener.wait_for_connection_observation(0.15) is True
        assert time.monotonic() - started < 0.1
    assert listener._socket.fileno() == -1
    assert not listener._thread.is_alive()


def test_listener_observation_wait_is_bounded_without_connection():
    with isolation_module._LoopbackListener() as listener:
        started = time.monotonic()
        assert listener.wait_for_connection_observation(0.02) is False
        assert time.monotonic() - started < 0.2
    assert listener._socket.fileno() == -1
    assert not listener._thread.is_alive()


def test_listener_error_fails_closed_and_cleans_up(
    sealed_scenario, memory_codex_factory, tmp_path, monkeypatch
):
    instances = []
    real_listener = isolation_module._LoopbackListener

    class BrokenListener(real_listener):
        def __init__(self):
            super().__init__()
            instances.append(self)

        def _accept(self):
            self._listener_error = OSError("test listener failure")

    monkeypatch.setattr(isolation_module, "_LoopbackListener", BrokenListener)
    manifest = TemporalRunner(memory_codex_factory()).run(
        _specification(sealed_scenario, "R-BROKEN-LISTENER"), _runs_root(tmp_path)
    )
    assert manifest.run_status is RunStatus.FAILED
    assert "loopback listener failed" in (manifest.failure_reason or "")
    assert len(instances) == 1
    assert instances[0]._socket.fileno() == -1
    assert not instances[0]._thread.is_alive()


def test_listener_closes_after_adapter_exception(
    sealed_scenario, memory_codex_factory, tmp_path, monkeypatch
):
    instances = []
    real_listener = isolation_module._LoopbackListener

    class TrackingListener(real_listener):
        def __init__(self):
            super().__init__()
            instances.append(self)

    adapter = memory_codex_factory()

    def raise_adapter_error(*_args, **_kwargs):
        raise OSError("test adapter failure")

    monkeypatch.setattr(isolation_module, "_LoopbackListener", TrackingListener)
    monkeypatch.setattr(adapter, "execute_sandbox_probe", raise_adapter_error)
    manifest = TemporalRunner(adapter).run(
        _specification(sealed_scenario, "R-ADAPTER-ERROR"), _runs_root(tmp_path)
    )
    assert manifest.run_status is RunStatus.FAILED
    assert "test adapter failure" in (manifest.failure_reason or "")
    assert len(instances) == 1
    assert instances[0]._socket.fileno() == -1
    assert not instances[0]._thread.is_alive()


def test_network_lying_leak_remains_failed_under_repetition(
    sealed_scenario, memory_codex_factory, tmp_path
):
    runs_root = _runs_root(tmp_path)
    for index in range(50):
        manifest = TemporalRunner(
            memory_codex_factory(PROBE_MODE="network_lying_leak")
        ).run(_specification(sealed_scenario, f"R-NETWORK-LYING-{index}"), runs_root)
        assert manifest.run_status is RunStatus.FAILED
        assert manifest.isolation_probe_succeeded is False


@pytest.mark.parametrize("probe_mode", ["success", "network_leak"])
def test_loopback_listener_closes_after_every_probe_outcome(
    sealed_scenario, memory_codex_factory, tmp_path, monkeypatch, probe_mode
):
    instances = []
    real_listener = isolation_module._LoopbackListener

    class TrackingListener(real_listener):
        def __init__(self):
            super().__init__()
            instances.append(self)

    monkeypatch.setattr(isolation_module, "_LoopbackListener", TrackingListener)
    TemporalRunner(memory_codex_factory(PROBE_MODE=probe_mode)).run(
        _specification(sealed_scenario, f"R-LISTENER-{probe_mode}"),
        _runs_root(tmp_path),
    )
    assert len(instances) == 1
    assert instances[0]._socket.fileno() == -1


def test_default_runs_root_is_outside_scenario_directory(sealed_scenario):
    root = default_runs_root(sealed_scenario).resolve()
    scenario_directory = sealed_scenario.parent.resolve()
    assert not root.is_relative_to(scenario_directory)
    assert not scenario_directory.is_relative_to(root)


@pytest.mark.parametrize("runs_root_kind", ["scenario", "scenario_child", "scenario_parent"])
def test_scenario_and_run_path_overlap_is_rejected(
    sealed_scenario, memory_codex_factory, runs_root_kind
):
    scenario = sealed_scenario.parent.resolve()
    roots = {
        "scenario": scenario,
        "scenario_child": scenario / "runs",
        "scenario_parent": scenario.parent,
    }
    with pytest.raises(RunWorkspaceError, match="non-overlapping"):
        TemporalRunner(memory_codex_factory()).run(
            _specification(sealed_scenario, f"R-OVERLAP-{runs_root_kind}"),
            roots[runs_root_kind],
        )
