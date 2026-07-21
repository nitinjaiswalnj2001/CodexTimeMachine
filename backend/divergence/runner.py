"""Phase 8 deterministic observable-history divergence runner and CLI."""
from __future__ import annotations
import argparse,os,shutil,sys,uuid
from datetime import datetime,timezone
from pathlib import Path
from backend.temporal.integrity import canonical_json_bytes,sha256_bytes,sha256_file
from .alignment import align_events
from .classifier import dimensions,first_structural_divergence,first_investigative_divergence,first_replay_evaluation_divergence,outcome
from .loader import load_inputs
from .models import DivergenceManifest,ObservableHistoryDivergence,POLICY_VERSION
from .normalizer import comparison_events
from .renderer import render

class DivergenceRunnerError(RuntimeError):pass
def _write(path,data):
    temp=path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}");temp.write_bytes(data);os.replace(temp,path)
def _protect(output,source):
    output=Path(output).resolve();run=source.run_directory
    if output==run or not output.is_relative_to(run):raise DivergenceRunnerError("divergence output must remain inside the baseline run directory")
    assessment=run.joinpath(*source.intervention.assessment_source_directory.split("/")).resolve()
    protected=[run/"workspace",run/"trajectory",run/"evaluation",assessment,run/"assessment_failures",run/"intervention",source.replay_directory,run/"replay_failures",run/"phase_receipts"]
    for root in protected:
        root=root.resolve()
        if output==root or output.is_relative_to(root) or root.is_relative_to(output):raise DivergenceRunnerError(f"divergence output overlaps protected evidence: {root.name}")
    for name in ("run_manifest.json","raw_codex_events.jsonl","final_message.txt"):
        path=(run/name).resolve()
        if output==path or output.is_relative_to(path) or path.is_relative_to(output):raise DivergenceRunnerError(f"divergence output targets protected run evidence: {name}")
def _hash(value):return sha256_bytes(canonical_json_bytes(value.model_dump(mode="json",exclude={"divergence_hash","created_at"})))
class DivergenceRunner:
    def run(self,run_directory,replay_directory,output_directory=None,*,created_at=None,overwrite=False,include_alignment_debug=False):
        source=load_inputs(run_directory,replay_directory);output=Path(output_directory).resolve() if output_directory else source.run_directory/"divergence";_protect(output,source)
        if output.exists() and not overwrite:raise DivergenceRunnerError(f"divergence output already exists; use --overwrite: {output}")
        receipt_path = source.run_directory / "phase_receipts" / "phase-8.json"
        if output == source.run_directory / "divergence" and receipt_path.exists() and overwrite:
            # A receipted canonical artifact is immutable.  An explicit receipt migration
            # is required for any replacement, so failure occurs before staging/mutation.
            from .receipt import PhaseReceipt
            existing = PhaseReceipt.model_validate_json(receipt_path.read_text("utf-8"))
            requested = created_at or datetime.now(timezone.utc)
            requested = requested if requested.tzinfo else requested.replace(tzinfo=timezone.utc)
            if existing.created_at != requested:
                raise DivergenceRunnerError("canonical divergence is protected by an existing Phase 8 receipt")
        timestamp=created_at or datetime.now(timezone.utc);timestamp=timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        b=comparison_events(source.baseline.events);r=comparison_events(source.replay.events)
        if not b or not r:raise DivergenceRunnerError("both trajectories must contain observable events")
        differences,summary=align_events(b,r)
        identity={"baseline_run_id":source.run.run_id,"replay_id":source.replay_manifest.replay_id,"baseline_trajectory_hash":source.baseline.trajectory_hash,"replay_trajectory_hash":source.replay.trajectory_hash,"intervention_id":source.intervention.intervention_id,"policy_version":POLICY_VERSION}
        divergence_id="div-"+sha256_bytes(canonical_json_bytes(identity))[:24]
        dimension_values=dimensions(b,r)
        bare=ObservableHistoryDivergence(divergence_id=divergence_id,baseline_run_id=source.run.run_id,replay_id=source.replay_manifest.replay_id,scenario_id=source.run.scenario_id,base_snapshot_hash=source.run.base_snapshot_hash,baseline_trajectory_hash=source.baseline.trajectory_hash,replay_trajectory_hash=source.replay.trajectory_hash,intervention_id=source.intervention.intervention_id,normalized_baseline_events=b,normalized_replay_events=r,alignment=summary,first_structural_divergence=first_structural_divergence(differences),first_investigative_divergence=first_investigative_divergence(differences,b,r),first_replay_evaluation_divergence=first_replay_evaluation_divergence(differences,r),event_differences=differences,behavioral_dimensions=dimension_values,observable_outcome=outcome(differences,b,r,dimension_values),limitations=["This comparison evaluates observable event histories only and does not reconstruct hidden chain-of-thought.","Lexical message classes and structural signatures do not establish technical correctness.","The replay differs from the baseline after receiving the approved clue, but this comparison alone does not prove that the clue caused every observed difference."],warnings=[],created_at=timestamp,divergence_hash="0"*64)
        value=bare.model_copy(update={"divergence_hash":_hash(bare)});json_bytes=canonical_json_bytes(value.model_dump(mode="json"))+b"\n";md=render(value).encode("utf-8")
        stage=output.parent/f".{output.name}.stage-{uuid.uuid4().hex}";stage.mkdir(parents=True)
        try:
            _write(stage/"history_divergence.json",json_bytes);_write(stage/"history_divergence.md",md)
            if include_alignment_debug:_write(stage/"alignment_debug.json",canonical_json_bytes({"baseline": [x.model_dump(mode="json") for x in b],"replay":[x.model_dump(mode="json") for x in r]})+b"\n")
            output_names=["history_divergence.json","history_divergence.md"] + (["alignment_debug.json"] if include_alignment_debug else [])
            outputs={name:sha256_file(stage/name) for name in output_names}
            manifest=DivergenceManifest(divergence_id=divergence_id,baseline_run_id=source.run.run_id,replay_id=source.replay_manifest.replay_id,scenario_id=source.run.scenario_id,base_snapshot_hash=source.run.base_snapshot_hash,baseline_trajectory_hash=source.baseline.trajectory_hash,replay_trajectory_hash=source.replay.trajectory_hash,intervention_id=source.intervention.intervention_id,divergence_hash=value.divergence_hash,replay_directory_relative_path=source.replay_directory.relative_to(source.run_directory).as_posix(),baseline_event_count=len(b),replay_event_count=len(r),matched_count=summary.matched_count,baseline_only_count=summary.baseline_only_count,replay_only_count=summary.replay_only_count,modified_count=summary.modified_count,reordered_count=summary.reordered_count,expanded_count=summary.expanded_count,contracted_count=summary.contracted_count,warning_count=0,input_file_hashes=source.input_hashes,output_file_hashes=outputs,created_at=timestamp)
            _write(stage/"divergence_manifest.json",canonical_json_bytes(manifest.model_dump(mode="json"))+b"\n")
            receipt_value = None
            receipt_payload = None
            receipt_path = source.run_directory / "phase_receipts" / "phase-8.json"
            if output == source.run_directory / "divergence":
                # Build and validate the candidate receipt against the staged artifact before
                # any accepted output is replaced.
                from .receipt import build_receipt
                receipt_value = build_receipt(source.run_directory, stage, replay_directory=source.replay_directory, created_at=timestamp, output_relative_path=output.relative_to(source.run_directory).as_posix())
                receipt_payload = canonical_json_bytes(receipt_value.model_dump(mode="json")) + b"\n"
                if receipt_path.exists() and receipt_path.read_bytes() != receipt_payload:
                    raise DivergenceRunnerError("conflicting Phase 8 receipt prevents canonical publication")
            backup=None
            if output.exists():backup=output.parent/f".{output.name}.backup-{uuid.uuid4().hex}";os.replace(output,backup)
            receipt_backup=None
            try:
                os.replace(stage,output)
                if receipt_payload is not None and not receipt_path.exists():
                    receipt_path.parent.mkdir(exist_ok=True)
                    receipt_temp=receipt_path.with_name(f".{receipt_path.name}.tmp-{uuid.uuid4().hex}")
                    receipt_temp.write_bytes(receipt_payload);os.replace(receipt_temp,receipt_path)
            except Exception:
                if backup:os.replace(backup,output)
                elif output.exists(): shutil.rmtree(output)
                raise
            if backup:shutil.rmtree(backup)
            return value,manifest
        finally:
            if stage.exists():shutil.rmtree(stage)
