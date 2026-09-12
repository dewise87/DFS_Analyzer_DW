"""Manifest-driven projection and ownership ingestion with source adapters."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from narrative_alpha.identity import PlayerCrosswalk, PlayerIdentityInput
from narrative_alpha.identity.defense import is_defense_position, resolve_team_defense
from narrative_alpha.ingest.timestamps import ensure_utc, optional_utc_timestamp, utc_timestamp
from narrative_alpha.snapshots import MANIFEST_FILENAME, CaptureKind, load_manifest, sha256_file
from narrative_alpha.snapshots.core import snapshot_week_path
from narrative_alpha.snapshots.models import SnapshotManifest


class ProjectionIngestError(RuntimeError):
    """Raised when capture integrity or loader configuration is invalid."""


class SourceFormatError(ValueError):
    """Structured, source-specific parse failure with no fallback parser."""


class SourcePlayerFields(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name_raw: str
    team: str
    opponent: str | None = None
    position: str | None = None
    roster_status: str | None = None
    external_player_id: str | None = None
    birth_date: date | None = None
    eligible_positions: tuple[str, ...] = ()
    vendor_salary: int | None = Field(default=None, gt=0)
    """The site salary the vendor priced the row against, when the export carries one."""
    published_at: datetime | None = None
    effective_at: datetime | None = None
    source_version: str | None = None

    @field_validator("name_raw", "team")
    @classmethod
    def required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator(
        "opponent", "position", "roster_status", "external_player_id", "source_version"
    )
    @classmethod
    def optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("team", "opponent", "position", "roster_status")
    @classmethod
    def uppercase_codes(cls, value: str | None) -> str | None:
        return None if value is None else value.upper()

    @field_validator("eligible_positions")
    @classmethod
    def normalize_positions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(position.strip().upper() for position in value if position.strip())
        )

    @field_validator("published_at", "effective_at")
    @classmethod
    def utc_timestamps(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("source timestamps must include a timezone")
        return value.astimezone(UTC)


class ParsedProjection(SourcePlayerFields):
    projection_mean: float = Field(allow_inf_nan=False)
    projection_floor: float | None = Field(default=None, allow_inf_nan=False)
    projection_ceiling: float | None = Field(default=None, allow_inf_nan=False)
    ownership_projection: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.projection_floor is not None and self.projection_floor > self.projection_mean:
            raise ValueError("projection floor must not exceed mean")
        if self.projection_ceiling is not None and self.projection_ceiling < self.projection_mean:
            raise ValueError("projection ceiling must not be below mean")
        return self


class ParsedOwnership(SourcePlayerFields):
    role: Literal["classic", "flex", "captain"]
    ownership: float = Field(ge=0, le=1, allow_inf_nan=False)


class RejectedSourceRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row_number: int = Field(ge=2)
    reasons: tuple[str, ...] = Field(min_length=1)


class ProjectionParseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    site: str
    """The site the adapter attributed the file to, derived, never guessed."""
    rows_seen: int = Field(ge=0)
    rows: tuple[ParsedProjection, ...]
    rejected: tuple[RejectedSourceRow, ...] = ()
    zero_projection_rows: int = Field(default=0, ge=0)
    """Vendor means of exactly zero: real values for a deep bench, not errors."""
    range_dropped: tuple[str, ...] = ()
    """Rows whose vendor bounds contradicted the mean; the mean was kept, bounds dropped."""

    @field_validator("site")
    @classmethod
    def required_site(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not normalized:
            raise ValueError("site must not be empty")
        return normalized

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.rows_seen != len(self.rows) + len(self.rejected):
            raise ValueError("rows_seen must equal parsed plus rejected rows")
        return self


class OwnershipParseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    site: str
    """The site the adapter attributed the file to, derived, never guessed."""
    rows_seen: int = Field(ge=0)
    rows: tuple[ParsedOwnership, ...]
    rejected: tuple[RejectedSourceRow, ...] = ()

    @field_validator("site")
    @classmethod
    def required_site(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not normalized:
            raise ValueError("site must not be empty")
        return normalized

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.rows_seen != len(self.rows) + len(self.rejected):
            raise ValueError("rows_seen must equal parsed plus rejected rows")
        return self


class SourceFormat(Protocol):
    """One explicitly registered vendor schema; no format guessing is permitted."""

    name: str

    def parse_projections(self, path: Path) -> ProjectionParseResult: ...

    def parse_ownership(self, path: Path) -> OwnershipParseResult: ...


class SourceFormatRegistry:
    """Explicit registry keyed by the manifest's source label."""

    def __init__(self) -> None:
        self._formats: dict[str, SourceFormat] = {}

    def register(self, source_format: SourceFormat) -> None:
        name = _source_name(source_format.name)
        if name in self._formats:
            raise ProjectionIngestError(f"source format is already registered: {name}")
        self._formats[name] = source_format

    def get(self, source: str) -> SourceFormat:
        name = _source_name(source)
        try:
            return self._formats[name]
        except KeyError as error:
            raise ProjectionIngestError(
                f"no SourceFormat is registered for manifest source {source!r}"
            ) from error

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._formats))


