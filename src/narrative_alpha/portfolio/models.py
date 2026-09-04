"""Solver-independent optimizer request, lineup, and validation contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DfsSite(StrEnum):
    DRAFTKINGS = "draftkings"
    FANDUEL = "fanduel"


@dataclass(frozen=True)
class ClassicSiteRules:
    slots: tuple[str, ...]
    default_salary_cap: int
    default_max_players_per_team: int | None
    default_min_teams: int | None
    default_min_games: int | None


@dataclass(frozen=True)
class ShowdownSiteRules:
    slots: tuple[str, ...]
    default_salary_cap: int
    default_max_players_per_team: int
    default_min_teams: int
    default_min_games: int | None
    captain_slot: str
    flex_slot: str
    captain_points_multiplier: float
    captain_salary_multiplier: float


CLASSIC_SITE_RULES = {
    DfsSite.DRAFTKINGS: ClassicSiteRules(
        slots=("QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"),
        default_salary_cap=50_000,
        default_max_players_per_team=None,
        default_min_teams=None,
        default_min_games=2,
    ),
    DfsSite.FANDUEL: ClassicSiteRules(
        slots=("QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DEF"),
        default_salary_cap=60_000,
        default_max_players_per_team=4,
        default_min_teams=3,
        default_min_games=None,
    ),
}


SHOWDOWN_SITE_RULES = {
    DfsSite.DRAFTKINGS: ShowdownSiteRules(
        slots=("CPT", "FLEX", "FLEX", "FLEX", "FLEX", "FLEX"),
        default_salary_cap=50_000,
        default_max_players_per_team=5,
        default_min_teams=2,
        default_min_games=None,
        captain_slot="CPT",
        flex_slot="FLEX",
        captain_points_multiplier=1.5,
        captain_salary_multiplier=1.5,
    ),
    DfsSite.FANDUEL: ShowdownSiteRules(
        slots=("MVP", "FLEX", "FLEX", "FLEX", "FLEX"),
        default_salary_cap=60_000,
        default_max_players_per_team=4,
        default_min_teams=2,
        default_min_games=None,
        captain_slot="MVP",
        flex_slot="FLEX",
        captain_points_multiplier=1.5,
        captain_salary_multiplier=1.0,
    ),
}


class SlateType(StrEnum):
    CLASSIC = "classic"
    SHOWDOWN = "showdown"


class ContestArchetype(StrEnum):
    CASH = "cash"
    SINGLE_ENTRY = "single_entry"
    THREE_MAX = "3max"
    TWENTY_MAX = "20max"
    MASS_MULTI_ENTRY = "mass_multi_entry"
    SHOWDOWN = "showdown"


class CandidatePlayer(BaseModel):
    """One canonical player in an optimizer scenario."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    player_id: int
    site_player_id: str
    name: str
    team: str
    opponent: str
    position: str
    eligible_roster_slots: tuple[str, ...] = Field(min_length=1)
    salary: int = Field(gt=0)
    projection: float = Field(ge=0)
    projected_ownership: float | None = Field(default=None, ge=0, le=1)
    projected_ownership_captain: float | None = Field(
        default=None,
        ge=0,
        le=1,
        exclude_if=lambda value: value is None,
    )
    game_id: str
    game_start: datetime | None = None
    is_injured: bool = False

    @field_validator("site_player_id", "name", "team", "opponent", "position", "game_id")
    @classmethod
    def required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator("team", "opponent", "position")
    @classmethod
    def uppercase_codes(cls, value: str) -> str:
        return value.upper()

    @field_validator("eligible_roster_slots")
    @classmethod
    def normalize_slots(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(slot.strip().upper() for slot in value if slot.strip()))

    @field_validator("game_start")
    @classmethod
    def utc_game_start(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("game_start must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def reject_same_team(self) -> Self:
        if self.team == self.opponent:
            raise ValueError("team and opponent must differ")
        return self


class CandidatePlayerScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str
    players: tuple[CandidatePlayer, ...] = Field(min_length=5)
    projection_source_versions: tuple[str, ...] = Field(min_length=1)

    @field_validator("scenario_id")
    @classmethod
    def required_scenario_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("scenario_id must not be empty")
        return normalized

    @model_validator(mode="after")
    def unique_players(self) -> Self:
        player_ids = [player.player_id for player in self.players]
        site_ids = [player.site_player_id for player in self.players]
        if len(player_ids) != len(set(player_ids)):
            raise ValueError("scenario contains duplicate canonical player IDs")
        if len(site_ids) != len(set(site_ids)):
            raise ValueError("scenario contains duplicate site player IDs")
        return self


class StackRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    positions: tuple[str, ...] = Field(min_length=1)
    count: int = Field(ge=1)
    same_team: bool = True

    @field_validator("positions")
    @classmethod
    def required_positions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(position.strip().upper() for position in value)
        if any(not position for position in normalized):
            raise ValueError("stack positions must not be empty")
        return normalized


class BringBackRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stack_positions: tuple[str, ...] = Field(min_length=1)
    opponent_positions: tuple[str, ...] = Field(min_length=1)
    count: int = Field(ge=1)


class ExposureLimit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    minimum: float = Field(default=0, ge=0, le=1)
    maximum: float = Field(default=1, ge=0, le=1)

    @field_validator("key")
    @classmethod
    def required_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("exposure key must not be empty")
        return normalized

    @model_validator(mode="after")
    def valid_range(self) -> Self:
        if self.minimum > self.maximum:
            raise ValueError("minimum exposure must not exceed maximum")
        return self


class PlayerExposureRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    player_id: int
    minimum: float = Field(default=0, ge=0, le=1)
    maximum: float = Field(default=1, ge=0, le=1)

    @model_validator(mode="after")
    def valid_range(self) -> Self:
        if self.minimum > self.maximum:
            raise ValueError("minimum exposure must not exceed maximum")
        return self


class NumericRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum: float = Field(ge=0)
    maximum: float = Field(ge=0)

    @model_validator(mode="after")
    def valid_range(self) -> Self:
        if self.minimum > self.maximum:
            raise ValueError("minimum must not exceed maximum")
        return self


class UploadEntry(BaseModel):
    """Reserved-entry metadata copied from a site's upload template."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entry_id: str
    contest_id: str
    contest_name: str
    entry_fee: str = ""

    @field_validator("entry_id", "contest_id", "contest_name")
    @classmethod
    def required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("upload entry fields must not be empty")
        return normalized


class OptimizationRequest(BaseModel):
    """The complete section 6.5 request, including unsupported Phase 0 controls."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    site: DfsSite
    slate_id: int
    slate_type: SlateType
    contest_archetype: ContestArchetype
    objective: str = "projection"
    salary_cap: int = Field(gt=0)
    candidate_player_scenario: CandidatePlayerScenario
    stack_rules: tuple[StackRule, ...] = ()
    bring_back_rules: tuple[BringBackRule, ...] = ()
    team_exposure_limits: tuple[ExposureLimit, ...] = ()
    game_exposure_limits: tuple[ExposureLimit, ...] = ()
    player_exposure_ranges: tuple[PlayerExposureRange, ...] = ()
    lineup_uniqueness: int = Field(default=1, ge=1, le=9)
    ownership_sum_range: NumericRange | None = None
    duplication_penalty: float = Field(default=0, ge=0)
    late_game_optionality_value: float = Field(default=0, ge=0)
    portfolio_covariance_penalty: float = Field(default=0, ge=0)
    number_of_lineups: int = Field(default=1, ge=1, le=150)
    excluded_lineup_player_ids: tuple[tuple[int, ...], ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    # Lineups the optimizer must return verbatim, first and in this order, generating
    # only the remainder. A re-freeze that touches some of a portfolio keeps the rest
    # this way, so the new snapshot is the whole decision and replays as one.
    pinned_lineups: tuple[Lineup, ...] = Field(default=(), exclude_if=lambda value: not value)
    time_limit_seconds: float | None = Field(default=None, gt=0)
    max_players_per_team: int | None = Field(default=None, ge=1, le=9)
    min_teams: int | None = Field(default=None, ge=1, le=9)
    min_games: int | None = Field(default=None, ge=1, le=9)
    upload_entries: tuple[UploadEntry, ...] = ()

    @field_validator("objective")
    @classmethod
    def required_objective(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("objective must not be empty")
        return normalized

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        if (self.slate_type is SlateType.SHOWDOWN) != (
            self.contest_archetype is ContestArchetype.SHOWDOWN
        ):
            raise ValueError("showdown slate type and contest archetype must be used together")
        scenario_ids = {player.player_id for player in self.candidate_player_scenario.players}
        if self.slate_type is SlateType.CLASSIC and any(
            player.projected_ownership_captain is not None
            for player in self.candidate_player_scenario.players
        ):
            raise ValueError("projected_ownership_captain must be None on classic slates")
        exposure_ids = [exposure.player_id for exposure in self.player_exposure_ranges]
        if len(exposure_ids) != len(set(exposure_ids)):
            raise ValueError("player exposure ranges contain duplicate player IDs")
        unknown_ids = set(exposure_ids) - scenario_ids
        if unknown_ids:
            raise ValueError(f"player exposure ranges reference unknown IDs: {sorted(unknown_ids)}")
        if self.upload_entries and len(self.upload_entries) != self.number_of_lineups:
            raise ValueError("upload_entries must have exactly one row per requested lineup")
        rules = site_rules(self.site, self.slate_type)
        roster_size = len(rules.slots)
        if len(scenario_ids) < roster_size:
            raise ValueError(
                f"candidate scenario must contain at least {roster_size} players for "
                f"{self.site.value} {self.slate_type.value}"
            )
        excluded = self.excluded_lineup_player_ids
        if len(excluded) != len({tuple(sorted(lineup)) for lineup in excluded}):
            raise ValueError("excluded lineups contain duplicates")
        for lineup in excluded:
            if len(lineup) != roster_size or len(set(lineup)) != roster_size:
                raise ValueError(
                    f"each excluded lineup must contain {roster_size} unique player IDs"
                )
            unknown_lineup_ids = set(lineup) - scenario_ids
            if unknown_lineup_ids:
                raise ValueError(
                    f"excluded lineup references unknown IDs: {sorted(unknown_lineup_ids)}"
                )
        pinned = self.pinned_lineups
        if len(pinned) > self.number_of_lineups:
            raise ValueError("pinned_lineups cannot exceed number_of_lineups")
        pinned_keys = [_lineup_uniqueness_key(lineup, self.slate_type) for lineup in pinned]
        if len(pinned_keys) != len(set(pinned_keys)):
            raise ValueError("pinned lineups contain duplicates")
        if set(pinned_keys) & {tuple(sorted(lineup)) for lineup in excluded}:
            raise ValueError("a lineup cannot be both pinned and excluded")
        for pinned_lineup in pinned:
            if pinned_lineup.site is not self.site or pinned_lineup.slate_id != self.slate_id:
                raise ValueError("each pinned lineup must belong to this request's site and slate")
            unknown_pinned_ids = {
                player.player_id for player in pinned_lineup.players
            } - scenario_ids
            if unknown_pinned_ids:
                raise ValueError(
                    f"pinned lineup references unknown IDs: {sorted(unknown_pinned_ids)}"
                )
        return self


class LineupPlayer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slot: str
    player_id: int
    site_player_id: str
    name: str
    team: str
    opponent: str
    position: str
    salary: int = Field(gt=0)
    projection: float = Field(ge=0)
    projected_ownership: float | None = Field(default=None, ge=0, le=1)
    projected_ownership_captain: float | None = Field(
        default=None,
        ge=0,
        le=1,
        exclude_if=lambda value: value is None,
    )
    game_id: str

    @field_validator("slot", "team", "opponent", "position")
    @classmethod
    def uppercase_codes(cls, value: str) -> str:
        return value.strip().upper()


class Lineup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lineup_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    site: DfsSite
    slate_id: int
    players: tuple[LineupPlayer, ...] = Field(min_length=1)
    total_salary: int = Field(gt=0)
    total_projection: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_totals_and_identity(self) -> Self:
        slate_type = (
            SlateType.SHOWDOWN
            if any(player.slot in {"CPT", "MVP"} for player in self.players)
            else SlateType.CLASSIC
        )
        roster_size = len(site_rules(self.site, slate_type).slots)
        if len(self.players) != roster_size:
            raise ValueError(
                f"lineup must contain exactly {roster_size} players for {self.site.value}"
            )
        if self.total_salary != sum(player.salary for player in self.players):
            raise ValueError("total_salary does not equal player salaries")
        projection = round(sum(player.projection for player in self.players), 6)
        if abs(self.total_projection - projection) > 1e-6:
            raise ValueError("total_projection does not equal player projections")
        expected_id = lineup_sha256(self.site, self.slate_id, self.players)
        if self.lineup_id != expected_id:
            raise ValueError("lineup_id does not match lineup contents")
        return self


def site_rules(site: DfsSite, slate_type: SlateType) -> ClassicSiteRules | ShowdownSiteRules:
    """Return the published roster and salary rules for one site/slate pair."""

    return (
        CLASSIC_SITE_RULES[site] if slate_type is SlateType.CLASSIC else SHOWDOWN_SITE_RULES[site]
    )


def _lineup_uniqueness_key(lineup: Lineup, slate_type: SlateType) -> tuple[object, ...]:
    if slate_type is SlateType.CLASSIC:
        return tuple(sorted(player.player_id for player in lineup.players))
    return tuple(sorted((player.player_id, player.slot) for player in lineup.players))


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    valid: bool
    errors: tuple[ValidationIssue, ...] = ()

    @model_validator(mode="after")
    def consistent_status(self) -> Self:
        if self.valid == bool(self.errors):
            raise ValueError("valid must be true exactly when errors is empty")
        return self


def lineup_sha256(site: DfsSite, slate_id: int, players: tuple[LineupPlayer, ...]) -> str:
    payload = {
        "players": [
            {
                "player_id": player.player_id,
                "site_player_id": player.site_player_id,
                "slot": player.slot,
            }
            for player in players
        ],
        "site": site.value,
        "slate_id": slate_id,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
