"""Shared capture integrity, reports and insert-only writes for game inputs."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.snapshots import MANIFEST_FILENAME, CaptureKind, load_manifest, sha256_file
from narrative_alpha.snapshots.core import snapshot_week_path
from narrative_alpha.snapshots.models import SnapshotFile, SnapshotManifest


class GameInputIngestError(ValueError):
    """Capture or source format refused; no guessed input."""


class MissingGameInputCapture(GameInputIngestError):
    """No capture of the requested kind exists for this week."""


class GameInputLoadReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    files_seen: int = Field(ge=0)
    rows_seen: int = Field(ge=0)
    games_matched: int = Field(ge=0)
    rows_inserted: int = Field(ge=0)
    duplicate_rows: int = Field(ge=0)
    rejected_rows: int = Field(ge=0)
    # Events the feed carries for games this store has not ingested (other weeks, other
    # slates). Not a rejection: the feed is league-wide and the store is slate-scoped.
    unmatched_rows: int = Field(default=0, ge=0)
    errors: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors and self.rejected_rows == 0


def render_game_input_load(report: GameInputLoadReport) -> str:
    return "\n".join(
        [
            f"games matched: {report.games_matched}; rows inserted: {report.rows_inserted}; "
            f"duplicates: {report.duplicate_rows}; skipped: {report.rejected_rows}; "
            f"no ingested game: {report.unmatched_rows}",
            *(f"SKIPPED: {error}" for error in report.errors),
            *(f"NOTE: {note}" for note in report.notes),
            "",
        ]
    )


def newest_game_input_capture(root: Path, season: int, week: int, kind: CaptureKind) -> Path:
    week_path = snapshot_week_path(root, season, week)
    candidates: list[tuple[datetime, str, Path]] = []
    if week_path.is_dir():
        for path in week_path.iterdir():
            if (path / MANIFEST_FILENAME).is_file():
                manifest = load_manifest(path / MANIFEST_FILENAME)
                # The capture writer creates the kind directory even for a failed fetch.
                # Never silently select an older success after a newer empty failure.
                if (
                    any(record.kind is kind for record in manifest.files)
                    or (path / kind.value).is_dir()
                ):
                    candidates.append((manifest.captured_at, path.name, path))
    if not candidates:
        raise MissingGameInputCapture(f"no {kind.value} capture under {week_path}")
    return max(candidates)[2]


def verified_capture(
    path: Path,
    season: int,
    week: int,
    kind: CaptureKind,
    source: str,
) -> tuple[SnapshotManifest, tuple[SnapshotFile, ...]]:
    manifest = load_manifest(path / MANIFEST_FILENAME)
    if (manifest.season, manifest.week) != (season, week):
        raise GameInputIngestError("capture season/week does not match requested season/week")
    records = tuple(record for record in manifest.files if record.kind is kind)
    if not records:
        reasons = "; ".join(f"[{error.error_type}] {error.message}" for error in manifest.errors)
        raise GameInputIngestError(
            f"capture contains no {kind.value} files" + (f": {reasons}" if reasons else "")
        )
    # Verify ALL files before any write, including a later file in a multi-response capture.
    for record in records:
        actual = sha256_file(path / record.path)
        if actual != record.sha256:
            raise GameInputIngestError(
                f"captured file hash mismatch for {record.path}: "
                f"expected {record.sha256}, got {actual}"
            )
        if record.source != source:
            raise GameInputIngestError(f"unsupported {kind.value} source {record.source!r}")
    return manifest, records


def insert_observation(
    connection: sqlite3.Connection,
    table: str,
    keys: dict[str, object],
    content: dict[str, object],
    *,
    ingested_at: datetime,
) -> bool:
    """True inserted, False identical duplicate; conflicts refuse, never overwrite.

    Table/column names are internal constants supplied by the two loaders only.
    Ingestion time is not content: a reload retains the original first ingestion.
    """
    fields = {**keys, **content}
    existing = connection.execute(
        f"SELECT {', '.join(fields)} FROM {table} WHERE "
        + " AND ".join(f"{key} = ?" for key in keys),
        tuple(keys.values()),
    ).fetchone()
    if existing is not None:
        if tuple(existing) == tuple(fields.values()):
            return False
        raise GameInputIngestError(f"{table} key conflict for {keys}: different content")
    fields["ingested_at"] = utc_timestamp(ingested_at)
    connection.execute(
        f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
        tuple(fields.values()),
    )
    return True
