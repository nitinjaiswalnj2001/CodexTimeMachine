from __future__ import annotations
from pathlib import Path
import shutil
import json
import inspect
import pytest
import backend.replay.runner as replay_runner
from backend.replay.fake_provider import DeterministicFakeReplayProvider
from backend.replay.models import (ReplayExecutionMode, ReplayIdentity, ReplayKind,
    ReplayManifest, ReplayProviderResult, SandboxBackend, SandboxPlatform)
from backend.replay.prompt import build_replay_prompt
from backend.replay.runner import (CounterfactualReplayRunner, ReplayRunnerError,
    _output_ok, main, validate_replay_artifacts, REQUIRED_OUTPUTS)
from backend.runs.models import CodexExecutionConfiguration
from backend.runs.workspace import compute_workspace_tree_hash
from backend.temporal.integrity import canonical_json_bytes, sha256_bytes, sha256_file
from backend.trajectory.extractor import TrajectoryExtractor
from backend.evaluation.context_builder import EvaluationContextBuilder
from backend.assessment.runner import AssessmentRunner
from backend.intervention.policy import DeterministicInterventionPolicy
from backend.intervention.runner import InterventionRunner
from test_trajectory_extractor import _prepare_run
from test_evaluation_context import FIXED_TIME, _packet_data, _write_packet
from test_blind_spot_assessment import _provider

TASK="Improve the default retrieval strategy."
CLUE="Before selecting a production retrieval default, verify the recommendation on a representative labeled query set covering multiple query types."

def test_prompt_injects_only_task_and_approved_clue():
    value=build_replay_prompt(TASK,CLUE)
    assert value.count(CLUE)==1 and value.count(TASK)==1
    forbidden=("BM25","MRR","MISSED","future evidence","baseline final")
    assert not any(word in value for word in forbidden)
    assert "ORIGINAL TASK" in value and "MINIMUM INVESTIGATIVE CLUE" in value

def test_prompt_rejects_empty_values():
    with pytest.raises(ValueError): build_replay_prompt(TASK,"")

def test_replay_identity_is_deterministic():
    semantic=b"baseline|snapshot|intervention|medium"
    assert f"replay-{sha256_bytes(semantic)[:24]}"==f"replay-{sha256_bytes(semantic)[:24]}"

@pytest.mark.parametrize("platform",range(2))
def test_fake_provider_is_deterministic_and_fresh(tmp_path,platform):
    prompt=build_replay_prompt(TASK,CLUE); ids=[]; raws=[]
    for index in range(2):
        workspace=tmp_path/f"w{index}";workspace.mkdir()
        provider=DeterministicFakeReplayProvider();result=provider.execute(prompt,workspace,CodexExecutionConfiguration(),tmp_path/"r",tmp_path/"f",tmp_path/"e")
        ids.append(result.thread_ids);raws.append(result.raw_event_bytes)
        assert provider.execution_count==1 and (workspace/"tests/representative_retrieval_cases.json").is_file()
    assert ids[0]==ids[1] and raws[0]==raws[1]

@pytest.mark.parametrize("mode,count",[("no_thread",0),("multiple_threads",2),("success",1)])
def test_fake_thread_modes(tmp_path,mode,count):
    workspace=tmp_path/"w";workspace.mkdir();result=DeterministicFakeReplayProvider(mode=mode).execute(build_replay_prompt(TASK,CLUE),workspace,CodexExecutionConfiguration(),tmp_path/"r",tmp_path/"f",tmp_path/"e")
    assert len(result.thread_ids)==count

def test_fake_provider_models_representative_evaluation_without_future_results(tmp_path):
    workspace=tmp_path/"w";workspace.mkdir()
    result=DeterministicFakeReplayProvider().execute(build_replay_prompt(TASK,CLUE),workspace,CodexExecutionConfiguration(),tmp_path/"r",tmp_path/"f",tmp_path/"e")
    fixture=json.loads((workspace/"tests/representative_retrieval_cases.json").read_text("utf-8"))
    assert fixture["query_categories"]==["lexical overlap","paraphrased","synonym-heavy","long-form legal","exact-term"]
    assert fixture["metrics"] is None and not any(token in result.raw_event_bytes.decode("utf-8") for token in ("Hit@5","MRR","BM25","semantic retrieval"))
    assert "test_representative_retrieval.py" in result.raw_event_bytes.decode("utf-8")
    assert b"leaving the default unchanged" in result.final_response_bytes
    assert DeterministicFakeReplayProvider.version=="2.0.0"

