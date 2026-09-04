"""Stable public contracts for simulation inputs and report metrics."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from narrative_alpha.portfolio import CandidatePlayer
from narrative_alpha.quant import PlayerOutcomeDistribution

EXPERIMENTAL_NOTICE = "EXPERIMENTAL — not calibrated against a real contest"


@dataclass(frozen=True)
class PlayerSimulationInput:
    player: CandidatePlayer
    distribution: PlayerOutcomeDistribution
    player_distribution_id: int | None = None
    distribution_source: str | None = None


class OwnershipMarginal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    player_id: int
    name: str
    position: str
    source_target: float = Field(ge=0, le=1, allow_inf_nan=False)
    calibrated_target: float = Field(ge=0, le=1, allow_inf_nan=False)
    achieved: float = Field(ge=0, le=1, allow_inf_nan=False)
    absolute_error: float = Field(ge=0, le=1, allow_inf_nan=False)


class MetricSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_payout_cents: float = Field(ge=0, allow_inf_nan=False)
    expected_roi: float | None = Field(default=None, allow_inf_nan=False)
    cash_probability: float = Field(ge=0, le=1, allow_inf_nan=False)
    top_one_percent_probability: float = Field(ge=0, le=1, allow_inf_nan=False)
    duplication_distribution: tuple[tuple[int, float], ...]
    downside_p5_payout_cents: float = Field(ge=0, allow_inf_nan=False)


class LineupSimulationResult(MetricSummary):
    lineup_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class PortfolioSimulationResult(MetricSummary):
    lineup_count: int = Field(ge=1)
    mean_pairwise_outcome_correlation: float | None = Field(
        default=None, ge=-1, le=1, allow_inf_nan=False
    )
    outcome_correlation_matrix: tuple[tuple[float, ...], ...]


class SimulationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    notice: str | None
    decision_snapshot_id: str
    contest_external_id: str
    contest_id: int = Field(gt=0)
    site: str
    season: int = Field(ge=1)
    week: int = Field(ge=1, le=99)
    draws: int = Field(ge=1)
    seed: int = Field(ge=0)
    independent: bool
    config_version: str
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    game_factor_loading: float = Field(ge=0, lt=1, allow_inf_nan=False)
    team_factor_loading: float = Field(ge=0, lt=1, allow_inf_nan=False)
    within_position_negative_loading: float = Field(ge=0, lt=1, allow_inf_nan=False)
    configured_stack_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    ownership_source: Literal["scenario_model", "vendor_baseline"]
    ownership_scenario_run_id: str | None = None
    field_lineup_count: int = Field(ge=0)
    field_stack_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    ownership_tolerance: float = Field(gt=0, lt=1, allow_inf_nan=False)
    ownership_marginals: tuple[OwnershipMarginal, ...]
    lineup_results: tuple[LineupSimulationResult, ...]
    portfolio_result: PortfolioSimulationResult
    simulated_score_quantiles: tuple[tuple[float, float], ...]
    simulated_field_duplication_distribution: tuple[tuple[int, float], ...]


@dataclass(frozen=True)
class SimulationRunResult:
    report: SimulationReport
    report_path: Path
    report_bytes: bytes
    simulation_run_id: int
