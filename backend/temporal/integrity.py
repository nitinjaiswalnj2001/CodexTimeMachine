"""Shared deterministic serialization, hashing, and path-boundary helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PureWindowsPath
from typing import Any


class IntegrityError(ValueError):
    """Raised when a value cannot safely participate in a sealed snapshot."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def contains_git_part(path: Path | PureWindowsPath) -> bool:
    return any(part.casefold() == ".git" for part in path.parts)


def validate_logical_path(logical_path: str) -> Path:
    """Return a normalized safe relative path for a snapshot repository."""
    windows = PureWindowsPath(logical_path)
    if windows.is_absolute() or windows.drive or logical_path.startswith(("/", "\\")):
        raise IntegrityError(f"absolute logical path is forbidden: {logical_path!r}")
    parts = logical_path.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise IntegrityError(f"unsafe logical path: {logical_path!r}")
    if any(part.casefold() == ".git" for part in parts):
        raise IntegrityError(f".git content is forbidden: {logical_path!r}")
    return Path(*parts)