@pytest.mark.parametrize("mode",["failure","auth_failure","invalid_json","missing_final","web","mcp"])
def test_fake_failure_modes_preserve_evidence(tmp_path,mode):
    workspace=tmp_path/"w";workspace.mkdir();result=DeterministicFakeReplayProvider(mode=mode).execute(build_replay_prompt(TASK,CLUE),workspace,CodexExecutionConfiguration(),tmp_path/"r",tmp_path/"f",tmp_path/"e")
    assert result.raw_event_bytes
    if mode in {"failure","auth_failure"}: assert result.exit_code!=0
    if mode=="missing_final": assert result.final_response_bytes is None

def test_output_protection(tmp_path):
    run=tmp_path/"run";intervention=run/"intervention";(run/"workspace").mkdir(parents=True);intervention.mkdir()
    for bad in (tmp_path/"outside",run,run/"workspace",intervention,run/"trajectory/child",run/"assessment"):
        with pytest.raises(ReplayRunnerError): _output_ok(bad.resolve(),run.resolve(),intervention.resolve())
    _output_ok((run/"replay-fake").resolve(),run.resolve(),intervention.resolve())

def test_cli_help_has_no_runtime_warning(capsys):
    with pytest.raises(SystemExit) as exc: main(["--help"])
    assert exc.value.code==0

def test_no_comparison_or_score_fields():
    fields=set(__import__("backend.replay.models",fromlist=["ReplayManifest"]).ReplayManifest.model_fields)
    assert not fields.intersection({"improvement_score","divergence_score","baseline_comparison"})


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def _full_chain(tmp_path: Path):
    run = _prepare_run(tmp_path / "baseline")
    snapshot = Path("backend/scenarios/legalrag_reranker_t001/sealed_snapshot").resolve()
    snapshot_manifest = json.loads((snapshot / "manifest.json").read_text("utf-8"))
    shutil.rmtree(run / "workspace")
    shutil.copytree(snapshot / "repo", run / "workspace")
    start_hash = compute_workspace_tree_hash(run / "workspace")
    # This proves replay does not copy the baseline final workspace.
    (run / "workspace/baseline-only.txt").write_text("baseline mutation\n", encoding="utf-8")
    manifest_path = run / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest.update(base_snapshot_id=snapshot_manifest["snapshot_id"],
                    base_snapshot_hash=snapshot_manifest["snapshot_root_hash"],
                    workspace_start_hash=start_hash,
                    workspace_end_hash=compute_workspace_tree_hash(run / "workspace"))
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
    trajectory = run / "trajectory"
    TrajectoryExtractor().extract(run, trajectory, extracted_at=FIXED_TIME)
    packet_data = _packet_data(snapshot_manifest["snapshot_root_hash"])
    packet = _write_packet(tmp_path / "packet", packet_data)
    context = EvaluationContextBuilder().build(run, trajectory, packet, run / "evaluation",
                                               created_at=FIXED_TIME)
    AssessmentRunner().run(run / "evaluation", _provider(context), run / "assessment-fake",
                           created_at=FIXED_TIME)
    InterventionRunner().run(run / "assessment-fake", DeterministicInterventionPolicy(),
                             run / "intervention", created_at=FIXED_TIME)
    return run, run / "intervention", snapshot


@pytest.fixture
def full_chain(tmp_path):
    return _full_chain(tmp_path)


def _run_replay(full_chain, provider=None, name="replay-fake", overwrite=False):
    run, intervention, _ = full_chain
    provider = provider or DeterministicFakeReplayProvider()
    manifest = CounterfactualReplayRunner().run(run, intervention, provider, run / name,
        created_at=FIXED_TIME, overwrite=overwrite)
    return manifest, run, intervention, provider, run / name


