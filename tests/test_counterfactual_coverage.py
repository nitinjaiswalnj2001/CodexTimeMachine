from __future__ import annotations
import json
import pytest
from backend.counterfactual.coverage import target_coverages
from backend.counterfactual.loader import CounterfactualInputError,load_inputs
from backend.counterfactual.models import ReplayCoverageStatus,ShiftStatus
from backend.counterfactual.runner import CounterfactualCoverageRunner,CounterfactualRunnerError
from backend.divergence.runner import DivergenceRunner
from backend.temporal.integrity import canonical_json_bytes
from test_counterfactual_replay import _full_chain,_run_replay,FIXED_TIME

@pytest.fixture
def sources(tmp_path):
 chain=_full_chain(tmp_path);_,run,_,_,replay=_run_replay(chain);div=run/"divergence";DivergenceRunner().run(run,replay,div,created_at=FIXED_TIME)
 return run,replay,div
def test_valid_lineage_and_controlled_coverage(sources):
 run,replay,div=sources;value,manifest=CounterfactualCoverageRunner().run(run,replay,div,run/"counterfactual",created_at=FIXED_TIME)
 assert value.target_coverages[0].baseline_verdict.value=="MISSED"
 assert value.target_coverages[0].replay_coverage_status is ReplayCoverageStatus.OBSERVED
 assert value.shift.status is ShiftStatus.TARGET_COVERAGE_INCREASED
 assert (run/"counterfactual/counterfactual_manifest.json").is_file()
def test_generic_announcement_is_not_observed(sources):
 inputs=load_inputs(*sources);events=[x for x in inputs.trajectory.events if x.source_item_type=="agent_message"][:1]
 values=target_coverages(inputs.context,inputs.assessment,inputs.trajectory.model_copy(update={"events":events}))
 assert values[0].replay_coverage_status is not ReplayCoverageStatus.OBSERVED
@pytest.mark.parametrize("relative",["evaluation/evaluation_context.json","intervention/replay_intervention.json","replay-fake/replay_trajectory.json","divergence/history_divergence.json"])
def test_hash_tampering_fails(sources,relative):
 run,replay,div=sources;path=run/relative;path.write_bytes(path.read_bytes()+b" ")
 with pytest.raises(CounterfactualInputError):load_inputs(run,replay,div)
def test_fixed_time_is_deterministic_and_output_protected(sources):
 run,replay,div=sources;one=run/"counter-one";two=run/"counter-two"
 first,_=CounterfactualCoverageRunner().run(run,replay,div,one,created_at=FIXED_TIME);second,_=CounterfactualCoverageRunner().run(run,replay,div,two,created_at=FIXED_TIME)
 assert (one/"counterfactual_coverage.json").read_bytes()==(two/"counterfactual_coverage.json").read_bytes()
 assert first.coverage_hash==second.coverage_hash
 with pytest.raises(CounterfactualRunnerError):CounterfactualCoverageRunner().run(run,replay,div,run/"trajectory",created_at=FIXED_TIME)
def test_citations_resolve(sources):
 run,replay,div=sources;value,_=CounterfactualCoverageRunner().run(run,replay,div,run/"counterfactual",created_at=FIXED_TIME)
 inputs=load_inputs(run,replay,div);baseline={x.event_id for x in inputs.context.past_observable_evidence};replay_ids={x.event_id for x in inputs.trajectory.events}
 assert set(value.target_coverages[0].baseline_evidence)<=baseline
 assert set(value.target_coverages[0].replay_evidence)<=replay_ids

def test_canonical_divergence_tampering_fails_even_when_manifest_hash_is_updated(sources):
 run,replay,div=sources
 path=div/"history_divergence.json";data=json.loads(path.read_text("utf-8"))
 data["observable_outcome"]["statement"]="Tampered observable conclusion."
 path.write_text(json.dumps(data),encoding="utf-8")
 manifest_path=div/"divergence_manifest.json";manifest=json.loads(manifest_path.read_text("utf-8"))
 from backend.temporal.integrity import sha256_file
 manifest["output_file_hashes"]["history_divergence.json"]=sha256_file(path)
 manifest_path.write_text(json.dumps(manifest),encoding="utf-8")
 with pytest.raises(CounterfactualInputError,match="observable outcome mismatch"):
  load_inputs(run,replay,div)