@dataclass(frozen=True)
class _InsertOutcome:
    """Result of one keyed point-in-time insert attempt."""

    inserted: bool = False
    duplicate: bool = False
    error: str | None = None


class SkippedProjectionFile(BaseModel):
    """A manifested file the loader did not read, and the reason it did not."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    reason: str


MAX_LISTED_SALARY_MISMATCHES = 10
"""How many mismatching players a report names before it stops at a count."""


class ProjectionLoadReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    files_seen: int = Field(ge=0)
    rows_seen: int = Field(ge=0)
    projection_rows_inserted: int = Field(ge=0)
    ownership_rows_inserted: int = Field(ge=0)
    duplicate_rows: int = Field(ge=0)
    unresolved_rows: int = Field(ge=0)
    rejected_rows: int = Field(ge=0)
    unresolved_ids: tuple[int, ...] = ()
    ignored_rows: int = Field(default=0, ge=0)
    """Vendor rows whose identity a human already ignored; skipped, never re-queued."""
    skipped_files: tuple[SkippedProjectionFile, ...] = ()
    """Files the capture manifests that belong to another site or refused to parse."""
    zero_projection_rows: int = Field(default=0, ge=0)
    """Vendor means of exactly zero, counted rather than mistaken for missing data."""
    range_dropped: tuple[str, ...] = ()
    """Players whose vendor bounds contradicted the mean; the bounds were dropped."""
    salary_mismatches: int = Field(default=0, ge=0)
    """Resolved rows whose vendor salary differs from the slate's — reported, not refused."""
    salary_mismatch_names: tuple[str, ...] = ()
    """Up to ten of those as ``name: vendor vs slate``; salaries move before lock."""
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors and self.unresolved_rows == 0 and self.rejected_rows == 0


VENDOR_KINDS = frozenset({CaptureKind.PROJECTIONS, CaptureKind.OWNERSHIP})
"""The two capture kinds a vendor projection adapter loads into a slate."""


def vendor_captures(
    snapshot_root: Path,
    season: int,
    week: int,
) -> Iterator[tuple[Path, SnapshotManifest]]:
    """Yield the week's captures that manifest a projections or ownership file, oldest first.

    Oldest first so a Sunday re-download lands after Saturday's, leaving the newest
    observation newest in the store as well.
    """

    week_path = snapshot_week_path(snapshot_root, season, week)
    if not week_path.is_dir():
        return
    for capture_path in sorted(path for path in week_path.iterdir() if path.is_dir()):
        manifest_path = capture_path / MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        manifest = load_manifest(manifest_path)
        if any(record.kind in VENDOR_KINDS for record in manifest.files):
            yield capture_path, manifest