def test_end_to_end_fake_replay_creates_complete_evidence(full_chain):
    before_run = _tree_bytes(full_chain[0])
    manifest, run, intervention, provider, output = _run_replay(full_chain)
    assert manifest.status.value == "SUCCEEDED"
    assert manifest.execution_mode is ReplayExecutionMode.DETERMINISTIC_FAKE
    assert manifest.live_model_invoked is False
    assert manifest.requested_model is None
    assert manifest.effective_model == "deterministic-fake-replay"
    assert manifest.sandbox_platform is SandboxPlatform.FAKE
    assert manifest.sandbox_backend is SandboxBackend.FAKE_ISOLATION
    assert manifest.effective_sandbox_path == json.loads(
        (run / "run_manifest.json").read_text())["effective_sandbox_path"]
    assert provider.execution_count == 1
    assert all((output / name).is_file() for name in (*REQUIRED_OUTPUTS, "replay_manifest.json"))
    assert (output / "workspace").is_dir()
    assert manifest.replay_workspace_start_hash == manifest.baseline_workspace_start_hash
    assert not (output / "workspace/baseline-only.txt").exists()
    prompt = (output / "replay_prompt.txt").read_text("utf-8")
    assert prompt.count(CLUE) == 1
    scenario_task = json.loads((full_chain[2] / "manifest.json").read_text())["task"]
    assert prompt.count(scenario_task) == 1
    ghost = json.loads((intervention / "ghost_intervention.json").read_text())
    assert ghost["rationale"] not in prompt
    assert (run / "final_message.txt").read_text("utf-8").strip() not in prompt
    assert not any(token in prompt for token in ("MISSED", "Hit@5", "MRR"))
    assert manifest.raw_event_count > 0 and manifest.normalized_event_count > 0
    trajectory = json.loads((output / "replay_trajectory.json").read_text())
    assert trajectory["events"] and "comparison" not in trajectory
    # All pre-existing source evidence is unchanged.
    after_source = {k: v for k, v in _tree_bytes(run).items()
                    if not k.startswith(("replay-fake/", "replay_failures/"))}
    assert after_source == before_run


def test_assessment_source_is_explicit_and_not_hardcoded(full_chain):
    run, intervention, _ = full_chain
    manifest = json.loads((intervention / "intervention_manifest.json").read_text())
    assert manifest["assessment_source_directory"] == "assessment-fake"
    value, *_ = _run_replay(full_chain)
    assert value.replay_thread_id


@pytest.mark.parametrize("bad", ["/absolute", "C:\\assessment", "\\\\server\\share", "../assessment"])
def test_unsafe_assessment_source_fails_validation(full_chain, bad):
    _, intervention, _ = full_chain
    data = json.loads((intervention / "intervention_manifest.json").read_text())
    data["assessment_source_directory"] = bad
    (intervention / "intervention_manifest.json").write_bytes(canonical_json_bytes(data)+b"\n")
    with pytest.raises(Exception, match="assessment_source_directory|invalid replay source"):
        _run_replay(full_chain)


def test_output_protects_actual_assessment_and_failures(full_chain):
    run, intervention, _ = full_chain
    for output in (run/"assessment-fake", run/"assessment-fake/child",
                   run/"replay_failures", run/"replay_failures/child"):
        with pytest.raises(ReplayRunnerError, match="protected"):
            CounterfactualReplayRunner().run(run, intervention,
                DeterministicFakeReplayProvider(), output, created_at=FIXED_TIME, overwrite=True)


@pytest.mark.parametrize("name", ["run_manifest.json", "raw_codex_events.jsonl", "final_message.txt"])
def test_output_cannot_replace_root_run_evidence(full_chain, name):
    run, intervention, _ = full_chain
    before = (run / name).read_bytes()
    with pytest.raises(ReplayRunnerError, match="protected source"):
        CounterfactualReplayRunner().run(run, intervention,
            DeterministicFakeReplayProvider(), run / name,
            created_at=FIXED_TIME, overwrite=True)
    assert (run / name).read_bytes() == before


@pytest.mark.parametrize("mode,match", [
    ("no_thread", "exactly one"), ("multiple_threads", "exactly one"),
])
def test_end_to_end_thread_cardinality_fails(full_chain, mode, match):
    provider = DeterministicFakeReplayProvider(mode=mode)
    with pytest.raises(ReplayRunnerError, match=match): _run_replay(full_chain, provider)
    assert provider.execution_count == 1


def test_provider_and_raw_thread_mismatch_fails(full_chain):
    provider = DeterministicFakeReplayProvider(thread_id="provider-thread", raw_thread_id="raw-thread")
    with pytest.raises(ReplayRunnerError, match="does not match"):
        _run_replay(full_chain, provider)
    failure = next((full_chain[0] / "replay_failures").iterdir())
    assert (failure / "raw_replay_events.jsonl").is_file()
    assert json.loads((failure / "replay_failure_manifest.json").read_text())["stage"] == "PROVIDER_RESULT_VALIDATION"


