"""Fresh per-run workspace creation from an audited immutable base snapshot."""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from backend.temporal.audit import BoundaryAuditor
from backend.temporal.integrity import canonical_json_bytes, sha256_bytes, sha256_file
from backend.temporal.models import SnapshotManifest


class RunWorkspaceError(RuntimeError):
    """Raised when a run workspace cannot prove an exact audited start state."""


@dataclass(frozen=True)
class PreparedWorkspace:
    run_directory: Path
    workspace: Path
    snapshot_manifest: SnapshotManifest
    workspace_start_hash: str


def _assert_not_within(path: Path, forbidden_root: Path, label: str) -> None:
    if path == forbidden_root or path.is_relative_to(forbidden_root):
        raise RunWorkspaceError(f"{label} must be outside sealed snapshot: {path}")


def tree_entries(root: str | Path) -> list[dict[str, str]]:
    """Return a deterministic regular-file tree description, rejecting redirects."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise RunWorkspaceError(f"workspace directory does not exist: {root}")
    entries: list[dict[str, str]] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RunWorkspaceError(f"symbolic links are forbidden in run workspace: {path}")
        relative = path.relative_to(root)
        if any(part.casefold() == ".git" for part in relative.parts):
            raise RunWorkspaceError(f".git is forbidden in run workspace: {path}")
        if any(part.casefold() == ".codex" for part in relative.parts):
            raise RunWorkspaceError(f".codex is forbidden in controlled run workspace: {path}")
        if path.is_file():
            entries.append(
                {
                    "logical_path": relative.as_posix(),
                    "content_sha256": sha256_file(path),
                }
            )
    return sorted(entries, key=lambda entry: entry["logical_path"])


def compute_workspace_tree_hash(root: str | Path) -> str:
    return sha256_bytes(canonical_json_bytes(tree_entries(root)))


class RunWorkspaceBuilder:
    def __init__(self, future_canary_token: str | None = None) -> None:
        self.future_canary_token = future_canary_token

    def prepare(
        self,
        sealed_snapshot: str | Path,
        run_directory: str | Path,
    ) -> PreparedWorkspace:
        sealed = Path(sealed_snapshot).resolve()
        run_directory = Path(run_directory).resolve()
        repo = (sealed / "repo").resolve()
        if run_directory == repo:
            raise RunWorkspaceError("sealed_snapshot/repo cannot be used as a run workspace")
        _assert_not_within(run_directory, sealed, "run directory")

        # Audit before creating any run-state directory or copying project evidence.
        BoundaryAuditor(self.future_canary_token).audit(sealed)
        try:
            manifest = SnapshotManifest.model_validate_json(
                (sealed / "manifest.json").read_text("utf-8")
            )
        except (OSError, ValidationError, ValueError) as exc:
            raise RunWorkspaceError(f"invalid audited snapshot manifest: {exc}") from exc
        if run_directory.exists():
            raise RunWorkspaceError(f"run directory already exists: {run_directory}")

        stage = run_directory.parent / f".{run_directory.name}.preparing-{uuid.uuid4().hex}"
        workspace = stage / "workspace"
        try:
            run_directory.parent.mkdir(parents=True, exist_ok=True)
            stage.mkdir()
            shutil.copytree(repo, workspace)
            start_hash = self.verify_starting_workspace(workspace, manifest)
            os.replace(stage, run_directory)
        except Exception:
            if stage.exists():
                shutil.rmtree(stage)
            raise
        return PreparedWorkspace(
            run_directory=run_directory,
            workspace=run_directory / "workspace",
            snapshot_manifest=manifest,
            workspace_start_hash=start_hash,
        )

    def verify_starting_workspace(
        self,
        workspace: str | Path,
        manifest: SnapshotManifest,
    ) -> str:
        workspace = Path(workspace).resolve()
        entries = tree_entries(workspace)
        actual = {
            entry["logical_path"].casefold(): entry["content_sha256"] for entry in entries
        }
        expected = {
            asset.logical_path.casefold(): asset.content_sha256
            for asset in manifest.materialized_assets
        }
        if set(actual) != set(expected):
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            raise RunWorkspaceError(
                f"workspace files differ from snapshot manifest; missing={missing}, extra={extra}"
            )
        mismatched = sorted(path for path in expected if actual[path] != expected[path])
        if mismatched:
            raise RunWorkspaceError(
                f"workspace file hashes differ from snapshot manifest: {mismatched}"
            )
        if self.future_canary_token is not None:
            canary = self.future_canary_token.encode("utf-8")
            for path in workspace.rglob("*"):
                if path.is_file() and canary in path.read_bytes():
                    raise RunWorkspaceError(f"future canary found in run workspace: {path}")
        return sha256_bytes(canonical_json_bytes(entries))
