"""Availability classification for the Past Codex information boundary."""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from .models import (
    AvailabilityStatus,
    BoundaryClassification,
    BoundaryEntry,
    BoundarySummary,
    TemporalAsset,
    VisibilityScope,
)


def derive_boundary_classification(
    availability_status: AvailabilityStatus,
    visibility_scope: VisibilityScope,
) -> BoundaryClassification:
    """Derive the only valid boundary classification from raw declaration data."""
    if availability_status is AvailabilityStatus.LOCKED_FUTURE:
        return BoundaryClassification.LOCKED_FUTURE
    if availability_status is AvailabilityStatus.EXCLUDED:
        return BoundaryClassification.EXCLUDED
    if visibility_scope is not VisibilityScope.PAST_CODEX:
        return BoundaryClassification.NOT_VISIBLE_TO_PAST
    return BoundaryClassification.MATERIALIZED


def derive_classification_reason(
    availability_reason: str,
    availability_status: AvailabilityStatus,
    visibility_scope: VisibilityScope,
    classification: BoundaryClassification,
) -> str:
    """Derive the human-readable boundary reason from raw declaration data."""
    expected = derive_boundary_classification(availability_status, visibility_scope)
    if classification is not expected:
        raise ValueError("classification must be derived from availability and visibility")
    if classification is BoundaryClassification.NOT_VISIBLE_TO_PAST:
        return f"{availability_reason} Visibility scope is {visibility_scope}."
    return availability_reason


def summarize_boundary_classifications(
    classifications: Iterable[BoundaryClassification],
) -> BoundarySummary:
    """Create the exact boundary summary for a sequence of derived states."""
    counts = Counter(classifications)
    return BoundarySummary(
        total=sum(counts.values()),
        materialized=counts[BoundaryClassification.MATERIALIZED],
        locked_future=counts[BoundaryClassification.LOCKED_FUTURE],
        excluded=counts[BoundaryClassification.EXCLUDED],
        not_visible_to_past=counts[BoundaryClassification.NOT_VISIBLE_TO_PAST],
    )


def classify_asset(asset: TemporalAsset) -> BoundaryEntry:
    """Classify one declared asset, failing closed for non-Past visibility."""
    status = derive_boundary_classification(asset.availability.status, asset.visibility_scope)
    reason = derive_classification_reason(
        asset.availability.reason,
        asset.availability.status,
        asset.visibility_scope,
        status,
    )
    return BoundaryEntry(
        asset_id=asset.asset_id,
        logical_path=asset.logical_path,
        status=status,
        reason=reason,
    )
