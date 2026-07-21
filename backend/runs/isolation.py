"""Deterministic permission-profile isolation probe run before evaluated Codex."""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from backend.temporal.integrity import canonical_json_bytes, sha256_bytes, sha256_file

from .codex_cli import (
    PERMISSION_PROFILE_HASH,
    CodexCLIAdapter,
    CodexPreflightResult,
    permission_profile_hash,
)
from .models import (
    CodexExecutionConfiguration,
    IsolationProbeCommand,
    IsolationProbeResult,
)


ENVIRONMENT_CANARY_NAME = "TM_FUTURE_ENV_CANARY_71C8D4"
ENVIRONMENT_CANARY_VALUE = "TM_FUTURE_ENV_CANARY_71C8D4"
OUTSIDE_READ_CANARY = "TM_OUTSIDE_READ_CANARY_2E71A9"
# Wait only long enough for the listener thread to dequeue a connection that a
# just-finished probe may already have placed in the kernel accept queue.
LOOPBACK_OBSERVATION_GRACE_SECONDS = 0.15


class _RawProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_read_succeeded: bool
    workspace_write_succeeded: bool
    outside_read_blocked: bool
    outside_write_blocked: bool
    environment_canary_absent: bool
    network_connect_blocked: bool
    unrelated_home_read_blocked: bool


@dataclass(frozen=True)
class IsolationProbeExecution:
    result: IsolationProbeResult
    stdout_sha256: str
    stderr_sha256: str
    command_sha256: str
    result_sha256: str


class _LoopbackListener:
    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self._socket.settimeout(0.05)
        self.host, self.port = self._socket.getsockname()
        self.connection_accepted = False
        self._connection_observed = threading.Event()
        self._stopped = threading.Event()
        self._listener_error: OSError | None = None
        self._thread = threading.Thread(target=self._accept, daemon=True)

    def _accept(self) -> None:
        while not self._stopped.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                if not self._stopped.is_set():
                    self._listener_error = exc
                return
            self.connection_accepted = True
            self._connection_observed.set()
            try:
                connection.close()
            except OSError:
                pass
            return

    def wait_for_connection_observation(self, timeout_seconds: float) -> bool:
        """Synchronize against queued loopback connections before shutdown."""
        observed = self._connection_observed.wait(timeout_seconds)
        if self._listener_error is not None:
            raise RuntimeError(f"loopback listener failed: {self._listener_error}")
        if not observed and not self._thread.is_alive():
            raise RuntimeError("loopback listener stopped before observation completed")
        return observed

    def __enter__(self) -> "_LoopbackListener":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stopped.set()
        self._socket.close()
        self._thread.join(timeout=1)
        if self._thread.is_alive():
            raise RuntimeError("loopback listener did not terminate during shutdown")


_PROBE_SCRIPT = '''from __future__ import annotations
import json
import os
from pathlib import Path
import socket
import sys

outside = Path(sys.argv[1])
unrelated_home_canary = Path(sys.argv[2])
result_path = Path(sys.argv[3])
network_host = sys.argv[4]
network_port = int(sys.argv[5])
workspace = Path.cwd()
result = {}
try:
    result["workspace_read_succeeded"] = workspace.joinpath("allowed_read.txt").read_text(encoding="utf-8") == "allowed"
except OSError:
    result["workspace_read_succeeded"] = False
try:
    workspace.joinpath("allowed_write.txt").write_text("written", encoding="utf-8")
    result["workspace_write_succeeded"] = True
except OSError:
    result["workspace_write_succeeded"] = False
try:
    outside.joinpath("future_read_canary.txt").read_bytes()
    result["outside_read_blocked"] = False
except OSError:
    result["outside_read_blocked"] = True
try:
    outside.joinpath("blocked_write.txt").write_text("leak", encoding="utf-8")
    result["outside_write_blocked"] = False
except OSError:
    result["outside_write_blocked"] = True
result["environment_canary_absent"] = "TM_FUTURE_ENV_CANARY_71C8D4" not in os.environ
try:
    unrelated_home_canary.read_bytes()
    result["unrelated_home_read_blocked"] = False
except OSError:
    result["unrelated_home_read_blocked"] = True
try:
    connection = socket.create_connection((network_host, network_port), timeout=0.25)
    connection.close()
    result["network_connect_blocked"] = False
except OSError:
    result["network_connect_blocked"] = True
result_path.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")), encoding="utf-8")
'''


