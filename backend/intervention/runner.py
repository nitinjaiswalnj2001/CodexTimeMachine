"""Phase 6 deterministic Ghost Engineer intervention lifecycle."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from backend.temporal.integrity import canonical_json_bytes, sha256_bytes, sha256_file

from .loader import InterventionInputError, load_intervention_inputs
from .models import (GhostIntervention, InterventionManifest, InterventionStatus,
                     ReplayIntervention)
from .policy import DeterministicInterventionPolicy
from .provider import FakeInterventionProvider, InterventionProvider
from .renderer import render_intervention
from .validator import (CONSTRAINTS, InterventionValidationError, clue_word_count,
                        validate_generated_intervention)


class InterventionRunnerError(RuntimeError):
    pass


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_bytes(data); os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()


def _intervention_hash(value: GhostIntervention) -> str:
    return sha256_bytes(canonical_json_bytes(value.model_dump(mode="json", exclude={"intervention_hash", "created_at"})))


def _validate_output(output: Path, run_dir: Path, assessment_dir: Path) -> None:
    run_dir = run_dir.resolve()
    if not output.is_relative_to(run_dir):
        raise InterventionRunnerError("intervention output must resolve inside the accepted run directory")
    if output == run_dir:
        raise InterventionRunnerError("intervention output overlaps protected run evidence")
    protected_roots = [run_dir / "workspace", run_dir / "trajectory", run_dir / "evaluation",
                       assessment_dir, run_dir / "assessment_failures"]
    for root in protected_roots:
        root = root.resolve()
        if output == root or output.is_relative_to(root) or root.is_relative_to(output):
            raise InterventionRunnerError(f"intervention output overlaps protected evidence directory: {root.name}")
    protected_files = [run_dir / "run_manifest.json", run_dir / "raw_codex_events.jsonl",
                       run_dir / "final_message.txt", assessment_dir / "blind_spot_assessment.json",
                       assessment_dir / "assessment_manifest.json", run_dir / "evaluation/evaluation_context.json",
                       run_dir / "evaluation/evaluation_manifest.json"]
    for path in protected_files:
        path = path.resolve()
        if output == path or output.is_relative_to(path) or path.is_relative_to(output):
            raise InterventionRunnerError(f"intervention output targets protected source evidence: {path.name}")


def _validate_packet_overlap(output: Path, run_dir: Path, evaluation_manifest) -> None:
    outcome_hash = evaluation_manifest.input_file_hashes.get("outcome.yaml")
    future_hashes = set(evaluation_manifest.future_evidence_file_hashes.values())
    candidates: list[Path] = []
    for ancestor in (output, *output.parents):
        if not ancestor.is_relative_to(run_dir):
            break
        candidates.append(ancestor / "outcome.yaml")
    if output.is_file():
        candidates.append(output)
    elif output.is_dir():
        candidates.extend(path for path in output.rglob("*") if path.is_file())
    for candidate in candidates:
        try:
            if candidate.is_file():
                digest = sha256_file(candidate)
                if (candidate.name == "outcome.yaml" and digest == outcome_hash) or digest in future_hashes:
                    raise InterventionRunnerError("intervention output overlaps protected outcome or future evidence")
        except OSError as exc:
            raise InterventionRunnerError(f"could not validate intervention output evidence overlap: {exc}") from exc


class InterventionRunner:
    def run(self, assessment_directory: str | Path, generator: InterventionProvider,
            output_directory: str | Path | None = None, *, created_at: datetime | None = None,
            overwrite: bool = False) -> GhostIntervention:
        assessment_dir = Path(assessment_directory).resolve(); run_dir = assessment_dir.parent
        try:
            assessment_source_directory = assessment_dir.relative_to(run_dir).as_posix()
        except ValueError as exc:
            raise InterventionRunnerError(
                "assessment source must resolve inside the accepted run directory"
            ) from exc
        output = Path(output_directory).resolve() if output_directory else run_dir / "intervention"
        _validate_output(output, run_dir, assessment_dir)
        assessment, assessment_manifest, context, evaluation_manifest = load_intervention_inputs(assessment_dir)
        _validate_packet_overlap(output, run_dir, evaluation_manifest)
        generated = generator.generate(assessment, context)
        warnings = validate_generated_intervention(generated, assessment, context)
        target = next((item for item in assessment.target_assessments if item.target_id == generated.target_id), None)
        semantic = canonical_json_bytes({"assessment_hash": assessment.assessment_hash,
            "generator": generator.name, "generator_version": generator.version,
            "generated": generated.model_dump(mode="json")})
        intervention_id = f"int-{sha256_bytes(semantic)[:24]}"
        timestamp = created_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None: timestamp = timestamp.replace(tzinfo=timezone.utc)
        unhashed = GhostIntervention(intervention_id=intervention_id, status=generated.status,
            run_id=assessment.run_id, scenario_id=assessment.scenario_id, context_id=assessment.context_id,
            assessment_id=assessment.assessment_id, assessment_hash=assessment.assessment_hash,
            target_id=generated.target_id, blind_spot_category=target.category if target else None,
            intervention_type=generated.intervention_type, clue=generated.clue, reason=generated.reason,
            rationale=generated.rationale,
            supporting_assessment_references=generated.supporting_assessment_references,
            constraints=CONSTRAINTS, warnings=warnings, created_at=timestamp, intervention_hash="0" * 64)
        intervention = unhashed.model_copy(update={"intervention_hash": _intervention_hash(unhashed)})
        replay = ReplayIntervention(intervention_id=intervention_id,
            intervention_hash=intervention.intervention_hash, clue=intervention.clue)
        intervention_json = canonical_json_bytes(intervention.model_dump(mode="json")) + b"\n"
        replay_json = canonical_json_bytes(replay.model_dump(mode="json")) + b"\n"
        markdown = render_intervention(intervention).encode("utf-8")
        replay_hash = sha256_bytes(replay_json)
        inputs = {"blind_spot_assessment.json": sha256_file(assessment_dir / "blind_spot_assessment.json"),
                  "assessment_manifest.json": sha256_file(assessment_dir / "assessment_manifest.json"),
                  "evaluation_context.json": sha256_file(run_dir / "evaluation/evaluation_context.json"),
                  "evaluation_manifest.json": sha256_file(run_dir / "evaluation/evaluation_manifest.json")}
        outputs = {"ghost_intervention.json": sha256_bytes(intervention_json),
                   "ghost_intervention.md": sha256_bytes(markdown),
                   "replay_intervention.json": replay_hash}
        manifest = InterventionManifest(intervention_id=intervention_id, status=intervention.status,
            run_id=intervention.run_id, scenario_id=intervention.scenario_id,
            context_id=intervention.context_id, assessment_id=intervention.assessment_id,
            assessment_hash=intervention.assessment_hash,
            assessment_source_directory=assessment_source_directory,
            generator_type=generator.name,
            generator_version=generator.version, intervention_hash=intervention.intervention_hash,
            replay_intervention_hash=replay_hash, input_file_hashes=inputs,
            output_file_hashes=outputs, warning_count=len(warnings), created_at=timestamp)
        manifest_json = canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"
        if output.exists() and not overwrite:
            raise InterventionRunnerError(f"intervention output already exists; use --overwrite: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        stage = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
        backup = output.parent / f".{output.name}.backup-{uuid.uuid4().hex}"
        try:
            stage.mkdir(); _atomic_write(stage / "ghost_intervention.json", intervention_json)
            _atomic_write(stage / "ghost_intervention.md", markdown)
            _atomic_write(stage / "replay_intervention.json", replay_json)
            _atomic_write(stage / "intervention_manifest.json", manifest_json)
            if output.exists(): output.replace(backup)
            stage.replace(output)
            if backup.exists(): shutil.rmtree(backup)
        except OSError as exc:
            if not output.exists() and backup.exists(): backup.replace(output)
            raise InterventionRunnerError(f"could not publish intervention: {exc}") from exc
        finally:
            if stage.exists(): shutil.rmtree(stage)
            if backup.exists() and output.exists(): shutil.rmtree(backup)
        return intervention


def _timestamp(value: str) -> datetime:
    try: result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc: raise argparse.ArgumentTypeError(str(exc)) from exc
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a minimum future clue without executing replay")
    parser.add_argument("--assessment-directory", required=True, type=Path)
    parser.add_argument("--generator", choices=("policy", "fake"), default="policy")
    parser.add_argument("--output-dir", type=Path); parser.add_argument("--fixed-created-at", type=_timestamp)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        policy = DeterministicInterventionPolicy()
        generator = policy
        if args.generator == "fake":
            assessment, _, context, _ = load_intervention_inputs(args.assessment_directory)
            generator = FakeInterventionProvider(policy.generate(assessment, context))
        value = InterventionRunner().run(args.assessment_directory, generator, args.output_dir,
            created_at=args.fixed_created_at, overwrite=args.overwrite)
        destination = args.output_dir or args.assessment_directory.parent / "intervention"
        print("GHOST ENGINEER INTERVENTION\n")
        print(f"Run                {value.run_id}\nScenario           {value.scenario_id}\nAssessment         {value.assessment_id}")
        print(f"Status             {value.status}\nIntervention type  {value.intervention_type or 'none'}")
        print(f"Clue words         {clue_word_count(value.clue)}\nLeakage validation PASS")
        print(f"Intervention hash  {value.intervention_hash}\nOutput directory   {Path(destination).resolve()}")
        return 0
    except (InterventionInputError, InterventionRunnerError, InterventionValidationError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