def main(argv=None):
    parser=argparse.ArgumentParser(description="Build deterministic observable history divergence evidence.");parser.add_argument("--run-directory",required=True);parser.add_argument("--replay-directory",required=True);parser.add_argument("--output-dir");parser.add_argument("--fixed-created-at");parser.add_argument("--overwrite",action="store_true");parser.add_argument("--include-alignment-debug",action="store_true");args=parser.parse_args(argv)
    try:
        timestamp=datetime.fromisoformat(args.fixed_created_at.replace("Z","+00:00")) if args.fixed_created_at else None
        value,_=DivergenceRunner().run(args.run_directory,args.replay_directory,args.output_dir,created_at=timestamp,overwrite=args.overwrite,include_alignment_debug=args.include_alignment_debug)
    except Exception as exc:print(f"ERROR: {exc}",file=sys.stderr);return 1
    a=value.alignment;print("OBSERVABLE HISTORY DIVERGENCE");print(f"Baseline run       {value.baseline_run_id}\nReplay             {value.replay_id}\nScenario           {value.scenario_id}\nBaseline events    {a.baseline_event_count}\nReplay events      {a.replay_event_count}\nMatched            {a.matched_count}\nBaseline-only      {a.baseline_only_count}\nReplay-only        {a.replay_only_count}\nModified           {a.modified_count}\nFirst structural   {value.first_structural_divergence.summary if value.first_structural_divergence else 'None'}\nFirst investigative {value.first_investigative_divergence.summary if value.first_investigative_divergence else 'None'}\nFirst replay evaluation {value.first_replay_evaluation_divergence.summary if value.first_replay_evaluation_divergence else 'None'}\nObservable outcome {value.observable_outcome.status.value}\nDivergence hash    {value.divergence_hash}\nOutput directory   {Path(args.output_dir).resolve() if args.output_dir else Path(args.run_directory).resolve()/'divergence'}");return 0
if __name__=="__main__":raise SystemExit(main())
