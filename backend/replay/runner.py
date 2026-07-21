"""Controlled counterfactual replay lifecycle and CLI."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from backend.runs.codex_cli import permission_profile_hash
from backend.runs.events import summarize_event_stream
from backend.runs.isolation import IsolationProbeRunner
from backend.runs.models import CodexExecutionConfiguration, IsolationProbeResult, RunKind
from backend.runs.workspace import RunWorkspaceBuilder, compute_workspace_tree_hash
from backend.temporal.integrity import canonical_json_bytes, sha256_bytes, sha256_file
from backend.trajectory.models import ObservableTrajectory, TrajectoryManifest
from backend.trajectory.normalizer import normalize_records
from backend.trajectory.parser import parse_raw_events
from .codex_provider import CodexReplayProvider
from .fake_provider import DeterministicFakeReplayProvider
from .loader import ReplayInputError, ReplayInputs, load_replay_inputs
from .models import (ReplayExecutionMode, ReplayFailureManifest, ReplayFailureStage,
    ReplayKind, ReplayManifest, ReplayStatus, SandboxBackend, SandboxPlatform)
from .prompt import build_replay_prompt
from .render import render_replay_trajectory

class ReplayRunnerError(RuntimeError): pass

REQUIRED_OUTPUTS=("replay_prompt.txt","raw_replay_events.jsonl","final_replay_message.txt",
 "replay_stderr.log","isolation_probe.json","isolation_probe_command.json",
 "replay_trajectory.json","replay_trajectory.md","replay_trajectory_manifest.json")

def _write(path: Path, data: bytes):
    temporary=path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    temporary.write_bytes(data);os.replace(temporary,path)

def _trajectory_hash(value):
    return sha256_bytes(canonical_json_bytes(value.model_dump(mode="json",exclude={"trajectory_hash","extracted_at"})))

def _output_ok(output,run,intervention,assessment_source=None):
    output,run,intervention=Path(output).resolve(),Path(run).resolve(),Path(intervention).resolve()
    if not output.is_relative_to(run) or output==run:
        raise ReplayRunnerError("replay output must remain inside baseline run directory")
    protected=[run/"workspace",run/"trajectory",run/"evaluation",run/"assessment",run/"assessment_failures",
               run/"intervention",intervention,run/"replay_failures"]
    protected.append(Path(assessment_source) if assessment_source is not None else run/"assessment")
    for root in protected:
        root=root.resolve()
        if output==root or output.is_relative_to(root) or root.is_relative_to(output):
            raise ReplayRunnerError(f"replay output overlaps protected evidence: {root.name}")
    protected_files = [run/"run_manifest.json", run/"raw_codex_events.jsonl",
        run/"final_message.txt", run/"isolation_probe.json",
        run/"isolation_probe_command.json"]
    for path in protected_files:
        path = path.resolve()
        if output == path or output.is_relative_to(path) or path.is_relative_to(output):
            raise ReplayRunnerError(f"replay output targets protected source evidence: {path.name}")

def _successful_output_hashes(stage):
    missing=[name for name in REQUIRED_OUTPUTS if not (stage/name).is_file()]
    if missing: raise ReplayRunnerError("successful replay is missing evidence: "+", ".join(missing))
    return {name:sha256_file(stage/name) for name in REQUIRED_OUTPUTS}

def validate_replay_outputs(directory):
    directory=Path(directory).resolve()
    manifest=ReplayManifest.model_validate_json((directory/"replay_manifest.json").read_text("utf-8"))
    for name,expected in manifest.output_file_hashes.items():
        path=directory/name
        if not path.is_file() or sha256_file(path)!=expected:
            raise ReplayRunnerError(f"replay output hash mismatch: {name}")
    trajectory=ObservableTrajectory.model_validate_json((directory/"replay_trajectory.json").read_text("utf-8"))
    if trajectory.trajectory_hash!=manifest.replay_trajectory_hash:
        raise ReplayRunnerError("replay trajectory hash mismatch")
    return manifest

validate_replay_artifacts=validate_replay_outputs

def _prior_replay_threads(run):
    result=set()
    for path in run.glob("replay*/replay_manifest.json"):
        try: result.add(ReplayManifest.model_validate_json(path.read_text("utf-8")).replay_thread_id)
        except (OSError,ValueError): pass
    return result

def _fake_isolation(source:ReplayInputs,provider,stage):
    effective={"permission_profile":source.baseline.permission_profile,
      "permission_profile_hash":source.baseline.permission_profile_hash,
      "runtime_read_paths":list(source.baseline.runtime_read_paths),
      "runtime_read_paths_hash":source.baseline.runtime_read_paths_hash,
      "sandbox_platform":SandboxPlatform.FAKE,
      "sandbox_backend":SandboxBackend.FAKE_ISOLATION,
      "effective_sandbox_path":source.baseline.effective_sandbox_path,
      "network_enabled":False,
      "approval_policy":source.baseline.approval_policy,
      "web_search_mode":source.baseline.web_search_mode}
    effective.update(provider.effective_overrides);provider.call_order.append("isolation")
    isolation=IsolationProbeResult(permission_profile=str(effective["permission_profile"]),platform="fake",
      workspace_read_succeeded=True,workspace_write_succeeded=True,outside_read_blocked=True,
      outside_write_blocked=True,environment_canary_absent=True,
      network_configured_disabled=effective["network_enabled"] is False,network_connect_blocked=True,
      unrelated_home_read_blocked=True,probe_succeeded=provider.isolation_succeeded,
      failure_reasons=[] if provider.isolation_succeeded else ["simulated isolation failure"],
      probe_output_hash=sha256_bytes(b"fake-isolation"))
    _write(stage/"isolation_probe.json",canonical_json_bytes(isolation.model_dump(mode="json"))+b"\n")
    _write(stage/"isolation_probe_command.json",canonical_json_bytes({"provider":"fake",**effective})+b"\n")
    return isolation,effective,None

def _real_isolation(source,provider,configuration,stage):
    preflight=provider.adapter.preflight(configuration.preflight_timeout_seconds)
    isolation=IsolationProbeRunner().run(provider.adapter,preflight,configuration,stage).result
    platform = SandboxPlatform(preflight.sandbox_platform)
    backend = {
      SandboxPlatform.LINUX: SandboxBackend.CODEX_LINUX_SANDBOX,
      SandboxPlatform.WINDOWS: SandboxBackend.ELEVATED_WINDOWS_SANDBOX,
      SandboxPlatform.MACOS: SandboxBackend.CODEX_MACOS_SANDBOX,
    }[platform]
    effective={"permission_profile":configuration.permission_profile,
      "permission_profile_hash":permission_profile_hash(preflight.runtime_read_paths),
      "runtime_read_paths":list(preflight.runtime_read_paths),
      "runtime_read_paths_hash":preflight.runtime_read_paths_hash,
      "sandbox_platform":platform,"sandbox_backend":backend,
      "effective_sandbox_path":preflight.effective_sandbox_path,
      "network_enabled":configuration.network_enabled,
      "approval_policy":configuration.approval_policy,"web_search_mode":configuration.web_search_mode}
    return isolation,effective,preflight

def _validate_parity(source,isolation,effective):
    checks=((source.baseline.isolation_probe_succeeded is True,"baseline isolation was not accepted"),
      (isolation.probe_succeeded,"replay isolation probe failed"),
      (effective["permission_profile"]==source.baseline.permission_profile,"permission profile mismatch"),
      (effective["permission_profile_hash"]==source.baseline.permission_profile_hash,"permission profile hash mismatch"),
      (effective["runtime_read_paths"]==source.baseline.runtime_read_paths,"runtime read paths mismatch"),
      (effective["runtime_read_paths_hash"]==source.baseline.runtime_read_paths_hash,"runtime read paths hash mismatch"),
      (((effective["sandbox_platform"] is SandboxPlatform.FAKE
          and effective["sandbox_backend"] is SandboxBackend.FAKE_ISOLATION)
        or (effective["sandbox_platform"] is not SandboxPlatform.FAKE
            and effective["sandbox_backend"] is not SandboxBackend.FAKE_ISOLATION)),
          "sandbox backend mismatch"),
      (effective["effective_sandbox_path"]==source.baseline.effective_sandbox_path,"effective sandbox path mismatch"),
      (effective["network_enabled"] is False and source.baseline.network_enabled is False,"network enabled replay is forbidden"),
      (effective["approval_policy"]==source.baseline.approval_policy,"approval policy mismatch"),
      (effective["web_search_mode"]==source.baseline.web_search_mode,"web search mode mismatch"))
    for valid,message in checks:
        if not valid: raise ReplayRunnerError(message)

class CounterfactualReplayRunner:
 def run(self,run_directory,intervention_directory,provider,output_directory=None,*,model=None,reasoning_effort=None,created_at=None,overwrite=False):
  run,intervention=Path(run_directory).resolve(),Path(intervention_directory).resolve()
  output=Path(output_directory).resolve() if output_directory else run/"replay"
  execution_mode=getattr(provider,"execution_mode",None)
  if execution_mode is ReplayExecutionMode.DETERMINISTIC_FAKE:
   if getattr(provider,"name",None)!="fake": raise ReplayRunnerError("deterministic fake execution requires the fake provider")
   if model is not None or reasoning_effort is not None:
    raise ReplayRunnerError("fake replay does not accept live model or reasoning configuration")
   configuration=None
  elif execution_mode is ReplayExecutionMode.LIVE_MODEL:
   if not model or not reasoning_effort:
    raise ReplayRunnerError("Live Codex replay requires explicit model selection and --confirm-live-model. Explicit reasoning effort is also required.")
   configuration=CodexExecutionConfiguration(model=model,reasoning_effort=reasoning_effort)
  else:
   raise ReplayRunnerError("replay provider has no supported explicit execution mode")
  source=load_replay_inputs(run,intervention)
  _output_ok(output,run,intervention,source.assessment_directory)
  if output.exists() and not overwrite: raise ReplayRunnerError(f"replay output already exists; use --overwrite: {output}")
  now=created_at or datetime.now(timezone.utc);now=now if now.tzinfo else now.replace(tzinfo=timezone.utc)
  attempt_id=f"attempt-{uuid.uuid4().hex}";stage=run/f".replay-{uuid.uuid4().hex}"
  phase=ReplayFailureStage.WORKSPACE_MATERIALIZATION;replay_id=None;backup=None
  try:
   identity={"baseline_run_id":source.baseline.run_id,"base_snapshot_hash":source.baseline.base_snapshot_hash,
     "intervention_hash":source.intervention.intervention_hash,"provider":provider.name,
     "provider_version":provider.version,"execution_mode":execution_mode}
   if execution_mode is ReplayExecutionMode.LIVE_MODEL:
    identity.update(model=model,reasoning_effort=reasoning_effort)
   replay_id=f"replay-{sha256_bytes(canonical_json_bytes(identity))[:24]}"
   prompt=build_replay_prompt(source.task,source.payload.clue or "")
   if prompt.count(source.payload.clue or "")!=1 or prompt.count(source.task)!=1:
    raise ReplayRunnerError("task and approved clue must each appear exactly once")
   stage.mkdir();prepared=RunWorkspaceBuilder().prepare(source.snapshot_directory,stage/"prepared")
   shutil.move(str(prepared.workspace),stage/"workspace");shutil.rmtree(stage/"prepared")
   start=compute_workspace_tree_hash(stage/"workspace")
   if start!=source.baseline.workspace_start_hash: raise ReplayRunnerError("replay workspace start hash differs from baseline")
   _write(stage/"replay_prompt.txt",prompt.encode("utf-8"));phase=ReplayFailureStage.ISOLATION_PROBE
   if execution_mode is ReplayExecutionMode.LIVE_MODEL: isolation,effective,preflight=_real_isolation(source,provider,configuration,stage)
   else: isolation,effective,preflight=_fake_isolation(source,provider,stage)
   _validate_parity(source,isolation,effective)
   phase=ReplayFailureStage.PROVIDER_EXECUTION;raw=stage/"raw_replay_events.jsonl";final=stage/"final_replay_message.txt";stderr=stage/"replay_stderr.log"
   result=provider.execute(prompt,stage/"workspace",configuration,raw,final,stderr,preflight=preflight)
   _write(raw,result.raw_event_bytes);_write(stderr,result.stderr_bytes)
   if result.final_response_bytes is not None:_write(final,result.final_response_bytes)
   if result.exit_code:raise ReplayRunnerError(f"replay provider exited with code {result.exit_code}")
   phase=ReplayFailureStage.PROVIDER_RESULT_VALIDATION;summary=summarize_event_stream(raw)
   if summary.thread_started_count!=1 or len(result.thread_ids)!=1 or not summary.thread_id:
    raise ReplayRunnerError("replay requires exactly one provider thread and one thread.started event")
   if result.thread_ids[0]!=summary.thread_id:raise ReplayRunnerError("provider thread ID does not match raw-event thread ID")
   thread=summary.thread_id;protected={source.baseline.thread_id,source.evaluator_thread_id}
   if execution_mode is ReplayExecutionMode.LIVE_MODEL or getattr(provider,"enforce_thread_freshness",False): protected.update(_prior_replay_threads(run))
   if thread in protected:
    label="previously accepted replay thread" if thread in _prior_replay_threads(run) else "protected prior thread"
    raise ReplayRunnerError(f"replay thread reuses {label}")
   if summary.has_error_event or summary.forbidden_item_types:raise ReplayRunnerError("replay contains failed or forbidden external-context events")
   if not final.is_file() or not final.read_bytes():raise ReplayRunnerError("replay final response is missing")
   phase=ReplayFailureStage.TRAJECTORY_EXTRACTION;records=parse_raw_events(raw)
   events,warnings=normalize_records(records,replay_id,stage/"workspace",final.read_text("utf-8"),sha256_file(final))
   source_hash=sha256_bytes(canonical_json_bytes(identity))
   bare=ObservableTrajectory(run_id=replay_id,scenario_id=source.baseline.scenario_id,thread_id=thread,
    run_kind=RunKind.REPLAY,source_manifest_hash=source_hash,source_raw_events_hash=sha256_file(raw),
    extracted_at=now,event_count=len(events),events=events,warnings=warnings,trajectory_hash="0"*64)
   trajectory=bare.model_copy(update={"trajectory_hash":_trajectory_hash(bare)})
   tj=canonical_json_bytes(trajectory.model_dump(mode="json"))+b"\n";tm=render_replay_trajectory(trajectory).encode()
   _write(stage/"replay_trajectory.json",tj);_write(stage/"replay_trajectory.md",tm)
   tmanifest=TrajectoryManifest(run_id=replay_id,extracted_at=now,source_manifest_sha256=source_hash,
    source_raw_events_sha256=sha256_file(raw),source_final_message_sha256=sha256_file(final),
    trajectory_json_sha256=sha256_bytes(tj),trajectory_markdown_sha256=sha256_bytes(tm),
    trajectory_hash=trajectory.trajectory_hash,event_count=len(events),warnings_count=len(warnings))
   _write(stage/"replay_trajectory_manifest.json",canonical_json_bytes(tmanifest.model_dump(mode="json"))+b"\n")
   end=compute_workspace_tree_hash(stage/"workspace");outputs=_successful_output_hashes(stage)
   manifest=ReplayManifest(replay_id=replay_id,status=ReplayStatus.SUCCEEDED,baseline_run_id=source.baseline.run_id,
    scenario_id=source.baseline.scenario_id,replay_kind=ReplayKind.COUNTERFACTUAL_WITH_MINIMUM_CLUE,
    base_snapshot_hash=source.baseline.base_snapshot_hash,baseline_workspace_start_hash=source.baseline.workspace_start_hash,
    replay_workspace_start_hash=start,replay_workspace_end_hash=end,baseline_thread_id=source.baseline.thread_id or "",
    replay_thread_id=thread,intervention_id=source.intervention.intervention_id,intervention_hash=source.intervention.intervention_hash,
    replay_prompt_hash=outputs["replay_prompt.txt"],provider=provider.name,provider_version=result.provider_version,
    execution_mode=execution_mode,
    live_model_invoked=(execution_mode is ReplayExecutionMode.LIVE_MODEL),
    requested_model=(model if execution_mode is ReplayExecutionMode.LIVE_MODEL else None),
    effective_model=(model if execution_mode is ReplayExecutionMode.LIVE_MODEL else "deterministic-fake-replay"),
    reasoning_effort=(reasoning_effort if execution_mode is ReplayExecutionMode.LIVE_MODEL else "deterministic"),permission_profile=effective["permission_profile"],
    permission_profile_hash=effective["permission_profile_hash"],runtime_read_paths=effective["runtime_read_paths"],
    runtime_read_paths_hash=effective["runtime_read_paths_hash"],
    sandbox_platform=effective["sandbox_platform"],sandbox_backend=effective["sandbox_backend"],
    effective_sandbox_path=effective["effective_sandbox_path"],
    network_enabled=effective["network_enabled"],approval_policy=effective["approval_policy"],
    web_search_mode=effective["web_search_mode"],isolation_result=isolation,raw_event_count=summary.event_count,
    normalized_event_count=len(events),replay_trajectory_hash=trajectory.trajectory_hash,input_file_hashes=source.input_hashes,
    output_file_hashes=outputs,created_at=now,warnings=warnings)
   _write(stage/"replay_manifest.json",canonical_json_bytes(manifest.model_dump(mode="json"))+b"\n");validate_replay_outputs(stage)
   phase=ReplayFailureStage.PUBLICATION;backup=output.parent/f".{output.name}.backup-{uuid.uuid4().hex}"
   if output.exists():output.replace(backup)
   stage.replace(output);validate_replay_outputs(output)
   if backup.exists():shutil.rmtree(backup)
   return manifest
  except Exception as exc:
   if backup is not None and backup.exists():
    if output.exists():shutil.rmtree(output)
    backup.replace(output)
   failure=run/"replay_failures"/attempt_id;failure.parent.mkdir(parents=True,exist_ok=True)
   if stage.exists():stage.replace(failure)
   else:failure.mkdir()
   hashes={p.name:sha256_file(p) for p in failure.iterdir() if p.is_file()}
   fm=ReplayFailureManifest(attempt_id=attempt_id,baseline_run_id=source.baseline.run_id,replay_id=replay_id,
    stage=phase,reason=str(exc),created_at=now,artifact_hashes=hashes)
   _write(failure/"replay_failure_manifest.json",canonical_json_bytes(fm.model_dump(mode="json"))+b"\n")
   if isinstance(exc,(ReplayInputError,ReplayRunnerError)):raise
   raise ReplayRunnerError(str(exc)) from exc

def _time(value):
 result=datetime.fromisoformat(value.replace("Z","+00:00"));return result if result.tzinfo else result.replace(tzinfo=timezone.utc)

def main(argv=None):
 p=argparse.ArgumentParser(description="Execute an isolated replay without comparing trajectories")
 p.add_argument("--run-directory",required=True,type=Path);p.add_argument("--intervention-directory",required=True,type=Path)
 p.add_argument("--provider",choices=("fake","codex"),default="fake");p.add_argument("--output-dir",type=Path)
 p.add_argument("--model",help="Explicit live Codex model; required only with --provider codex")
 p.add_argument("--reasoning-effort",help="Explicit live reasoning effort; required only with --provider codex")
 p.add_argument("--confirm-live-model",action="store_true",help="Required opt-in before a live Codex replay")
 p.add_argument("--fixed-created-at",type=_time);p.add_argument("--overwrite",action="store_true");a=p.parse_args(argv)
 try:
  if a.provider=="codex":
   if not a.model or not a.confirm_live_model:
    raise ReplayRunnerError("Live Codex replay requires explicit model selection and --confirm-live-model.")
   if not a.reasoning_effort:
    raise ReplayRunnerError("Live Codex replay requires explicit reasoning effort.")
  elif a.model or a.reasoning_effort or a.confirm_live_model:
   raise ReplayRunnerError("fake replay does not accept live model, reasoning, or confirmation options")
  provider=DeterministicFakeReplayProvider() if a.provider=="fake" else CodexReplayProvider()
  m=CounterfactualReplayRunner().run(a.run_directory,a.intervention_directory,provider,a.output_dir,
    model=a.model,reasoning_effort=a.reasoning_effort,created_at=a.fixed_created_at,overwrite=a.overwrite)
  print("CONTROLLED COUNTERFACTUAL REPLAY\n");print(f"Baseline run       {m.baseline_run_id}\nScenario           {m.scenario_id}\nReplay ID          {m.replay_id}\nProvider           {m.provider}\nExecution mode     {m.execution_mode}\nLive model invoked {'YES' if m.live_model_invoked else 'NO'}\nModel              {m.effective_model}\nBaseline thread    {m.baseline_thread_id}\nReplay thread      {m.replay_thread_id}\nIntervention       {m.intervention_id}\nIsolation          SUCCEEDED\nRaw events         {m.raw_event_count}\nNormalized events  {m.normalized_event_count}\nReplay trajectory  {m.replay_trajectory_hash}\nOutput directory   {(a.output_dir or a.run_directory/'replay').resolve()}");return 0
 except (ReplayInputError,ReplayRunnerError,OSError,ValueError) as exc:print(f"ERROR: {exc}",file=sys.stderr);return 1

if __name__=="__main__":raise SystemExit(main())
