def render(value):
 rows="\n".join(f"- {item.target_id}: **{item.replay_coverage_status.value}** — {item.coverage_statement}" for item in value.target_coverages)
 return f"""# Counterfactual Target-Coverage Assessment

This assessment determines whether the replay observably addressed a specified investigation target. It does not judge technical correctness, measure performance improvement or prove causality.

## Evidence Boundary

Only accepted Phase 4–8 artifacts were used.

## Evaluation Target

{rows}

## Baseline Coverage

Baseline verdicts are recorded from the accepted blind-spot assessment.

## Replay Coverage

{rows}

## Observable Shift

**{value.shift.status.value}** — {value.shift.statement}

## Activity Volume Versus Target Coverage

{value.activity_volume_context.statement}

Total activity: **{value.activity_volume_context.total_activity_status.value}**

Target coverage: **{value.activity_volume_context.target_coverage_relationship.value}**

- Evaluation breadth: **{value.activity_volume_context.evaluation_breadth_status.value}**
- Evidence gathering: **{value.activity_volume_context.evidence_gathering_status.value}**

## Remaining Uncertainty

- Target coverage describes whether a required investigation was observably attempted. It does not establish the quality, validity or correctness of that investigation.

## Causality and Correctness Limitations

- This assessment does not prove that the clue caused the observed change.

## Integrity Summary

- Coverage hash: `{value.coverage_hash}`
"""
