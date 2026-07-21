"""Markdown rendering with explicit past/future separation."""

from __future__ import annotations

from .models import EvaluationContext


WARNING = (
    "This context contains observable run evidence and separately controlled future evidence. "
    "It does not reconstruct hidden chain-of-thought, score the agent, identify a blind spot, "
    "generate an intervention, or perform a replay."
)


def render_context(context: EvaluationContext) -> str:
    lines = [
        "# Temporal Evaluation Context", "", WARNING, "",
        f"Run: {context.run_id}", f"Scenario: {context.scenario_id}",
        f"Thread: {context.thread_id}", f"Provenance: {context.provenance_type}", "",
        "## Past Observable Trajectory", "",
    ]
    for event in context.past_observable_evidence:
        lines.append(f"- {event.sequence}. {event.event_type}: {event.summary}")
    lines.extend(["", "## Temporal Boundary", "", context.boundary_validation.limitation, ""])
    if context.fixture_notice:
        lines.extend([context.fixture_notice, ""])
    lines.extend(["## Known Future Outcome", "", context.known_future_outcome, "", "## Future Evidence", ""])
    for item in context.known_future_evidence:
        lines.append(f"- {item.evidence_id} ({item.evidence_kind}): {item.summary} [SHA-256 {item.sha256}]")
    lines.extend(["", "## Evaluation Targets", ""])
    for target in context.evaluation_targets:
        lines.append(f"- {target.target_id} ({target.category}): {target.description}")
    lines.extend([
        "", "## Integrity Summary", "",
        f"Boundary validation: {'SUCCEEDED' if context.boundary_validation.validation_succeeded else 'FAILED'}",
        f"Trajectory hash: {context.trajectory_hash}",
        f"Outcome packet hash: {context.outcome_packet_hash}",
        f"Context hash: {context.context_hash}", "",
        "No agent score has been produced. No blind spot has been identified. "
        "No intervention has been generated. No replay has been performed.", "",
    ])
    return "\n".join(lines)
