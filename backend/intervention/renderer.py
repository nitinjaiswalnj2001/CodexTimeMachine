"""Render a Ghost Engineer intervention without exposing replay-excluded metadata."""

from __future__ import annotations

from .models import GhostIntervention, InterventionStatus


NOTICE = ("This artifact generates a minimal future-informed investigative clue. "
          "It does not reveal the known future result and does not execute a replay.")


def render_intervention(value: GhostIntervention) -> str:
    missing = value.rationale or value.reason or "No grounded missing investigation supported an intervention."
    clue = value.clue if value.status is InterventionStatus.INTERVENTION_GENERATED else "No intervention generated."
    lines = ["# Ghost Engineer Intervention", "", NOTICE, "",
             "## Identified Missing Investigation", "", missing, "",
             "## Minimum Future Clue", "", clue, "",
             "## Information Intentionally Withheld", "",
             "Future results, preferred solutions, metric values, assessment verdicts, and evaluator-only identifiers.", "",
             "## Safety and Leakage Checks", ""]
    lines.extend(f"- {constraint}" for constraint in value.constraints)
    lines.extend(f"- Warning: {warning}" for warning in value.warnings)
    lines.extend(["", "## Integrity Summary", "", f"Assessment hash: {value.assessment_hash}",
                  f"Intervention hash: {value.intervention_hash}", ""])
    return "\n".join(lines)
