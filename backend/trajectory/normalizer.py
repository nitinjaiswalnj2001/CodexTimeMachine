"""Deterministic normalization of observable raw Codex events."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable

from backend.temporal.integrity import canonical_json_bytes, sha256_bytes

from .classifier import classify_command, redact_text
from .models import (
    EvidenceReference,
    MessageClassification,
    NormalizedEvent,
    ObservableEventStatus,
    ObservableEventType,
)
from .parser import RawEventRecord, event_thread_id


class TrajectoryNormalizationError(RuntimeError):
    """Raised for contradictory or boundary-unsafe observable evidence."""


_FILE_EVENT_TYPES = {
    "add": ObservableEventType.FILE_CREATED,
    "update": ObservableEventType.FILE_UPDATED,
    "delete": ObservableEventType.FILE_DELETED,
}
_SUPPORTED_ITEM_TYPES = frozenset({"agent_message", "command_execution", "file_change"})


def _normalize_workspace_references(value: str, workspace: Path, run_id: str) -> str:
    """Remove only demonstrable current-run locations from visible text.

    Raw evidence and its hashes retain the original values.  This helper is
    applied only to user-visible normalized fields, with workspace replacement
    preceding parent-run replacement so a workspace never degrades to
    ``<RUN_DIR>/workspace``.
    """
    resolved_workspace = workspace.resolve()
    resolved_run = resolved_workspace.parent
    normalized = value
    for variant in (str(resolved_workspace), resolved_workspace.as_posix()):
        normalized = normalized.replace(variant, ".")

    path_tail = r"(?=$|[\\/\s'\"),:;])"
    run_workspace = re.compile(
        rf"(?:(?:[A-Za-z]:)?(?:[\\/][^\\/\s'\"()]+)*)?"
        rf"[\\/]\.ctm_runs[\\/]{re.escape(run_id)}[\\/]workspace{path_tail}",
        re.IGNORECASE,
    )
    normalized = run_workspace.sub(".", normalized)
    normalized = normalized.replace(".\\", "./")

    for variant in (str(resolved_run), resolved_run.as_posix()):
        normalized = normalized.replace(variant, "<RUN_DIR>")
    run_directory = re.compile(
        rf"(?:(?:[A-Za-z]:)?(?:[\\/][^\\/\s'\"()]+)*)?"
        rf"[\\/]\.ctm_runs[\\/]{re.escape(run_id)}{path_tail}",
        re.IGNORECASE,
    )
    normalized = run_directory.sub("<RUN_DIR>", normalized)
    normalized = re.sub(
        r"\.(?:[\\/][^\\/\s'\"(),;]+)+",
        lambda match: match.group(0).replace("\\", "/"),
        normalized,
    )
    return re.sub(
        r"<RUN_DIR>(?:[\\/][^\\/\s'\"(),;]+)+",
        lambda match: match.group(0).replace("\\", "/"),
        normalized,
    )


def _source_hash(records: Iterable[RawEventRecord]) -> str:
    return sha256_bytes(canonical_json_bytes([record.payload for record in records]))


def _event_id(
    run_id: str,
    source_key: str,
    event_type: ObservableEventType,
    expansion_index: int,
) -> str:
    value = f"{run_id}\0{source_key}\0{event_type.value}\0{expansion_index}"
    return f"evt-{sha256_bytes(value.encode('utf-8'))[:24]}"


def _batch_id(run_id: str, item_id: str) -> str:
    value = f"{run_id}\0{item_id}".encode("utf-8")
    return f"batch-{sha256_bytes(value)[:20]}"


def _evidence(records: list[RawEventRecord], item_id: str | None) -> EvidenceReference:
    return EvidenceReference(
        raw_line_indexes=[record.line_index for record in records],
        source_item_id=item_id,
        source_raw_event_types=[record.event_type for record in records],
        source_fragments_sha256=_source_hash(records),
    )


def _timestamp(event: dict[str, Any], item: dict[str, Any] | None = None) -> Any:
    if item is not None and item.get("timestamp") is not None:
        return item["timestamp"]
    return event.get("timestamp")


def _path_parts(raw_path: str, workspace: Path, run_id: str) -> list[str]:
    if not raw_path or "\x00" in raw_path:
        raise TrajectoryNormalizationError(f"unsafe file-change path: {raw_path!r}")
    slash_path = raw_path.replace("\\", "/")
    windows = PureWindowsPath(raw_path)
    posix = PurePosixPath(slash_path)
    absolute = bool(windows.drive or windows.is_absolute() or posix.is_absolute())
    if not absolute:
        parts = slash_path.split("/")
    else:
        workspace_slash = workspace.resolve().as_posix().rstrip("/")
        comparison_path = slash_path.rstrip("/")
        casefold = bool(windows.drive)
        left = comparison_path.casefold() if casefold else comparison_path
        root = workspace_slash.casefold() if casefold else workspace_slash
        if left == root:
            parts = []
        elif left.startswith(root + "/"):
            parts = comparison_path[len(workspace_slash) + 1 :].split("/")
        else:
            all_parts = [part for part in slash_path.split("/") if part]
            marker: int | None = None
            for index in range(len(all_parts) - 2):
                if (
                    all_parts[index].casefold() == ".ctm_runs"
                    and all_parts[index + 1] == run_id
                    and all_parts[index + 2].casefold() == "workspace"
                ):
                    marker = index + 3
                    break
            if marker is None:
                raise TrajectoryNormalizationError(
                    f"file-change path is outside the run workspace: {raw_path!r}"
                )
            parts = all_parts[marker:]
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise TrajectoryNormalizationError(f"unsafe file-change path: {raw_path!r}")
    if PureWindowsPath("/".join(parts)).drive:
        raise TrajectoryNormalizationError(f"unsafe file-change path: {raw_path!r}")
    return parts


def normalize_workspace_path(raw_path: str, workspace: Path, run_id: str) -> str:
    workspace = workspace.resolve()
    parts = _path_parts(raw_path, workspace, run_id)
    candidate = workspace.joinpath(*parts)
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(workspace):
        raise TrajectoryNormalizationError(
            f"file-change path resolves outside the run workspace: {raw_path!r}"
        )
    return PurePosixPath(*parts).as_posix()


def _item_pair(
    records: list[RawEventRecord], item_id: str
) -> tuple[list[RawEventRecord], dict[str, Any], bool]:
    started = [record for record in records if record.event_type == "item.started"]
    completed = [record for record in records if record.event_type == "item.completed"]
    if len(started) > 1 or len(completed) > 1:
        raise TrajectoryNormalizationError(f"duplicate lifecycle event for item {item_id}")
    ordered = sorted(records, key=lambda record: record.line_index)
    primary = (completed or started)[0].payload["item"]
    return ordered, primary, bool(completed)


def _base_event(
    *,
    run_id: str,
    source_key: str,
    event_type: ObservableEventType,
    status: ObservableEventStatus,
    records: list[RawEventRecord],
    summary: str,
    item_id: str | None = None,
    item_type: str | None = None,
    expansion_index: int = 0,
    **values: Any,
) -> NormalizedEvent:
    evidence = _evidence(records, item_id)
    return NormalizedEvent(
        event_id=_event_id(run_id, source_key, event_type, expansion_index),
        sequence=1,
        event_type=event_type,
        status=status,
        source_event_indexes=evidence.raw_line_indexes,
        source_item_id=item_id,
        source_item_type=item_type,
        timestamp=_timestamp(records[-1].payload, values.pop("source_item", None)),
        summary=summary,
        evidence=evidence,
        **values,
    )


def _normalize_item(
    run_id: str,
    item_id: str,
    records: list[RawEventRecord],
    workspace: Path,
) -> list[NormalizedEvent]:
    ordered, item, completed = _item_pair(records, item_id)
    item_type = item["type"]
    incomplete = not completed
    status = ObservableEventStatus.INCOMPLETE if incomplete else ObservableEventStatus.SUCCEEDED
    source_item = item
    if item_type == "agent_message":
        raw_text = item.get("text", "")
        if not isinstance(raw_text, str):
            raise TrajectoryNormalizationError(f"agent message text is invalid for item {item_id}")
        visible_text = _normalize_workspace_references(raw_text, workspace, run_id)
        text, redacted = redact_text(visible_text)
        return [
            _base_event(
                run_id=run_id,
                source_key=item_id,
                event_type=ObservableEventType.AGENT_MESSAGE,
                status=status,
                records=ordered,
                summary=text,
                item_id=item_id,
                item_type=item_type,
                source_item=source_item,
                redactions_applied=redacted,
                metadata={
                    "message_classification": MessageClassification.OTHER.value,
                    "observable_text_sha256": sha256_bytes(raw_text.rstrip("\r\n").encode("utf-8")),
                },
            )
        ]
    if item_type == "command_execution":
        raw_command = item.get("command", "")
        if not isinstance(raw_command, str) or not raw_command:
            raise TrajectoryNormalizationError(f"command is invalid for item {item_id}")
        visible_command = _normalize_workspace_references(raw_command, workspace, run_id)
        command, command_redacted = redact_text(visible_command)
        exit_code = item.get("exit_code")
        if exit_code is not None and not isinstance(exit_code, int):
            raise TrajectoryNormalizationError(f"exit code is invalid for item {item_id}")
        raw_status = str(item.get("status", "")).casefold()
        if completed and (raw_status in {"failed", "error"} or (exit_code is not None and exit_code != 0)):
            status = ObservableEventStatus.FAILED
        raw_output = item.get("aggregated_output", item.get("output"))
        output_preview: str | None = None
        output_hash: str | None = None
        output_redacted = False
        if isinstance(raw_output, str):
            output_hash = sha256_bytes(raw_output.encode("utf-8"))
            visible_output = _normalize_workspace_references(raw_output[:500], workspace, run_id)
            output_preview, output_redacted = redact_text(visible_output)
        return [
            _base_event(
                run_id=run_id,
                source_key=item_id,
                event_type=ObservableEventType.COMMAND_EXECUTED,
                status=status,
                records=ordered,
                summary=f"Command {status.value.casefold()}.",
                item_id=item_id,
                item_type=item_type,
                source_item=source_item,
                command=command,
                exit_code=exit_code,
                command_tags=classify_command(raw_command),
                output_preview=output_preview,
                output_sha256=output_hash,
                redactions_applied=command_redacted or output_redacted,
            )
        ]
    if item_type == "file_change":
        changes = item.get("changes")
        if not isinstance(changes, list) or not changes:
            raise TrajectoryNormalizationError(f"file changes are invalid for item {item_id}")
        batch = _batch_id(run_id, item_id)
        normalized: list[NormalizedEvent] = []
        for change_index, change in enumerate(changes):
            if not isinstance(change, dict):
                raise TrajectoryNormalizationError(f"file change is invalid for item {item_id}")
            kind = change.get("kind")
            if kind not in _FILE_EVENT_TYPES:
                raise TrajectoryNormalizationError(
                    f"unknown file change kind {kind!r} for item {item_id}"
                )
            raw_path = change.get("path")
            if not isinstance(raw_path, str):
                raise TrajectoryNormalizationError(f"file path is invalid for item {item_id}")
            relative = normalize_workspace_path(raw_path, workspace, run_id)
            event_type = _FILE_EVENT_TYPES[kind]
            normalized.append(
                _base_event(
                    run_id=run_id,
                    source_key=item_id,
                    event_type=event_type,
                    status=status,
                    records=ordered,
                    summary=f"{event_type.value.replace('_', ' ').title()}: {relative}",
                    item_id=item_id,
                    item_type=item_type,
                    expansion_index=change_index,
                    source_item=source_item,
                    workspace_relative_paths=[relative],
                    metadata={"batch_id": batch, "change_index": change_index},
                )
            )
        return normalized
    return []


def normalize_records(
    records: list[RawEventRecord],
    run_id: str,
    workspace: Path,
    final_message: str | None,
    final_message_sha256: str | None,
) -> tuple[list[NormalizedEvent], list[str]]:
    warnings: list[str] = []
    positioned: list[tuple[int, int, NormalizedEvent]] = []
    item_records: dict[str, list[RawEventRecord]] = defaultdict(list)
    item_types: dict[str, str] = {}

    for record in records:
        if record.event_type in {"item.started", "item.completed"}:
            item = record.payload.get("item")
            if not isinstance(item, dict):
                raise TrajectoryNormalizationError(
                    f"{record.event_type} at line {record.line_index} has no item object"
                )
            item_id = item.get("id")
            item_type = item.get("type")
            if not isinstance(item_id, str) or not item_id:
                raise TrajectoryNormalizationError(f"item at line {record.line_index} has no ID")
            if not isinstance(item_type, str) or not item_type:
                raise TrajectoryNormalizationError(f"item {item_id} has no valid type")
            previous = item_types.setdefault(item_id, item_type)
            if previous != item_type:
                raise TrajectoryNormalizationError(
                    f"conflicting item types for source item {item_id}"
                )
            item_records[item_id].append(record)
            continue
        if record.event_type == "thread.started":
            thread_id = event_thread_id(record.payload)
            event = _base_event(
                run_id=run_id,
                source_key=f"line-{record.line_index}",
                event_type=ObservableEventType.THREAD_STARTED,
                status=ObservableEventStatus.SUCCEEDED,
                records=[record],
                summary=f"Thread started: {thread_id or 'unavailable'}",
            )
            positioned.append((record.line_index, 0, event))
        elif record.event_type == "turn.started":
            event = _base_event(
                run_id=run_id,
                source_key=f"line-{record.line_index}",
                event_type=ObservableEventType.TURN_STARTED,
                status=ObservableEventStatus.SUCCEEDED,
                records=[record],
                summary="Turn started.",
            )
            positioned.append((record.line_index, 0, event))
        elif record.event_type == "turn.completed":
            event = _base_event(
                run_id=run_id,
                source_key=f"line-{record.line_index}",
                event_type=ObservableEventType.TURN_COMPLETED,
                status=ObservableEventStatus.SUCCEEDED,
                records=[record],
                summary="Turn completed.",
            )
            positioned.append((record.line_index, 0, event))
        else:
            warnings.append(
                f"raw event at line {record.line_index} with type {record.event_type!r} was not normalized"
            )

    for item_id, lifecycle in item_records.items():
        started = any(record.event_type == "item.started" for record in lifecycle)
        completed = any(record.event_type == "item.completed" for record in lifecycle)
        if completed and not started:
            warnings.append(f"item {item_id} completed without a matching item.started event")
        item_type = item_types[item_id]
        if item_type not in _SUPPORTED_ITEM_TYPES:
            warnings.append(f"unsupported observable item type {item_type!r} for item {item_id}")
            continue
        normalized = _normalize_item(run_id, item_id, lifecycle, workspace)
        first_line = min(record.line_index for record in lifecycle)
        for expansion_index, event in enumerate(normalized):
            positioned.append((first_line, expansion_index, event))

    positioned.sort(key=lambda value: (value[0], value[1], value[2].event_id))
    events = [event.model_copy(update={"sequence": index}) for index, (_, _, event) in enumerate(positioned, 1)]

    agent_indexes = [
        index
        for index, event in enumerate(events)
        if event.event_type is ObservableEventType.AGENT_MESSAGE
        and event.status is ObservableEventStatus.SUCCEEDED
    ]
    if final_message is not None:
        if not agent_indexes:
            raise TrajectoryNormalizationError(
                "final_message.txt exists but no completed observable agent message was emitted"
            )
        final_index = agent_indexes[-1]
        expected_hash = sha256_bytes(final_message.rstrip("\r\n").encode("utf-8"))
        actual_hash = events[final_index].metadata["observable_text_sha256"]
        if actual_hash != expected_hash:
            raise TrajectoryNormalizationError(
                "final observable agent message does not match final_message.txt"
            )
        for index in agent_indexes:
            classification = (
                MessageClassification.FINAL_RESPONSE
                if index == final_index
                else MessageClassification.PROGRESS_UPDATE
            )
            metadata = {**events[index].metadata, "message_classification": classification.value}
            if index == final_index and final_message_sha256 is not None:
                metadata["final_message_sha256"] = final_message_sha256
            events[index] = events[index].model_copy(update={"metadata": metadata})
    return events, warnings
