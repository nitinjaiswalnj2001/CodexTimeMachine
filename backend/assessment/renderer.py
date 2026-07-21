"""Human-readable observable-evidence assessment rendering."""

from __future__ import annotations

from .models import TemporalBlindSpotAssessment


NOTICE = ("This assessment evaluates observable actions against separately grounded future evidence. "
          "It does not reconstruct hidden chain-of-thought and does not generate a replay intervention.")


def render_assessment(assessment: TemporalBlindSpotAssessment) -> str:
    lines = ["# Temporal Blind-Spot Assessment", "", NOTICE, "", "## Decision Under Evaluation", "",
             assessment.decision_under_evaluation, "", "## Evidence Boundary", "",
             f"Context: {assessment.context_id}", f"Context hash: {assessment.context_hash}", "",
             "## Target Assessments", ""]
    for target in assessment.target_assessments:
        lines.extend([f"### {target.target_id}", "", f"Verdict: {target.verdict}", "", target.summary, ""])
        if target.missing_investigation:
            lines.extend([f"Missing investigation: {target.missing_investigation}", ""])
        for ref in target.observed_past_evidence:
            lines.append(f"- Past event {ref.event_id} (sequence {ref.sequence}, {ref.event_type}): {ref.relevance}")
        for ref in target.known_future_evidence:
            lines.append(f"- Future evidence {ref.evidence_id}: {ref.relevance}")
        lines.append("")
    overall = assessment.overall_finding
    lines.extend(["## Overall Finding", "", f"Status: {overall.status}", "", overall.statement, "",
                  "## Limitations", ""])
    lines.extend(f"- {value}" for value in [*assessment.limitations, *overall.limitations])
    lines.extend(["", "## Integrity Summary", "", f"Assessment hash: {assessment.assessment_hash}",
                  f"Evaluator thread: {assessment.evaluator_metadata.thread_id}", ""])
    return "\n".join(lines)
