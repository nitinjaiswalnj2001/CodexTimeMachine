from __future__ import annotations
import json, shutil
from pathlib import Path
import pytest
from backend.divergence.alignment import align_events
from backend.divergence.classifier import dimensions,first_structural_divergence,first_investigative_divergence,outcome
from backend.divergence.loader import DivergenceInputError,load_inputs
from backend.divergence.models import (ComparisonEvent,ComparisonSignature,DifferenceType,DimensionStatus,EventCategory,MessageAction,OutcomeStatus)
from backend.divergence.normalizer import (classify_message,classify_message_actions,comparison_events,normalize_command,normalize_path)
from backend.divergence.runner import DivergenceRunner,DivergenceRunnerError,main
from backend.replay.fake_provider import DeterministicFakeReplayProvider
from backend.temporal.integrity import canonical_json_bytes,sha256_file
from backend.trajectory.models import NormalizedEvent
from test_counterfactual_replay import _full_chain,_run_replay,FIXED_TIME

@pytest.fixture
def sources(tmp_path):
    chain=_full_chain(tmp_path);manifest,run,intervention,provider,replay=_run_replay(chain)
    return run,replay
@pytest.fixture
def built(sources):
    run,replay=sources;output=run/"divergence";value,manifest=DivergenceRunner().run(run,replay,output,created_at=FIXED_TIME)
    return run,replay,output,value,manifest

def test_end_to_end_divergence_outputs_and_integrity(built):
    run,replay,output,value,manifest=built
    assert all((output/x).is_file() for x in ("history_divergence.json","history_divergence.md","divergence_manifest.json"))
    assert value.observable_outcome.status is OutcomeStatus.OBSERVABLE_CHANGE_DETECTED
    assert manifest.baseline_event_count>0 and manifest.replay_event_count>0
    assert manifest.output_file_hashes["history_divergence.json"]==sha256_file(output/"history_divergence.json")
    assert manifest.output_file_hashes["history_divergence.md"]==sha256_file(output/"history_divergence.md")
def test_first_divergence_references_replay_evaluation(built):
    first=built[3].first_investigative_divergence
    assert first is not None

def test_structural_and_investigative_divergences_are_distinct(built):
    value=built[3]
    assert value.first_structural_divergence is not None
    assert value.first_investigative_divergence is not None
    assert value.first_structural_divergence.divergence_type is not DifferenceType.MATCHED
def test_markdown_has_boundary_and_no_quality_claim(built):
    text=(built[2]/"history_divergence.md").read_text("utf-8")
    assert "does not reconstruct hidden chain-of-thought" in text
    assert "does not" in text and "prove causal improvement" in text
    assert "improvement score" not in text.casefold()
def test_every_event_is_accounted_for(built):
    v=built[3];b={x.event_id for x in v.normalized_baseline_events};r={x.event_id for x in v.normalized_replay_events}
    assert b=={i for d in v.event_differences for i in d.baseline_event_ids}
    assert r=={i for d in v.event_differences for i in d.replay_event_ids}
def test_difference_references_are_valid(built):
    v=built[3];b={x.event_id for x in v.normalized_baseline_events};r={x.event_id for x in v.normalized_replay_events}
    assert all(set(d.baseline_event_ids)<=b and set(d.replay_event_ids)<=r for d in v.event_differences)
def test_behavioral_dimensions_have_neutral_statuses(built):
    for d in built[3].behavioral_dimensions:
        assert d.status in DimensionStatus
        assert not any(x in d.status.value for x in ("BETTER","WORSE","IMPROVED"))
def test_causality_limitation_is_always_present(built):
    assert any("does not prove" in x for x in built[3].limitations)

@pytest.mark.parametrize("command",["bash -lc 'pytest -q'","sh -c \"pytest -q\"","powershell -Command pytest -q"])
def test_shell_wrappers_normalize(command): assert normalize_command(command)=="pytest -q"
def test_cross_platform_paths_normalize_equivalently():
    assert normalize_path("C:\\tmp\\workspace\\src\\app.py")==normalize_path("/tmp/workspace/src/app.py")=="src/app.py"
def test_path_traversal_fails():
    with pytest.raises(ValueError):normalize_path("workspace/../future.yaml")
