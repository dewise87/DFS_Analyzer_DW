"""Typed row contracts for the Phase 0/1 SQLite schema."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
DatabaseValue = None | int | float | str | bytes


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def _decode_json(value: object) -> object:
    if isinstance(value, str):
        return json.loads(value)
    return value


class StoreRow(BaseModel):
    """Base for strict, immutable rows that can cross the SQLite boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @classmethod
    def from_db(cls, row: sqlite3.Row | Mapping[str, object]) -> Self:
        """Validate a ``sqlite3.Row`` or mapping-like row returned by SQLite."""

        column_names = row.keys()
        values = {key: row[key] for key in column_names}
        return cls.model_validate(values)

    def db_values(self) -> dict[str, DatabaseValue]:
        """Return SQLite-bindable values without hiding any SQL operation."""

        values: dict[str, DatabaseValue] = {}
        for key, value in self.model_dump(mode="json").items():
            if isinstance(value, (dict, list)):
                values[key] = json.dumps(value, sort_keys=True, separators=(",", ":"))
            else:
                values[key] = value
        return values


class PointInTimeRow(StoreRow):
    """The common section 3.2 provenance columns for external records."""

    source: str
    published_at: datetime | None
    observed_at: datetime
    ingested_at: datetime
    effective_at: datetime | None
    valid_from: datetime
    valid_to: datetime | None
    source_version: str | None
    run_id: str | None

    @field_validator(
        "published_at",
        "observed_at",
        "ingested_at",
        "effective_at",
        "valid_from",
        "valid_to",
    )
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str) -> str:
        source = value.strip()
        if not source:
            raise ValueError("source must not be empty")
        return source

    @model_validator(mode="after")
    def validate_version_interval(self) -> Self:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        return self


class AppliedMigrationRow(StoreRow):
    version: int = Field(ge=0, le=9999)
    name: str
    sha256: Sha256
    applied_at: datetime

    @field_validator("applied_at")
    @classmethod
    def normalize_applied_at(cls, value: datetime) -> datetime:
        return _utc(value)