def _atomic_write(path: Path, content: bytes) -> None:
    temp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temp.write_bytes(content)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _atomic_write_model(path: Path, model: BaseModel) -> None:
    _atomic_write(path, canonical_json_bytes(model.model_dump(mode="json")) + b"\n")


def _create_unrelated_home_canary_directory(platform: str) -> Path:
    """Create one controlled canary directory under the user's home tree.

    A direct one-attempt mkdir avoids tempfile's retry loop when a managed
    Windows profile disallows writing at the home root. Linux/WSL is the
    evaluated profile target and therefore uses the home root itself.
    """
    home = Path.home().resolve()
    if platform == "linux":
        candidate = home / f".ctm-isolation-probe-{uuid.uuid4().hex}"
    else:
        candidate = Path(tempfile.gettempdir()).resolve() / f"ctm-isolation-probe-{uuid.uuid4().hex}"
    candidate.mkdir()
    candidate = candidate.resolve()
    if not candidate.is_relative_to(home):
        shutil.rmtree(candidate)
        raise RuntimeError("unable to create the unrelated-home canary under the user home directory")
    return candidate


class IsolationProbeRunner:
    def run(
        self,
        adapter: CodexCLIAdapter,
        preflight: CodexPreflightResult,
        configuration: CodexExecutionConfiguration,
        run_directory: str | Path,
    ) -> IsolationProbeExecution:
        run_directory = Path(run_directory).resolve()
        stage = run_directory / f".isolation-probe-{uuid.uuid4().hex}"
        workspace = stage / "workspace"
        outside = stage / "outside"
        home_canary_directory: Path | None = None
        raw_result = workspace / "probe_raw.json"
        result_artifact = run_directory / "isolation_probe.json"
        stdout_artifact = run_directory / "isolation_probe_stdout.log"
        stderr_artifact = run_directory / "isolation_probe_stderr.log"
        command_artifact = run_directory / "isolation_probe_command.json"
        process_returncode: int | None = None
        orchestration_error: str | None = None
        stdout = b""
        stderr = b""
        network_connection_accepted = False
        try:
            workspace.mkdir(parents=True)
            outside.mkdir()
            (workspace / "allowed_read.txt").write_text("allowed", encoding="utf-8")
            (outside / "future_read_canary.txt").write_text(
                OUTSIDE_READ_CANARY, encoding="utf-8"
            )
            home_canary_directory = _create_unrelated_home_canary_directory(
                preflight.sandbox_platform
            )
            if (
                home_canary_directory.is_relative_to(workspace)
                or home_canary_directory.is_relative_to(outside)
                or any(home_canary_directory.is_relative_to(Path(path)) for path in preflight.runtime_read_paths)
            ):
                raise RuntimeError("unrelated home canary path overlaps an allowed probe path")
            home_canary = home_canary_directory / "home_read_canary.txt"
            home_canary.write_text("TM_HOME_READ_CANARY_6D18A4", encoding="utf-8")
            probe_script = workspace / "probe.py"
            probe_script.write_text(_PROBE_SCRIPT, encoding="utf-8")
            with _LoopbackListener() as listener:
                command = adapter.build_sandbox_command(
                    preflight,
                    configuration,
                    workspace,
                    probe_script,
                    outside,
                    home_canary,
                    raw_result,
                    listener.host,
                    listener.port,
                )
                command_record = IsolationProbeCommand(
                    platform=preflight.sandbox_platform,
                    resolved_codex_executable=preflight.executable,
                    codex_version=preflight.version or "unavailable",
                    working_directory=str(workspace),
                    arguments=command,
                    permission_profile=configuration.permission_profile,
                    permission_profile_hash=permission_profile_hash(preflight.runtime_read_paths),
                    runtime_read_paths=list(preflight.runtime_read_paths),
                    runtime_read_paths_hash=preflight.runtime_read_paths_hash,
                    probe_interpreter=preflight.probe_interpreter,
                    effective_sandbox_path=preflight.effective_sandbox_path,
                )
                _atomic_write_model(command_artifact, command_record)
                process = adapter.execute_sandbox_probe(
                    command,
                    workspace,
                    ENVIRONMENT_CANARY_NAME,
                    ENVIRONMENT_CANARY_VALUE,
                )
                process_returncode = process.returncode
                stdout = process.stdout or b""
                stderr = process.stderr or b""
                network_connection_accepted = listener.wait_for_connection_observation(
                    LOOPBACK_OBSERVATION_GRACE_SECONDS
                )
        except (OSError, RuntimeError) as exc:
            orchestration_error = f"isolation probe execution failed: {exc}"
        finally:
            _atomic_write(stdout_artifact, stdout)
            _atomic_write(stderr_artifact, stderr)
            if home_canary_directory is not None and home_canary_directory.exists():
                shutil.rmtree(home_canary_directory)

        raw_hash = sha256_file(raw_result) if raw_result.is_file() else sha256_bytes(b"")
        reasons: list[str] = []
        raw = _RawProbeResult(
            workspace_read_succeeded=False,
            workspace_write_succeeded=False,
            outside_read_blocked=False,
            outside_write_blocked=False,
            environment_canary_absent=False,
            network_connect_blocked=False,
            unrelated_home_read_blocked=False,
        )
        if orchestration_error:
            reasons.append(orchestration_error)
        elif not raw_result.is_file():
            reasons.append("isolation probe did not produce a structured result")
        else:
            try:
                raw = _RawProbeResult.model_validate_json(raw_result.read_text("utf-8"))
            except (OSError, ValidationError, ValueError) as exc:
                reasons.append(f"invalid isolation probe result: {exc}")
        if process_returncode not in (0, None):
            reasons.append(f"isolation probe exited with code {process_returncode}")
        network_blocked = raw.network_connect_blocked and not network_connection_accepted
        checks = {
            "workspace read": raw.workspace_read_succeeded,
            "workspace write": raw.workspace_write_succeeded,
            "outside read blocked": raw.outside_read_blocked,
            "outside write blocked": raw.outside_write_blocked,
            "environment canary absent": raw.environment_canary_absent,
            "unrelated home read blocked": raw.unrelated_home_read_blocked,
            "loopback network connection blocked": network_blocked,
        }
        for label, passed in checks.items():
            if not passed:
                reasons.append(f"isolation probe check failed: {label}")
        network_disabled = configuration.network_enabled is False
        if not network_disabled:
            reasons.append("permission profile network is not disabled")
        result = IsolationProbeResult(
            permission_profile=configuration.permission_profile,
            platform=preflight.sandbox_platform,
            workspace_read_succeeded=raw.workspace_read_succeeded,
            workspace_write_succeeded=raw.workspace_write_succeeded,
            outside_read_blocked=raw.outside_read_blocked,
            outside_write_blocked=raw.outside_write_blocked,
            environment_canary_absent=raw.environment_canary_absent,
            network_configured_disabled=network_disabled,
            network_connect_blocked=network_blocked,
            unrelated_home_read_blocked=raw.unrelated_home_read_blocked,
            probe_succeeded=not reasons,
            failure_reasons=reasons,
            probe_output_hash=raw_hash,
        )
        _atomic_write_model(result_artifact, result)
        if stage.exists():
            shutil.rmtree(stage)
        return IsolationProbeExecution(
            result=result,
            stdout_sha256=sha256_file(stdout_artifact),
            stderr_sha256=sha256_file(stderr_artifact),
            command_sha256=(
                sha256_file(command_artifact) if command_artifact.is_file() else sha256_bytes(b"")
            ),
            result_sha256=sha256_file(result_artifact),
        )
