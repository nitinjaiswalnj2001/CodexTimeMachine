from __future__ import annotations

from pathlib import Path
import json
import socket
import subprocess
import sys

import pytest
import yaml

from backend.runs.codex_cli import CodexCLIAdapter, CodexPreflightError, CodexPreflightResult, CodexProcessResult
from backend.temporal.integrity import canonical_json_bytes, sha256_bytes
from backend.temporal.snapshot import TemporalSnapshotBuilder


@pytest.fixture
def manifest_factory(tmp_path: Path):
    def create(assets: list[dict], *, output_name: str = "sealed") -> Path:
        scenario = {
            "schema_version": "1.0",
            "scenario_id": "test-scenario",
            "scenario_type": "controlled_fixture",
            "cutoff": {"kind": "FIXTURE_REVISION", "value": "T0"},
            "task": "Exercise the temporal boundary.",
            "network_policy": "disabled",
            "assets_manifest": "assets.yaml",
            "output_directory": output_name,
            "audit": {"future_canary_token": "TM_FUTURE_CANARY_9F41B7"},
        }
        asset_manifest = {
            "schema_version": "1.0",
            "scenario_id": "test-scenario",
            "assets": assets,
        }
        scenario_path = tmp_path / "scenario.yaml"
        scenario_path.write_text(yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8")
        (tmp_path / "assets.yaml").write_text(
            yaml.safe_dump(asset_manifest, sort_keys=False), encoding="utf-8"
        )
        return scenario_path

    return create


@pytest.fixture
def asset_factory(tmp_path: Path):
    counter = 0

    def create(
        *,
        asset_id: str | None = None,
        logical_path: str = "src/example.py",
        status: str = "AVAILABLE",
        visibility: str = "PAST_CODEX",
        content: str = "past evidence\n",
        source_path: str | None = None,
    ) -> dict:
        nonlocal counter
        counter += 1
        if source_path is None:
            source = tmp_path / "sources" / f"source-{counter}.txt"
            source.parent.mkdir(exist_ok=True)
            source.write_text(content, encoding="utf-8")
            source_path = source.relative_to(tmp_path).as_posix()
        return {
            "asset_id": asset_id or f"asset-{counter}",
            "logical_path": logical_path,
            "asset_kind": "SOURCE",
            "source_path": source_path,
            "availability_basis": "Test fixture classification.",
            "visibility_scope": visibility,
            "availability": {"status": status, "reason": f"Test reason for {status}."},
            "metadata": {},
        }

    return create


@pytest.fixture
def sealed_scenario(manifest_factory, asset_factory, tmp_path):
    assets = [
        asset_factory(asset_id="source", logical_path="src/example.py", content="print('past')\n"),
        asset_factory(
            asset_id="future",
            logical_path="evidence/future.txt",
            status="LOCKED_FUTURE",
            content="TM_FUTURE_CANARY_9F41B7",
        ),
    ]
    scenario = manifest_factory(assets, output_name="sealed")
    TemporalSnapshotBuilder().build(scenario, tmp_path / "sealed")
    return scenario


@pytest.fixture
def fake_codex_script(tmp_path: Path) -> Path:
    script = tmp_path / "fake_codex.py"
    script.write_text(
        """from __future__ import annotations
import os
from pathlib import Path
import sys
import time

args = sys.argv[1:]
if args == ["--version"]:
    print(os.environ.get("FAKE_VERSION", "fake-codex 1.0.0"))
    raise SystemExit(0)
if args == ["--help"]:
    flags = ["exec", "sandbox", "--ask-for-approval", "--strict-config", "--model", "--config", "-c", "--cd", "-C"]
    missing = os.environ.get("FAKE_MISSING_FLAG")
    print(" ".join(flag for flag in flags if flag != missing))
    raise SystemExit(0)
if args == ["exec", "--help"]:
    flags = ["exec", "--json", "--strict-config", "--ephemeral", "--skip-git-repo-check", "--ask-for-approval", "--ignore-user-config", "--ignore-rules", "--model", "--config", "-c", "--cd", "-C", "--output-last-message"]
    missing = os.environ.get("FAKE_MISSING_FLAG")
    print(" ".join(flag for flag in flags if flag != missing))
    raise SystemExit(0)
if args == ["sandbox", "--help"]:
    if os.environ.get("FAKE_NO_SANDBOX") == "1":
        raise SystemExit(2)
    flags = ["sandbox", "--permission-profile", "--include-managed-config", "--config", "-c", "--cd", "-C"]
    missing = os.environ.get("FAKE_MISSING_SANDBOX_FLAG")
    print(" ".join(flag for flag in flags if flag != missing))
    raise SystemExit(0)
if len(args) == 3 and args[0] == "sandbox" and args[2] == "--help":
    print("Error: platform sandbox subcommands are unsupported", file=sys.stderr)
    raise SystemExit(64)
if "sandbox" in args:
    sandbox_index = args.index("sandbox")
    if len(args) > sandbox_index + 1 and args[sandbox_index + 1] in {"linux", "windows", "macos"}:
        print("Error: platform sandbox subcommands are unsupported", file=sys.stderr)
        raise SystemExit(64)
    if "--strict-config" in args:
        print("Error: --strict-config is not supported for codex sandbox", file=sys.stderr)
        raise SystemExit(2)
    result_path = Path(args[-3])
    mode = os.environ.get("FAKE_PROBE_MODE", "success")
    if mode == "network_leak":
        import socket
        connection = socket.create_connection((args[-2], int(args[-1])), timeout=1)
        connection.close()
    result = {
        "workspace_read_succeeded": mode != "workspace_read_failure",
        "workspace_write_succeeded": mode != "workspace_write_failure",
        "outside_read_blocked": mode != "outside_read_leak",
        "outside_write_blocked": mode != "outside_write_leak",
        "environment_canary_absent": mode != "environment_leak",
        "network_connect_blocked": mode != "network_leak",
        "unrelated_home_read_blocked": mode != "home_read_leak",
    }
    result_path.write_text(__import__("json").dumps(result, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    print("fake sandbox stdout")
    print("fake sandbox stderr", file=sys.stderr)
    raise SystemExit(0)
if "exec" not in args or "resume" in args:
    raise SystemExit(64)
marker = os.environ.get("FAKE_EXEC_MARKER")
if marker:
    Path(marker).write_text("executed", encoding="utf-8")
workspace = Path(args[args.index("--cd") + 1])
final_path = Path(args[args.index("--output-last-message") + 1])
prompt = args[-1]
workspace.joinpath("received_prompt.txt").write_text(prompt, encoding="utf-8")
workspace.joinpath("codex_edit.txt").write_text("edited by fake Codex\\n", encoding="utf-8")
final_path.write_text("fake final message\\n", encoding="utf-8")
thread_id = os.environ.get("FAKE_THREAD_ID", "thread-default")
mode = os.environ.get("FAKE_MODE", "success")
if mode == "missing_thread_id":
    sys.stdout.buffer.write(b'{"type":"thread.started"}\\n')
elif mode == "multiple_threads":
    sys.stdout.buffer.write(f'{{"type":"thread.started","thread_id":"{thread_id}-one"}}\\n'.encode())
    sys.stdout.buffer.write(f'{{"type":"thread.started","thread_id":"{thread_id}-two"}}\\n'.encode())
elif mode != "no_thread":
    sys.stdout.buffer.write(f'{{"type":"thread.started","thread_id":"{thread_id}"}}\\n'.encode())
sys.stdout.buffer.flush()
sys.stderr.buffer.write(f"fake stderr: {mode}\\n".encode())
sys.stderr.buffer.flush()
if mode == "invalid":
    sys.stdout.buffer.write(b"not-json\\n")
elif mode == "failure":
    sys.stdout.buffer.write(b'{"type":"error","message":"fake failure"}\\n')
elif mode == "error_zero":
    sys.stdout.buffer.write(b'{"type":"error","message":"fake failure"}\\n')
elif mode == "turn_failed":
    sys.stdout.buffer.write(b'{"type":"turn.failed","error":{"message":"boom"}}\\n')
elif mode == "item_failed":
    sys.stdout.buffer.write(b'{"type":"item.failed","error":{"message":"boom"}}\\n')
elif mode == "web_search":
    sys.stdout.buffer.write(b'{"type":"item.completed","item":{"type":"web_search"}}\\n')
elif mode == "mcp":
    sys.stdout.buffer.write(b'{"type":"item.started","item":{"type":"mcp_tool_call"}}\\n')
elif mode == "unknown_item":
    sys.stdout.buffer.write(b'{"type":"item.completed","item":{"type":"novel_local_item"}}\\n')
elif mode == "symlink":
    sys.stdout.buffer.write(b'{"type":"item.completed","item":{"type":"agent_message"}}\\n')
    try:
        os.symlink(workspace / "src/example.py", workspace / "post-run-link")
    except OSError:
        workspace.joinpath("post-run-link.symlink-unavailable").write_text("platform denied symlink", encoding="utf-8")
elif mode == "sleep":
    sys.stdout.buffer.flush()
    time.sleep(float(os.environ.get("FAKE_SLEEP_SECONDS", "2")))
else:
    sys.stdout.buffer.write(b'{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\\n')
    sys.stdout.buffer.write(b'{"type":"future.unknown","payload":1}\\n')
sys.stdout.buffer.flush()
raise SystemExit(7 if mode == "failure" else 0)
""",
        encoding="utf-8",
    )
    return script


@pytest.fixture
def fake_codex_factory(fake_codex_script):
    def create(**environment: str) -> CodexCLIAdapter:
        return CodexCLIAdapter(
            executable=sys.executable,
            executable_prefix_args=[str(fake_codex_script)],
            environment=environment,
        )

    return create


class InMemoryCodexAdapter(CodexCLIAdapter):
    """Fast runner-semantic fake; subprocess behavior has separate integration tests."""

    def __init__(self, **settings: str) -> None:
        super().__init__(executable="memory-codex")
        self.settings = settings
        self.preflight_called = False
        self.probe_called = False
        self.execute_called = False

    def preflight(self, timeout_seconds: int = 15) -> CodexPreflightResult:
        self.preflight_called = True
        if error := self.settings.get("PREFLIGHT_ERROR"):
            raise CodexPreflightError(error)
        platform = self.settings.get(
            "SANDBOX_PLATFORM", "windows" if sys.platform == "win32" else "linux"
        )
        runtime_read_paths = ("/opt/memory-codex",)
        return CodexPreflightResult(
            executable="memory-codex",
            version="memory-codex 0.138.0",
            help_text="all required capabilities",
            version_tuple=(0, 138, 0),
            sandbox_platform=platform,
            runtime_read_paths=runtime_read_paths,
            runtime_read_paths_hash=sha256_bytes(canonical_json_bytes(list(runtime_read_paths))),
            probe_interpreter=sys.executable,
            effective_sandbox_path=(
                "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
                if platform == "linux"
                else None
            ),
        )

    def execute_sandbox_probe(
        self, command, workspace, environment_canary_name, environment_canary_value, timeout_seconds=30
    ):
        self.probe_called = True
        mode = self.settings.get("PROBE_MODE", "success")
        result_path = Path(command[-3])
        if mode in {"network_leak", "network_lying_leak"}:
            connection = socket.create_connection((command[-2], int(command[-1])), timeout=1)
            connection.close()
        result = {
            "workspace_read_succeeded": mode != "workspace_read_failure",
            "workspace_write_succeeded": mode != "workspace_write_failure",
            "outside_read_blocked": mode != "outside_read_leak",
            "outside_write_blocked": mode != "outside_write_leak",
            "environment_canary_absent": mode != "environment_leak",
            "network_connect_blocked": mode != "network_leak",
            "unrelated_home_read_blocked": mode != "home_read_leak",
        }
        result_path.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")), "utf-8")
        return subprocess.CompletedProcess(command, 0, b"memory probe stdout\n", b"memory probe stderr\n")

    def execute(self, command, workspace, raw_events_path, stderr_path, timeout_seconds):
        self.execute_called = True
        mode = self.settings.get("MODE", "success")
        thread_id = self.settings.get("THREAD_ID", "thread-default")
        workspace = Path(workspace)
        final_path = Path(command[command.index("--output-last-message") + 1])
        prompt = command[-1]
        workspace.joinpath("received_prompt.txt").write_text(prompt, "utf-8")
        workspace.joinpath("codex_edit.txt").write_text("edited by memory Codex\n", "utf-8")
        final_path.write_text("memory final message\n", "utf-8")
        lines: list[bytes] = []
        if mode == "missing_thread_id":
            lines.append(b'{"type":"thread.started"}\n')
        elif mode == "multiple_threads":
            lines.extend([
                json.dumps({"type": "thread.started", "thread_id": f"{thread_id}-one"}, separators=(",", ":")).encode() + b"\n",
                json.dumps({"type": "thread.started", "thread_id": f"{thread_id}-two"}, separators=(",", ":")).encode() + b"\n",
            ])
        elif mode != "no_thread":
            lines.append(json.dumps({"type": "thread.started", "thread_id": thread_id}, separators=(",", ":")).encode() + b"\n")
        payloads = {
            "invalid": b"not-json\n",
            "failure": b'{"type":"error","message":"fake failure"}\n',
            "error_zero": b'{"type":"error","message":"fake failure"}\n',
            "turn_failed": b'{"type":"turn.failed","error":{"message":"boom"}}\n',
            "item_failed": b'{"type":"item.failed","error":{"message":"boom"}}\n',
            "web_search": b'{"type":"item.completed","item":{"type":"web_search"}}\n',
            "mcp": b'{"type":"item.started","item":{"type":"mcp_tool_call"}}\n',
            "unknown_item": b'{"type":"item.completed","item":{"type":"novel_local_item"}}\n',
        }
        if mode in payloads:
            lines.append(payloads[mode])
        else:
            lines.extend([
                b'{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n',
                b'{"type":"future.unknown","payload":1}\n',
            ])
        Path(raw_events_path).write_bytes(b"".join(lines))
        Path(stderr_path).write_bytes(f"memory stderr: {mode}\n".encode())
        if mode == "timeout":
            return CodexProcessResult(None, timed_out=True, failure_reason="Codex process timed out")
        return CodexProcessResult(7 if mode == "failure" else 0)


@pytest.fixture
def memory_codex_factory():
    def create(**settings: str) -> InMemoryCodexAdapter:
        return InMemoryCodexAdapter(**settings)

    return create
