"""Read-only parsing of raw Codex JSONL evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class TrajectoryParseError(RuntimeError):
    """Raised when raw evidence cannot be structurally validated."""


@dataclass(frozen=True)
class RawEventRecord:
    line_index: int
    event_type: str
    payload: dict[str, Any]


def parse_raw_events(path: str | Path) -> list[RawEventRecord]:
    records: list[RawEventRecord] = []
    for line_index, raw_line in enumerate(Path(path).read_bytes().splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TrajectoryParseError(f"invalid JSONL at line {line_index}: {exc}") from exc
        if not isinstance(payload, dict):
            raise TrajectoryParseError(f"raw event at line {line_index} is not an object")
        event_type = payload.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise TrajectoryParseError(f"raw event at line {line_index} has no valid type")
        records.append(RawEventRecord(line_index, event_type, payload))
    return records


def event_thread_id(payload: dict[str, Any]) -> str | None:
    for key in ("thread_id", "threadId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    thread = payload.get("thread")
    if isinstance(thread, dict):
        value = thread.get("id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