@pytest.mark.parametrize(("text","kind"),[("I will inspect the repository",MessageAction.INVESTIGATION_ANNOUNCEMENT),("I will run a representative evaluation",MessageAction.EVALUATION_INTENT),("I recommend a production default",MessageAction.RECOMMENDATION),("I updated the file",MessageAction.IMPLEMENTATION_INTENT),("Results were verified",MessageAction.EVIDENCE_SUMMARY),("Done",MessageAction.COMPLETION_STATEMENT),("Hello",MessageAction.OTHER)])
def test_message_actions_are_deterministic(text,kind): assert classify_message(text) is kind

def _normalized(sources):
    loaded=load_inputs(*sources);return comparison_events(loaded.baseline.events),comparison_events(loaded.replay.events)
def test_identical_stream_aligns_fully(sources):
    b,_=_normalized(sources);diffs,summary=align_events(b,b)
    assert summary.matched_count==len(b) and all(x.difference_type is DifferenceType.MATCHED for x in diffs)
def test_baseline_and_replay_only_classification(sources):
    b,r=_normalized(sources);diffs,_=align_events(b,r)
    assert any(x.difference_type in (DifferenceType.BASELINE_ONLY,DifferenceType.CONTRACTED) for x in diffs)
    assert any(x.difference_type in (DifferenceType.REPLAY_ONLY,DifferenceType.EXPANDED) for x in diffs)
def test_modified_event_classification(sources):
    b,r=_normalized(sources);diffs,_=align_events(b[:3],r[:3])
    assert any(x.difference_type is DifferenceType.MODIFIED for x in diffs)
def test_empty_trajectory_rejected(sources):
    run,replay=sources;data=json.loads((run/"trajectory/trajectory.json").read_text());data["events"]=[];data["event_count"]=0
    (run/"trajectory/trajectory.json").write_bytes(canonical_json_bytes(data)+b"\n")
    with pytest.raises(Exception):DivergenceRunner().run(run,replay,run/"divergence")

@pytest.mark.parametrize(("target,message"),[("baseline","baseline trajectory"),("replay","replay trajectory")])
def test_trajectory_tampering_fails(sources,target,message):
    run,replay=sources;path=(run/"trajectory/trajectory.json") if target=="baseline" else replay/"replay_trajectory.json";path.write_bytes(path.read_bytes()+b" ")
    with pytest.raises(Exception,match="hash|output"):load_inputs(run,replay)
@pytest.mark.parametrize(("field","value","message"),[("status","FAILED","replay"),("scenario_id","wrong","scenario"),("base_snapshot_hash","0"*64,"snapshot"),("baseline_workspace_start_hash","0"*64,"workspace-start"),("replay_thread_id","019f6bed-3413-7fe1-8e78-0e1f29e55d95","thread")])
def test_replay_lineage_mismatch_fails(sources,field,value,message):
    run,replay=sources;path=replay/"replay_manifest.json";data=json.loads(path.read_text());data[field]=value;path.write_bytes(canonical_json_bytes(data)+b"\n")
    with pytest.raises(Exception,match=message):load_inputs(run,replay)
def test_failed_isolation_fails(sources):
    run,replay=sources;path=replay/"replay_manifest.json";data=json.loads(path.read_text());data["isolation_result"]["probe_succeeded"]=False;data["isolation_result"]["failure_reasons"]=["x"];path.write_bytes(canonical_json_bytes(data)+b"\n")
    with pytest.raises(Exception):load_inputs(run,replay)
def test_ambiguous_live_provider_metadata_fails(sources):
    run,replay=sources;path=replay/"replay_manifest.json";data=json.loads(path.read_text());data.update(provider="codex",execution_mode="LIVE_MODEL",live_model_invoked=True,requested_model="x",effective_model="x");path.write_bytes(canonical_json_bytes(data)+b"\n")
    with pytest.raises(Exception,match="controlled fake"):load_inputs(run,replay)

@pytest.mark.parametrize("relative",["workspace/x","trajectory/x","evaluation/x","assessment-fake/x","intervention/x","replay-fake/x"])
def test_output_protection(sources,relative):
    run,replay=sources
    with pytest.raises(DivergenceRunnerError):DivergenceRunner().run(run,replay,run/relative,created_at=FIXED_TIME)
def test_output_outside_run_fails(sources,tmp_path):
    with pytest.raises(DivergenceRunnerError):DivergenceRunner().run(*sources,tmp_path/"outside",created_at=FIXED_TIME)
def test_overwrite_policy_and_determinism(sources):
    run,replay=sources;out=run/"divergence";first,_=DivergenceRunner().run(run,replay,out,created_at=FIXED_TIME)
    before=(out/"history_divergence.json").read_bytes()
    with pytest.raises(DivergenceRunnerError):DivergenceRunner().run(run,replay,out,created_at=FIXED_TIME)
    second,_=DivergenceRunner().run(run,replay,out,created_at=FIXED_TIME,overwrite=True)
    assert before==(out/"history_divergence.json").read_bytes() and first.divergence_hash==second.divergence_hash
