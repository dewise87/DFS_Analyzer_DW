"""Versioned, hash-pinned assumptions for the shadow contest simulator."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_SIMULATION_CONFIG_PATH = Path("config/simulation.toml")


class SimulationConfigError(ValueError):
    """Raised when simulation assumptions are absent or internally inconsistent."""


class DependenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    game_loading: float = Field(ge=0, lt=1, allow_inf_nan=False)
    team_loading: float = Field(ge=0, lt=1, allow_inf_nan=False)
    within_position_negative_loading: float = Field(ge=0, lt=1, allow_inf_nan=False)
    touch_positions: tuple[str, ...] = Field(min_length=1)

    @field_validator("touch_positions")
    @classmethod
    def normalize_positions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(item.strip().upper() for item in value))
        if any(not item for item in normalized):
            raise ValueError("touch_positions may not contain an empty position")
        return normalized

    @model_validator(mode="after")
    def leave_idiosyncratic_variance(self) -> Self:
        variance = (
            self.game_loading**2 + self.team_loading**2 + self.within_position_negative_loading**2
        )
        if variance >= 1.0:
            raise ValueError("squared dependence loadings must sum to less than one")
        return self


class FieldConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stack_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    stack_weight: float = Field(gt=0, allow_inf_nan=False)
    ownership_tolerance: float = Field(gt=0, lt=1, allow_inf_nan=False)
    calibration_iterations: int = Field(ge=1, le=100)
    lineup_attempts: int = Field(ge=1, le=10000)


class CalibrationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    score_quantiles: tuple[float, ...] = Field(min_length=1)
    score_sample_limit: int = Field(ge=100)

    @field_validator("score_quantiles")
    @classmethod
    def validate_quantiles(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if tuple(sorted(set(value))) != value or any(not 0 < item < 1 for item in value):
            raise ValueError("score_quantiles must be unique, increasing values in (0, 1)")
        return value


class SimulationConfig(BaseModel):
    """Parsed assumptions plus the SHA-256 of the exact TOML bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    config_version: str
    calibrated_against_real_contest: bool
    default_draws: int = Field(ge=1)
    default_seed: int = Field(ge=0, le=2**63 - 1)
    dependence: DependenceConfig
    field: FieldConfig
    calibration: CalibrationConfig
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("config_version")
    @classmethod
    def require_version(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("config_version must not be empty")
        return normalized


def load_simulation_config(
    path: Path = DEFAULT_SIMULATION_CONFIG_PATH,
) -> SimulationConfig:
    """Load and hash one immutable set of simulator assumptions."""

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise SimulationConfigError(f"cannot read simulation config {path}: {error}") from error
    try:
        payload = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SimulationConfigError(f"invalid simulation config {path}: {error}") from error
    try:
        return SimulationConfig.model_validate(
            payload | {"sha256": hashlib.sha256(raw).hexdigest()}
        )
    except ValueError as error:
        raise SimulationConfigError(f"invalid simulation config {path}: {error}") from error
