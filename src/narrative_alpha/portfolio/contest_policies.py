"""Strict, byte-versioned contest construction policy."""

from __future__ import annotations

import hashlib
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from narrative_alpha.portfolio.models import (
    CandidatePlayerScenario,
    ContestArchetype,
    NumericRange,
    PlayerExposureRange,
)

DEFAULT_CONTEST_POLICIES_PATH = Path("config/contest_policies.toml")
CONTEST_POLICY_ARTIFACT_KIND: Literal["contest_policy"] = "contest_policy"
_SUPPORTED_ARCHETYPES = frozenset(
    {
        ContestArchetype.CASH.value,
        ContestArchetype.SINGLE_ENTRY.value,
        ContestArchetype.THREE_MAX.value,
        ContestArchetype.TWENTY_MAX.value,
        ContestArchetype.MASS_MULTI_ENTRY.value,
        ContestArchetype.SHOWDOWN.value,
    }
)


class ContestPolicyError(ValueError):
    """Raised when a contest policy cannot be trusted or does not cover a request."""


class _StrictPolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OwnershipSumPoints(_StrictPolicyModel):
    min: float = Field(ge=0, allow_inf_nan=False)
    max: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_range(self) -> Self:
        if self.min >= self.max:
            raise ValueError("ownership_sum_points min must be below max")
        return self


class ContestPolicy(_StrictPolicyModel):
    ownership_sum_points: OwnershipSumPoints | None = None
    lineup_uniqueness: int = Field(ge=1, le=9)
    max_player_exposure: float = Field(gt=0, le=1, allow_inf_nan=False)
    objective: Literal["projection"]


class _ContestPolicyFile(_StrictPolicyModel):
    policy_version: str = Field(min_length=1)
    archetypes: dict[str, ContestPolicy]

    @field_validator("policy_version")
    @classmethod
    def normalized_version(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("policy_version must not be blank")
        return normalized

    @model_validator(mode="after")
    def exact_archetypes(self) -> Self:
        present = set(self.archetypes)
        if present != _SUPPORTED_ARCHETYPES:
            unknown = sorted(present - _SUPPORTED_ARCHETYPES)
            missing = sorted(_SUPPORTED_ARCHETYPES - present)
            details = []
            if unknown:
                details.append(f"unknown archetypes: {', '.join(unknown)}")
            if missing:
                details.append(f"missing archetypes: {', '.join(missing)}")
            raise ValueError(
                "contest policy must define exactly the supported archetypes ("
                + "; ".join(details)
                + ")"
            )
        if self.archetypes[ContestArchetype.CASH.value].ownership_sum_points is not None:
            raise ValueError("cash ownership_sum_points must be omitted")
        return self


@dataclass(frozen=True)
class ContestPolicies:
    """Validated policy values plus the identity of their exact source bytes."""

    policy_version: str
    archetypes: dict[str, ContestPolicy]
    sha256: str
    raw_bytes: bytes

    def for_archetype(self, archetype: ContestArchetype | str) -> ContestPolicy:
        value = archetype.value if isinstance(archetype, ContestArchetype) else str(archetype)
        policy = self.archetypes.get(value)
        if policy is None:
            raise ContestPolicyError(
                f"contest policy {self.policy_version!r} does not support archetype {value!r}"
            )
        return policy


@dataclass(frozen=True)
class PolicyRequestFields:
    objective: str
    ownership_sum_range: NumericRange | None
    lineup_uniqueness: int
    player_exposure_ranges: tuple[PlayerExposureRange, ...]

    def minimum_lineups(self) -> int:
        """The smallest portfolio in which the exposure maximum allows any player a slot."""

        maximum = min((r.maximum for r in self.player_exposure_ranges), default=1.0)
        return 1 if maximum >= 1.0 else math.ceil(1.0 / maximum - 1e-9)

    def as_update(self) -> dict[str, object]:
        return {
            "objective": self.objective,
            "ownership_sum_range": self.ownership_sum_range,
            "lineup_uniqueness": self.lineup_uniqueness,
            "player_exposure_ranges": self.player_exposure_ranges,
        }


def load_contest_policies(
    path: Path = DEFAULT_CONTEST_POLICIES_PATH,
) -> ContestPolicies:
    """Load and hash the exact TOML bytes used by a decision."""

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ContestPolicyError(f"cannot read contest policy {path}: {error}") from error
    return load_contest_policies_bytes(raw, source=str(path))


def load_contest_policies_bytes(raw: bytes, *, source: str) -> ContestPolicies:
    """Validate frozen policy bytes read from a decision artifact."""

    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ContestPolicyError(f"contest policy is not valid UTF-8 TOML: {source}") from error
    try:
        policy_file = _ContestPolicyFile.model_validate(parsed)
    except ValidationError as error:
        raise ContestPolicyError(f"invalid contest policy {source}: {error}") from error
    return ContestPolicies(
        policy_version=policy_file.policy_version,
        archetypes=dict(policy_file.archetypes),
        sha256=hashlib.sha256(raw).hexdigest(),
        raw_bytes=raw,
    )


def policy_request_fields(
    policies: ContestPolicies,
    archetype: ContestArchetype,
    scenario: CandidatePlayerScenario,
) -> PolicyRequestFields:
    """Translate point-based policy values into the request's fractional controls."""

    policy = policies.for_archetype(archetype)
    points = policy.ownership_sum_points
    ownership_range = (
        None
        if points is None
        else NumericRange(minimum=points.min / 100.0, maximum=points.max / 100.0)
    )
    # A maximum of 1.0 constrains nothing, so it puts nothing in the request: the cash
    # request stays the bytes it was, and the optimizer is not handed a vacuous bound
    # for every candidate on the slate.
    exposures = (
        ()
        if policy.max_player_exposure >= 1.0
        else tuple(
            PlayerExposureRange(
                player_id=player.player_id,
                minimum=0.0,
                maximum=policy.max_player_exposure,
            )
            for player in scenario.players
        )
    )
    return PolicyRequestFields(
        objective=policy.objective,
        ownership_sum_range=ownership_range,
        lineup_uniqueness=policy.lineup_uniqueness,
        player_exposure_ranges=exposures,
    )
