"""Manifest-driven projection and ownership ingestion with source adapters."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from narrative_alpha.identity import PlayerCrosswalk, PlayerIdentityInput
from narrative_alpha.identity.defense import is_defense_position, resolve_team_defense
from narrative_alpha.ingest.timestamps import ensure_utc, optional_utc_timestamp, utc_timestamp
from narrative_alpha.snapshots import MANIFEST_FILENAME, CaptureKind, load_manifest, sha256_file


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

    rows_seen: int = Field(ge=0)
    rows: tuple[ParsedProjection, ...]
    rejected: tuple[RejectedSourceRow, ...] = ()

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.rows_seen != len(self.rows) + len(self.rejected):
            raise ValueError("rows_seen must equal parsed plus rejected rows")
        return self


class OwnershipParseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rows_seen: int = Field(ge=0)
    rows: tuple[ParsedOwnership, ...]
    rejected: tuple[RejectedSourceRow, ...] = ()

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
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors and self.unresolved_rows == 0 and self.rejected_rows == 0


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
    rejected_rows = 0
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
                rows_seen += parsed_projections.rows_seen
                rejected_rows += len(parsed_projections.rejected)
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
            else:
                parsed_ownership = source_format.parse_ownership(source_path)
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
        errors=tuple(errors),
    )


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
            unresolved_ids.append(result.unresolved_id)
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
