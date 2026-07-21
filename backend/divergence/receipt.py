"""Run-level immutable acceptance receipt for a validated Phase 8 artifact."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from backend.temporal.integrity import canonical_json_bytes, sha256_bytes, sha256_file
from .models import DivergenceManifest, ObservableHistoryDivergence
from .validator import validate_divergence_artifact

class PhaseReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    phase: Literal["phase-8"] = "phase-8"
    artifact_id: str = Field(pattern=r"^div-[0-9a-f]{24}$")
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_directory_relative_path: str = Field(min_length=1)
    source_lineage_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

def _canonical(value):
    return sha256_bytes(canonical_json_bytes(value.model_dump(mode="json",exclude={"divergence_hash","created_at"})))

def _input_path(run, manifest, key, replay_directory):
    if key.startswith("replay_input/"):
        key = key.removeprefix("replay_input/")
        if key.startswith("assessment_source/"):
            from backend.intervention.models import InterventionManifest
            im=InterventionManifest.model_validate_json((run/"intervention"/"intervention_manifest.json").read_text("utf-8"))
            return run.joinpath(*im.assessment_source_directory.split("/")) / key.removeprefix("assessment_source/")
        if key.startswith("evaluation/"):
            return run / key
        if key.startswith("sealed_snapshot/"):
            from backend.runs.models import RunManifest
            scenario=RunManifest.model_validate_json((run/"run_manifest.json").read_text("utf-8")).scenario_id
            project=Path(__file__).resolve().parents[2]
            return project / "backend" / "scenarios" / scenario.replace("-", "_") / "sealed_snapshot" / key.removeprefix("sealed_snapshot/")
        if key in {"ghost_intervention.json", "intervention_manifest.json", "replay_intervention.json"}:
            return run / "intervention" / key
        if key in {"trajectory.json", "trajectory.md", "trajectory_manifest.json"}:
            return run / "trajectory" / key
        return run / key
    if key.startswith("replay/"):
        return replay_directory / key.removeprefix("replay/")
    if key.startswith("trajectory/"):
        return run / "trajectory" / key.removeprefix("trajectory/")
    return run / key

def build_receipt(run_directory, divergence_directory, *, replay_directory=None, created_at=None, output_relative_path=None):
    run=Path(run_directory).resolve(); output=Path(divergence_directory).resolve()
    if not output.is_relative_to(run): raise ValueError("divergence directory must remain inside run directory")
    if replay_directory is None:
        raise ValueError("receipt requires explicit replay directory")
    # One authoritative validation path, including deterministic structural semantics.
    validate_divergence_artifact(run, replay_directory, output)
    divergence=ObservableHistoryDivergence.model_validate_json((output/'history_divergence.json').read_text('utf-8'))
    manifest=DivergenceManifest.model_validate_json((output/'divergence_manifest.json').read_text('utf-8'))
    if _canonical(divergence)!=divergence.divergence_hash or divergence.divergence_hash!=manifest.divergence_hash: raise ValueError("canonical divergence hash mismatch")
    for name, expected in manifest.output_file_hashes.items():
        if sha256_file(output/name)!=expected: raise ValueError(f"output hash mismatch: {name}")
    relative = manifest.replay_directory_relative_path
    if replay_directory is None:
        if not relative: raise ValueError("receipt requires explicit replay directory for legacy divergence manifest")
        if Path(relative).is_absolute() or ".." in Path(relative).parts: raise ValueError("unsafe replay directory lineage")
        replay_directory = (run / relative).resolve()
    else:
        replay_directory = Path(replay_directory).resolve()
    if not replay_directory.is_relative_to(run) or not (replay_directory/"replay_manifest.json").is_file(): raise ValueError("invalid replay directory")
    if relative and replay_directory.relative_to(run).as_posix() != relative: raise ValueError("receipt replay directory mismatch")
    for name, expected in manifest.input_file_hashes.items():
        source = _input_path(run, manifest, name, replay_directory)
        if not source.is_file() or sha256_file(source) != expected:
            raise ValueError(f"input lineage hash mismatch: {name}")
    value=PhaseReceipt(artifact_id=divergence.divergence_id,artifact_hash=divergence.divergence_hash,manifest_sha256=sha256_file(output/'divergence_manifest.json'),output_directory_relative_path=output_relative_path or output.relative_to(run).as_posix(),source_lineage_hash=sha256_bytes(canonical_json_bytes(manifest.input_file_hashes)),created_at=created_at or datetime.now(timezone.utc))
    return value

def issue_receipt(run_directory, divergence_directory, *, replay_directory=None, created_at=None):
    run=Path(run_directory).resolve(); value=build_receipt(run,divergence_directory,replay_directory=replay_directory,created_at=created_at)
    receipts=run/'phase_receipts'; receipts.mkdir(exist_ok=True)
    path=receipts/'phase-8.json';payload=canonical_json_bytes(value.model_dump(mode='json'))+b'\n'
    if path.exists() and path.read_bytes()!=payload: raise ValueError("conflicting Phase 8 receipt already exists")
    if not path.exists(): path.write_bytes(payload)
    return value

def main(argv=None):
    parser=argparse.ArgumentParser(description='Issue a validated Phase 8 acceptance receipt.');parser.add_argument('--run-directory',required=True);parser.add_argument('--replay-directory');parser.add_argument('--divergence-directory',required=True);parser.add_argument('--fixed-created-at');a=parser.parse_args(argv)
    timestamp=datetime.fromisoformat(a.fixed_created_at.replace('Z','+00:00')) if a.fixed_created_at else None
    try: issue_receipt(a.run_directory,a.divergence_directory,replay_directory=a.replay_directory,created_at=timestamp)
    except Exception as exc: print(f'ERROR: {exc}');return 1
    return 0
if __name__=='__main__': raise SystemExit(main())
