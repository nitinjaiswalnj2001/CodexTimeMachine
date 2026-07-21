"""Read-only validation and summary of raw Codex JSONL event evidence."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .models import EventStreamSummary


class EventStreamError(RuntimeError):
    """Raised when captured stdout is not a valid JSONL event stream."""


FAILURE_EVENT_TYPES = frozenset({"error", "turn.failed", "item.failed"})
DEFAULT_FORBIDDEN_ITEM_TYPES = frozenset(
    {
        "web_search",
        "web_search_call",
        "web_search_result",
        "mcp_tool_call",
        "mcp_call",
        "mcp_tool",
    }
)


def is_failure_event_type(event_type: str) -> bool:
    """Classify explicit top-level execution failures without rejecting evolution."""
    return event_type in FAILURE_EVENT_TYPES or event_type.endswith(".error")


def _thread_id(event: dict[str, Any]) -> str | None:
    for key in ("thread_id", "threadId"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    thread = event.get("thread")
    if isinstance(thread, dict) and isinstance(thread.get("id"), str) and thread["id"].strip():
        return thread["id"].strip()
    return None


def summarize_event_stream(
    path: str | Path,
    forbidden_item_types: frozenset[str] = DEFAULT_FORBIDDEN_ITEM_TYPES,
) -> EventStreamSummary:
    """Validate JSONL without modifying the raw bytes or rejecting unknown types."""
    counts: Counter[str] = Counter()
    thread_id: str | None = None
    failure_counts: Counter[str] = Counter()
    item_counts: Counter[str] = Counter()
    thread_started_count = 0
    for line_number, raw_line in enumerate(Path(path).read_bytes().splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise EventStreamError(f"invalid JSONL at line {line_number}: {exc}") from exc
        if not isinstance(event, dict):
            raise EventStreamError(f"JSONL event at line {line_number} is not an object")
        event_type = event.get("type", "unknown")
        if not isinstance(event_type, str):
            event_type = "unknown"
        counts[event_type] += 1
        if event_type == "thread.started":
            thread_started_count += 1
            captured = _thread_id(event)
            if thread_id is None and captured:
                thread_id = captured
        if is_failure_event_type(event_type):
            failure_counts[event_type] += 1
        item = event.get("item")
        if isinstance(item, dict) and isinstance(item.get("type"), str):
            item_counts[item["type"]] += 1
    forbidden = sorted(item_type for item_type in item_counts if item_type in forbidden_item_types)
    return EventStreamSummary(
        event_count=sum(counts.values()),
        event_types=dict(sorted(counts.items())),
        thread_id=thread_id,
        has_error_event=bool(failure_counts),
        failure_event_types=dict(sorted(failure_counts.items())),
        thread_started_count=thread_started_count,
        item_types=dict(sorted(item_counts.items())),
        forbidden_item_types=forbidden,
    )
