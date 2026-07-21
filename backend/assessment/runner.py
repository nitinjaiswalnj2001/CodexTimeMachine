"""Phase 5 evidence-grounded Temporal Blind-Spot Assessment lifecycle."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from backend.temporal.integrity import canonical_json_bytes, sha256_bytes, sha256_file

from .codex_provider import CodexEvaluatorProvider, CodexEvaluatorProviderError
from .loader import AssessmentInputError, load_evaluation_context
from .models import (AssessmentFailureManifest, AssessmentFailureStage, AssessmentManifest,
                     EvaluatorMetadata, OverallStatus, TemporalBlindSpotAssessment)
from .prompt import build_evaluator_input, render_evaluator_prompt
from .provider import BlindSpotEvaluatorProvider, EvaluationConfiguration, FakeEvaluatorProvider, ProviderResult
from .renderer import render_assessment
from .validator import AssessmentValidationError, parse_evaluator_output, validate_grounding


class AssessmentRunnerError(RuntimeError):
    pass


_FORBIDDEN_TOOL_ITEMS = {"command_execution", "file_change", "web_search", "web_search_call",
                         "mcp_tool_call", "mcp_call", "mcp_tool"}


def _assessment_hash(value: TemporalBlindSpotAssessment) -> str:
    return sha256_bytes(canonical_json_bytes(value.model_dump(
        mode="json", exclude={"assessment_hash", "created_at", "evaluator_metadata"}
    )))


def _atomic_write(path: Path, data: bytes) -> None:
    temp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temp.write_bytes(data); os.replace(temp, path)
    finally:
        if temp.exists(): temp.unlink()


def _validate_provider_result(result: ProviderResult, baseline_thread_id: str) -> str:
    if result.exit_code != 0:
        raise AssessmentRunnerError(f"evaluator provider exited with code {result.exit_code}")
    if len(result.thread_ids) != 1 or not result.thread_ids[0]:
        raise AssessmentRunnerError("evaluator must emit exactly one non-empty fresh thread ID")
    thread_id = result.thread_ids[0]
    if thread_id == baseline_thread_id:
        raise AssessmentRunnerError("evaluator thread must differ from the baseline Past Codex thread")
    if not result.raw_response.strip():
        raise AssessmentRunnerError("structured evaluator response is missing")
    if result.raw_events is not None:
        for line in result.raw_events.splitlines():
            if not line.strip(): continue
            try: event = json.loads(line)
            except json.JSONDecodeError as exc: raise AssessmentRunnerError(f"invalid evaluator event JSONL: {exc}") from exc
            event_type = str(event.get("type", ""))
            if event_type in {"error", "turn.failed", "item.failed"} or event_type.endswith(".error"):
                raise AssessmentRunnerError(f"evaluator emitted failure event: {event_type}")
            item = event.get("item")
            item_type = item.get("type") if isinstance(item, dict) else None
            if item_type in _FORBIDDEN_TOOL_ITEMS:
                raise AssessmentRunnerError(f"evaluator tool calls are forbidden: {item_type}")
    return thread_id


def _validate_output(output: Path, evaluation_dir: Path) -> None:
    run_dir = evaluation_dir.parent.resolve()
    if not output.is_relative_to(run_dir):
        raise AssessmentRunnerError("assessment output must resolve inside the accepted run directory")
    protected_roots = [evaluation_dir, run_dir / "workspace", run_dir / "trajectory"]
    if output == run_dir:
        raise AssessmentRunnerError("assessment output overlaps protected run evidence")
    for root in protected_roots:
        root = root.resolve()
        if output == root or output.is_relative_to(root):
            raise AssessmentRunnerError(f"assessment output overlaps protected evidence directory: {root.name}")
    protected_files = [run_dir / "run_manifest.json", run_dir / "raw_codex_events.jsonl",
                       run_dir / "final_message.txt", evaluation_dir / "evaluation_context.json",
                       evaluation_dir / "evaluation_context.md", evaluation_dir / "evaluation_manifest.json"]
    for path in protected_files:
        resolved = path.resolve()
        if output == resolved or resolved.is_relative_to(output) or output.is_relative_to(resolved):
            raise AssessmentRunnerError(f"assessment output targets protected source evidence: {path.name}")


def _preserve_failure(run_dir: Path, stage: Path, context, provider: BlindSpotEvaluatorProvider,
                      configuration: EvaluationConfiguration, evaluator_input_hash: str,
                      result: ProviderResult | None, failure_stage: AssessmentFailureStage,
                      exc: Exception, created_at: datetime) -> None:
    """Preserve raw post-input evidence without publishing an assessment."""
    attempts = run_dir / "assessment_failures"
    attempt_id = f"attempt-{created_at.strftime('%Y%m%dT%H%M%S%fZ')}-{evaluator_input_hash[:12]}-{uuid.uuid4().hex[:8]}"
    destination = attempts / attempt_id
    destination.mkdir(parents=True, exist_ok=False)
    available: dict[str, str] = {}
    for name in ("evaluator_input.json", "raw_evaluator_response.txt", "raw_evaluator_events.jsonl", "evaluator_stderr.log"):
        source = stage / name
        if source.is_file():
            target = destination / name
            shutil.copyfile(source, target)
            available[name] = sha256_file(target)
    manifest = AssessmentFailureManifest(
        attempt_id=attempt_id, run_id=context.run_id, scenario_id=context.scenario_id,
        context_id=context.context_id, context_hash=context.context_hash,
        evaluator_provider=provider.name, evaluator_model=configuration.model,
        reasoning_effort=configuration.reasoning_effort,
        evaluator_thread_ids=list(result.thread_ids) if result else [],
        exit_code=result.exit_code if result else None, failure_stage=failure_stage,
        failure_type=type(exc).__name__, failure_message=str(exc)[:1000],
        evaluator_input_hash=evaluator_input_hash,
        raw_evaluator_response_hash=available.get("raw_evaluator_response.txt"),
        raw_evaluator_events_hash=available.get("raw_evaluator_events.jsonl"),
        evaluator_stderr_hash=available.get("evaluator_stderr.log"),
        created_at=created_at, available_artifact_hashes=available,
    )
    _atomic_write(destination / "assessment_failure_manifest.json",
                  canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n")


def _default_fake_response(context) -> bytes:
    useful = [event for event in context.past_observable_evidence if str(event.event_type) not in {"THREAD_STARTED", "TURN_STARTED", "TURN_COMPLETED"}]
    def observable_text(event) -> str:
        return " ".join(filter(None, (event.summary, event.command, event.output_preview))).casefold()
    comparison = next((event for event in useful if any(term in observable_text(event) for term in ("comparison", "pytest", "retrieval", "focused"))), None)
    recommendation = next((event for event in useful if any(term in observable_text(event) for term in ("recommend", "production default", "bm25")) and event is not comparison), None)
    cited = [event for event in (comparison, recommendation) if event is not None]
    for event in useful or context.past_observable_evidence:
        if event not in cited and len(cited) < 2: cited.append(event)
    future = context.known_future_evidence[0]
    data = {
        "target_assessments": [{"target_id": target.target_id, "category": str(target.category), "verdict": "MISSED",
            "summary": "The observable trajectory did not establish the target success condition before the decision.",
            "observed_past_evidence": [{"event_id": event.event_id, "sequence": event.sequence,
                "event_type": str(event.event_type), "relevance": "Observable action relevant to the bounded investigation."} for event in cited],
            "known_future_evidence": [{"evidence_id": future.evidence_id,
                "relevance": "Synthetic future evidence shows the focused comparison was insufficient, without establishing a universal winner."}],
            "missing_investigation": "A representative labeled retrieval evaluation was not observed before the production-default recommendation.",
            "confidence": 0.9, "limitations": ["Future values are controlled synthetic fixture evidence, not real LegalRAG performance."]}
            for target in context.evaluation_targets],
        "overall_finding": {"status": "BLIND_SPOT_IDENTIFIED", "blind_spot_category": str(context.evaluation_targets[0].category),
            "statement": "Observable evidence supports an insufficient-evaluation blind spot; it does not establish that BM25 was universally wrong or that semantic retrieval is the correct default.",
            "supporting_target_ids": [target.target_id for target in context.evaluation_targets], "supporting_past_event_ids": [event.event_id for event in cited],
            "supporting_future_evidence_ids": [future.evidence_id], "confidence": 0.9,
            "limitations": ["Natural-language entailment remains evaluator judgment bounded by cited evidence."]},
        "limitations": ["The assessment uses observable evidence only and does not reconstruct hidden reasoning."]}
    return canonical_json_bytes(data)


class AssessmentRunner:
    def run(self, evaluation_directory: str | Path, provider: BlindSpotEvaluatorProvider,
            output_directory: str | Path | None = None, *, configuration: EvaluationConfiguration | None = None,
            created_at: datetime | None = None, overwrite: bool = False) -> TemporalBlindSpotAssessment:
        evaluation_dir = Path(evaluation_directory).resolve()
        run_dir = evaluation_dir.parent
        output = Path(output_directory).resolve() if output_directory else run_dir / "assessment"
        _validate_output(output, evaluation_dir)
        context, _ = load_evaluation_context(evaluation_dir)
        config = configuration or EvaluationConfiguration()
        evaluator_input = build_evaluator_input(context)
        input_bytes = canonical_json_bytes(evaluator_input.model_dump(mode="json")) + b"\n"
        input_hash = sha256_bytes(input_bytes)
        prompt = render_evaluator_prompt(evaluator_input)
        if output.exists() and not overwrite:
            raise AssessmentRunnerError(f"assessment output already exists; use --overwrite: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        stage = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
        backup = output.parent / f".{output.name}.backup-{uuid.uuid4().hex}"
        stage.mkdir()
        result: ProviderResult | None = None
        failure_stage = AssessmentFailureStage.PROVIDER_EXECUTION
        timestamp = created_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None: timestamp = timestamp.replace(tzinfo=timezone.utc)
        try:
            _atomic_write(stage / "evaluator_input.json", input_bytes)
            result = provider.evaluate(evaluator_input, prompt, config, stage)
            _atomic_write(stage / "raw_evaluator_response.txt", result.raw_response)
            _atomic_write(stage / "evaluator_stderr.log", result.stderr)
            if result.raw_events is not None:
                _atomic_write(stage / "raw_evaluator_events.jsonl", result.raw_events)
            failure_stage = AssessmentFailureStage.PROVIDER_RESULT_VALIDATION
            thread_id = _validate_provider_result(result, context.thread_id)
            raw_hash = sha256_bytes(result.raw_response)
            failure_stage = AssessmentFailureStage.STRUCTURED_OUTPUT_PARSING
            parsed = parse_evaluator_output(result.raw_response)
            failure_stage = AssessmentFailureStage.GROUNDING_VALIDATION
            warnings = validate_grounding(parsed, context)
            assessment_id = f"asm-{sha256_bytes((context.context_hash + '\0' + raw_hash).encode())[:24]}"
            metadata = EvaluatorMetadata(provider=provider.name, model=config.model,
                reasoning_effort=config.reasoning_effort, thread_id=thread_id, exit_code=result.exit_code,
                provider_version=result.provider_version)
            unhashed = TemporalBlindSpotAssessment(assessment_id=assessment_id, run_id=context.run_id,
                scenario_id=context.scenario_id, thread_id=context.thread_id, context_id=context.context_id,
                context_hash=context.context_hash, decision_under_evaluation=context.decision_under_evaluation,
                target_assessments=parsed.target_assessments, overall_finding=parsed.overall_finding,
                limitations=[*parsed.limitations, *warnings], evaluator_metadata=metadata,
                created_at=timestamp, assessment_hash="0" * 64)
            assessment = unhashed.model_copy(update={"assessment_hash": _assessment_hash(unhashed)})
            assessment_json = canonical_json_bytes(assessment.model_dump(mode="json")) + b"\n"
            markdown = render_assessment(assessment).encode()
            _atomic_write(stage / "blind_spot_assessment.json", assessment_json)
            _atomic_write(stage / "blind_spot_assessment.md", markdown)
            verdicts = Counter(str(item.verdict) for item in assessment.target_assessments)
            stderr_hash = sha256_bytes(result.stderr)
            outputs = {"evaluator_input.json": input_hash, "raw_evaluator_response.txt": raw_hash,
                "evaluator_stderr.log": stderr_hash,
                "blind_spot_assessment.json": sha256_bytes(assessment_json), "blind_spot_assessment.md": sha256_bytes(markdown)}
            raw_events_hash = None
            if result.raw_events is not None:
                raw_events_hash = sha256_bytes(result.raw_events); outputs["raw_evaluator_events.jsonl"] = raw_events_hash
            manifest = AssessmentManifest(assessment_id=assessment_id, run_id=context.run_id,
                scenario_id=context.scenario_id, context_id=context.context_id, context_hash=context.context_hash,
                evaluator_provider=provider.name, evaluator_model=config.model, reasoning_effort=config.reasoning_effort,
                evaluator_thread_id=thread_id, evaluator_input_hash=input_hash,
                raw_evaluator_response_hash=raw_hash, raw_evaluator_events_hash=raw_events_hash,
                evaluator_stderr_hash=stderr_hash,
                assessment_hash=assessment.assessment_hash, target_count=len(assessment.target_assessments),
                verdict_counts=dict(sorted(verdicts.items())), warning_count=len(warnings),
                input_file_hashes={"evaluation_context.json": sha256_file(evaluation_dir / "evaluation_context.json"),
                    "evaluation_manifest.json": sha256_file(evaluation_dir / "evaluation_manifest.json")},
                output_file_hashes=outputs, created_at=timestamp)
            _atomic_write(stage / "assessment_manifest.json", canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n")
            failure_stage = AssessmentFailureStage.PUBLICATION
            if output.exists(): output.replace(backup)
            stage.replace(output)
            if backup.exists(): shutil.rmtree(backup)
            return assessment
        except Exception as exc:
            if failure_stage is AssessmentFailureStage.PROVIDER_EXECUTION and "preflight" in str(exc).casefold():
                failure_stage = AssessmentFailureStage.PROVIDER_PREFLIGHT
            try:
                _preserve_failure(run_dir, stage, context, provider, config, input_hash, result,
                                  failure_stage, exc, timestamp)
            except OSError:
                pass
            if not output.exists() and backup.exists(): backup.replace(output)
            raise
        finally:
            if stage.exists(): shutil.rmtree(stage)
            if backup.exists() and output.exists(): shutil.rmtree(backup)


def _timestamp(value: str) -> datetime:
    try: result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc: raise argparse.ArgumentTypeError(str(exc)) from exc
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a Temporal Blind-Spot Assessment")
    parser.add_argument("--evaluation-directory", required=True, type=Path)
    parser.add_argument("--provider", choices=("fake", "codex"), default="fake")
    parser.add_argument("--output-dir", type=Path); parser.add_argument("--fixed-created-at", type=_timestamp)
    parser.add_argument("--overwrite", action="store_true"); parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning-effort", choices=("high",), default="high")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        context, _ = load_evaluation_context(args.evaluation_directory)
        provider = FakeEvaluatorProvider(_default_fake_response(context)) if args.provider == "fake" else CodexEvaluatorProvider()
        assessment = AssessmentRunner().run(args.evaluation_directory, provider, args.output_dir,
            configuration=EvaluationConfiguration(args.model, args.reasoning_effort),
            created_at=args.fixed_created_at, overwrite=args.overwrite)
        output = args.output_dir or args.evaluation_directory.parent / "assessment"
        counts = Counter(str(item.verdict) for item in assessment.target_assessments)
        print("TEMPORAL BLIND-SPOT ASSESSMENT\n")
        print(f"Run                {assessment.run_id}\nScenario           {assessment.scenario_id}\nContext            {assessment.context_id}")
        print(f"Evaluator provider {assessment.evaluator_metadata.provider}\nEvaluator thread   {assessment.evaluator_metadata.thread_id}")
        print(f"Targets            {len(assessment.target_assessments)}\nVerdict counts     {dict(sorted(counts.items()))}")
        print(f"Overall status     {assessment.overall_finding.status}\nAssessment hash    {assessment.assessment_hash}\nOutput directory   {Path(output).resolve()}")
        return 0
    except (AssessmentInputError, AssessmentRunnerError, AssessmentValidationError, CodexEvaluatorProviderError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
