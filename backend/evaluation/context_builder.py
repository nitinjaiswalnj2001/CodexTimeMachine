"""Build a deterministic Phase 4 temporal evaluation context."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from backend.runs.models import RunManifest, RunStatus
from backend.temporal.integrity import canonical_json_bytes, sha256_bytes, sha256_file
from backend.trajectory.models import ObservableTrajectory, TrajectoryManifest

from .integrity import EvaluationIntegrityError, trajectory_hash, validate_future_boundary
from .loader import EvaluationPacketError, load_outcome_packet
from .models import EvaluationContext, EvaluationManifest
from .render import render_context


class EvaluationContextError(RuntimeError):
    pass


def _load_json(path: Path, model):
    try:
        return model.model_validate_json(path.read_text("utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise EvaluationContextError(f"invalid {path.name}: {exc}") from exc


def _context_hash(context: EvaluationContext) -> str:
    return sha256_bytes(canonical_json_bytes(context.model_dump(mode="json", exclude={"context_hash", "created_at"})))


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _is_within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _validate_output_location(
    output: Path,
    run_directory: Path,
    workspace: Path,
    trajectory_directory: Path,
    packet_path: Path,
    evidence_files: dict[str, Path],
) -> None:
    """Reject output locations that can replace or contain source evidence."""
    packet_directory = packet_path.parent.resolve()
    if output == run_directory:
        raise EvaluationContextError("evaluation output targets protected run evidence")
    for root, message in (
        (workspace, "evaluation output overlaps protected workspace"),
        (trajectory_directory, "evaluation output overlaps protected trajectory directory"),
        (packet_directory, "evaluation output overlaps protected outcome packet directory"),
    ):
        if _is_within(output, root):
            raise EvaluationContextError(message)

    protected_files = [
        run_directory / "run_manifest.json",
        run_directory / "raw_codex_events.jsonl",
        run_directory / "final_message.txt",
        run_directory / "isolation_probe.json",
        run_directory / "isolation_probe_command.json",
        trajectory_directory / "trajectory.json",
        trajectory_directory / "trajectory.md",
        trajectory_directory / "trajectory_manifest.json",
        packet_path,
        *evidence_files.values(),
    ]
    for protected in protected_files:
        protected = protected.resolve()
        if output == protected or protected.is_relative_to(output) or output.is_relative_to(protected):
            raise EvaluationContextError(f"evaluation output targets protected source evidence: {protected.name}")


class EvaluationContextBuilder:
    def build(self, run_directory: str | Path, trajectory_directory: str | Path,
              outcome_packet: str | Path, output_directory: str | Path | None = None,
              *, created_at: datetime | None = None, overwrite: bool = False) -> EvaluationContext:
        run_dir = Path(run_directory).resolve()
        trajectory_dir = Path(trajectory_directory).resolve()
        packet_path = Path(outcome_packet).resolve()
        # Preserve the lexical workspace entry until boundary validation so a
        # symlink cannot be hidden by Path.resolve().
        workspace = run_dir / "workspace"
        output = Path(output_directory).resolve() if output_directory else run_dir / "evaluation"
        run_path = run_dir / "run_manifest.json"
        raw_path = run_dir / "raw_codex_events.jsonl"
        trajectory_path = trajectory_dir / "trajectory.json"
        trajectory_md = trajectory_dir / "trajectory.md"
        trajectory_manifest_path = trajectory_dir / "trajectory_manifest.json"
        run = _load_json(run_path, RunManifest)
        trajectory = _load_json(trajectory_path, ObservableTrajectory)
        trajectory_manifest = _load_json(trajectory_manifest_path, TrajectoryManifest)
        packet, evidence_files = load_outcome_packet(packet_path)
        _validate_output_location(output, run_dir, workspace, trajectory_dir, packet_path, evidence_files)
        if run.run_status is not RunStatus.SUCCEEDED:
            raise EvaluationContextError("evaluation context requires a SUCCEEDED run")
        raw_hash = sha256_file(raw_path)
        checks = [
            (run.raw_events_sha256 == raw_hash, "raw event hash mismatch"),
            (trajectory.source_raw_events_hash == raw_hash, "trajectory source raw-event hash mismatch"),
            (trajectory.source_manifest_hash == sha256_file(run_path), "trajectory source manifest hash mismatch"),
            (trajectory_hash(trajectory) == trajectory.trajectory_hash, "trajectory canonical hash mismatch"),
            (trajectory_manifest.trajectory_json_sha256 == sha256_file(trajectory_path), "trajectory output hash mismatch"),
            (trajectory_manifest.trajectory_markdown_sha256 == sha256_file(trajectory_md), "trajectory Markdown hash mismatch"),
            (trajectory_manifest.trajectory_hash == trajectory.trajectory_hash, "trajectory manifest identity mismatch"),
            (trajectory.event_count == len(trajectory.events) == trajectory_manifest.event_count, "trajectory event-count mismatch"),
            (run.run_id == trajectory.run_id == trajectory_manifest.run_id, "run ID mismatch"),
            (run.scenario_id == trajectory.scenario_id == packet.scenario_id, "scenario mismatch"),
            (run.thread_id == trajectory.thread_id, "thread mismatch"),
            (run.base_snapshot_hash == packet.base_snapshot_hash, "base snapshot mismatch"),
        ]
        for valid, message in checks:
            if not valid:
                raise EvaluationContextError(message)
        boundary = validate_future_boundary(
            workspace,
            packet_path.parent,
            packet.evidence_items,
            evidence_files,
            run.workspace_end_hash,
        )
        timestamp = created_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        identity_source = f"{run.run_id}\0{trajectory.trajectory_hash}\0{packet.outcome_id}\0{packet.packet_hash}"
        context_id = f"ctx-{sha256_bytes(identity_source.encode())[:24]}"
        unhashed = EvaluationContext(
            context_id=context_id, run_id=run.run_id, scenario_id=run.scenario_id,
            run_kind=run.run_kind, base_snapshot_hash=run.base_snapshot_hash,
            thread_id=trajectory.thread_id, trajectory_hash=trajectory.trajectory_hash,
            outcome_id=packet.outcome_id, outcome_packet_hash=packet.packet_hash,
            provenance_type=packet.provenance_type, fixture_notice=packet.fixture_notice,
            decision_under_evaluation=packet.decision_under_evaluation,
            known_future_outcome=packet.known_future_outcome,
            past_observable_evidence=trajectory.events,
            known_future_evidence=packet.evidence_items,
            evaluation_targets=packet.evaluation_targets,
            boundary_validation=boundary, warnings=list(trajectory.warnings), created_at=timestamp,
            context_hash="0" * 64,
        )
        context = unhashed.model_copy(update={"context_hash": _context_hash(unhashed)})
        context_json = canonical_json_bytes(context.model_dump(mode="json")) + b"\n"
        markdown = render_context(context).encode("utf-8")
        inputs = {"run_manifest.json": sha256_file(run_path), "raw_codex_events.jsonl": raw_hash,
                  "trajectory.json": sha256_file(trajectory_path), "trajectory.md": sha256_file(trajectory_md),
                  "trajectory_manifest.json": sha256_file(trajectory_manifest_path), "outcome.yaml": sha256_file(packet_path)}
        manifest = EvaluationManifest(
            context_id=context_id, run_id=run.run_id, scenario_id=run.scenario_id,
            trajectory_hash=trajectory.trajectory_hash, outcome_packet_hash=packet.packet_hash,
            context_hash=context.context_hash, input_file_hashes=inputs,
            output_file_hashes={"evaluation_context.json": sha256_bytes(context_json), "evaluation_context.md": sha256_bytes(markdown)},
            future_evidence_file_hashes={item.evidence_id: item.sha256 for item in packet.evidence_items},
            event_count=len(trajectory.events), evidence_item_count=len(packet.evidence_items),
            evaluation_target_count=len(packet.evaluation_targets), warnings_count=len(context.warnings), created_at=timestamp)
        manifest_json = canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"
        if output.exists() and not overwrite:
            raise EvaluationContextError(f"evaluation output already exists; use --overwrite: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        stage = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
        backup = output.parent / f".{output.name}.backup-{uuid.uuid4().hex}"
        try:
            stage.mkdir()
            _atomic_write(stage / "evaluation_context.json", context_json)
            _atomic_write(stage / "evaluation_context.md", markdown)
            _atomic_write(stage / "evaluation_manifest.json", manifest_json)
            if output.exists(): output.replace(backup)
            stage.replace(output)
            if backup.exists(): shutil.rmtree(backup)
        except OSError as exc:
            if not output.exists() and backup.exists(): backup.replace(output)
            raise EvaluationContextError(f"could not publish evaluation context: {exc}") from exc
        finally:
            if stage.exists(): shutil.rmtree(stage)
            if backup.exists() and output.exists(): shutil.rmtree(backup)
        return context


def _timestamp(value: str) -> datetime:
    try: result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc: raise argparse.ArgumentTypeError(str(exc)) from exc
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build grounded temporal evaluation context")
    parser.add_argument("--run-directory", required=True, type=Path)
    parser.add_argument("--trajectory-directory", required=True, type=Path)
    parser.add_argument("--outcome-packet", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--fixed-created-at", type=_timestamp)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        context = EvaluationContextBuilder().build(args.run_directory, args.trajectory_directory,
            args.outcome_packet, args.output_dir, created_at=args.fixed_created_at, overwrite=args.overwrite)
        destination = args.output_dir or args.run_directory / "evaluation"
        print("TEMPORAL EVALUATION CONTEXT\n")
        print(f"Run                 {context.run_id}\nScenario            {context.scenario_id}\nThread              {context.thread_id}")
        print(f"Trajectory events    {len(context.past_observable_evidence)}\nFuture evidence items {len(context.known_future_evidence)}")
        print(f"Evaluation targets   {len(context.evaluation_targets)}\nProvenance           {context.provenance_type}")
        print(f"Boundary validation {'SUCCEEDED' if context.boundary_validation.validation_succeeded else 'FAILED'}")
        print(f"Outcome packet hash  {context.outcome_packet_hash}\nContext hash         {context.context_hash}\nOutput directory     {Path(destination).resolve()}")
        return 0
    except (EvaluationContextError, EvaluationPacketError, EvaluationIntegrityError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
