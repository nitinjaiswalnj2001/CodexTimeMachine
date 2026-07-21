"""Known-future packet loading and evidence-file validation."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

import yaml
from pydantic import ValidationError

from backend.temporal.integrity import canonical_json_bytes, sha256_bytes, sha256_file

from .models import KnownFutureOutcomePacket


class EvaluationPacketError(RuntimeError):
    pass


def packet_hash(packet: KnownFutureOutcomePacket) -> str:
    return sha256_bytes(
        canonical_json_bytes(packet.model_dump(mode="json", exclude={"packet_hash"}))
    )


def _safe_evidence_path(root: Path, relative_path: str) -> Path:
    windows = PureWindowsPath(relative_path)
    if windows.is_absolute() or windows.drive or relative_path.startswith(("/", "\\")):
        raise EvaluationPacketError(f"absolute evidence path is forbidden: {relative_path!r}")
    parts = relative_path.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise EvaluationPacketError(f"unsafe evidence path: {relative_path!r}")
    candidate = root.joinpath(*parts)
    if candidate.is_symlink():
        raise EvaluationPacketError(f"symlink evidence path is forbidden: {relative_path!r}")
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise EvaluationPacketError(f"evidence path escapes packet directory: {relative_path!r}")
    return resolved


def load_outcome_packet(path: str | Path) -> tuple[KnownFutureOutcomePacket, dict[str, Path]]:
    path = Path(path).resolve()
    root = path.parent
    try:
        packet = KnownFutureOutcomePacket.model_validate(yaml.safe_load(path.read_text("utf-8")))
    except (OSError, yaml.YAMLError, ValidationError, ValueError) as exc:
        raise EvaluationPacketError(f"invalid outcome packet: {exc}") from exc
    files: dict[str, Path] = {}
    for item in packet.evidence_items:
        evidence = _safe_evidence_path(root, item.relative_path)
        if not evidence.is_file():
            raise EvaluationPacketError(f"evidence file is missing or not regular: {item.relative_path}")
        if sha256_file(evidence) != item.sha256:
            raise EvaluationPacketError(f"evidence SHA-256 mismatch: {item.evidence_id}")
        files[item.evidence_id] = evidence
    computed = packet_hash(packet)
    if computed != packet.packet_hash:
        raise EvaluationPacketError("outcome packet hash mismatch")
    return packet, files