class ModelRunRow(StoreRow):
    run_id: str
    run_type: str
    started_at: datetime
    completed_at: datetime | None
    status: Literal["running", "succeeded", "failed", "degraded"]
    code_version: str
    config_sha256: Sha256 | None
    parent_run_id: str | None
    error_message: str | None
    created_at: datetime

    @field_validator("started_at", "completed_at", "created_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def validate_completed_at(self) -> Self:
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        return self


class TeamRow(PointInTimeRow):
    team_id: int
    team_key: str
    abbreviation: str
    canonical_name: str
    league: str = "NFL"


class PlayerRow(PointInTimeRow):
    player_id: int
    player_key: str
    canonical_name: str
    position: str | None
    birth_date: date | None


class PlayerAliasRow(PointInTimeRow):
    alias_id: int
    player_id: int
    team_id: int | None
    alias: str
    normalized_alias: str
    match_method: str
    match_confidence: float = Field(ge=0, le=1)
    manual_override: bool = False


class ExternalPlayerIdRow(PointInTimeRow):
    external_player_id_record_id: int
    player_id: int
    site: str | None
    external_player_id: str
    match_method: str = "seed"
    match_confidence: float = Field(default=1.0, ge=0, le=1)
    manual_override: bool = False


class PlayerTeamHistoryRow(PointInTimeRow):
    player_team_history_id: int
    player_id: int
    team: str
    position: str | None
    roster_status: str | None
    season: int | None = Field(default=None, ge=1)
    week: int | None = Field(default=None, ge=1, le=99)


class UnresolvedPlayerMatchRow(StoreRow):
    unresolved_id: int
    identity_key: Sha256
    source: str
    site: str | None
    external_player_id: str | None
    name_raw: str
    normalized_name: str
    team: str
    opponent: str | None
    position: str | None
    roster_status: str | None
    birth_date: date | None
    eligible_positions_json: tuple[str, ...]
    candidates_json: tuple[dict[str, Any], ...]
    source_file_sha256: Sha256 | None
    first_observed_at: datetime
    last_observed_at: datetime
    occurrences: int = Field(ge=1)
    status: Literal["pending", "resolved", "ignored"]
    resolved_player_id: int | None
    resolved_at: datetime | None
    resolution_note: str | None
    match_method: str | None
    match_confidence: float | None = Field(default=None, ge=0, le=1)
    manual_override: bool
    run_id: str | None

    @field_validator("eligible_positions_json", "candidates_json", mode="before")
    @classmethod
    def decode_json_fields(cls, value: object) -> object:
        return _decode_json(value)

    @field_validator("first_observed_at", "last_observed_at", "resolved_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def validate_resolution(self) -> Self:
        if self.last_observed_at < self.first_observed_at:
            raise ValueError("last_observed_at must not precede first_observed_at")
        resolved = self.status == "resolved"
        ignored = self.status == "ignored"
        if resolved != (self.resolved_player_id is not None):
            raise ValueError("resolved status and resolved_player_id must agree")
        if (resolved or ignored) != (self.resolved_at is not None):
            raise ValueError("resolved_at must be set exactly when review is complete")
        return self


class GameRow(PointInTimeRow):
    game_id: int
    external_game_id: str
    season: int = Field(ge=1)
    week: int = Field(ge=1, le=99)
    kickoff_at: datetime
    home_team_id: int
    away_team_id: int
    stadium_name: str | None
    game_status: str | None

    @field_validator("kickoff_at")
    @classmethod
    def normalize_kickoff_at(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def reject_same_team(self) -> Self:
        if self.home_team_id == self.away_team_id:
            raise ValueError("home and away teams must differ")
        return self


class SlateRow(PointInTimeRow):
    slate_id: int
    external_slate_id: str
    site: str
    slate_type: Literal["classic", "showdown"]
    season: int = Field(ge=1)
    week: int = Field(ge=1, le=99)
    name: str
    starts_at: datetime
    locks_at: datetime

    @field_validator("starts_at", "locks_at")
    @classmethod
    def normalize_slate_times(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_lock_time(self) -> Self:
        if self.locks_at < self.starts_at:
            raise ValueError("locks_at must not precede starts_at")
        return self


class SalaryRow(PointInTimeRow):
    salary_id: int
    slate_id: int
    player_id: int
    game_id: int | None
    team_id: int
    opponent_team_id: int | None
    site_player_id: str
    roster_positions_json: tuple[str, ...] = Field(min_length=1)
    salary: int = Field(ge=0)
    player_status: str | None
    source_file_sha256: Sha256

    @field_validator("roster_positions_json", mode="before")
    @classmethod
    def decode_roster_positions(cls, value: object) -> object:
        return _decode_json(value)


class ProjectionSnapshotRow(PointInTimeRow):
    projection_snapshot_id: int
    slate_id: int
    player_id: int
    site: str
    projection_mean: float
    projection_floor: float | None
    projection_ceiling: float | None
    ownership_projection: float | None = Field(default=None, ge=0, le=1)
    source_file_sha256: Sha256

    @model_validator(mode="after")
    def validate_projection_range(self) -> Self:
        if self.projection_floor is not None and self.projection_floor > self.projection_mean:
            raise ValueError("projection_floor must not exceed projection_mean")
        if self.projection_ceiling is not None and self.projection_ceiling < self.projection_mean:
            raise ValueError("projection_ceiling must not be below projection_mean")
        return self


class OwnershipBaselineRow(PointInTimeRow):
    ownership_baseline_id: int
    slate_id: int
    player_id: int
    site: str
    role: Literal["classic", "flex", "captain"]
    ownership: float = Field(ge=0, le=1)
    source_file_sha256: Sha256


class ActualOwnershipRow(PointInTimeRow):
    actual_ownership_id: int
    external_contest_id: str
    site: str
    slate_id: int
    contest_archetype: Literal[
        "cash", "single_entry", "3max", "20max", "mass_multi_entry", "showdown"
    ]
    field_size: int = Field(gt=0)
    entry_limit: int = Field(gt=0)
    entry_fee_cents: int = Field(ge=0)
    payout_curve_id: str | None
    player_id: int
    role: Literal["classic", "flex", "captain"]
    lineup_count: int = Field(gt=0)
    roster_count: int = Field(ge=0)
    actual_ownership: float = Field(ge=0, le=1)
    source_file_sha256: Sha256

    @model_validator(mode="after")
    def validate_roster_count(self) -> Self:
        if self.roster_count > self.lineup_count:
            raise ValueError("roster_count must not exceed lineup_count")
        return self


ContestArchetypeValue = Literal[
    "cash", "single_entry", "3max", "20max", "mass_multi_entry", "showdown"
]


class ContestRow(PointInTimeRow):
    contest_id: int
    external_contest_id: str
    site: str
    slate_id: int = Field(gt=0)
    archetype: ContestArchetypeValue
    field_size: int = Field(gt=0)
    entry_limit: int = Field(gt=0)
    entry_fee_cents: int = Field(ge=0)
    total_prizes_cents: int | None = Field(default=None, ge=0)
    payout_curve_id: str | None


class ContestPayoutRow(PointInTimeRow):
    contest_payout_id: int
    payout_curve_id: str
    rank_from: int = Field(ge=1)
    rank_to: int = Field(ge=1)
    prize_cents: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_rank_band(self) -> Self:
        if self.rank_from > self.rank_to:
            raise ValueError("rank_from must not exceed rank_to")
        return self


class OddsSnapshotRow(PointInTimeRow):
    odds_snapshot_id: int
    game_id: int
    sportsbook: str | None
    home_spread: float | None
    away_spread: float | None
    total: float | None
    home_spread_price: int | None
    away_spread_price: int | None
    over_price: int | None
    under_price: int | None
    response_file_sha256: Sha256

    @model_validator(mode="after")
    def validate_spreads(self) -> Self:
        if (
            self.home_spread is not None
            and self.away_spread is not None
            and self.home_spread != -self.away_spread
        ):
            raise ValueError("home and away spreads must be opposites")
        return self


class WeatherSnapshotRow(PointInTimeRow):
    weather_snapshot_id: int
    game_id: int
    stadium_name: str
    forecast_model: str
    forecast_run_at: datetime
    forecast_for_at: datetime
    lead_time_seconds: int = Field(ge=0)
    temperature_c: float | None
    precipitation_probability: float | None = Field(default=None, ge=0, le=1)
    wind_speed_kph: float | None = Field(default=None, ge=0)
    wind_gust_kph: float | None = Field(default=None, ge=0)
    weather_code: int | None
    response_file_sha256: Sha256

    @field_validator("forecast_run_at", "forecast_for_at")
    @classmethod
    def normalize_forecast_times(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_forecast_lead(self) -> Self:
        calculated = int((self.forecast_for_at - self.forecast_run_at).total_seconds())
        if calculated != self.lead_time_seconds:
            raise ValueError("lead_time_seconds must equal forecast_for_at - forecast_run_at")
        return self


class ResultRow(PointInTimeRow):
    result_id: int
    game_id: int
    player_id: int
    site: str
    fantasy_points: float
    stat_line_json: dict[str, Any] | None
    source_file_sha256: Sha256

    @field_validator("stat_line_json", mode="before")
    @classmethod
    def decode_stat_line(cls, value: object) -> object:
        return _decode_json(value)


ManifestArtifactKind = Literal[
    "salary",
    "projection",
    "ownership",
    "market",
    "weather",
    "signal_features",
    "model_parameters",
    "optimizer_request",
    "generated_lineups",
]


class DecisionManifestHash(StoreRow):
    """One artifact hash included in a decision snapshot's immutable hash-set."""

    artifact_kind: ManifestArtifactKind
    sha256: Sha256
    path: str
    source: str | None = None


def canonical_manifest_hashes(hashes: tuple[DecisionManifestHash, ...]) -> str:
    """Serialize a manifest hash-set deterministically, independent of input order."""

    values = [item.model_dump(mode="json") for item in hashes]
    values.sort(
        key=lambda item: (
            str(item["artifact_kind"]),
            str(item["sha256"]),
            str(item["path"]),
            str(item["source"]),
        )
    )
    return json.dumps(values, sort_keys=True, separators=(",", ":"))


def manifest_hash_set_sha256(hashes: tuple[DecisionManifestHash, ...]) -> str:
    """Hash the canonical JSON representation of a decision manifest hash-set."""

    return hashlib.sha256(canonical_manifest_hashes(hashes).encode("utf-8")).hexdigest()


class DecisionSnapshotRow(StoreRow):
    decision_snapshot_id: str
    slate_id: int
    decision_at: datetime
    created_at: datetime
    manifest_schema_version: str
    manifest_hashes_json: tuple[DecisionManifestHash, ...] = Field(min_length=1)
    manifest_hash_set_sha256: Sha256
    run_id: str | None
    note: str | None

    @field_validator("decision_at", "created_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("manifest_hashes_json", mode="before")
    @classmethod
    def decode_manifest_hashes(cls, value: object) -> object:
        return _decode_json(value)

    @model_validator(mode="after")
    def validate_manifest_hash_set(self) -> Self:
        identities = {
            (item.artifact_kind, item.sha256, item.path, item.source)
            for item in self.manifest_hashes_json
        }
        if len(identities) != len(self.manifest_hashes_json):
            raise ValueError("manifest hash-set contains duplicate entries")
        expected = manifest_hash_set_sha256(self.manifest_hashes_json)
        if self.manifest_hash_set_sha256 != expected:
            raise ValueError("manifest_hash_set_sha256 does not match manifest hashes")
        return self

    def db_values(self) -> dict[str, DatabaseValue]:
        values = super().db_values()
        values["manifest_hashes_json"] = canonical_manifest_hashes(self.manifest_hashes_json)
        return values
