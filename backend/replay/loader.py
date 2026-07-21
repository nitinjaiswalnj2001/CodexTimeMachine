"""Fail-closed replay source validation."""
from dataclasses import dataclass
from pathlib import Path
from backend.intervention.models import GhostIntervention,InterventionManifest,InterventionStatus,ReplayIntervention
from backend.intervention.loader import load_intervention_inputs
from backend.assessment.models import AssessmentManifest
from backend.runs.models import RunManifest,RunStatus
from backend.runs.workspace import compute_workspace_tree_hash
from backend.temporal.audit import BoundaryAuditor
from backend.temporal.integrity import canonical_json_bytes,sha256_bytes,sha256_file
from backend.temporal.models import SnapshotManifest
from backend.trajectory.models import ObservableTrajectory,TrajectoryManifest

class ReplayInputError(RuntimeError): pass
@dataclass(frozen=True)
class ReplayInputs:
    run_directory:Path; intervention_directory:Path; baseline:RunManifest; trajectory:ObservableTrajectory
    intervention:GhostIntervention; payload:ReplayIntervention; intervention_manifest:InterventionManifest
    assessment_directory:Path; evaluator_thread_id:str
    snapshot_directory:Path; snapshot_manifest:SnapshotManifest; task:str; input_hashes:dict[str,str]

