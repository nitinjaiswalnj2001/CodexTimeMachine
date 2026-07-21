"""Human-readable rendering of observable divergence evidence."""
from .models import DifferenceType
def render(value):
    structural=value.first_structural_divergence.summary if value.first_structural_divergence else "No structural divergence was detected."
    investigative=value.first_investigative_divergence.summary if value.first_investigative_divergence else "No investigative divergence was detected."
    replay_evaluation=value.first_replay_evaluation_divergence.summary if value.first_replay_evaluation_divergence else "No replay-specific evaluation divergence was detected."
    def section(kind):
        rows=[x for x in value.event_differences if x.difference_type in kind]
        return "\n".join(f"- {x.difference_type.value}: {x.summary} (baseline: {', '.join(x.baseline_event_ids) or 'none'}; replay: {', '.join(x.replay_event_ids) or 'none'})" for x in rows) or "- None."
    dims="\n".join(f"- {x.dimension.value}: {x.status.value} — {x.summary}" for x in value.behavioral_dimensions)
    return f"""# Observable History Divergence

This comparison evaluates observable event histories only. It does not reconstruct hidden chain-of-thought, judge technical correctness, or prove causal improvement.

## Evidence Boundary

Only accepted normalized baseline and replay events and intervention lineage metadata were compared.

## Baseline and Replay Identity

- Baseline: {value.baseline_run_id}
- Replay: {value.replay_id}
- Scenario: {value.scenario_id}

## First Structural Divergence

{structural}

## First Investigative Divergence

{investigative}

## First Replay Evaluation Divergence

{replay_evaluation}

## Event Alignment Summary

- Matched: {value.alignment.matched_count}
- Baseline-only: {value.alignment.baseline_only_count}
- Replay-only: {value.alignment.replay_only_count}
- Modified: {value.alignment.modified_count}
- Reordered: {value.alignment.reordered_count}

## Baseline-Only Events

{section({DifferenceType.BASELINE_ONLY, DifferenceType.CONTRACTED})}

## Replay-Only Events

{section({DifferenceType.REPLAY_ONLY, DifferenceType.EXPANDED})}

## Modified or Reordered Events

{section({DifferenceType.MODIFIED, DifferenceType.REORDERED})}

## Behavioral Dimensions

{dims}

## Observable Outcome

**{value.observable_outcome.status.value}** — {value.observable_outcome.statement}

## Limitations

"""+"\n".join(f"- {x}" for x in value.limitations)+f"""

## Integrity Summary

- Divergence hash: `{value.divergence_hash}`
- Baseline trajectory: `{value.baseline_trajectory_hash}`
- Replay trajectory: `{value.replay_trajectory_hash}`
"""
