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


def _canonical_timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return _canonical_timestamp(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
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
        for key, value in self.model_dump(mode="python").items():
            if isinstance(value, datetime):
                values[key] = _canonical_timestamp(value)
            elif isinstance(value, date):
                values[key] = value.isoformat()
            elif isinstance(value, (dict, list, tuple)):
                values[key] = json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"))
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


class PlayerDistributionSourceRef(StoreRow):
    """One exact projection snapshot contributing to a fitted source-set."""

    projection_snapshot_id: int = Field(gt=0)
    source: str
    source_file_sha256: Sha256

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str) -> str:
        source = value.strip().casefold()
        if not source:
            raise ValueError("source must not be empty")
        return source


def canonical_distribution_source_set(
    source_set: tuple[PlayerDistributionSourceRef, ...],
) -> str:
    """Serialize exact projection inputs deterministically and order-independently."""

    if not source_set:
        raise ValueError("distribution source-set must not be empty")
    snapshot_ids = {item.projection_snapshot_id for item in source_set}
    if len(snapshot_ids) != len(source_set):
        raise ValueError("distribution source-set contains duplicate projection snapshots")
    values = [item.model_dump(mode="json") for item in source_set]
    values.sort(
        key=lambda item: (
            str(item["source"]),
            int(item["projection_snapshot_id"]),
            str(item["source_file_sha256"]),
        )
    )
    return json.dumps(values, sort_keys=True, separators=(",", ":"))


def distribution_source_set_sha256(
    source_set: tuple[PlayerDistributionSourceRef, ...],
) -> str:
    """Hash the canonical projection source-set used by a fitted distribution."""

    canonical = canonical_distribution_source_set(source_set)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sorted_distribution_source_set(
    source_set: tuple[PlayerDistributionSourceRef, ...],
) -> tuple[PlayerDistributionSourceRef, ...]:
    canonical_distribution_source_set(source_set)
    return tuple(
        sorted(
            source_set,
            key=lambda item: (
                item.source,
                item.projection_snapshot_id,
                item.source_file_sha256,
            ),
        )
    )