def load_replay_inputs(run_directory,intervention_directory):
    run,idir=Path(run_directory).resolve(),Path(intervention_directory).resolve()
    p={"run_manifest.json":run/"run_manifest.json","raw_codex_events.jsonl":run/"raw_codex_events.jsonl","final_message.txt":run/"final_message.txt",
       "trajectory.json":run/"trajectory/trajectory.json","trajectory.md":run/"trajectory/trajectory.md","trajectory_manifest.json":run/"trajectory/trajectory_manifest.json",
       "ghost_intervention.json":idir/"ghost_intervention.json","replay_intervention.json":idir/"replay_intervention.json","intervention_manifest.json":idir/"intervention_manifest.json"}
    try:
        b=RunManifest.model_validate_json(p["run_manifest.json"].read_text("utf-8")); t=ObservableTrajectory.model_validate_json(p["trajectory.json"].read_text("utf-8")); tm=TrajectoryManifest.model_validate_json(p["trajectory_manifest.json"].read_text("utf-8")); i=GhostIntervention.model_validate_json(p["ghost_intervention.json"].read_text("utf-8")); rp=ReplayIntervention.model_validate_json(p["replay_intervention.json"].read_text("utf-8")); im=InterventionManifest.model_validate_json(p["intervention_manifest.json"].read_text("utf-8"))
    except Exception as exc: raise ReplayInputError(f"invalid replay source: {exc}") from exc
    assessment_dir = run.joinpath(*im.assessment_source_directory.split("/")).resolve()
    if not assessment_dir.is_relative_to(run) or assessment_dir == run:
        raise ReplayInputError("assessment source directory escapes the accepted run directory")
    if not assessment_dir.is_dir():
        raise ReplayInputError("manifested assessment source directory is missing")
    for required in ("blind_spot_assessment.json", "assessment_manifest.json"):
        if not (assessment_dir / required).is_file():
            raise ReplayInputError(f"manifested assessment source is missing {required}")
    try:
        assessment, assessment_manifest, context, _ = load_intervention_inputs(assessment_dir)
    except Exception as exc:
        raise ReplayInputError(f"invalid manifested assessment source: {exc}") from exc
    th=sha256_bytes(canonical_json_bytes(t.model_dump(mode="json",exclude={"trajectory_hash","extracted_at"})))
    ih=sha256_bytes(canonical_json_bytes(i.model_dump(mode="json",exclude={"intervention_hash","created_at"})))
    checks=[(b.run_status is RunStatus.SUCCEEDED,"baseline run is not SUCCEEDED"),(b.workspace_end_hash is not None,"baseline workspace end hash is missing"),(compute_workspace_tree_hash(run/"workspace")==b.workspace_end_hash,"baseline workspace integrity mismatch"),(sha256_file(p["raw_codex_events.jsonl"])==b.raw_events_sha256,"baseline raw event hash mismatch"),(not p["final_message.txt"].exists() or sha256_file(p["final_message.txt"])==b.final_message_sha256,"baseline final message hash mismatch"),(th==t.trajectory_hash==tm.trajectory_hash,"baseline trajectory hash mismatch"),(tm.trajectory_json_sha256==sha256_file(p["trajectory.json"]),"baseline trajectory output hash mismatch"),(tm.trajectory_markdown_sha256==sha256_file(p["trajectory.md"]),"baseline trajectory Markdown hash mismatch"),(ih==i.intervention_hash==im.intervention_hash,"intervention hash mismatch"),(im.replay_intervention_hash==sha256_file(p["replay_intervention.json"]),"replay payload hash mismatch"),(rp.intervention_id==i.intervention_id and rp.intervention_hash==i.intervention_hash and rp.clue==i.clue and bool(rp.clue),"replay payload differs from approved clue"),(i.status is InterventionStatus.INTERVENTION_GENERATED,"no approved intervention"),(i.assessment_id==assessment.assessment_id and i.assessment_hash==assessment.assessment_hash and i.context_id==context.context_id,"assessment/context lineage mismatch"),(b.run_id==t.run_id==i.run_id==im.run_id,"run lineage mismatch"),(b.scenario_id==t.scenario_id==i.scenario_id==im.scenario_id,"scenario lineage mismatch")]
    for ok,msg in checks:
        if not ok: raise ReplayInputError(msg)
    for name,expected in im.output_file_hashes.items():
        if not (idir/name).is_file() or sha256_file(idir/name)!=expected: raise ReplayInputError(f"intervention output hash mismatch: {name}")
    phase6_inputs = {
        "blind_spot_assessment.json": assessment_dir / "blind_spot_assessment.json",
        "assessment_manifest.json": assessment_dir / "assessment_manifest.json",
        "evaluation_context.json": run / "evaluation/evaluation_context.json",
        "evaluation_manifest.json": run / "evaluation/evaluation_manifest.json",
    }
    for name, expected in im.input_file_hashes.items():
        source_path = phase6_inputs.get(name)
        if source_path is None or not source_path.is_file() or sha256_file(source_path) != expected:
            raise ReplayInputError(f"intervention input hash mismatch: {name}")
    isolation_files={"isolation_probe.json":b.isolation_probe_result_sha256,"isolation_probe_command.json":b.isolation_probe_command_sha256,"isolation_probe_stdout.log":b.isolation_probe_stdout_sha256,"isolation_probe_stderr.log":b.isolation_probe_stderr_sha256}
    for name,expected in isolation_files.items():
        if expected is not None and (not (run/name).is_file() or sha256_file(run/name)!=expected): raise ReplayInputError(f"baseline isolation evidence hash mismatch: {name}")
    project=Path(__file__).resolve().parents[2]; snapshot=project/"backend/scenarios"/b.scenario_id.replace("-","_")/"sealed_snapshot"
    try: BoundaryAuditor().audit(snapshot); sm=SnapshotManifest.model_validate_json((snapshot/"manifest.json").read_text("utf-8"))
    except Exception as exc: raise ReplayInputError(f"invalid sealed base snapshot: {exc}") from exc
    if sm.snapshot_root_hash!=b.base_snapshot_hash or sm.scenario_id!=b.scenario_id: raise ReplayInputError("sealed snapshot identity mismatch")
    input_hashes = {n:sha256_file(x) for n,x in p.items() if x.is_file()}
    input_hashes.update({
        "assessment_source/blind_spot_assessment.json": sha256_file(assessment_dir / "blind_spot_assessment.json"),
        "assessment_source/assessment_manifest.json": sha256_file(assessment_dir / "assessment_manifest.json"),
        "evaluation/evaluation_context.json": sha256_file(run / "evaluation/evaluation_context.json"),
        "evaluation/evaluation_manifest.json": sha256_file(run / "evaluation/evaluation_manifest.json"),
        "sealed_snapshot/manifest.json": sha256_file(snapshot / "manifest.json"),
    })
    return ReplayInputs(run,idir,b,t,i,rp,im,assessment_dir,
        assessment_manifest.evaluator_thread_id,snapshot,sm,sm.task,
        input_hashes)
