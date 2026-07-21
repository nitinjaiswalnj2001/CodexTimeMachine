"""Publish deterministic Phase 9 counterfactual target-coverage evidence."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from backend.temporal.integrity import canonical_json_bytes, sha256_bytes, sha256_file

from .coverage import activity_volume_context, shift, target_coverages
from .loader import Inputs, load_inputs
from .models import COUNTERFACTUAL_POLICY_VERSION, CounterfactualCoverageAssessment, CounterfactualManifest, ReplayCoverageStatus
from .renderer import render
from .validator import canonical_coverage_hash, validate_counterfactual_manifest, validate_generated_assessment


class CounterfactualRunnerError(RuntimeError):
    """Raised for unsafe publication or rejected generated evidence."""


def _write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _protect(output: Path, inputs: Inputs) -> None:
    output = output.resolve()
    run = inputs.run.resolve()
    if output == run or not output.is_relative_to(run):
        raise CounterfactualRunnerError("counterfactual output must remain inside the run directory")
    protected = (
        run / "workspace", run / "trajectory", run / "evaluation", inputs.assessment_dir,
        run / "assessment_failures", run / "intervention", inputs.replay_dir,
        run / "replay_failures", inputs.divergence_dir,
        run / "phase_receipts",
        run / "run_manifest.json", run / "raw_codex_events.jsonl", run / "final_message.txt",
    )
    for source in protected:
        if _overlaps(output, source.resolve()):
            raise CounterfactualRunnerError(f"counterfactual output overlaps protected evidence: {source.name}")


class CounterfactualCoverageRunner:
    def run(
        self,
        run_directory: str | Path,
        replay_directory: str | Path,
        divergence_directory: str | Path,
        output_directory: str | Path | None = None,
        *,
        created_at: datetime | None = None,
        overwrite: bool = False,
    ) -> tuple[CounterfactualCoverageAssessment, CounterfactualManifest]:
        inputs = load_inputs(run_directory, replay_directory, divergence_directory)
        output = Path(output_directory).resolve() if output_directory else inputs.run / "counterfactual"
        _protect(output, inputs)
        if output.exists() and not overwrite:
            raise CounterfactualRunnerError("counterfactual output already exists; use --overwrite")

        now = created_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        coverages = target_coverages(inputs.context, inputs.assessment, inputs.trajectory)
        value_shift = shift(coverages, inputs.divergence)
        activity_context = activity_volume_context(inputs.divergence, coverages, value_shift)
        identity = {"policy_version": COUNTERFACTUAL_POLICY_VERSION, "run": inputs.context.run_id, "replay": inputs.replay.replay_id,
                    "context": inputs.context.context_id, "assessment": inputs.assessment.assessment_id,
                    "intervention": inputs.intervention.intervention_id, "divergence": inputs.divergence.divergence_id}
        coverage_id = "cov-" + sha256_bytes(canonical_json_bytes(identity))[:24]
        bare = CounterfactualCoverageAssessment(
            coverage_id=coverage_id,
            run_id=inputs.context.run_id,
            replay_id=inputs.replay.replay_id,
            scenario_id=inputs.context.scenario_id,
            context_id=inputs.context.context_id,
            assessment_id=inputs.assessment.assessment_id,
            intervention_id=inputs.intervention.intervention_id,
            divergence_id=inputs.divergence.divergence_id,
            target_coverages=coverages,
            shift=value_shift,
            activity_volume_context=activity_context,
            limitations=[
                "Target coverage is not a technical-correctness or performance assessment.",
                "Target coverage does not prove causality.",
            ],
            warnings=[],
            created_at=now,
            coverage_hash="0" * 64,
        )
        value = bare.model_copy(update={"coverage_hash": canonical_coverage_hash(bare)})
        try:
            validate_generated_assessment(value, inputs.context, inputs.assessment, inputs.trajectory, inputs.divergence)
        except Exception as exc:
            raise CounterfactualRunnerError(f"generated coverage validation failed: {exc}") from exc

        stage = output.parent / f".{output.name}.stage-{uuid.uuid4().hex}"
        stage.mkdir(parents=True)
        try:
            _write(stage / "counterfactual_coverage.json", canonical_json_bytes(value.model_dump(mode="json")) + b"\n")
            _write(stage / "counterfactual_coverage.md", render(value).encode("utf-8"))
            output_hashes = {
                name: sha256_file(stage / name)
                for name in ("counterfactual_coverage.json", "counterfactual_coverage.md")
            }
            status_counts = dict(Counter(item.replay_coverage_status.value for item in coverages))
            manifest = CounterfactualManifest(
                coverage_id=coverage_id, run_id=value.run_id, replay_id=value.replay_id,
                scenario_id=value.scenario_id, context_id=value.context_id,
                assessment_id=value.assessment_id, intervention_id=value.intervention_id,
                divergence_id=value.divergence_id, input_file_hashes=inputs.hashes,
                output_file_hashes=output_hashes, target_count=len(coverages),
                coverage_status_counts={status.value: status_counts.get(status.value, 0) for status in ReplayCoverageStatus},
                shift_status=value_shift.status, coverage_hash=value.coverage_hash, created_at=now,
            )
            _write(stage / "counterfactual_manifest.json", canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n")
            validate_counterfactual_manifest(value, manifest, stage, inputs.hashes)
            backup = None
            if output.exists():
                backup = output.parent / f".{output.name}.backup-{uuid.uuid4().hex}"
                os.replace(output, backup)
            try:
                os.replace(stage, output)
            except Exception:
                if backup:
                    os.replace(backup, output)
                raise
            if backup:
                shutil.rmtree(backup)
            return value, manifest
        finally:
            if stage.exists():
                shutil.rmtree(stage)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build deterministic counterfactual target-coverage evidence.")
    parser.add_argument("--run-directory", required=True)
    parser.add_argument("--replay-directory", required=True)
    parser.add_argument("--divergence-directory", required=True)
    parser.add_argument("--output-dir")
    parser.add_argument("--fixed-created-at")
    parser.add_argument("--overwrite", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        created_at = datetime.fromisoformat(arguments.fixed_created_at.replace("Z", "+00:00")) if arguments.fixed_created_at else None
        value, _ = CounterfactualCoverageRunner().run(
            arguments.run_directory, arguments.replay_directory, arguments.divergence_directory,
            arguments.output_dir, created_at=created_at, overwrite=arguments.overwrite,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        "COUNTERFACTUAL TARGET-COVERAGE\n"
        f"Run {value.run_id}\nReplay {value.replay_id}\n"
        f"Shift {value.shift.status.value}\nCoverage hash {value.coverage_hash}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
