"""Deterministic positive-inclusion temporal snapshot builder and CLI."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml
from pydantic import BaseModel, ValidationError

from .availability import classify_asset, summarize_boundary_classifications
from .integrity import (
    IntegrityError,
    canonical_json_bytes,
    contains_git_part,
    sha256_bytes,
    sha256_file,
    validate_logical_path,
)
from .models import (
    AssetAvailabilityManifest,
    BoundaryClassification,
    BoundaryControl,
    BoundaryControlEntry,
    BoundaryEntry,
    BoundaryReport,
    BoundarySummary,
    MaterializedAsset,
    SnapshotManifest,
    TemporalAsset,
    TemporalScenario,
)


class SnapshotBuildError(RuntimeError):
    """Raised when a snapshot cannot be built without violating its boundary."""


def _load_yaml(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as stream:
            return yaml.safe_load(stream)
    except OSError as exc:
        raise SnapshotBuildError(f"cannot read manifest {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise SnapshotBuildError(f"invalid YAML in {path}: {exc}") from exc


def _write_json(path: Path, model: BaseModel) -> None:
    path.write_bytes(canonical_json_bytes(model.model_dump(mode="json")) + b"\n")


def _stable_root_inputs(
    manifest: SnapshotManifest,
) -> dict[str, Any]:
    """Return the timestamp-free inputs that identify an audited base snapshot."""
    return {
        "schema_version": manifest.schema_version,
        "scenario_id": manifest.scenario_id,
        "scenario_type": manifest.scenario_type,
        "cutoff": manifest.cutoff.model_dump(mode="json"),
        "task": manifest.task,
        "network_policy": manifest.network_policy,
        "asset_manifest_hash": manifest.asset_manifest_hash,
        "boundary_control_hash": manifest.boundary_control_hash,
        "boundary_report_hash": manifest.boundary_report_hash,
    }


class TemporalSnapshotBuilder:
    """Build an audited, immutable Past Codex base snapshot from allowed files."""

    def build(
        self,
        scenario_path: str | Path,
        output_directory: str | Path | None = None,
    ) -> SnapshotManifest:
        scenario_path = Path(scenario_path).resolve()
        try:
            scenario = TemporalScenario.model_validate(_load_yaml(scenario_path))
        except ValidationError as exc:
            raise SnapshotBuildError(f"invalid scenario manifest: {exc}") from exc
        assets_path = (scenario_path.parent / scenario.assets_manifest).resolve()
        try:
            assets_manifest = AssetAvailabilityManifest.model_validate(_load_yaml(assets_path))
        except ValidationError as exc:
            raise SnapshotBuildError(f"invalid asset manifest: {exc}") from exc
        if assets_manifest.scenario_id != scenario.scenario_id:
            raise SnapshotBuildError("scenario_id differs between scenario and asset manifests")

        output = Path(output_directory) if output_directory else scenario_path.parent / scenario.output_directory
        output = output.resolve()
        temp = output.parent / f".{output.name}.building-{uuid.uuid4().hex}"
        backup = output.parent / f".{output.name}.backup-{uuid.uuid4().hex}"
        try:
            manifest = self._build_staging(scenario, assets_manifest, assets_path, temp)
            # The staging tree is the only tree audited before promotion.
            from .audit import BoundaryAuditor

            BoundaryAuditor(scenario.audit.future_canary_token).audit(temp)
            self._replace_safely(temp, output, backup)
            return manifest
        except Exception:
            if temp.exists():
                shutil.rmtree(temp)
            raise

    def _build_staging(
        self,
        scenario: TemporalScenario,
        assets_manifest: AssetAvailabilityManifest,
        assets_path: Path,
        temp: Path,
    ) -> SnapshotManifest:
        temp.mkdir(parents=True, exist_ok=False)
        repo = temp / "repo"
        repo.mkdir()
        controls: list[BoundaryControlEntry] = []
        selected: list[tuple[TemporalAsset, Path, Path]] = []
        destinations: dict[str, str] = {}

        for asset in assets_manifest.assets:
            try:
                logical = validate_logical_path(asset.logical_path)
            except IntegrityError as exc:
                raise SnapshotBuildError(str(exc)) from exc
            classification = classify_asset(asset)
            controls.append(
                BoundaryControlEntry(
                    asset_id=asset.asset_id,
                    logical_path=logical.as_posix(),
                    availability_status=asset.availability.status,
                    visibility_scope=asset.visibility_scope,
                    classification=classification.status,
                    availability_reason=asset.availability.reason,
                    classification_reason=classification.reason,
                    asset_kind=asset.asset_kind,
                    availability_basis=asset.availability_basis,
                )
            )
            source_declared = Path(asset.source_path)
            source = (assets_path.parent / source_declared).resolve()
            if contains_git_part(source_declared) or contains_git_part(source):
                raise SnapshotBuildError(f".git source content is forbidden: {asset.source_path!r}")
            if not source.is_file():
                raise SnapshotBuildError(f"source file does not exist: {source}")
            if classification.status is not BoundaryClassification.MATERIALIZED:
                continue
            collision_key = logical.as_posix().casefold()
            overlapping = next(
                (
                    existing
                    for existing in destinations
                    if collision_key == existing
                    or collision_key.startswith(existing + "/")
                    or existing.startswith(collision_key + "/")
                ),
                None,
            )
            if overlapping is not None:
                raise SnapshotBuildError(
                    f"logical path collision between {destinations[overlapping]!r} "
                    f"and {asset.asset_id!r}: {asset.logical_path!r}"
                )
            destinations[collision_key] = asset.asset_id
            selected.append((asset, source, repo / logical))

        materialized: list[MaterializedAsset] = []
        for asset, source, destination in sorted(selected, key=lambda item: item[0].logical_path):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            content_hash = sha256_file(destination)
            if asset.content_sha256 is not None and asset.content_sha256 != content_hash:
                raise SnapshotBuildError(f"declared content hash mismatch for {asset.asset_id}")
            materialized.append(
                MaterializedAsset(
                    asset_id=asset.asset_id,
                    logical_path=destination.relative_to(repo).as_posix(),
                    asset_kind=asset.asset_kind,
                    source_path=asset.source_path,
                    content_sha256=content_hash,
                    availability_basis=asset.availability_basis,
                    visibility_scope=asset.visibility_scope,
                    availability_status=asset.availability.status,
                    availability_reason=asset.availability.reason,
                    metadata=asset.metadata,
                )
            )

        control = BoundaryControl(
            scenario_id=scenario.scenario_id,
            entries=sorted(controls, key=lambda entry: entry.asset_id),
        )
        report = _boundary_report_from_control(control)
        canonical_assets = [asset.model_dump(mode="json") for asset in materialized]
        asset_manifest_hash = sha256_bytes(canonical_json_bytes(canonical_assets))
        boundary_control_hash = sha256_bytes(canonical_json_bytes(control.model_dump(mode="json")))
        boundary_report_hash = sha256_bytes(canonical_json_bytes(report.model_dump(mode="json")))
        provisional = SnapshotManifest(
            snapshot_id="pending",
            scenario_id=scenario.scenario_id,
            scenario_type=scenario.scenario_type,
            cutoff=scenario.cutoff,
            task=scenario.task,
            network_policy=scenario.network_policy,
            network_policy_enforced=False,
            asset_manifest_hash=asset_manifest_hash,
            boundary_control_hash=boundary_control_hash,
            boundary_report_hash=boundary_report_hash,
            snapshot_root_hash="0" * 64,
            created_at=datetime.now(timezone.utc),
            materialized_assets=materialized,
        )
        root_hash = sha256_bytes(canonical_json_bytes(_stable_root_inputs(provisional)))
        manifest = provisional.model_copy(
            update={
                "snapshot_id": f"{scenario.scenario_id}-{root_hash[:16]}",
                "snapshot_root_hash": root_hash,
            }
        )
        _write_json(temp / "manifest.json", manifest)
        _write_json(temp / "boundary_control.json", control)
        _write_json(temp / "boundary_report.json", report)
        return manifest

    @staticmethod
    def _replace_safely(temp: Path, output: Path, backup: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        had_output = output.exists()
        if had_output:
            os.replace(output, backup)
        try:
            os.replace(temp, output)
        except Exception:
            if had_output and backup.exists():
                os.replace(backup, output)
            raise
        if backup.exists():
            shutil.rmtree(backup)


def _boundary_report_from_control(control: BoundaryControl) -> BoundaryReport:
    entries = [
        BoundaryEntry(
            asset_id=entry.asset_id,
            logical_path=entry.logical_path,
            status=entry.classification,
            reason=entry.classification_reason,
        )
        for entry in control.entries
    ]
    return BoundaryReport(
        scenario_id=control.scenario_id,
        entries=entries,
        summary=summarize_boundary_classifications(entry.status for entry in entries),
    )


def _scenario_for_cli(path: Path) -> TemporalScenario:
    try:
        return TemporalScenario.model_validate(_load_yaml(path.resolve()))
    except ValidationError as exc:
        raise SnapshotBuildError(f"invalid scenario manifest: {exc}") from exc


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and audit a sealed temporal snapshot")
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)
    from .audit import BoundaryAuditError, BoundaryAuditor

    try:
        scenario = _scenario_for_cli(args.scenario)
        output = args.output or args.scenario.resolve().parent / scenario.output_directory
        manifest = TemporalSnapshotBuilder().build(args.scenario, output)
        report = BoundaryReport.model_validate_json((output / "boundary_report.json").read_text("utf-8"))
        # Verify the promoted bytes as well; staging was already audited before replacement.
        BoundaryAuditor(scenario.audit.future_canary_token).audit(output)
        print("TEMPORAL SNAPSHOT CREATED\n")
        print(f"Scenario       {manifest.scenario_id}")
        print(f"Cutoff         {manifest.cutoff.value}")
        print(f"Assets         {report.summary.total}")
        print(f"Materialized   {report.summary.materialized}")
        print(f"Future locked  {report.summary.locked_future}")
        print(f"Network        {manifest.network_policy} (metadata; not enforced here)\n")
        print(f"Snapshot hash {manifest.snapshot_root_hash}\n")
        print("BOUNDARY AUDIT PASS")
        return 0
    except (SnapshotBuildError, BoundaryAuditError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