class PlayerDistributionCreate(PointInTimeRow):
    """Write-side metadata; fitted parameters come from a validated quant fit result."""

    slate_id: int = Field(gt=0)
    player_id: int = Field(gt=0)
    source_set_json: tuple[PlayerDistributionSourceRef, ...] = Field(min_length=1)
    as_of_at: datetime

    @field_validator("source_set_json", mode="before")
    @classmethod
    def decode_source_set(cls, value: object) -> object:
        return _decode_json(value)

    @field_validator("source_set_json")
    @classmethod
    def normalize_source_set(
        cls,
        value: tuple[PlayerDistributionSourceRef, ...],
    ) -> tuple[PlayerDistributionSourceRef, ...]:
        return _sorted_distribution_source_set(value)

    @field_validator("as_of_at")
    @classmethod
    def normalize_as_of_at(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_as_of_at(self) -> Self:
        if self.as_of_at > self.ingested_at:
            raise ValueError("as_of_at must not be later than ingested_at")
        return self

    def db_values(self) -> dict[str, DatabaseValue]:
        values = super().db_values()
        values["source_set_json"] = canonical_distribution_source_set(self.source_set_json)
        return values


class PlayerDistributionRow(PointInTimeRow):
    """Stored parameters and fit provenance for one player marginal as of a cutoff.

    For fitter v1, the inherited ``source`` is the vendor whose mean/floor/ceiling
    triplet was fitted and the source-set contains that one exact projection snapshot.
    """

    player_distribution_id: int = Field(gt=0)
    slate_id: int = Field(gt=0)
    player_id: int = Field(gt=0)
    position: str
    source_set_json: tuple[PlayerDistributionSourceRef, ...] = Field(min_length=1)
    source_set_sha256: Sha256
    as_of_at: datetime
    distribution_family: Literal["lognormal"]
    p_active: float = Field(ge=0, le=1, allow_inf_nan=False)
    p_full_role_given_active: float = Field(ge=0, le=1, allow_inf_nan=False)
    conditional_location: float = Field(ge=0, allow_inf_nan=False)
    conditional_scale: float = Field(gt=0, allow_inf_nan=False)
    conditional_shape: float = Field(gt=0, allow_inf_nan=False)
    input_mean: float = Field(gt=0, allow_inf_nan=False)
    input_floor: float = Field(gt=0, allow_inf_nan=False)
    input_ceiling: float = Field(gt=0, allow_inf_nan=False)
    floor_quantile: float = Field(gt=0, lt=1, allow_inf_nan=False)
    ceiling_quantile: float = Field(gt=0, lt=1, allow_inf_nan=False)
    fit_tolerance: float = Field(gt=0, allow_inf_nan=False)
    fit_max_relative_error: float = Field(ge=0, allow_inf_nan=False)
    fit_config_sha256: Sha256
    fitter_version: str

    @field_validator("source_set_json", mode="before")
    @classmethod
    def decode_source_set(cls, value: object) -> object:
        return _decode_json(value)

    @field_validator("source_set_json")
    @classmethod
    def normalize_source_set(
        cls,
        value: tuple[PlayerDistributionSourceRef, ...],
    ) -> tuple[PlayerDistributionSourceRef, ...]:
        return _sorted_distribution_source_set(value)

    @field_validator("as_of_at")
    @classmethod
    def normalize_as_of_at(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("position")
    @classmethod
    def normalize_position(cls, value: str) -> str:
        position = value.strip().upper()
        if not position:
            raise ValueError("position must not be empty")
        return position

    @field_validator("fitter_version")
    @classmethod
    def normalize_fitter_version(cls, value: str) -> str:
        version = value.strip()
        if not version:
            raise ValueError("fitter_version must not be empty")
        return version

    @model_validator(mode="after")
    def validate_fit_provenance(self) -> Self:
        if self.conditional_location != 0.0:
            raise ValueError("lognormal conditional_location must be zero")
        if not self.input_floor < self.input_mean < self.input_ceiling:
            raise ValueError("inputs must satisfy input_floor < input_mean < input_ceiling")
        if self.floor_quantile >= self.ceiling_quantile:
            raise ValueError("floor_quantile must be below ceiling_quantile")
        if self.fit_max_relative_error > self.fit_tolerance:
            raise ValueError("fit_max_relative_error must not exceed fit_tolerance")
        if self.as_of_at > self.ingested_at:
            raise ValueError("as_of_at must not be later than ingested_at")
        expected_hash = distribution_source_set_sha256(self.source_set_json)
        if self.source_set_sha256 != expected_hash:
            raise ValueError("source_set_sha256 does not match distribution source-set")
        if len(self.source_set_json) != 1:
            raise ValueError(
                "distribution fitter v1 requires exactly one projection snapshot"
            )
        canonical_source = self.source.strip().casefold()
        if self.source != canonical_source:
            raise ValueError("distribution source must be canonical")
        if self.source_set_json[0].source != canonical_source:
            raise ValueError("distribution source does not match its source-set reference")

        # Re-run the quant-layer invariants at the read boundary. This rejects rows whose
        # config hash, version, claimed fit error, inputs, and parameters disagree even if
        # they were written outside the supported store API.
        from narrative_alpha.quant.distributions import DistributionFitResult

        try:
            DistributionFitResult.model_validate(
                {
                    "distribution": {
                        "distribution_family": self.distribution_family,
                        "p_active": self.p_active,
                        "p_full_role_given_active": self.p_full_role_given_active,
                        "conditional_location": self.conditional_location,
                        "conditional_scale": self.conditional_scale,
                        "conditional_shape": self.conditional_shape,
                    },
                    "source": self.source,
                    "position": self.position,
                    "input_mean": self.input_mean,
                    "input_floor": self.input_floor,
                    "input_ceiling": self.input_ceiling,
                    "floor_quantile": self.floor_quantile,
                    "ceiling_quantile": self.ceiling_quantile,
                    "fit_tolerance": self.fit_tolerance,
                    "fit_max_relative_error": self.fit_max_relative_error,
                    "fit_config_sha256": self.fit_config_sha256,
                    "fitter_version": self.fitter_version,
                }
            )
        except ValueError as error:
            raise ValueError(
                "stored distribution fit provenance is internally inconsistent"
            ) from error
        return self

    def db_values(self) -> dict[str, DatabaseValue]:
        values = super().db_values()
        values["source_set_json"] = canonical_distribution_source_set(self.source_set_json)
        return values


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


class SourceRow(PointInTimeRow):
    """One explicitly configured public feed source."""

    source_record_id: int = Field(gt=0)
    source_id: str
    display_name: str
    source_family: str
    collector_kind: Literal["rss_atom", "official_team_feed"]
    feed_url: str
    enabled: bool = True

    @field_validator("source_id", "display_name", "source_family")
    @classmethod
    def nonempty_source_fields(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("source fields must not be empty")
        return normalized

    @field_validator("feed_url")
    @classmethod
    def public_feed_url(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith(("https://", "http://")):
            raise ValueError("feed_url must be an HTTP(S) URL")
        return normalized


class SourcePolicyRow(PointInTimeRow):
    """A reviewed, versioned rights and retention decision for one source."""

    source_policy_id: int
    source_id: str
    permitted_use: str
    raw_retention_days: int = Field(ge=0)
    personal_data_fields_allowed: tuple[str, ...]
    must_honor_deletions: bool
    redistribution_allowed: bool
    third_party_processing_allowed: bool
    commercial_use_status: str
    terms_reviewed_at: datetime

    @field_validator("personal_data_fields_allowed", mode="before")
    @classmethod
    def decode_personal_data_fields(cls, value: object) -> object:
        return _decode_json(value)

    @field_validator("terms_reviewed_at")
    @classmethod
    def normalize_terms_reviewed_at(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("source_id", "permitted_use", "commercial_use_status")
    @classmethod
    def nonempty_policy_fields(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("policy fields must not be empty")
        return normalized

    @model_validator(mode="after")
    def unique_personal_data_fields(self) -> Self:
        if len(set(self.personal_data_fields_allowed)) != len(
            self.personal_data_fields_allowed
        ):
            raise ValueError("personal_data_fields_allowed contains duplicates")
        return self


class SourceItemRow(PointInTimeRow):
    """One inert feed item, deduplicated only inside its originating source."""

    source_item_id: int
    source_id: str
    external_item_id: str | None
    canonical_url: str | None
    title: str | None
    raw_content: bytes | None
    cleaned_text: str | None
    content_sha256: Sha256

    @model_validator(mode="after")
    def validate_retained_content_pair(self) -> Self:
        if (self.raw_content is None) != (self.cleaned_text is None):
            raise ValueError("raw_content and cleaned_text must be retained or purged together")
        return self


ClaimTypeValue = Literal[
    "availability",
    "usage",
    "health",
    "performance_observation",
    "narrative",
    "life_event",
    "environment",
    "team_context",
    "field_propagation",
    "none",
]
ClaimDimensionValue = Literal[
    "active_status",
    "snap_share",
    "route_share",
    "touch_share",
    "target_share",
    "role",
    "health",
    "efficiency",
    "mean",
    "tail",
    "dependence",
    "ownership",
    "none",
]
ClaimDirectionValue = Literal["decrease", "neutral", "increase", "unknown"]
EvidenceClassValue = Literal["A", "B", "C"]
EvidenceBasisValue = Literal[
    "official",
    "direct_quote",
    "beat_report",
    "film_claim",
    "play_by_play",
    "statistics",
    "community_observation",
    "generic_sentiment",
    "joke",
    "unknown",
]
ClaimNoveltyValue = Literal["new", "corroborating", "contradicting", "derivative", "stale"]
ModelConfidenceValue = Literal["low", "medium", "high", "unknown"]
SuggestedChannelValue = Literal["availability", "mean", "shape", "dependence", "ownership"]


def prompt_version_sha256(
    *,
    stage: str,
    schema_version: str,
    system_prompt: str,
    user_prompt_template: str,
    output_schema: Mapping[str, object],
) -> str:
    """Hash the exact prompt and strict output contract as one immutable artifact."""

    payload = {
        "output_schema": output_schema,
        "schema_version": schema_version,
        "stage": stage,
        "system_prompt": system_prompt,
        "user_prompt_template": user_prompt_template,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PromptVersionRow(PointInTimeRow):
    """The exact prompt text and strict schema used for one extraction version."""

    prompt_version_id: str
    stage: Literal["stage_1_extraction"]
    schema_version: str
    system_prompt: str
    user_prompt_template: str
    output_schema_json: dict[str, object]
    prompt_sha256: Sha256
    created_at: datetime

    @field_validator("output_schema_json", mode="before")
    @classmethod
    def decode_output_schema(cls, value: object) -> object:
        return _decode_json(value)

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_prompt_hash(self) -> Self:
        expected = prompt_version_sha256(
            stage=self.stage,
            schema_version=self.schema_version,
            system_prompt=self.system_prompt,
            user_prompt_template=self.user_prompt_template,
            output_schema=self.output_schema_json,
        )
        if self.prompt_sha256 != expected:
            raise ValueError("prompt_sha256 does not match the prompt artifact")
        return self


class SourceItemExtractionRow(PointInTimeRow):
    """One provider attempt, including durable terminal zero-claim outcomes."""

    extraction_id: str
    source_item_id: int = Field(gt=0)
    source_policy_id: int = Field(gt=0)
    source_family: str
    source_content_sha256: Sha256
    prompt_version_id: str
    model_id: str
    max_output_tokens: int = Field(gt=0)
    request_sha256: Sha256
    provider_request_id: str | None
    batch_submission_request_id: str | None
    provider_batch_id: str | None
    provider_custom_id: str | None
    provider_message_id: str | None
    status: Literal[
        "creating", "submitted", "settling", "succeeded", "flagged", "failed"
    ]
    output_json: dict[str, object] | None
    output_sha256: Sha256 | None
    output_redacted_at: datetime | None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_nanos_usd: int | None = Field(default=None, ge=0)
    pricing_version: str
    pricing_effective_at: date
    pricing_source_url: str
    input_nanos_per_token: int = Field(ge=0)
    output_nanos_per_token: int = Field(ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    error_code: str | None
    error_message: str | None

    @field_validator(
        "provider_request_id",
        "batch_submission_request_id",
        "provider_batch_id",
        "provider_custom_id",
        "provider_message_id",
        "error_code",
    )
    @classmethod
    def validate_optional_identifier(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("provider trace identifiers and error codes must not be blank")
        return value

    @field_validator("output_json", mode="before")
    @classmethod
    def decode_output_json(cls, value: object) -> object:
        return _decode_json(value)

    @field_validator("output_redacted_at")
    @classmethod
    def normalize_output_redacted_at(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def validate_attempt_outcome(self) -> Self:
        if self.ingested_at != self.valid_from or self.ingested_at < self.observed_at:
            raise ValueError(
                "extraction ingestion/validity timestamps must align after observation"
            )
        if self.status in {"settling", "succeeded"}:
            complete_batch_trace = all(
                value is not None
                for value in (
                    self.batch_submission_request_id,
                    self.provider_batch_id,
                    self.provider_custom_id,
                )
            )
            if (
                self.output_sha256 is None
                or self.error_code is not None
                or self.provider_message_id is None
                or self.provider_request_id is not None
                or not complete_batch_trace
            ):
                raise ValueError("succeeded extraction requires output hash and no error")
            if self.status == "settling" and (
                self.output_json is None or self.output_redacted_at is not None
            ):
                raise ValueError("settling extraction requires unredacted output JSON")
            if self.status == "succeeded" and (
                (self.output_json is None) == (self.output_redacted_at is None)
            ):
                raise ValueError(
                    "succeeded extraction requires output JSON unless compliance-redacted"
                )
            if self.output_json is not None:
                canonical = json.dumps(
                    self.output_json,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                if hashlib.sha256(canonical).hexdigest() != self.output_sha256:
                    raise ValueError("output_sha256 does not match canonical output_json")
        elif self.output_json is not None or self.output_sha256 is not None:
            raise ValueError("non-succeeded extraction cannot carry model output")
        elif self.output_redacted_at is not None:
            raise ValueError("only succeeded output can be compliance-redacted")
        elif self.status in {"flagged", "failed"} and self.error_code is None:
            raise ValueError("flagged/failed extraction requires an error and no output hash")
        elif self.status == "submitted" and not all(
            value is not None
            for value in (
                self.provider_batch_id,
                self.provider_custom_id,
            )
        ):
            raise ValueError("submitted extraction requires a complete batch trace")
        elif self.status == "creating" and any(
            value is not None
            for value in (
                self.provider_request_id,
                self.batch_submission_request_id,
                self.provider_batch_id,
                self.provider_custom_id,
                self.provider_message_id,
            )
        ):
            raise ValueError("creating extraction cannot claim provider acceptance")
        return self


class SourceItemReviewFlagRow(PointInTimeRow):
    """A durable prompt-injection or prohibited-output review item."""

    source_item_review_flag_id: str
    source_item_id: int = Field(gt=0)
    source_id: str
    source_policy_id: int = Field(gt=0)
    flag_type: Literal[
        "prompt_injection_input",
        "prompt_injection_output",
        "prohibited_output",
        "provider_trace_missing",
        "policy_blocked_output",
    ]
    reason: str
    prompt_version_id: str
    model_id: str
    provider_request_id: str | None
    batch_submission_request_id: str | None
    provider_batch_id: str | None
    provider_custom_id: str | None
    review_status: Literal["pending", "confirmed", "dismissed"]
    reviewed_at: datetime | None

    @field_validator(
        "provider_request_id",
        "batch_submission_request_id",
        "provider_batch_id",
        "provider_custom_id",
    )
    @classmethod
    def validate_optional_identifier(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("provider trace identifiers must not be blank")
        return value

    @field_validator("reviewed_at")
    @classmethod
    def normalize_reviewed_at(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def validate_review_state(self) -> Self:
        if (self.review_status == "pending") != (self.reviewed_at is None):
            raise ValueError("reviewed_at must be absent exactly while a flag is pending")
        if self.reviewed_at is not None and self.reviewed_at < max(
            self.observed_at,
            self.ingested_at,
            self.valid_from,
        ):
            raise ValueError("reviewed_at cannot predate the review flag")
        trace_values = (
            self.provider_request_id,
            self.batch_submission_request_id,
            self.provider_batch_id,
            self.provider_custom_id,
        )
        if self.flag_type == "prompt_injection_input" and any(
            value is not None for value in trace_values
        ):
            raise ValueError("input injection flags cannot carry provider trace")
        if self.flag_type != "prompt_injection_input" and not (
            self.provider_request_id is not None
            or (self.provider_batch_id is not None and self.provider_custom_id is not None)
        ):
            raise ValueError("provider-backed review flags require provider trace")
        return self


class ClaimRow(PointInTimeRow):
    """One Stage 1 claim; qualitative directions are not projection adjustments."""

    claim_id: str
    extraction_id: str
    source_item_id: int = Field(gt=0)
    source_policy_id: int = Field(gt=0)
    prompt_version_id: str
    model_id: str
    provider_request_id: str | None
    batch_submission_request_id: str | None
    provider_batch_id: str | None
    provider_custom_id: str | None
    provider_message_id: str | None
    claim_type: ClaimTypeValue
    claim_dimension: ClaimDimensionValue
    outcome_direction: ClaimDirectionValue
    roster_behavior_direction: ClaimDirectionValue
    evidence_class: EvidenceClassValue
    evidence_basis: EvidenceBasisValue
    falsifiable: bool
    specificity: float = Field(ge=0, le=1)
    actionability: float = Field(ge=0, le=1)
    novelty: ClaimNoveltyValue
    model_confidence: ModelConfidenceValue
    team_refs_json: tuple[str, ...]
    uncertainty_flags_json: tuple[str, ...]
    ambiguity_flags_json: tuple[str, ...]
    suggested_channels_json: tuple[SuggestedChannelValue, ...]
    disconfirming_context: str | None
    disconfirming_context_sha256: Sha256 | None
    context_redacted_at: datetime | None

    @field_validator(
        "provider_request_id",
        "batch_submission_request_id",
        "provider_batch_id",
        "provider_custom_id",
        "provider_message_id",
    )
    @classmethod
    def validate_optional_identifier(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("provider trace identifiers must not be blank")
        return value

    @field_validator("context_redacted_at")
    @classmethod
    def normalize_context_redacted_at(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def validate_provider_trace(self) -> Self:
        batch_trace = all(
            value is not None
            for value in (
                self.batch_submission_request_id,
                self.provider_batch_id,
                self.provider_custom_id,
            )
        )
        if (
            self.provider_message_id is None
            or self.provider_request_id is not None
            or not batch_trace
        ):
            raise ValueError("claim has no complete provider request trace")
        if self.disconfirming_context is not None:
            digest = hashlib.sha256(self.disconfirming_context.encode("utf-8")).hexdigest()
            if self.disconfirming_context_sha256 != digest:
                raise ValueError("disconfirming_context_sha256 does not match context")
            if self.context_redacted_at is not None:
                raise ValueError("retained disconfirming context cannot be marked redacted")
        elif (self.disconfirming_context_sha256 is None) != (
            self.context_redacted_at is None
        ):
            raise ValueError("redacted context must retain its hash and redaction time")
        return self

    @field_validator(
        "uncertainty_flags_json",
        "ambiguity_flags_json",
        "suggested_channels_json",
        "team_refs_json",
        mode="before",
    )
    @classmethod
    def decode_claim_lists(cls, value: object) -> object:
        return _decode_json(value)

    @field_validator(
        "uncertainty_flags_json",
        "ambiguity_flags_json",
        "suggested_channels_json",
        "team_refs_json",
    )
    @classmethod
    def require_unique_claim_lists(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("claim metadata lists must not contain duplicates")
        return value


class ClaimPlayerRefRow(PointInTimeRow):
    """A model-returned name linked only by the deterministic player crosswalk."""

    claim_player_ref_id: int = Field(gt=0)
    claim_id: str
    ordinal: int = Field(ge=0)
    name_raw: str
    player_id: int | None
    unresolved_id: int | None
    resolution_method: str | None
    resolution_confidence: float | None = Field(default=None, ge=0, le=1)
    manual_override: bool

    @model_validator(mode="after")
    def validate_resolution_link(self) -> Self:
        if (self.player_id is None) == (self.unresolved_id is None):
            raise ValueError("exactly one of player_id and unresolved_id must be populated")
        if self.player_id is None and (
            self.resolution_method is not None or self.resolution_confidence is not None
        ):
            raise ValueError("unresolved player references cannot claim a match method")
        if self.player_id is None and self.manual_override:
            raise ValueError("unresolved player references cannot claim a manual override")
        if self.player_id is not None and (
            self.resolution_method is None or self.resolution_confidence is None
        ):
            raise ValueError("resolved player references require match metadata")
        return self


class ClaimEvidenceRefRow(PointInTimeRow):
    """A bounded, verbatim span in the canonical collected item text."""

    claim_evidence_ref_id: int = Field(gt=0)
    claim_id: str
    ordinal: int = Field(ge=0)
    source_item_id: int = Field(gt=0)
    source_text_sha256: Sha256
    extract_start: int = Field(ge=0)
    extract_end: int = Field(gt=0)
    verbatim_extract: str | None = Field(default=None, min_length=1, max_length=512)
    extract_sha256: Sha256
    redacted_at: datetime | None

    @field_validator("redacted_at")
    @classmethod
    def normalize_redacted_at(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def validate_extract_interval(self) -> Self:
        if self.extract_end <= self.extract_start:
            raise ValueError("extract_end must be greater than extract_start")
        if self.verbatim_extract is not None:
            digest = hashlib.sha256(self.verbatim_extract.encode("utf-8")).hexdigest()
            if digest != self.extract_sha256:
                raise ValueError("extract_sha256 does not match verbatim_extract")
            if self.redacted_at is not None:
                raise ValueError("retained evidence cannot be marked redacted")
        elif self.redacted_at is None:
            raise ValueError("redacted evidence requires redacted_at")
        return self


class ContentTombstoneRow(PointInTimeRow):
    """Durable evidence that reconstructive source content was removed."""

    content_tombstone_id: int
    source_item_id: int
    source_id: str
    content_sha256: Sha256
    reason: Literal["retention_expired", "platform_deleted"]
    tombstoned_at: datetime

    @field_validator("tombstoned_at")
    @classmethod
    def normalize_tombstoned_at(cls, value: datetime) -> datetime:
        return _utc(value)


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


class OpsRunRow(StoreRow):
    """One recorded ``na-ops batch`` step attempt; append-only operational history."""

    ops_run_id: int = Field(gt=0)
    batch_run_id: str = Field(min_length=1)
    step: Literal["collect", "purge", "extract", "nflverse_refresh"]
    status: Literal["succeeded", "failed", "skipped"]
    started_at: datetime
    finished_at: datetime
    summary_json: dict[str, object]
    code_version: str = Field(min_length=1)
    error_text: str | None

    @field_validator("started_at", "finished_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("summary_json", mode="before")
    @classmethod
    def decode_summary(cls, value: object) -> object:
        return _decode_json(value)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if self.status == "succeeded":
            if self.error_text is not None:
                raise ValueError("a succeeded ops run carries no error text")
        elif not (self.error_text or "").strip():
            raise ValueError(f"a {self.status} ops run must explain itself in error_text")
        return self
