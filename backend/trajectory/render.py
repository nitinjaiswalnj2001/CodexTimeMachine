"""Human-readable rendering of normalized observable evidence."""

from __future__ import annotations

from .models import NormalizedEvent, ObservableEventType, ObservableTrajectory


def _line_reference(indexes: list[int]) -> str:
    if len(indexes) == 1:
        return str(indexes[0])
    if indexes == list(range(indexes[0], indexes[-1] + 1)):
        return f"{indexes[0]}–{indexes[-1]}"
    return ", ".join(str(index) for index in indexes)


def _event_title(event: NormalizedEvent) -> str:
    label = event.event_type.value.replace("_", " ").title()
    if event.event_type is ObservableEventType.COMMAND_EXECUTED:
        label = f"Command {event.status.value.casefold()}"
    return label


def render_markdown(trajectory: ObservableTrajectory) -> str:
    lines = [
        "# Observable Engineering Trajectory",
        "",
        f"Run: {trajectory.run_id}",
        f"Scenario: {trajectory.scenario_id}",
        f"Thread: {trajectory.thread_id}",
        f"Events: {trajectory.event_count}",
        "",
        "This document contains observable run evidence only. It does not reconstruct hidden chain-of-thought.",
        "",
    ]
    for event in trajectory.events:
        lines.extend([f"## {event.sequence}. {_event_title(event)}", ""])
        if event.event_type is ObservableEventType.AGENT_MESSAGE:
            lines.extend(["Observable statement:", "", event.summary, ""])
        elif event.event_type is ObservableEventType.COMMAND_EXECUTED:
            lines.extend(["Command:", "", "```text", event.command or "", "```", ""])
            lines.extend([f"Exit code: {event.exit_code if event.exit_code is not None else 'unavailable'}", ""])
            lines.extend(
                ["Tags: " + (", ".join(tag.value for tag in event.command_tags) or "none"), ""]
            )
            if event.output_preview is not None:
                lines.extend(["Output preview:", "", "```text", event.output_preview, "```", ""])
        else:
            lines.extend([event.summary, ""])
        if event.workspace_relative_paths:
            lines.extend(["Path: " + ", ".join(event.workspace_relative_paths), ""])
        lines.extend(
            [
                f"Evidence: raw lines {_line_reference(event.evidence.raw_line_indexes)}; "
                f"SHA-256 {event.evidence.source_fragments_sha256}",
                "",
            ]
        )
    if trajectory.warnings:
        lines.extend(["## Extraction warnings", ""])
        lines.extend(f"- {warning}" for warning in trajectory.warnings)
        lines.append("")
    return "\n".join(lines)