def test_baseline_and_evaluator_thread_reuse_fail(full_chain):
    run, _, _ = full_chain
    baseline = json.loads((run / "run_manifest.json").read_text())["thread_id"]
    evaluator = json.loads((run / "assessment-fake/assessment_manifest.json").read_text())["evaluator_thread_id"]
    for thread in (baseline, evaluator):
        with pytest.raises(ReplayRunnerError, match="protected"):
            _run_replay(full_chain, DeterministicFakeReplayProvider(thread_id=thread), name=f"replay-{thread[-4:]}")


def test_prior_thread_reuse_fails_for_nonfake_provider(full_chain):
    first, run, _, _, _ = _run_replay(full_chain)
    provider = DeterministicFakeReplayProvider(thread_id=first.replay_thread_id)
    # Exercise the real-provider freshness rule without constructing Codex.
    provider.enforce_thread_freshness = True
    with pytest.raises(ReplayRunnerError, match="previously accepted"):
        _run_replay(full_chain, provider, name="replay-real-like")


def test_fake_overwrite_policy_is_deterministic(full_chain):
    first, run, _, _, output = _run_replay(full_chain)
    original = _tree_bytes(output)
    provider = DeterministicFakeReplayProvider()
    with pytest.raises(ReplayRunnerError, match="already exists"):
        _run_replay(full_chain, provider)
    assert provider.execution_count == 0 and _tree_bytes(output) == original
    second, *_ = _run_replay(full_chain, provider, overwrite=True)
    assert second.replay_thread_id == first.replay_thread_id


@pytest.mark.parametrize("field,value", [
    ("permission_profile", "other"), ("permission_profile_hash", "9"*64),
    ("network_enabled", True), ("approval_policy", "on-request"),
    ("web_search_mode", "enabled"), ("sandbox_backend", "other"),
])
def test_effective_permission_mismatch_fails_before_execution(full_chain, field, value):
    provider = DeterministicFakeReplayProvider(effective_overrides={field:value})
    with pytest.raises(ReplayRunnerError, match="mismatch|forbidden"):
        _run_replay(full_chain, provider)
    assert provider.execution_count == 0
    assert provider.call_order == ["isolation"]


def test_isolation_failure_prevents_provider(full_chain):
    provider = DeterministicFakeReplayProvider(isolation_succeeded=False)
    with pytest.raises(ReplayRunnerError, match="isolation"):
        _run_replay(full_chain, provider)
    assert provider.execution_count == 0 and provider.call_order == ["isolation"]


@pytest.mark.parametrize("mode,stage", [
    ("failure", "PROVIDER_EXECUTION"), ("auth_failure", "PROVIDER_EXECUTION"),
    ("invalid_json", "PROVIDER_RESULT_VALIDATION"),
    ("missing_final", "PROVIDER_RESULT_VALIDATION"),
    ("web", "PROVIDER_RESULT_VALIDATION"), ("mcp", "PROVIDER_RESULT_VALIDATION"),
])
def test_failed_attempt_preserves_evidence_and_stage(full_chain, mode, stage):
    run = full_chain[0]
    with pytest.raises(ReplayRunnerError):
        _run_replay(full_chain, DeterministicFakeReplayProvider(mode=mode))
    failure = next((run / "replay_failures").iterdir())
    data = json.loads((failure / "replay_failure_manifest.json").read_text())
    assert data["stage"] == stage
    assert (failure / "replay_prompt.txt").is_file()
    assert (failure / "raw_replay_events.jsonl").is_file()
    assert (failure / "replay_stderr.log").is_file()
    assert not (failure / "replay_manifest.json").exists()


def test_failed_overwrite_preserves_accepted_replay(full_chain):
    _, _, _, _, output = _run_replay(full_chain)
    before = _tree_bytes(output)
    with pytest.raises(ReplayRunnerError):
        _run_replay(full_chain, DeterministicFakeReplayProvider(mode="failure"), overwrite=True)
    assert _tree_bytes(output) == before


@pytest.mark.parametrize("tampered_name", REQUIRED_OUTPUTS)
def test_success_manifest_hashes_every_artifact_and_detects_tampering(full_chain, tampered_name):
    manifest, _, _, _, output = _run_replay(full_chain)
    assert set(REQUIRED_OUTPUTS).issubset(manifest.output_file_hashes)
    for name in REQUIRED_OUTPUTS:
        assert manifest.output_file_hashes[name] == sha256_file(output / name)
    assert validate_replay_artifacts(output).replay_trajectory_hash == manifest.replay_trajectory_hash
    with (output / tampered_name).open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ReplayRunnerError, match="hash mismatch"):
        validate_replay_artifacts(output)