def load_projection_capture(
    connection: sqlite3.Connection,
    capture_path: Path,
    *,
    site: str,
    slate_id: int,
    registry: SourceFormatRegistry,
    crosswalk: PlayerCrosswalk | None = None,
    ingested_at: datetime | None = None,
    run_id: str | None = None,
) -> ProjectionLoadReport:
    """Load manifested projection/ownership files using insert-only PIT writes."""

    manifest = load_manifest(capture_path / MANIFEST_FILENAME)
    site = _source_name(site)
    _validate_slate(connection, slate_id, site)
    identity_crosswalk = crosswalk or PlayerCrosswalk(connection)
    ingestion_time = _utc(ingested_at or datetime.now(UTC))

    files_seen = 0
    rows_seen = 0
    projection_rows_inserted = 0
    ownership_rows_inserted = 0
    duplicate_rows = 0
    unresolved_ids: list[int] = []
    ignored_ids: list[int] = []
    rejected_rows = 0
    skipped_files: list[SkippedProjectionFile] = []
    zero_projection_rows = 0
    range_dropped: list[str] = []
    salary_mismatches: list[str] = []
    errors = [
        f"capture error [{error.error_type}] {error.source}: {error.message}"
        for error in manifest.errors
    ]

    for file_record in manifest.files:
        if file_record.kind not in {CaptureKind.PROJECTIONS, CaptureKind.OWNERSHIP}:
            continue
        files_seen += 1
        source_path = capture_path.joinpath(*PurePosixPath(file_record.path).parts)
        actual_hash = sha256_file(source_path)
        if actual_hash != file_record.sha256:
            raise ProjectionIngestError(
                f"captured file hash mismatch for {source_path}: "
                f"expected {file_record.sha256}, got {actual_hash}"
            )
        source_format = registry.get(file_record.source)
        try:
            if file_record.kind is CaptureKind.PROJECTIONS:
                parsed_projections = source_format.parse_projections(source_path)
                if _source_name(parsed_projections.site) != site:
                    skipped_files.append(
                        SkippedProjectionFile(
                            path=file_record.path,
                            reason=(
                                f"the adapter attributed it to "
                                f"{parsed_projections.site}, not {site}"
                            ),
                        )
                    )
                    continue
                rows_seen += parsed_projections.rows_seen
                rejected_rows += len(parsed_projections.rejected)
                zero_projection_rows += parsed_projections.zero_projection_rows
                range_dropped.extend(parsed_projections.range_dropped)
                for projection in parsed_projections.rows:
                    player_id = _resolve_player(
                        connection,
                        identity_crosswalk,
                        projection,
                        source=file_record.source,
                        site=site,
                        file_sha256=file_record.sha256,
                        observed_at=file_record.observed_at,
                        ingested_at=ingestion_time,
                        run_id=run_id,
                        unresolved_ids=unresolved_ids,
                        ignored_ids=ignored_ids,
                    )
                    if player_id is None:
                        continue
                    outcome = _insert_projection(
                        connection,
                        projection,
                        source=file_record.source,
                        site=site,
                        slate_id=slate_id,
                        player_id=player_id,
                        file_sha256=file_record.sha256,
                        observed_at=file_record.observed_at,
                        ingested_at=ingestion_time,
                        source_format_name=source_format.name,
                        run_id=run_id,
                    )
                    projection_rows_inserted += int(outcome.inserted)
                    duplicate_rows += int(outcome.duplicate)
                    if outcome.error is not None:
                        errors.append(outcome.error)
                    mismatch = _salary_mismatch(connection, projection, slate_id, player_id)
                    if mismatch is not None:
                        salary_mismatches.append(mismatch)
            else:
                parsed_ownership = source_format.parse_ownership(source_path)
                if _source_name(parsed_ownership.site) != site:
                    skipped_files.append(
                        SkippedProjectionFile(
                            path=file_record.path,
                            reason=(
                                f"the adapter attributed it to {parsed_ownership.site}, not {site}"
                            ),
                        )
                    )
                    continue
                rows_seen += parsed_ownership.rows_seen
                rejected_rows += len(parsed_ownership.rejected)
                for ownership in parsed_ownership.rows:
                    player_id = _resolve_player(
                        connection,
                        identity_crosswalk,
                        ownership,
                        source=file_record.source,
                        site=site,
                        file_sha256=file_record.sha256,
                        observed_at=file_record.observed_at,
                        ingested_at=ingestion_time,
                        run_id=run_id,
                        unresolved_ids=unresolved_ids,
                        ignored_ids=ignored_ids,
                    )
                    if player_id is None:
                        continue
                    outcome = _insert_ownership(
                        connection,
                        ownership,
                        source=file_record.source,
                        site=site,
                        slate_id=slate_id,
                        player_id=player_id,
                        file_sha256=file_record.sha256,
                        observed_at=file_record.observed_at,
                        ingested_at=ingestion_time,
                        source_format_name=source_format.name,
                        run_id=run_id,
                    )
                    ownership_rows_inserted += int(outcome.inserted)
                    duplicate_rows += int(outcome.duplicate)
                    if outcome.error is not None:
                        errors.append(outcome.error)
        except SourceFormatError as error:
            errors.append(f"{file_record.source} {file_record.path}: {error}")

    return ProjectionLoadReport(
        files_seen=files_seen,
        rows_seen=rows_seen,
        projection_rows_inserted=projection_rows_inserted,
        ownership_rows_inserted=ownership_rows_inserted,
        duplicate_rows=duplicate_rows,
        unresolved_rows=len(unresolved_ids),
        rejected_rows=rejected_rows,
        unresolved_ids=tuple(unresolved_ids),
        ignored_rows=len(ignored_ids),
        skipped_files=tuple(skipped_files),
        zero_projection_rows=zero_projection_rows,
        range_dropped=tuple(range_dropped),
        salary_mismatches=len(salary_mismatches),
        salary_mismatch_names=tuple(salary_mismatches[:MAX_LISTED_SALARY_MISMATCHES]),
        errors=tuple(errors),
    )


