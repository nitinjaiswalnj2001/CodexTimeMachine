"""Portable deterministic event signatures for structural comparison."""
from __future__ import annotations
import re, shlex
from pathlib import PurePosixPath
from backend.trajectory.models import ObservableEventType
from .models import ComparisonEvent, ComparisonSignature, EventCategory, MessageAction

_WRAPPER = re.compile(r"^\s*(?:(?:/bin/)?(?:ba)?sh\s+-(?:l?c|cl)\s+|(?:powershell(?:\.exe)?|pwsh)\s+(?:-command|-c)\s+)", re.I)
def normalize_command(value):
    value = _WRAPPER.sub("", value.strip()).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'": value = value[1:-1]
    return " ".join(value.split())
def _executable(command):
    try: parts = shlex.split(command, posix=True)
    except ValueError: parts = command.split()
    return PurePosixPath(parts[0].replace("\\", "/")).name.casefold() if parts else None
def normalize_path(value):
    value = re.sub(r"^[A-Za-z]:[/\\]", "/", value).replace("\\", "/")
    parts = [p for p in value.split("/") if p not in ("", ".")]
    if ".." in parts: raise ValueError("comparison path traversal is forbidden")
    folded = [p.casefold() for p in parts]
    if "workspace" in folded: return "/".join(parts[folded.index("workspace") + 1:]) or "."
    return "/".join(parts)
def classify_message_actions(value):
    text = value.casefold()
    rules = [(r"\b(inspect|investigat|trace|review)\w*\b", MessageAction.INVESTIGATION_ANNOUNCEMENT),(r"\b(evaluat|benchmark|representative|query types)\w*\b", MessageAction.EVALUATION_INTENT),(r"\b(recommend|production default|select(?:ing)? a default|leaving the default unchanged|defer)\w*\b", MessageAction.RECOMMENDATION),(r"\b(implement|modify|update|change)\w*\b", MessageAction.IMPLEMENTATION_INTENT),(r"\b(evidence|result|observed|verified|comparison)\w*\b", MessageAction.EVIDENCE_SUMMARY),(r"\b(completed|finished|done)\b", MessageAction.COMPLETION_STATEMENT)]
    actions=[kind for pattern,kind in rules if re.search(pattern,text)]
    return actions or [MessageAction.OTHER]
def classify_message(value):
    return classify_message_actions(value)[0]
def _category(event):
    if event.event_type is ObservableEventType.AGENT_MESSAGE: return EventCategory.MESSAGE
    if event.event_type is ObservableEventType.COMMAND_EXECUTED: return EventCategory.COMMAND
    if event.event_type.value.startswith("FILE_"): return EventCategory.FILE
    if event.event_type is ObservableEventType.THREAD_STARTED: return EventCategory.THREAD
    return EventCategory.TURN
def comparison_event(event):
    cat = _category(event); command = normalize_command(event.command) if event.command else None
    actions=classify_message_actions(event.summary) if cat is EventCategory.MESSAGE else []
    sig = ComparisonSignature(event_type=event.event_type.value,event_category=cat,status=event.status.value,command_category=sorted(x.value for x in event.command_tags),command_executable=_executable(command) if command else None,normalized_command=command,paths=sorted(normalize_path(x) for x in event.workspace_relative_paths),file_operation=event.event_type.value if cat is EventCategory.FILE else None,message_action=actions[0] if actions else None,message_actions=actions)
    return ComparisonEvent(event_id=event.event_id, sequence=event.sequence, signature=sig, summary=event.summary)
def comparison_events(events): return [comparison_event(x) for x in events]
def signature_key(event):
    s = event.signature
    if s.event_category in (EventCategory.THREAD, EventCategory.TURN): return (s.event_type, s.status)
    if s.event_category is EventCategory.MESSAGE: return (s.event_type, tuple(x.value for x in s.message_actions), s.status)
    if s.event_category is EventCategory.COMMAND: return (s.event_type, tuple(s.command_category), s.command_executable, s.normalized_command, s.status)
    return (s.event_type, tuple(s.paths), s.file_operation, s.status)