def test_source_evidence_remains_unchanged(sources):
    run,replay=sources;protected=[run/"trajectory/trajectory.json",replay/"replay_trajectory.json",run/"intervention/replay_intervention.json"];before=[x.read_bytes() for x in protected]
    DivergenceRunner().run(run,replay,run/"divergence",created_at=FIXED_TIME)
    assert before==[x.read_bytes() for x in protected]
def test_cli_help_no_runtime_warning():
    with pytest.raises(SystemExit) as exc:main(["--help"])
    assert exc.value.code==0
def test_models_have_no_score_or_hidden_reasoning_fields():
    from backend.divergence.models import ObservableHistoryDivergence
    fields=set(ObservableHistoryDivergence.model_fields)
    assert not fields.intersection({"improvement_score","quality_score","divergence_score","chain_of_thought"})

@pytest.mark.parametrize("relative",[
    "intervention/replay_intervention.json", "intervention/ghost_intervention.json",
    "intervention/intervention_manifest.json", "final_message.txt", "trajectory/trajectory.md",
    "trajectory/trajectory_manifest.json", "evaluation/evaluation_context.json",
])
def test_parent_manifest_hash_tampering_fails(sources,relative):
    run,replay=sources;path=run/relative;path.write_bytes(path.read_bytes()+b" ")
    with pytest.raises(DivergenceInputError,match="hash mismatch|invalid divergence source"):
        load_inputs(run,replay)

@pytest.mark.parametrize("relative",[
    "replay_prompt.txt", "final_replay_message.txt", "replay_trajectory.md",
    "replay_trajectory_manifest.json",
])
def test_replay_output_tampering_fails(sources,relative):
    run,replay=sources;path=replay/relative;path.write_bytes(path.read_bytes()+b" ")
    with pytest.raises(DivergenceInputError,match="hash mismatch|invalid divergence source"):
        load_inputs(run,replay)

def test_file_only_change_has_neutral_outcome(sources):
    b,r=_normalized(sources)
    bf=[x for x in b if x.signature.event_category.value=="FILE"][:1]
    rf=[x for x in r if x.signature.event_category.value=="FILE"][:1]
    diffs,_=align_events(bf,rf);value=outcome(diffs,bf,rf)
    assert value.status is OutcomeStatus.OBSERVABLE_CHANGE_DETECTED
    assert "evaluation" not in value.statement.casefold()

def test_replay_evaluation_change_supports_evaluation_outcome(sources):
    b,r=_normalized(sources)
    replay_eval=[x for x in r if x.signature.message_action is MessageAction.EVALUATION_INTENT]
    diffs,_=align_events([],replay_eval);value=outcome(diffs,[],replay_eval)
    assert "evaluation" in value.statement.casefold()
    assert replay_eval[0].event_id in value.supporting_replay_event_ids

def test_true_reorder_is_detected(sources):
    b,_=_normalized(sources);events=b[:2]
    diffs,summary=align_events(events,list(reversed(events)))
    assert summary.reordered_count==2 and all(x.difference_type is DifferenceType.REORDERED for x in diffs)

def test_sequence_numbers_alone_do_not_imply_reorder(sources):
    b,_=_normalized(sources);other=[x.model_copy(update={"sequence":x.sequence+100}) for x in b[:2]]
    diffs,summary=align_events(b[:2],other)
    assert summary.reordered_count==0 and summary.matched_count==2

def test_alignment_debug_is_hashed_and_deterministic(sources):
    run,replay=sources;one=run/"debug-one";two=run/"debug-two"
    _,m1=DivergenceRunner().run(run,replay,one,created_at=FIXED_TIME,include_alignment_debug=True)
    _,m2=DivergenceRunner().run(run,replay,two,created_at=FIXED_TIME,include_alignment_debug=True)
    assert m1.output_file_hashes["alignment_debug.json"]==sha256_file(one/"alignment_debug.json")
    assert (one/"alignment_debug.json").read_bytes()==(two/"alignment_debug.json").read_bytes()
    (one/"alignment_debug.json").write_bytes(b"tampered")
    assert m1.output_file_hashes["alignment_debug.json"]!=sha256_file(one/"alignment_debug.json")

