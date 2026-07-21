"""Cross-phase integrity and temporal-boundary validation."""

from __future__ import annotations

from pathlib import Path

from backend.temporal.integrity import canonical_json_bytes, sha256_bytes, sha256_file
from backend.runs.workspace import RunWorkspaceError, compute_workspace_tree_hash, tree_entries
from backend.trajectory.models import ObservableTrajectory

from .models import BoundaryValidation, FutureEvidenceItem


class EvaluationIntegrityError(RuntimeError):
    pass


BOUNDARY_LIMITATION = (
    "Content-hash absence proves only that the declared evidence artifacts were not "
    "present as identical files; it does not prove semantically equivalent knowledge was impossible."
)


def trajectory_hash(trajectory: ObservableTrajectory) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            trajectory.model_dump(mode="json", exclude={"trajectory_hash", "extracted_at"})
        )
    )


def validate_future_boundary(
    workspace: Path,
    packet_directory: Path,
    items: list[FutureEvidenceItem],
    evidence_files: dict[str, Path],
    expected_workspace_hash: str | None,
) -> BoundaryValidation:
    # Do this before any absence check: an absent or altered workspace cannot
    # prove that future evidence was absent from the verified run state.
    if workspace.is_symlink():
        raise EvaluationIntegrityError("run workspace must not be a symbolic link")
    if not workspace.exists():
        raise EvaluationIntegrityError("run workspace is missing")
    if not workspace.is_dir():
        raise EvaluationIntegrityError("run workspace is not a directory")
    if not expected_workspace_hash:
        raise EvaluationIntegrityError("SUCCEEDED run manifest is missing workspace_end_hash")
    try:
        actual_workspace_hash = compute_workspace_tree_hash(workspace)
    except (RunWorkspaceError, OSError) as exc:
        raise EvaluationIntegrityError(f"could not verify final run workspace: {exc}") from exc
    if actual_workspace_hash != expected_workspace_hash:
        raise EvaluationIntegrityError("final run workspace hash mismatch")

    workspace = workspace.resolve()
    packet_directory = packet_directory.resolve()
    packet_outside = not (
        packet_directory == workspace or packet_directory.is_relative_to(workspace)
    )
    evidence_outside = all(
        not (path.resolve() == workspace or path.resolve().is_relative_to(workspace))
        for path in evidence_files.values()
    )
    no_paths = all(not workspace.joinpath(*item.relative_path.replace("\\", "/").split("/")).exists() for item in items)
    declared_hashes = {item.sha256 for item in items}
    workspace_hashes: set[str] = set()
    file_count = 0
    # tree_entries above already rejected symlinks, .git, and .codex. Reuse
    # that exact validated tree rather than treating a missing tree as empty.
    try:
        validated_entries = tree_entries(workspace)
    except (RunWorkspaceError, OSError) as exc:
        raise EvaluationIntegrityError(f"could not scan final run workspace: {exc}") from exc
    for entry in validated_entries:
        file_count += 1
        workspace_hashes.add(entry["content_sha256"])
    no_hashes = declared_hashes.isdisjoint(workspace_hashes)
    succeeded = packet_outside and evidence_outside and no_paths and no_hashes
    if not succeeded:
        failures = []
        if not packet_outside:
            failures.append("outcome packet is inside the Past workspace")
        if not evidence_outside:
            failures.append("future evidence is inside the Past workspace")
        if not no_paths:
            failures.append("a declared future evidence path exists in the Past workspace")
        if not no_hashes:
            failures.append("a declared future evidence hash exists in the Past workspace")
        raise EvaluationIntegrityError("; ".join(failures))
    return BoundaryValidation(
        expected_workspace_hash=expected_workspace_hash,
        actual_workspace_hash=actual_workspace_hash,
        workspace_integrity_succeeded=True,
        packet_outside_workspace=True,
        all_evidence_outside_workspace=True,
        no_evidence_paths_in_workspace=True,
        no_evidence_hashes_in_workspace=True,
        workspace_file_count_checked=file_count,
        validation_succeeded=True,
        warnings=[],
        limitation=BOUNDARY_LIMITATION,
    )