def _salary_mismatch(
    connection: sqlite3.Connection,
    parsed: ParsedProjection,
    slate_id: int,
    player_id: int,
) -> str | None:
    """Compare the vendor's salary with the slate's newest one, as a report line.

    Salaries move right up to lock, so a difference is information for the operator,
    not a reason to refuse the row.
    """

    if parsed.vendor_salary is None:
        return None
    row = connection.execute(
        """
        SELECT salary FROM salaries
        WHERE slate_id = ? AND player_id = ?
        ORDER BY observed_at DESC, salary_id DESC
        LIMIT 1
        """,
        (slate_id, player_id),
    ).fetchone()
    if row is None:
        return None
    slate_salary = int(row["salary"])
    if slate_salary == parsed.vendor_salary:
        return None
    return f"{parsed.name_raw}: {parsed.vendor_salary} vs {slate_salary}"


def render_projection_load(report: ProjectionLoadReport) -> str:
    """Render the load as fixed lines; nothing skipped, dropped, or queued is summarized away."""

    lines = [
        "PROJECTION LOAD",
        f"  files       {report.files_seen} manifested, {len(report.skipped_files)} skipped, "
        f"{report.rows_seen} row(s) read, {report.rejected_rows} rejected",
        f"  projections {report.projection_rows_inserted} inserted",
        f"  ownership   {report.ownership_rows_inserted} inserted",
        f"  duplicates  {report.duplicate_rows} already loaded",
        f"  zero means  {report.zero_projection_rows} row(s) the vendor projects at 0.0",
    ]
    for skipped in report.skipped_files:
        lines.append(f"  skipped     {skipped.path} — {skipped.reason}")
    if report.range_dropped:
        lines.append(
            f"  bounds      dropped on {len(report.range_dropped)} row(s) whose vendor "
            f"floor/ceiling contradicted the mean: {', '.join(report.range_dropped)}"
        )
    if report.salary_mismatches:
        lines.append(
            f"  salaries    {report.salary_mismatches} row(s) priced against a different "
            "salary than the slate carries (vendor vs slate):"
        )
        lines.extend(f"    ~ {name}" for name in report.salary_mismatch_names)
        remaining = report.salary_mismatches - len(report.salary_mismatch_names)
        if remaining > 0:
            lines.append(f"    ~ +{remaining} more")
    if report.unresolved_rows:
        lines.append(
            f"  unresolved  {report.unresolved_rows} vendor row(s) queued for "
            "`na-crosswalk resolve`: "
            + ", ".join(str(unresolved_id) for unresolved_id in report.unresolved_ids)
        )
    if report.ignored_rows:
        lines.append(
            f"  ignored     {report.ignored_rows} vendor row(s) skipped: a human already "
            "ignored their identities"
        )
    if report.errors:
        lines.append("")
        lines.append("  ERRORS")
        lines.extend(f"    ! {error}" for error in report.errors)
    lines.append("")
    return "\n".join(lines)