def _event(event_id, sequence, *, action=None, category=EventCategory.MESSAGE, tags=(), command=None, paths=(), file_op=None, summary=""):
    return ComparisonEvent(event_id=event_id,sequence=sequence,summary=summary,signature=ComparisonSignature(event_type="AGENT_MESSAGE" if category is EventCategory.MESSAGE else "COMMAND_EXECUTED",event_category=category,status="SUCCEEDED",message_action=action,command_category=list(tags),command_executable="pytest" if tags else None,normalized_command=command,paths=list(paths),file_operation=file_op))
def _dimension(values, kind): return next(x for x in values if x.dimension.value==kind)
def test_equivalent_profiles_ignore_different_event_ids():
    baseline=[_event("b1",1,action=MessageAction.EVALUATION_INTENT,summary="representative evaluation"),_event("b2",2,action=MessageAction.RECOMMENDATION,summary="production default recommendation")]
    replay=[_event("r1",1,action=MessageAction.EVALUATION_INTENT,summary="representative evaluation"),_event("r2",2,action=MessageAction.RECOMMENDATION,summary="production default recommendation")]
    values=dimensions(baseline,replay)
    assert _dimension(values,"EVALUATION_BREADTH").status is DimensionStatus.UNCHANGED
    assert _dimension(values,"RECOMMENDATION_SCOPE").status is DimensionStatus.UNCHANGED
    assert _dimension(values,"INVESTIGATIVE_SEQUENCE").status is DimensionStatus.UNCHANGED
def test_equivalent_file_and_command_profiles_ignore_ids():
    baseline=[_event("b1",1,category=EventCategory.FILE,paths=("tests/test_x.py",),file_op="FILE_CREATED"),_event("b2",2,category=EventCategory.COMMAND,tags=("test_execution",),command="pytest -q")]
    replay=[_event("r1",1,category=EventCategory.FILE,paths=("tests/test_x.py",),file_op="FILE_CREATED"),_event("r2",2,category=EventCategory.COMMAND,tags=("test_execution",),command="pytest -q")]
    values=dimensions(baseline,replay)
    assert _dimension(values,"FILE_ACTIVITY").status is DimensionStatus.UNCHANGED
    assert _dimension(values,"COMMAND_ACTIVITY").status is DimensionStatus.UNCHANGED
    assert _dimension(values,"EVIDENCE_GATHERING").status is DimensionStatus.UNCHANGED
def test_evaluation_profile_supersets_are_directional():
    baseline=[_event("b",1,action=MessageAction.EVALUATION_INTENT,summary="evaluation")]
    replay=baseline+[_event("r",2,category=EventCategory.COMMAND,tags=("test_execution",),command="pytest -q")]
    assert _dimension(dimensions(baseline,replay),"EVALUATION_BREADTH").status is DimensionStatus.MORE_OBSERVED_IN_REPLAY
    assert _dimension(dimensions(replay,baseline),"EVALUATION_BREADTH").status is DimensionStatus.LESS_OBSERVED_IN_REPLAY
def test_equal_profile_size_with_different_content_is_different():
    baseline=[_event("b",1,category=EventCategory.COMMAND,tags=("test_execution",),command="pytest -q")]
    replay=[_event("r",1,category=EventCategory.COMMAND,tags=("evaluation",),command="evaluate")]
    assert _dimension(dimensions(baseline,replay),"COMMAND_ACTIVITY").status is DimensionStatus.DIFFERENT
def test_generic_recommendation_scope_is_insufficient():
    baseline=[_event("b",1,action=MessageAction.RECOMMENDATION,summary="I recommend this.")]
    replay=[_event("r",1,action=MessageAction.RECOMMENDATION,summary="I recommend this.")]
    assert _dimension(dimensions(baseline,replay),"RECOMMENDATION_SCOPE").status is DimensionStatus.INSUFFICIENT_EVIDENCE
def test_first_investigative_divergence_uses_alignment_order():
    early=_event("b",2,action=MessageAction.INVESTIGATION_ANNOUNCEMENT,summary="inspect")
    later=_event("r",10,action=MessageAction.EVALUATION_INTENT,summary="evaluation")
    from backend.divergence.alignment import _diff
    differences=[_diff(DifferenceType.BASELINE_ONLY,[early],[],"early","basis"),_diff(DifferenceType.REPLAY_ONLY,[],[later],"later","basis")]
    value=first_investigative_divergence(differences,[early],[later])
    assert value.baseline_event_id=="b" and value.replay_event_id is None
