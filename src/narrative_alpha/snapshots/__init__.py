"""Phase -1: perishable-data capture.

Capture and freeze pre-lock snapshots of purchased projections, ownership,
salaries, odds, and weather at fixed times; hash and timestamp every file.
A folder of hashed, timestamped files is sufficient; the database ingests
them retroactively. See design doc section 9.0.
"""

from narrative_alpha.snapshots.core import (
    MANIFEST_FILENAME,
    CapturePayload,
    StatusReport,
    VerificationReport,
    WeekStatus,
    capture_files,
    capture_payloads,
    collect_status,
    initialize_week,
    load_manifest,
    sha256_file,
    verify_week,
)
from narrative_alpha.snapshots.models import (
    MANIFEST_SCHEMA_VERSION,
    CaptureKind,
    SnapshotError,
    SnapshotFile,
    SnapshotManifest,
    SnapshotRequest,
)

__all__ = [
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "CaptureKind",
    "CapturePayload",
    "SnapshotError",
    "SnapshotFile",
    "SnapshotManifest",
    "SnapshotRequest",
    "StatusReport",
    "VerificationReport",
    "WeekStatus",
    "capture_files",
    "capture_payloads",
    "collect_status",
    "initialize_week",
    "load_manifest",
    "sha256_file",
    "verify_week",
]