def _resolve_player(
    connection: sqlite3.Connection,
    crosswalk: PlayerCrosswalk,
    parsed: SourcePlayerFields,
    *,
    source: str,
    site: str,
    file_sha256: str,
    observed_at: datetime,
    ingested_at: datetime,
    run_id: str | None,
    unresolved_ids: list[int],
    ignored_ids: list[int],
) -> int | None:
    """A vendor row's canonical player: the franchise defense row for DST, else crosswalk."""

    if is_defense_position(parsed.position):
        return resolve_team_defense(
            connection,
            parsed.team,
            observed_at=observed_at,
            ingested_at=ingested_at,
            run_id=run_id,
        )
    result = crosswalk.match(
        _identity_input(parsed, source, site, file_sha256, observed_at, ingested_at, run_id)
    )
    if result.player_id is None:
        if result.unresolved_id is not None:
            (ignored_ids if result.ignored else unresolved_ids).append(result.unresolved_id)
        return None
    return result.player_id


def _identity_input(
    parsed: SourcePlayerFields,
    source: str,
    site: str,
    file_sha256: str,
    observed_at: datetime,
    ingested_at: datetime,
    run_id: str | None,
) -> PlayerIdentityInput:
    return PlayerIdentityInput(
        source=source,
        site=site,
        external_player_id=parsed.external_player_id,
        name_raw=parsed.name_raw,
        team=parsed.team,
        opponent=parsed.opponent,
        position=parsed.position,
        roster_status=parsed.roster_status,
        birth_date=parsed.birth_date,
        eligible_positions=parsed.eligible_positions,
        observed_at=observed_at,
        ingested_at=ingested_at,
        source_file_sha256=file_sha256,
        run_id=run_id,
    )


