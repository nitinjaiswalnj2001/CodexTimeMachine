"""Human-readable replay evidence rendering."""
from backend.trajectory.models import ObservableTrajectory

def render_replay_trajectory(value: ObservableTrajectory) -> str:
    lines = ["# Observable Counterfactual Replay Trajectory", "",
             "This document contains observable replay evidence only. It does not compare the replay with the baseline or reconstruct hidden chain-of-thought.", ""]
    for event in value.events:
        lines += [f"## {event.sequence}. {event.event_type.value}", "", event.summary, "",
                  "Evidence: raw lines " + ", ".join(map(str, event.source_event_indexes)), ""]
    return "\n".join(lines).rstrip() + "\n"
