"""Typed contracts for canonical player matching and manual review."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MatchMethod(StrEnum):
    EXACT_VENDOR_ID = "exact_vendor_id"
    EXACT_NAME_TEAM = "exact_name_team"
    DETERMINISTIC_ALIAS = "deterministic_alias"
    SUFFIX_TOLERANT_NAME = "suffix_tolerant_name"
    FUZZY = "fuzzy"
    MANUAL = "manual"


class PlayerIdentityInput(BaseModel):
    """A source-native identity presented to the crosswalk."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    site: str | None = None
    external_player_id: str | None = None
    name_raw: str
    team: str
    opponent: str | None = None
    position: str | None = None
    roster_status: str | None = None
    birth_date: date | None = None
    eligible_positions: tuple[str, ...] = ()
    observed_at: datetime
    ingested_at: datetime | None = None
    source_file_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    run_id: str | None = None

    @field_validator("source", "name_raw", "team")
    @classmethod
    def required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator(
        "site", "external_player_id", "opponent", "position", "roster_status", "run_id"
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

    @field_validator("observed_at", "ingested_at")
    @classmethod
    def utc_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("identity timestamps must include a timezone")
        return value.astimezone(UTC)


class MatchCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    player_id: int
    canonical_name: str
    team: str
    position: str | None
    score: float = Field(ge=0, le=1)


class IdentityMatchResult(BaseModel):
    """The explicit outcome of one crosswalk attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    player_id: int | None
    method: MatchMethod | None
    confidence: float | None = Field(default=None, ge=0, le=1)
    manual_override: bool = False
    unresolved_id: int | None = None
    candidates: tuple[MatchCandidate, ...] = ()

    @property
    def matched(self) -> bool:
        return self.player_id is not None
