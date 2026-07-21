"""Fail-closed cross-phase source loading for Phase 8."""
from dataclasses import dataclass
from pathlib import Path
from backend.runs.models import RunManifest,RunStatus
from backend.replay.models import ReplayStatus,ReplayExecutionMode
from backend.replay.runner import validate_replay_artifacts
from backend.intervention.models import InterventionManifest,ReplayIntervention
from backend.trajectory.models import ObservableTrajectory,TrajectoryManifest
from backend.temporal.integrity import canonical_json_bytes,sha256_bytes,sha256_file

class DivergenceInputError(RuntimeError):pass
@dataclass(frozen=True)
class DivergenceInputs:
    run_directory:Path; replay_directory:Path; run:RunManifest; baseline:ObservableTrajectory; replay_manifest:object; replay:ObservableTrajectory; intervention:InterventionManifest; input_hashes:dict[str,str]
def _trajectory_hash(t):return sha256_bytes(canonical_json_bytes(t.model_dump(mode="json",exclude={"trajectory_hash","extracted_at"})))
def _require_hash(path, expected, label):
    if not path.is_file(): raise DivergenceInputError(f"missing consumed source file: {label}")
    if sha256_file(path) != expected: raise DivergenceInputError(f"source hash mismatch: {label}")
def _replay_input_path(key, run, trajectory_dir, intervention_dir, assessment_dir, snapshot_dir):
    if key.startswith("assessment_source/"): return assessment_dir / key.split("/",1)[1]
    if key.startswith("evaluation/"): return run / key
    if key.startswith("sealed_snapshot/"): return snapshot_dir / key.split("/",1)[1]
    if key in {"ghost_intervention.json","replay_intervention.json","intervention_manifest.json"}: return intervention_dir / key
    if key in {"trajectory.json","trajectory.md","trajectory_manifest.json"}: return trajectory_dir / key
    return run / key
