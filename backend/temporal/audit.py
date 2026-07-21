"""Integrity and information-boundary audit for sealed snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .availability import (
    derive_boundary_classification,
    derive_classification_reason,
    summarize_boundary_classifications,
)
from .integrity import IntegrityError, canonical_json_bytes, sha256_bytes, sha256_file, validate_logical_path
from .models import (
    AvailabilityStatus,
    BoundaryClassification,
    BoundaryControl,
    BoundaryReport,
    SnapshotManifest,
    VisibilityScope,
)
from .snapshot import _stable_root_inputs


class BoundaryAuditError(RuntimeError):
    """Raised when any sealed snapshot boundary invariant is violated."""


@dataclass(frozen=True)
class BoundaryAuditResult:
    passed: bool
    checked_files: int
    canary_configured: bool


class BoundaryAuditor:
    def __init__(self, future_canary_token: str | None = None) -> None:
        if future_canary_token == "":
            raise ValueError("future canary token must not be empty")
        self.future_canary_token = future_canary_token

    def audit(self, snapshot_directory: str | Path) -> BoundaryAuditResult:
        root = Path(snapshot_directory).resolve()
        repo = root / "repo"
        manifest = self._read_model(root / "manifest.json", SnapshotManifest, "snapshot manifest")
        control = self._read_model(root / "boundary_control.json", BoundaryControl, "boundary control")
        report = self._read_model(root / "boundary_report.json", BoundaryReport, "boundary report")
        if not repo.is_dir():
            raise BoundaryAuditError("snapshot repo directory is missing")

        self._verify_artifact_hashes(manifest, control, report)
        control_by_id = self._cross_check_control_and_report(manifest, control, report)
        self._verify_report_summary(report)
        materialized_by_id = self._cross_check_materialization(manifest, control_by_id)
        self._verify_tree(root, repo, materialized_by_id)
        return BoundaryAuditResult(True, len(materialized_by_id), self.future_canary_token is not None)

    @staticmethod
    def _read_model(path: Path, model_type, label: str):
        try:
            return model_type.model_validate_json(path.read_text("utf-8"))
        except (OSError, ValidationError, ValueError) as exc:
            raise BoundaryAuditError(f"invalid {label}: {exc}") from exc

    @staticmethod
    def _verify_artifact_hashes(
        manifest: SnapshotManifest, control: BoundaryControl, report: BoundaryReport
    ) -> None:
        canonical_assets = [asset.model_dump(mode="json") for asset in manifest.materialized_assets]
        if sha256_bytes(canonical_json_bytes(canonical_assets)) != manifest.asset_manifest_hash:
            raise BoundaryAuditError("asset manifest hash mismatch")
        if sha256_bytes(canonical_json_bytes(control.model_dump(mode="json"))) != manifest.boundary_control_hash:
            raise BoundaryAuditError("boundary control hash mismatch")
        if sha256_bytes(canonical_json_bytes(report.model_dump(mode="json"))) != manifest.boundary_report_hash:
            raise BoundaryAuditError("boundary report hash mismatch")
        if sha256_bytes(canonical_json_bytes(_stable_root_inputs(manifest))) != manifest.snapshot_root_hash:
            raise BoundaryAuditError("snapshot root hash mismatch")

    @staticmethod
    def _cross_check_control_and_report(
        manifest: SnapshotManifest, control: BoundaryControl, report: BoundaryReport
    ) -> dict[str, object]:
        if control.scenario_id != manifest.scenario_id or report.scenario_id != manifest.scenario_id:
            raise BoundaryAuditError("scenario_id differs across snapshot control artifacts")
        control_by_id: dict[str, object] = {}
        for entry in control.entries:
            if entry.asset_id in control_by_id:
                raise BoundaryAuditError(f"duplicate boundary control asset_id: {entry.asset_id}")
            control_by_id[entry.asset_id] = entry
        report_by_id: dict[str, object] = {}
        for entry in report.entries:
            if entry.asset_id in report_by_id:
                raise BoundaryAuditError(f"duplicate boundary report asset_id: {entry.asset_id}")
            report_by_id[entry.asset_id] = entry
        if set(control_by_id) != set(report_by_id):
            raise BoundaryAuditError("boundary control and report declared assets differ")
        for asset_id, control_entry in control_by_id.items():
            expected_classification = derive_boundary_classification(
                control_entry.availability_status,
                control_entry.visibility_scope,
            )
            if control_entry.classification is not expected_classification:
                raise BoundaryAuditError(
                    f"boundary classification inconsistent for asset {asset_id}"
                )
            expected_reason = derive_classification_reason(
                control_entry.availability_reason,
                control_entry.availability_status,
                control_entry.visibility_scope,
                expected_classification,
            )
            if control_entry.classification_reason != expected_reason:
                raise BoundaryAuditError(
                    f"boundary classification reason inconsistent for asset {asset_id}"
                )
            report_entry = report_by_id[asset_id]
            if (
                control_entry.logical_path != report_entry.logical_path
                or control_entry.classification is not report_entry.status
                or control_entry.classification_reason != report_entry.reason
            ):
                raise BoundaryAuditError(
                    f"boundary control and report disagree for asset: {asset_id}"
                )
        return control_by_id

    @staticmethod
    def _verify_report_summary(report: BoundaryReport) -> None:
        expected = summarize_boundary_classifications(entry.status for entry in report.entries)
        if report.summary != expected:
            raise BoundaryAuditError("boundary report summary is inconsistent with entries")

    @staticmethod
    def _cross_check_materialization(
        manifest: SnapshotManifest, control_by_id: dict[str, object]
    ) -> dict[str, object]:
        materialized_by_id: dict[str, object] = {}
        paths: set[str] = set()
        for asset in manifest.materialized_assets:
            if asset.asset_id in materialized_by_id:
                raise BoundaryAuditError(f"duplicate materialized asset_id: {asset.asset_id}")
            control = control_by_id.get(asset.asset_id)
            if control is None:
                raise BoundaryAuditError(f"materialized asset missing from boundary control: {asset.asset_id}")
            if control.classification is not BoundaryClassification.MATERIALIZED:
                raise BoundaryAuditError(
                    f"non-materialized control asset appears in manifest: {asset.asset_id}"
                )
            if (
                control.availability_status is not AvailabilityStatus.AVAILABLE
                or control.visibility_scope is not VisibilityScope.PAST_CODEX
            ):
                raise BoundaryAuditError(f"invalid materialized control classification: {asset.asset_id}")
            if (
                asset.logical_path != control.logical_path
                or asset.asset_kind is not control.asset_kind
                or asset.availability_basis != control.availability_basis
                or asset.availability_status is not control.availability_status
                or asset.visibility_scope is not control.visibility_scope
                or asset.availability_reason != control.availability_reason
            ):
                raise BoundaryAuditError(
                    f"materialized asset provenance conflicts with boundary control: {asset.asset_id}"
                )
            try:
                relative = validate_logical_path(asset.logical_path)
            except IntegrityError as exc:
                raise BoundaryAuditError(str(exc)) from exc
            path_key = relative.as_posix().casefold()
            if path_key in paths:
                raise BoundaryAuditError(f"duplicate logical path in manifest: {asset.logical_path}")
            paths.add(path_key)
            materialized_by_id[asset.asset_id] = asset

        for asset_id, control in control_by_id.items():
            if control.classification is BoundaryClassification.MATERIALIZED:
                if asset_id not in materialized_by_id:
                    raise BoundaryAuditError(
                        f"materialized control asset missing from manifest: {asset_id}"
                    )
            elif asset_id in materialized_by_id:
                raise BoundaryAuditError(
                    f"non-materialized control asset appears in manifest: {asset_id}"
                )
        return materialized_by_id

    def _verify_tree(self, root: Path, repo: Path, materialized_by_id: dict[str, object]) -> None:
        for path in root.rglob("*"):
            if path.name.casefold() == ".git":
                raise BoundaryAuditError(f".git content found: {path}")
            if path.is_symlink():
                raise BoundaryAuditError(f"symbolic links are not allowed: {path}")

        expected: dict[str, Path] = {}
        for asset in materialized_by_id.values():
            relative = validate_logical_path(asset.logical_path)
            file_path = repo / relative
            if not file_path.is_file():
                raise BoundaryAuditError(f"materialized file is missing: {asset.logical_path}")
            if sha256_file(file_path) != asset.content_sha256:
                raise BoundaryAuditError(f"materialized file hash mismatch: {asset.logical_path}")
            expected[relative.as_posix().casefold()] = file_path

        actual: dict[str, Path] = {
            path.relative_to(repo).as_posix().casefold(): path
            for path in repo.rglob("*")
            if path.is_file()
        }
        extras = sorted(set(actual) - set(expected))
        if extras:
            raise BoundaryAuditError(f"unmanifested files found: {', '.join(extras)}")
        if self.future_canary_token is not None:
            token = self.future_canary_token.encode("utf-8")
            for path in actual.values():
                if token in path.read_bytes():
                    raise BoundaryAuditError(f"future canary found in Past-visible file: {path}")