def test_manifest_distinguishes_fake_and_live_execution_metadata(full_chain):
    fake, *_ = _run_replay(full_chain)
    data = fake.model_dump(mode="json")
    data.update(provider="codex", execution_mode="LIVE_MODEL", live_model_invoked=True,
                requested_model="gpt-5.6-sol", effective_model="gpt-5.6-sol",
                sandbox_platform="linux", sandbox_backend="codex-linux-sandbox")
    live = ReplayManifest.model_validate(data)
    assert live.live_model_invoked is True
    assert live.requested_model == "gpt-5.6-sol"
    assert live.effective_model == "gpt-5.6-sol"


def test_path_cannot_be_a_sandbox_backend(full_chain):
    fake, *_ = _run_replay(full_chain)
    data = fake.model_dump(mode="json")
    data["sandbox_backend"] = "/usr/local/bin:/usr/bin:/bin"
    with pytest.raises(Exception, match="sandbox_backend"):
        ReplayManifest.model_validate(data)


def test_fake_cli_reports_no_live_model(full_chain, capsys):
    run, intervention, _ = full_chain
    status = main([
        "--run-directory", str(run), "--intervention-directory", str(intervention),
        "--provider", "fake", "--output-dir", str(run / "replay-cli"),
        "--fixed-created-at", "2026-07-16T12:00:00Z",
    ])
    output = capsys.readouterr().out
    assert status == 0
    assert "Execution mode     DETERMINISTIC_FAKE" in output
    assert "Live model invoked NO" in output
    assert "Model              deterministic-fake-replay" in output
    assert "gpt-5.6-sol" not in output


def test_fake_execution_is_model_free_and_identity_is_stable(full_chain, monkeypatch):
    def forbidden_configuration(*_args, **_kwargs):
        raise AssertionError("fake replay must not construct a Codex configuration")
    monkeypatch.setattr(replay_runner, "CodexExecutionConfiguration", forbidden_configuration)
    first, _, _, _, _ = _run_replay(full_chain, name="replay-a")
    second, _, _, _, _ = _run_replay(full_chain, name="replay-b")
    assert first.replay_id == second.replay_id
    assert first.reasoning_effort == second.reasoning_effort == "deterministic"
    assert "gpt-5.6-sol" not in first.model_dump_json()
    signature = inspect.signature(CounterfactualReplayRunner.run)
    assert signature.parameters["model"].default is None
    assert signature.parameters["reasoning_effort"].default is None


@pytest.mark.parametrize("arguments", [
    ["--provider", "codex"],
    ["--provider", "codex", "--model", "test-live-model"],
    ["--provider", "codex", "--confirm-live-model"],
])
def test_live_cli_requires_explicit_opt_in_before_provider_construction(monkeypatch, capsys, arguments):
    constructed = []
    monkeypatch.setattr(replay_runner, "CodexReplayProvider", lambda: constructed.append(True))
    status = main(["--run-directory", "missing", "--intervention-directory", "missing", *arguments])
    captured = capsys.readouterr()
    assert status == 1
    assert "Live Codex replay requires explicit model selection and --confirm-live-model." in captured.err
    assert not constructed


def test_fake_cli_rejects_live_options_before_execution(capsys):
    status = main(["--run-directory", "missing", "--intervention-directory", "missing",
                   "--provider", "fake", "--model", "test-live-model"])
    assert status == 1
    assert "fake replay does not accept" in capsys.readouterr().err


def test_explicit_live_options_pass_cli_gate_without_live_execution(monkeypatch, capsys):
    captured = {}
    class StubProvider:
        pass
    def fake_run(self, *args, **kwargs):
        captured.update(kwargs)
        raise ReplayRunnerError("reached mocked live runner")
    monkeypatch.setattr(replay_runner, "CodexReplayProvider", StubProvider)
    monkeypatch.setattr(replay_runner.CounterfactualReplayRunner, "run", fake_run)
    status = main(["--run-directory", "missing", "--intervention-directory", "missing",
                   "--provider", "codex", "--model", "test-live-model",
                   "--reasoning-effort", "medium", "--confirm-live-model"])
    assert status == 1
    assert captured == {"model": "test-live-model", "reasoning_effort": "medium",
                        "created_at": None, "overwrite": False}
    assert "reached mocked live runner" in capsys.readouterr().err