def load_inputs(run_directory,replay_directory):
    run=Path(run_directory).resolve(); replay_dir=Path(replay_directory).resolve(); td=run/"trajectory"; idir=run/"intervention"
    paths={"run_manifest.json":run/"run_manifest.json","raw_codex_events.jsonl":run/"raw_codex_events.jsonl","final_message.txt":run/"final_message.txt","trajectory/trajectory.json":td/"trajectory.json","trajectory/trajectory.md":td/"trajectory.md","trajectory/trajectory_manifest.json":td/"trajectory_manifest.json","replay/replay_manifest.json":replay_dir/"replay_manifest.json","replay/raw_replay_events.jsonl":replay_dir/"raw_replay_events.jsonl","replay/replay_trajectory.json":replay_dir/"replay_trajectory.json","replay/replay_trajectory.md":replay_dir/"replay_trajectory.md","replay/replay_trajectory_manifest.json":replay_dir/"replay_trajectory_manifest.json","intervention/intervention_manifest.json":idir/"intervention_manifest.json","intervention/replay_intervention.json":idir/"replay_intervention.json","intervention/ghost_intervention.json":idir/"ghost_intervention.json"}
    try:
        rm=RunManifest.model_validate_json(paths["run_manifest.json"].read_text("utf-8")); bt=ObservableTrajectory.model_validate_json(paths["trajectory/trajectory.json"].read_text("utf-8")); bm=TrajectoryManifest.model_validate_json(paths["trajectory/trajectory_manifest.json"].read_text("utf-8")); rpm=validate_replay_artifacts(replay_dir); rt=ObservableTrajectory.model_validate_json(paths["replay/replay_trajectory.json"].read_text("utf-8")); rtm=TrajectoryManifest.model_validate_json(paths["replay/replay_trajectory_manifest.json"].read_text("utf-8")); im=InterventionManifest.model_validate_json(paths["intervention/intervention_manifest.json"].read_text("utf-8")); rp=ReplayIntervention.model_validate_json(paths["intervention/replay_intervention.json"].read_text("utf-8"))
    except Exception as exc:raise DivergenceInputError(f"invalid divergence source: {exc}") from exc
    project=Path(__file__).resolve().parents[2]; snapshot=project/"backend/scenarios"/rm.scenario_id.replace("-","_")/"sealed_snapshot"
    assessment_dir=run.joinpath(*im.assessment_source_directory.split("/")).resolve()
    checks=[(rm.run_status is RunStatus.SUCCEEDED,"baseline run is not SUCCEEDED"),(rpm.status is ReplayStatus.SUCCEEDED,"replay is not SUCCEEDED"),(_trajectory_hash(bt)==bt.trajectory_hash==bm.trajectory_hash,"baseline trajectory hash mismatch"),(sha256_file(paths["trajectory/trajectory.json"])==bm.trajectory_json_sha256,"baseline trajectory output hash mismatch"),(sha256_file(paths["trajectory/trajectory.md"])==bm.trajectory_markdown_sha256,"baseline trajectory Markdown hash mismatch"),(sha256_file(paths["run_manifest.json"])==bm.source_manifest_sha256,"baseline run_manifest.json hash mismatch"),(sha256_file(paths["raw_codex_events.jsonl"])==bm.source_raw_events_sha256,"baseline raw_codex_events.jsonl hash mismatch"),(bm.source_final_message_sha256 is None or sha256_file(paths["final_message.txt"])==bm.source_final_message_sha256,"baseline final_message.txt hash mismatch"),(bt.event_count==len(bt.events)==bm.event_count,"baseline trajectory event-count mismatch"),(_trajectory_hash(rt)==rt.trajectory_hash==rtm.trajectory_hash==rpm.replay_trajectory_hash,"replay trajectory hash mismatch"),(sha256_file(paths["replay/replay_trajectory.json"])==rtm.trajectory_json_sha256,"replay trajectory output hash mismatch"),(sha256_file(paths["replay/replay_trajectory.md"])==rtm.trajectory_markdown_sha256,"replay trajectory Markdown hash mismatch"),(rt.event_count==len(rt.events)==rtm.event_count==rpm.normalized_event_count,"replay trajectory event-count mismatch"),(sha256_file(paths["raw_codex_events.jsonl"])==rm.raw_events_sha256==bt.source_raw_events_hash,"baseline raw event hash mismatch"),(sha256_file(paths["replay/raw_replay_events.jsonl"])==rpm.output_file_hashes.get("raw_replay_events.jsonl")==rt.source_raw_events_hash,"replay raw event hash mismatch"),(rm.run_id==bt.run_id==rpm.baseline_run_id,"run lineage mismatch"),(rm.scenario_id==bt.scenario_id==rt.scenario_id==rpm.scenario_id,"scenario mismatch"),(rm.base_snapshot_hash==rpm.base_snapshot_hash,"snapshot mismatch"),(rpm.intervention_id==im.intervention_id==rp.intervention_id and rpm.intervention_hash==im.intervention_hash==rp.intervention_hash,"intervention mismatch"),(rm.thread_id==rpm.baseline_thread_id and rpm.replay_thread_id==rt.thread_id and rm.thread_id!=rpm.replay_thread_id,"thread identity mismatch"),(rm.workspace_start_hash==rpm.baseline_workspace_start_hash==rpm.replay_workspace_start_hash,"workspace-start hash mismatch"),(rpm.isolation_result.probe_succeeded,"replay isolation failed"),(rpm.provider=="fake" and rpm.execution_mode is ReplayExecutionMode.DETERMINISTIC_FAKE and rpm.live_model_invoked is False,"only accepted controlled fake replay metadata is supported")]
    for ok,msg in checks:
        if not ok:raise DivergenceInputError(msg)
    for name, expected in im.output_file_hashes.items(): _require_hash(idir/name, expected, f"intervention/{name}")
    _require_hash(idir/"replay_intervention.json", im.replay_intervention_hash, "intervention/replay_intervention.json")
    for name, expected in im.input_file_hashes.items():
        location=(assessment_dir/name if name.startswith("assessment_") or name.startswith("blind_") else run/"evaluation"/name)
        _require_hash(location, expected, f"intervention input {name}")
    for name, expected in rpm.output_file_hashes.items(): _require_hash(replay_dir/name, expected, f"replay/{name}")
    replay_inputs={}
    for name, expected in rpm.input_file_hashes.items():
        location=_replay_input_path(name,run,td,idir,assessment_dir,snapshot)
        _require_hash(location,expected,f"replay input {name}"); replay_inputs[f"replay_input/{name}"]=expected
    all_hashes={k:sha256_file(v) for k,v in paths.items() if v.is_file()}
    all_hashes.update(replay_inputs)
    return DivergenceInputs(run,replay_dir,rm,bt,rpm,rt,im,all_hashes)