def _insert_projection(
    connection: sqlite3.Connection,
    parsed: ParsedProjection,
    *,
    source: str,
    site: str,
    slate_id: int,
    player_id: int,
    file_sha256: str,
    observed_at: datetime,
    ingested_at: datetime,
    source_format_name: str,
    run_id: str | None,
) -> _InsertOutcome:
    observed_text = utc_timestamp(observed_at)
    content = (
        parsed.projection_mean,
        parsed.projection_floor,
        parsed.projection_ceiling,
        parsed.ownership_projection,
        file_sha256,
        optional_utc_timestamp(parsed.published_at),
        optional_utc_timestamp(parsed.effective_at),
        parsed.source_version or source_format_name,
    )
    existing = connection.execute(
        """
        SELECT projection_mean, projection_floor, projection_ceiling,
               ownership_projection, source_file_sha256, published_at,
               effective_at, source_version
        FROM projection_snapshots
        WHERE source = ? AND site = ? AND slate_id = ? AND player_id = ?
          AND observed_at = ?
        """,
        (source, site, slate_id, player_id, observed_text),
    ).fetchone()
    if existing is not None:
        if tuple(existing) == content:
            return _InsertOutcome(duplicate=True)
        return _InsertOutcome(
            error=(
                "projection_snapshots key conflict for "
                f"source={source} site={site} slate_id={slate_id} "
                f"player_id={player_id} observed_at={observed_text}: "
                "an existing row for this key has different content"
            )
        )

    connection.execute(
        """
        INSERT INTO projection_snapshots(
            slate_id, player_id, site, projection_mean, projection_floor,
            projection_ceiling, ownership_projection, source_file_sha256, source,
            published_at, observed_at, ingested_at, effective_at, valid_from,
            valid_to, source_version, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            slate_id,
            player_id,
            site,
            parsed.projection_mean,
            parsed.projection_floor,
            parsed.projection_ceiling,
            parsed.ownership_projection,
            file_sha256,
            source,
            optional_utc_timestamp(parsed.published_at),
            observed_text,
            utc_timestamp(ingested_at),
            optional_utc_timestamp(parsed.effective_at),
            observed_text,
            parsed.source_version or source_format_name,
            run_id,
        ),
    )
    return _InsertOutcome(inserted=True)


def _insert_ownership(
    connection: sqlite3.Connection,
    parsed: ParsedOwnership,
    *,
    source: str,
    site: str,
    slate_id: int,
    player_id: int,
    file_sha256: str,
    observed_at: datetime,
    ingested_at: datetime,
    source_format_name: str,
    run_id: str | None,
) -> _InsertOutcome:
    observed_text = utc_timestamp(observed_at)
    content = (
        parsed.ownership,
        file_sha256,
        optional_utc_timestamp(parsed.published_at),
        optional_utc_timestamp(parsed.effective_at),
        parsed.source_version or source_format_name,
    )
    existing = connection.execute(
        """
        SELECT ownership, source_file_sha256, published_at, effective_at,
               source_version
        FROM ownership_baselines
        WHERE source = ? AND site = ? AND slate_id = ? AND player_id = ?
          AND role = ? AND observed_at = ?
        """,
        (source, site, slate_id, player_id, parsed.role, observed_text),
    ).fetchone()
    if existing is not None:
        if tuple(existing) == content:
            return _InsertOutcome(duplicate=True)
        return _InsertOutcome(
            error=(
                "ownership_baselines key conflict for "
                f"source={source} site={site} slate_id={slate_id} "
                f"player_id={player_id} role={parsed.role} "
                f"observed_at={observed_text}: "
                "an existing row for this key has different content"
            )
        )

    connection.execute(
        """
        INSERT INTO ownership_baselines(
            slate_id, player_id, site, role, ownership, source_file_sha256,
            source, published_at, observed_at, ingested_at, effective_at,
            valid_from, valid_to, source_version, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            slate_id,
            player_id,
            site,
            parsed.role,
            parsed.ownership,
            file_sha256,
            source,
            optional_utc_timestamp(parsed.published_at),
            observed_text,
            utc_timestamp(ingested_at),
            optional_utc_timestamp(parsed.effective_at),
            observed_text,
            parsed.source_version or source_format_name,
            run_id,
        ),
    )
    return _InsertOutcome(inserted=True)


def _validate_slate(connection: sqlite3.Connection, slate_id: int, site: str) -> None:
    row = connection.execute(
        "SELECT site FROM slates WHERE slate_id = ?",
        (slate_id,),
    ).fetchone()
    if row is None:
        raise ProjectionIngestError(f"slate does not exist: {slate_id}")
    if _source_name(str(row["site"])) != site:
        raise ProjectionIngestError(
            f"slate {slate_id} belongs to {row['site']!r}, not requested site {site!r}"
        )


def _source_name(value: str) -> str:
    normalized = value.strip().casefold()
    if not normalized:
        raise ProjectionIngestError("source/site name must not be empty")
    return normalized


def _utc(value: datetime) -> datetime:
    try:
        return ensure_utc(value)
    except ValueError as error:
        raise ProjectionIngestError("ingested_at must include a timezone") from error