def test_message_actions_preserve_multiple_observable_actions():
    actions=classify_message_actions("I evaluated the options, verified the evidence, recommend a production default, and completed the work.")
    assert actions==[MessageAction.EVALUATION_INTENT,MessageAction.RECOMMENDATION,MessageAction.EVIDENCE_SUMMARY,MessageAction.COMPLETION_STATEMENT]
def test_multi_action_message_supports_decision_timing_and_scope():
    baseline=[_event("b",1,action=MessageAction.EVALUATION_INTENT,summary="I evaluated evidence and recommend a production default.")]
    replay=[_event("r",1,action=MessageAction.EVALUATION_INTENT,summary="I evaluated evidence and recommend a production default.")]
    for event in baseline+replay:
        event.signature.message_actions=[MessageAction.EVALUATION_INTENT,MessageAction.RECOMMENDATION,MessageAction.EVIDENCE_SUMMARY]
    values=dimensions(baseline,replay)
    assert _dimension(values,"DECISION_TIMING").status is DimensionStatus.UNCHANGED
    assert _dimension(values,"RECOMMENDATION_SCOPE").status is DimensionStatus.UNCHANGED

def test_command_activity_preserves_multiplicity():
    baseline=[_event("b",1,category=EventCategory.COMMAND,tags=("test_execution",),command="pytest -q")]
    replay=[_event("r1",1,category=EventCategory.COMMAND,tags=("test_execution",),command="pytest -q"),_event("r2",2,category=EventCategory.COMMAND,tags=("test_execution",),command="pytest -q")]
    values=dimensions(baseline,replay)
    assert _dimension(values,"COMMAND_ACTIVITY").status is DimensionStatus.MORE_OBSERVED_IN_REPLAY
    assert _dimension(values,"EVIDENCE_GATHERING").status is DimensionStatus.MORE_OBSERVED_IN_REPLAY
    assert _dimension(dimensions(replay,baseline),"COMMAND_ACTIVITY").status is DimensionStatus.LESS_OBSERVED_IN_REPLAY
    assert _dimension(values,"COMMAND_ACTIVITY").replay_evidence==["r1","r2"]
def test_equal_command_multiplicity_with_different_ids_is_unchanged():
    baseline=[_event("b",1,category=EventCategory.COMMAND,tags=("test_execution",),command="pytest -q")]
    replay=[_event("r",1,category=EventCategory.COMMAND,tags=("test_execution",),command="pytest -q")]
    assert _dimension(dimensions(baseline,replay),"COMMAND_ACTIVITY").status is DimensionStatus.UNCHANGED
def test_file_activity_preserves_multiplicity():
    baseline=[_event("b",1,category=EventCategory.FILE,paths=("tests/test_x.py",),file_op="FILE_CREATED")]
    replay=[_event("r1",1,category=EventCategory.FILE,paths=("tests/test_x.py",),file_op="FILE_CREATED"),_event("r2",2,category=EventCategory.FILE,paths=("tests/test_x.py",),file_op="FILE_CREATED")]
    assert _dimension(dimensions(baseline,replay),"FILE_ACTIVITY").status is DimensionStatus.MORE_OBSERVED_IN_REPLAY
    assert _dimension(dimensions(replay,baseline),"FILE_ACTIVITY").status is DimensionStatus.LESS_OBSERVED_IN_REPLAY
def test_evaluation_breadth_uses_distinct_categories_not_multiplicity():
    baseline=[_event("b",1,category=EventCategory.COMMAND,tags=("test_execution",),command="pytest -q")]
    replay=[_event("r1",1,category=EventCategory.COMMAND,tags=("test_execution",),command="pytest -q"),_event("r2",2,category=EventCategory.COMMAND,tags=("test_execution",),command="pytest -q")]
    assert _dimension(dimensions(baseline,replay),"EVALUATION_BREADTH").status is DimensionStatus.UNCHANGED
def test_investigative_sequence_preserves_repetition_and_order():
    baseline=[_event("b1",1,action=MessageAction.INVESTIGATION_ANNOUNCEMENT),_event("b2",2,action=MessageAction.EVALUATION_INTENT)]
    replay=[_event("r1",1,action=MessageAction.EVALUATION_INTENT),_event("r2",2,action=MessageAction.INVESTIGATION_ANNOUNCEMENT),_event("r3",3,action=MessageAction.EVALUATION_INTENT)]
    assert _dimension(dimensions(baseline,replay),"INVESTIGATIVE_SEQUENCE").status is DimensionStatus.DIFFERENT
