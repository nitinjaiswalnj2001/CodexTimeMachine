"""Phase 3 observable-trajectory extraction lifecycle and CLI."""

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

from .models import ObservableTrajectory, TrajectoryManifest
from .normalizer import TrajectoryNormalizationError, normalize_records
from .parser import TrajectoryParseError, event_thread_id, parse_raw_events
from .render import render_markdown


class TrajectoryExtractionError(RuntimeError):
    """Raised when a run cannot produce an accepted observable trajectory."""


def _load_manifest(path: Path) -> RunManifest:
    try:
        return RunManifest.model_validate_json(path.read_text("utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise TrajectoryExtractionError(f"invalid run manifest: {exc}") from exc


def _trajectory_hash(trajectory: ObservableTrajectory) -> str:
    payload = trajectory.model_dump(
        mode="json", exclude={"trajectory_hash", "extracted_at"}
    )
    return sha256_bytes(canonical_json_bytes(payload))


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_output_location(run_directory: Path, output_directory: Path) -> None:
    workspace = (run_directory / "workspace").resolve()
    output = output_directory.resolve()
    if output == run_directory or output.is_relative_to(workspace):
        raise TrajectoryExtractionError(
            "trajectory output must remain outside the evaluated workspace and must not replace the run root"
        )


class TrajectoryExtractor:
    def extract(
        self,
        run_directory: str | Path,
        output_directory: str | Path | None = None,
        *,
        extracted_at: datetime | None = None,
        overwrite: bool = False,
    ) -> ObservableTrajectory:
        run_directory = Path(run_directory).resolve()
        manifest_path = run_directory / "run_manifest.json"
        raw_path = run_directory / "raw_codex_events.jsonl"
        final_path = run_directory / "final_message.txt"
        workspace = run_directory / "workspace"
        output = (
            Path(output_directory).resolve()
            if output_directory is not None
            else run_directory / "trajectory"
        )
        _validate_output_location(run_directory, output)
        manifest = _load_manifest(manifest_path)
        if manifest.run_status is not RunStatus.SUCCEEDED:
            raise TrajectoryExtractionError(
                f"trajectory extraction requires a SUCCEEDED run; found {manifest.run_status}"
            )
        if not workspace.is_dir():
            raise TrajectoryExtractionError("run workspace is missing")
        if not raw_path.is_file():
            raise TrajectoryExtractionError("raw Codex event evidence is missing")
        actual_raw_hash = sha256_file(raw_path)
        if manifest.raw_events_sha256 != actual_raw_hash:
            raise TrajectoryExtractionError("raw event SHA-256 does not match run manifest")
        records = parse_raw_events(raw_path)
        if manifest.event_summary is None:
            raise TrajectoryExtractionError("run manifest has no event stream summary")
        if manifest.event_summary.event_count != len(records):
            raise TrajectoryExtractionError("raw event count does not match run manifest")
        thread_records = [record for record in records if record.event_type == "thread.started"]
        if len(thread_records) != 1:
            raise TrajectoryExtractionError(
                f"expected exactly one thread.started event; found {len(thread_records)}"
            )
        raw_thread_id = event_thread_id(thread_records[0].payload)
        if not raw_thread_id or raw_thread_id != manifest.thread_id:
            raise TrajectoryExtractionError("thread ID does not match run manifest")
        if manifest.event_summary.thread_started_count != 1:
            raise TrajectoryExtractionError("manifest thread-start count is not exactly one")
        if manifest.event_summary.thread_id != raw_thread_id:
            raise TrajectoryExtractionError("event-summary thread ID does not match raw evidence")

        final_message: str | None = None
        actual_final_hash: str | None = None
        if final_path.is_file():
            actual_final_hash = sha256_file(final_path)
            if manifest.final_message_sha256 != actual_final_hash:
                raise TrajectoryExtractionError(
                    "final-message SHA-256 does not match run manifest"
                )
            final_message = final_path.read_text("utf-8")
        elif manifest.final_message_sha256 is not None:
            raise TrajectoryExtractionError("manifested final message is missing")

        events, warnings = normalize_records(
            records,
            manifest.run_id,
            workspace,
            final_message,
            actual_final_hash,
        )
        extraction_time = extracted_at or datetime.now(timezone.utc)
        if extraction_time.tzinfo is None:
            extraction_time = extraction_time.replace(tzinfo=timezone.utc)
        source_manifest_hash = sha256_file(manifest_path)
        unhashed = ObservableTrajectory(
            run_id=manifest.run_id,
            scenario_id=manifest.scenario_id,
            thread_id=raw_thread_id,
            run_kind=manifest.run_kind,
            source_manifest_hash=source_manifest_hash,
            source_raw_events_hash=actual_raw_hash,
            extracted_at=extraction_time,
            event_count=len(events),
            events=events,
            warnings=warnings,
            trajectory_hash="0" * 64,
        )
        trajectory = unhashed.model_copy(
            update={"trajectory_hash": _trajectory_hash(unhashed)}
        )
        trajectory_json = canonical_json_bytes(trajectory.model_dump(mode="json")) + b"\n"
        trajectory_markdown = render_markdown(trajectory).encode("utf-8")
        trajectory_manifest = TrajectoryManifest(
            run_id=manifest.run_id,
            extracted_at=extraction_time,
            source_manifest_sha256=source_manifest_hash,
            source_raw_events_sha256=actual_raw_hash,
            source_final_message_sha256=actual_final_hash,
            trajectory_json_sha256=sha256_bytes(trajectory_json),
            trajectory_markdown_sha256=sha256_bytes(trajectory_markdown),
            trajectory_hash=trajectory.trajectory_hash,
            event_count=len(events),
            warnings_count=len(warnings),
        )
        trajectory_manifest_json = (
            canonical_json_bytes(trajectory_manifest.model_dump(mode="json")) + b"\n"
        )

        if output.exists() and not overwrite:
            raise TrajectoryExtractionError(
                f"trajectory output already exists; use --overwrite: {output}"
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        stage = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
        backup = output.parent / f".{output.name}.backup-{uuid.uuid4().hex}"
        try:
            stage.mkdir()
            _atomic_write(stage / "trajectory.json", trajectory_json)
            _atomic_write(stage / "trajectory.md", trajectory_markdown)
            _atomic_write(stage / "trajectory_manifest.json", trajectory_manifest_json)
            if output.exists():
                output.replace(backup)
            stage.replace(output)
            if backup.exists():
                shutil.rmtree(backup)
        except OSError as exc:
            if not output.exists() and backup.exists():
                backup.replace(output)
            raise TrajectoryExtractionError(f"could not publish trajectory output: {exc}") from exc
        finally:
            if stage.exists():
                shutil.rmtree(stage)
            if backup.exists() and output.exists():
                shutil.rmtree(backup)
        return trajectory


def _parse_fixed_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO-8601 timestamp: {value}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract deterministic observable trajectory evidence"
    )
    parser.add_argument("run_directory", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--fixed-extracted-at", type=_parse_fixed_timestamp)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        trajectory = TrajectoryExtractor().extract(
            args.run_directory,
            args.output_dir,
            extracted_at=args.fixed_extracted_at,
            overwrite=args.overwrite,
        )
        output = args.output_dir or args.run_directory / "trajectory"
        print("TRAJECTORY EXTRACTION\n")
        print(f"Run               {trajectory.run_id}")
        print(f"Thread            {trajectory.thread_id}")
        raw_count = len(parse_raw_events(Path(args.run_directory) / "raw_codex_events.jsonl"))
        print(f"Raw events        {raw_count}")
        print(f"Normalized events {trajectory.event_count}")
        print(f"Warnings          {len(trajectory.warnings)}")
        print(f"Trajectory hash   {trajectory.trajectory_hash}")
        print(f"Output directory  {Path(output).resolve()}")
        return 0
    except (
        TrajectoryExtractionError,
        TrajectoryParseError,
        TrajectoryNormalizationError,
        ValidationError,
        OSError,
        ValueError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
