"""Operator console settings: budget, schedule times, and the paths a job needs.

Everything here is configuration an operator may reasonably change between weeks. The
manual capture *times* are not: they are fixed by design-doc §9.0 and live in
:mod:`narrative_alpha.ops.schedule` as constants.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

DEFAULT_OPS_CONFIG_PATH = Path("config/ops.toml")
NANOS_PER_USD = 1_000_000_000
WEEKDAY_NUMBERS = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}


class OpsConfigError(RuntimeError):
    """Raised when the operator console configuration is missing or unusable."""


class _BatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    weekdays: tuple[Annotated[str, Field(min_length=3, max_length=3)], ...] = Field(min_length=1)
    local_time: str
    # Fresh items one scheduled run may submit to Stage 1; the rest wait for the next run.
    # Bounds both the budget-guard estimate and the blast radius of a bad prompt.
    max_items_per_run: int | None = Field(default=None, gt=0)

    @field_validator("weekdays")
    @classmethod
    def validate_weekdays(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = [day for day in value if day.lower() not in WEEKDAY_NUMBERS]
        if unknown:
            raise ValueError(f"unknown weekday name(s): {', '.join(sorted(unknown))}")
        lowered = tuple(day.lower() for day in value)
        if len(set(lowered)) != len(lowered):
            raise ValueError("weekdays contains a duplicate")
        return lowered

    @field_validator("local_time")
    @classmethod
    def validate_local_time(cls, value: str) -> str:
        try:
            time.fromisoformat(value)
        except ValueError as error:
            raise ValueError("local_time must be HH:MM in 24-hour local time") from error
        return value


class _PathsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    database: Path
    snapshot_root: Path
    nflverse_archive: Path
    log_directory: Path


class _OpsConfigFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timezone: str
    season: int = Field(ge=1920, le=2200)
    monthly_llm_budget_usd: Decimal = Field(ge=0)
    keychain_service: str = Field(min_length=1)
    batch: _BatchConfig
    paths: _PathsConfig

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError) as error:
            raise ValueError(f"unknown IANA timezone {value!r}") from error
        return value


@dataclass(frozen=True)
class OpsConfig:
    """Validated operator settings with resolved paths."""

    path: Path
    timezone: ZoneInfo
    season: int
    monthly_llm_budget_usd: Decimal
    keychain_service: str
    batch_weekdays: tuple[str, ...]
    batch_local_time: time
    batch_max_items_per_run: int | None
    database: Path
    snapshot_root: Path
    nflverse_archive: Path
    log_directory: Path

    @property
    def monthly_llm_budget_nanos(self) -> int:
        """The budget as integer USD-nanos, the unit stored on every extraction attempt."""

        return int(self.monthly_llm_budget_usd * NANOS_PER_USD)

    @property
    def batch_weekday_numbers(self) -> tuple[int, ...]:
        """launchd weekday numbers (Sunday is 0) for the configured batch days."""

        return tuple(sorted(WEEKDAY_NUMBERS[day] for day in self.batch_weekdays))


def load_ops_config(path: Path = DEFAULT_OPS_CONFIG_PATH) -> OpsConfig:
    """Load and strictly validate ``config/ops.toml``; no setting is inferred."""

    try:
        raw = tomllib.loads(path.read_bytes().decode("utf-8"))
    except OSError as error:
        raise OpsConfigError(f"cannot read operator config {path}: {error}") from error
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise OpsConfigError(f"invalid operator config {path}: {error}") from error
    try:
        parsed = _OpsConfigFile.model_validate(raw)
    except ValidationError as error:
        raise OpsConfigError(f"invalid operator config {path}: {error}") from error
    return OpsConfig(
        path=path,
        timezone=ZoneInfo(parsed.timezone),
        season=parsed.season,
        monthly_llm_budget_usd=parsed.monthly_llm_budget_usd,
        keychain_service=parsed.keychain_service,
        batch_weekdays=parsed.batch.weekdays,
        batch_local_time=time.fromisoformat(parsed.batch.local_time),
        batch_max_items_per_run=parsed.batch.max_items_per_run,
        database=parsed.paths.database,
        snapshot_root=parsed.paths.snapshot_root,
        nflverse_archive=parsed.paths.nflverse_archive,
        log_directory=parsed.paths.log_directory,
    )
