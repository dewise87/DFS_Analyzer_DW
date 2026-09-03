"""Versioned deterministic rule configuration for claim grading."""

from __future__ import annotations

import hashlib
import json
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from narrative_alpha.store.models import ClaimDimensionValue, ClaimTypeValue

DEFAULT_GRADING_CONFIG_PATH = Path("config/claim_grading.toml")


class GradingConfigError(ValueError):
    """The grading rules are absent, malformed, or internally inconsistent."""


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AvailabilityRule(_ConfigModel):
    rule_id: str
    claim_type: Literal["availability"]
    dimensions: tuple[Literal["active_status"], ...]

    @field_validator("rule_id")
    @classmethod
    def nonempty_rule_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("rule_id must not be empty")
        return normalized

    @field_validator("dimensions")
    @classmethod
    def unique_dimensions(
        cls, value: tuple[Literal["active_status"], ...]
    ) -> tuple[Literal["active_status"], ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("availability dimensions must be nonempty and unique")
        return value


class UsageDimensionRule(_ConfigModel):
    stat_key: str
    reference_key: str
    direction_threshold: float = Field(gt=0, lt=1, allow_inf_nan=False)

    @field_validator("stat_key", "reference_key")
    @classmethod
    def nonempty_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("stat-line keys must not be empty")
        return normalized



class UsageRule(_ConfigModel):
    rule_id: str
    claim_type: Literal["usage"]
    dimensions: dict[str, UsageDimensionRule]

    @field_validator("rule_id")
    @classmethod
    def nonempty_rule_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("rule_id must not be empty")
        return normalized

    @field_validator("dimensions")
    @classmethod
    def supported_dimensions(
        cls, value: dict[str, UsageDimensionRule]
    ) -> dict[str, UsageDimensionRule]:
        allowed = {"snap_share", "route_share", "touch_share", "target_share", "role"}
        if not value:
            raise ValueError("usage dimensions must not be empty")
        unexpected = sorted(set(value) - allowed)
        missing = sorted(allowed - set(value))
        if unexpected:
            raise ValueError(f"unsupported usage dimensions: {', '.join(unexpected)}")
        if missing:
            raise ValueError(f"missing usage dimensions: {', '.join(missing)}")
        return value


class FieldPropagationRule(_ConfigModel):
    rule_id: str
    claim_type: Literal["field_propagation"]
    dimensions: tuple[Literal["ownership"], ...]
    direction_field: Literal["roster_behavior_direction"]
    classic_neutral_threshold: float = Field(gt=0, lt=1, allow_inf_nan=False)
    showdown_neutral_threshold: float = Field(gt=0, lt=1, allow_inf_nan=False)

    @field_validator("rule_id")
    @classmethod
    def nonempty_rule_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("rule_id must not be empty")
        return normalized

    @field_validator("dimensions")
    @classmethod
    def unique_dimensions(
        cls, value: tuple[Literal["ownership"], ...]
    ) -> tuple[Literal["ownership"], ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("field-propagation dimensions must be nonempty and unique")
        return value


class RuleSet(_ConfigModel):
    availability: AvailabilityRule
    usage: UsageRule
    field_propagation: FieldPropagationRule


class ClaimGradingConfig(_ConfigModel):
    config_version: Literal["claim-grading-v1"]
    claim_lookback_days: int = Field(ge=1, le=31)
    decay_half_life_days: float = Field(gt=0, allow_inf_nan=False)
    beta_prior_alpha: float = Field(gt=0, allow_inf_nan=False)
    beta_prior_beta: float = Field(gt=0, allow_inf_nan=False)
    posterior_interval_mass: float = Field(gt=0, lt=1, allow_inf_nan=False)
    rules: RuleSet

    @model_validator(mode="after")
    def distinct_rule_ids(self) -> Self:
        if self.beta_prior_alpha != 1.0 or self.beta_prior_beta != 1.0:
            raise ValueError("claim-grading-v1 requires a Beta(1,1) accuracy prior")
        if self.posterior_interval_mass != 0.90:
            raise ValueError("claim-grading-v1 requires a 90% posterior interval")
        ids = (
            self.rules.availability.rule_id,
            self.rules.usage.rule_id,
            self.rules.field_propagation.rule_id,
        )
        if len(ids) != len(set(ids)):
            raise ValueError("grading rule ids must be unique")
        return self


@dataclass(frozen=True)
class LoadedGradingConfig:
    path: Path
    sha256: str
    config: ClaimGradingConfig

    def rule_for(
        self, claim_type: ClaimTypeValue, claim_dimension: ClaimDimensionValue
    ) -> AvailabilityRule | UsageRule | FieldPropagationRule | None:
        if (
            claim_type == "availability"
            and claim_dimension in self.config.rules.availability.dimensions
        ):
            return self.config.rules.availability
        if claim_type == "usage" and claim_dimension in self.config.rules.usage.dimensions:
            return self.config.rules.usage
        if (
            claim_type == "field_propagation"
            and claim_dimension in self.config.rules.field_propagation.dimensions
        ):
            return self.config.rules.field_propagation
        return None

    def rule_sha256(self, rule: AvailabilityRule | UsageRule | FieldPropagationRule) -> str:
        payload = json.dumps(
            rule.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def load_grading_config(path: Path = DEFAULT_GRADING_CONFIG_PATH) -> LoadedGradingConfig:
    """Load strict TOML and hash the exact reviewed bytes used by a grading run."""

    try:
        raw_bytes = path.read_bytes()
    except OSError as error:
        raise GradingConfigError(f"cannot read grading config {path}: {error}") from error
    try:
        raw = tomllib.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise GradingConfigError(f"invalid grading config {path}: {error}") from error
    try:
        config = ClaimGradingConfig.model_validate(raw)
    except ValueError as error:
        raise GradingConfigError(f"invalid grading config {path}: {error}") from error
    numeric_values = (
        config.decay_half_life_days,
        config.beta_prior_alpha,
        config.beta_prior_beta,
        config.posterior_interval_mass,
    )
    if not all(math.isfinite(value) for value in numeric_values):  # defensive around TOML
        raise GradingConfigError("grading config numeric values must be finite")
    return LoadedGradingConfig(
        path=path,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        config=config,
    )
