"""Honest point-in-time evaluation of the purchased projection baseline.

Forecast rows are selected at the decision cutoff. Outcome rows are labels, so they are
selected at a separate, explicit evaluation cutoff that may be after lock. Keeping the two
timestamps distinct prevents both projection look-ahead and mutable "latest result" reads.
Every SQLite read still crosses :class:`~narrative_alpha.replay.PointInTimeSession`; each
forecast-side external table is explicitly bounded by ``:as_of`` in its query.
"""

from __future__ import annotations

import csv
import io
import json
import math
import random
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.quant.distributions import PlayerOutcomeDistribution
from narrative_alpha.quant.scoring import crps, log_score, pit_histogram
from narrative_alpha.replay import PointInTimeSession, ReplayError
from narrative_alpha.store import DecisionSnapshotRow, PlayerDistributionRow

SHAPE_UNAVAILABLE_NOTICE = "shape channel unavailable — no configured source/position quantiles"
SHAPE_AVAILABLE_NOTICE = (
    "shape channel available — scored from stored point-in-time player distributions"
)
SHAPE_STALE_NOTICE = (
    "shape channel unavailable — stored distributions do not match the frozen projection source-set"
)
INACTIVE_ACCOUNTING_NOTICE = (
    "projected-but-inactive classification uses only explicit stored evidence "
    "(result stat_line_json active=false/inactive=true or a point-in-time salary status "
    "of O, OUT, INACTIVE, IR, PUP, or SUSPENDED); a missing or zero result is never "
    "inferred inactive; only explicit inactive players are excluded from error metrics; "
    "zero-point results with unknown activity are scored and counted as an overlapping "
    "diagnostic"
)
PROJECTION_AGGREGATION: Literal[
    "equal_weight_mean_of_latest_manifest_bound_row_per_source_player"
] = "equal_weight_mean_of_latest_manifest_bound_row_per_source_player"
POPULATION_DEFINITION: Literal["exact_manifest_bound_point_in_time_salary_pool"] = (
    "exact_manifest_bound_point_in_time_salary_pool"
)

_INACTIVE_SALARY_STATUSES = frozenset({"O", "OUT", "INACTIVE", "IR", "PUP", "SUSPENDED"})
_INACTIVE_RESULT_STATUSES = frozenset({"OUT", "INACTIVE", "DNP", "DID_NOT_PLAY", "NOT_ACTIVE"})


class BaselineReportError(RuntimeError):
    """Raised when a baseline report cannot be produced without guessing."""


@dataclass(frozen=True)
class BaselineThresholds:
    """Visible sample-size and PIT assumptions used by the report."""

    minimum_sample_size: int = 5
    pit_bins: int = 10
    pit_random_seed: int = 20260901

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_sample_size, bool)
            or not isinstance(self.minimum_sample_size, int)
            or self.minimum_sample_size < 1
        ):
            raise ValueError("minimum_sample_size must be a positive integer")
        if (
            isinstance(self.pit_bins, bool)
            or not isinstance(self.pit_bins, int)
            or self.pit_bins < 2
        ):
            raise ValueError("pit_bins must be an integer of at least 2")
        if isinstance(self.pit_random_seed, bool) or not isinstance(self.pit_random_seed, int):
            raise ValueError("pit_random_seed must be an integer")


MetricStatus = Literal["available", "insufficient_n"]
SpearmanStatus = Literal["available", "insufficient_n", "undefined_constant_rank"]


class BaselineShapeMetrics(BaseModel):
    """Distributional scores for one position/week cell."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    n_scored: int = Field(ge=0)
    n_log_score_finite: int = Field(ge=0)
    n_log_score_off_support: int = Field(ge=0)
    n_negative_outcomes: int = Field(ge=0)
    n_player_results_without_distribution: int = Field(ge=0)
    metric_status: MetricStatus
    log_score_status: MetricStatus
    mean_crps: float | None = Field(default=None, allow_inf_nan=False)
    mean_log_score: float | None = Field(default=None, allow_inf_nan=False)
    pit_bin_counts: tuple[int, ...] = ()
    pit_pearson_chi_square: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    pit_max_abs_frequency_deviation: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_counts(self) -> BaselineShapeMetrics:
        if self.n_log_score_finite + self.n_log_score_off_support != self.n_scored:
            raise ValueError("finite and off-support log-score counts must equal n_scored")
        if self.n_negative_outcomes > self.n_log_score_off_support:
            raise ValueError("negative outcomes must be a subset of off-support outcomes")
        return self


class BaselineEvaluationCell(BaseModel):
    """Complete accounting and metrics for one position in one NFL week."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    season: int = Field(ge=1)
    week: int = Field(ge=1, le=99)
    position: str
    n_scored: int = Field(ge=0)
    n_projected_without_result: int = Field(ge=0)
    n_result_without_projection: int = Field(ge=0)
    n_projected_but_inactive: int = Field(ge=0)
    n_scored_zero_activity_unknown: int = Field(ge=0)
    n_salary_without_projection_or_result: int = Field(ge=0)
    n_projected_partial_source_coverage: int = Field(ge=0)
    metric_status: MetricStatus
    spearman_status: SpearmanStatus
    signed_mean_error_bias: float | None = Field(default=None, allow_inf_nan=False)
    mae: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    rmse: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    spearman_rank_correlation: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)
    shape: BaselineShapeMetrics | None = None

    @field_validator("position")
    @classmethod
    def normalize_position(cls, value: str) -> str:
        position = _position(value)
        if not position:
            raise ValueError("position must not be empty")
        return position

    @model_validator(mode="after")
    def validate_diagnostic_counts(self) -> BaselineEvaluationCell:
        if self.n_scored_zero_activity_unknown > self.n_scored:
            raise ValueError(
                "unknown-activity zero diagnostic must be a subset of n_scored"
            )
        projected_population = (
            self.n_scored
            + self.n_projected_without_result
            + self.n_projected_but_inactive
        )
        if self.n_projected_partial_source_coverage > projected_population:
            raise ValueError(
                "partial-source diagnostic must be a subset of projected players"
            )
        return self


class BaselineEvaluationReport(BaseModel):
    """Structured baseline report with both decision and label cutoffs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime
    evaluation_as_of: datetime
    decision_snapshot_id: str
    run_id: str
    slate_id: int = Field(gt=0)
    external_slate_id: str
    site: str
    season: int = Field(ge=1)
    week: int = Field(ge=1, le=99)
    minimum_sample_size: int = Field(ge=1)
    pit_bins: int = Field(ge=2)
    pit_random_seed: int
    salary_population_n: int = Field(ge=1)
    salary_sources: tuple[str, ...]
    salary_file_hashes: tuple[str, ...]
    salary_run_ids: tuple[str, ...]
    projection_sources: tuple[str, ...]
    projection_file_hashes: tuple[str, ...]
    projection_run_ids: tuple[str, ...]
    result_sources: tuple[str, ...]
    result_file_hashes: tuple[str, ...]
    result_run_ids: tuple[str, ...]
    distribution_source_set_hashes: tuple[str, ...]
    distribution_run_ids: tuple[str, ...]
    distribution_rows_not_in_frozen_source_set: int = Field(ge=0)
    population_definition: Literal["exact_manifest_bound_point_in_time_salary_pool"] = (
        POPULATION_DEFINITION
    )
    projection_aggregation: Literal[
        "equal_weight_mean_of_latest_manifest_bound_row_per_source_player"
    ] = PROJECTION_AGGREGATION
    inactive_accounting_notice: str = INACTIVE_ACCOUNTING_NOTICE
    shape_channel_available: bool
    shape_channel_notice: str
    cells: tuple[BaselineEvaluationCell, ...] = Field(min_length=1)

    @field_validator("as_of", "evaluation_as_of")
    @classmethod
    def normalize_timestamps(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def validate_cutoffs(self) -> BaselineEvaluationReport:
        if self.evaluation_as_of < self.as_of:
            raise ValueError("evaluation_as_of must not precede the decision cutoff")
        expected_notice = (
            SHAPE_AVAILABLE_NOTICE
            if self.shape_channel_available
            else (
                SHAPE_STALE_NOTICE
                if self.distribution_rows_not_in_frozen_source_set
                else SHAPE_UNAVAILABLE_NOTICE
            )
        )
        if self.shape_channel_notice != expected_notice:
            raise ValueError("shape channel notice does not match availability")
        accounted = sum(
            cell.n_scored
            + cell.n_projected_without_result
            + cell.n_result_without_projection
            + cell.n_projected_but_inactive
            + cell.n_salary_without_projection_or_result
            for cell in self.cells
        )
        if accounted != self.salary_population_n:
            raise ValueError("salary population must equal the disjoint accounting counts")
        for cell in self.cells:
            shape = cell.shape
            if shape is None:
                continue
            expected_bins = self.pit_bins if shape.metric_status == "available" else 0
            if len(shape.pit_bin_counts) != expected_bins:
                raise ValueError("PIT bin count length does not match pit_bins")
        return self


@dataclass(frozen=True)
class _Projection:
    player_id: int
    prediction: float
    selected_snapshot_ids: frozenset[int]
    selected_sources: frozenset[str]


@dataclass(frozen=True)
class _SalaryPlayer:
    player_id: int
    season: int
    week: int
    position: str
    player_status: str | None


@dataclass(frozen=True)
class _Outcome:
    player_id: int
    realized: float
    season: int
    week: int
    position: str
    activity: bool | None


@dataclass(frozen=True)
class _StoredDistribution:
    player_id: int
    season: int
    week: int
    position: str
    distribution: PlayerOutcomeDistribution


@dataclass
class _CellAccumulator:
    predictions: list[float] = field(default_factory=list)
    realized: list[float] = field(default_factory=list)
    n_projected_without_result: int = 0
    n_result_without_projection: int = 0
    n_projected_but_inactive: int = 0
    n_scored_zero_activity_unknown: int = 0
    n_salary_without_projection_or_result: int = 0
    n_projected_partial_source_coverage: int = 0
    scored_player_ids: set[int] = field(default_factory=set)
    shape_eligible_player_ids: set[int] = field(default_factory=set)
    shape_player_ids: set[int] = field(default_factory=set)
    shape_distributions: list[PlayerOutcomeDistribution] = field(default_factory=list)
    shape_realized: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class _ProjectionLoad:
    values: Mapping[int, _Projection]
    artifacts: frozenset[tuple[str, str]]
    sources: frozenset[str]
    hashes: frozenset[str]
    run_ids: frozenset[str]


@dataclass(frozen=True)
class _SalaryLoad:
    values: Mapping[int, _SalaryPlayer]
    artifacts: frozenset[tuple[str, str]]
    sources: frozenset[str]
    hashes: frozenset[str]
    run_ids: frozenset[str]


@dataclass(frozen=True)
class _OutcomeLoad:
    values: Mapping[int, _Outcome]
    sources: frozenset[str]
    hashes: frozenset[str]
    run_ids: frozenset[str]


@dataclass(frozen=True)
class _DistributionLoad:
    values: tuple[_StoredDistribution, ...]
    source_set_hashes: frozenset[str]
    run_ids: frozenset[str]
    mismatched_source_set_rows: int


def build_baseline_report(
    connection: sqlite3.Connection,
    *,
    decision_snapshot_id: str,
    decision_at: datetime,
    evaluation_as_of: datetime,
    slate_id: int | None = None,
    thresholds: BaselineThresholds | None = None,
) -> BaselineEvaluationReport:
    """Evaluate one slate's frozen purchased baseline against bounded result labels.

    ``decision_at`` controls every forecast-side version. ``evaluation_as_of`` controls
    which later result corrections are visible. The separation is mandatory: using the
    result cutoff for projections would make a revised post-lock forecast look prescient.
    """

    selected_thresholds = thresholds or BaselineThresholds()
    try:
        cutoff = ensure_utc(decision_at)
        label_cutoff = ensure_utc(evaluation_as_of)
    except ValueError as error:
        raise BaselineReportError("report cutoffs must include a timezone") from error
    if label_cutoff < cutoff:
        raise BaselineReportError("evaluation_as_of must not precede the decision cutoff")

    session = PointInTimeSession(connection)
    try:
        snapshot = session.decision_snapshot(decision_snapshot_id, as_of=cutoff)
        slate = session.slate(snapshot.slate_id, as_of=cutoff)
    except ReplayError as error:
        raise BaselineReportError(str(error)) from error
    if slate_id is not None and snapshot.slate_id != slate_id:
        raise BaselineReportError(
            f"decision snapshot belongs to slate {snapshot.slate_id}, not {slate_id}"
        )
    if snapshot.run_id is None:
        raise BaselineReportError("decision snapshot has no run_id")

    expected_salary_artifacts = _manifest_artifacts(snapshot, "salary")
    expected_projection_artifacts = _manifest_artifacts(snapshot, "projection")
    salaries = _load_salary_population(
        session,
        slate_id=slate.slate_id,
        salary_artifacts=expected_salary_artifacts,
        default_season=slate.season,
        default_week=slate.week,
        as_of=cutoff,
    )
    if salaries.artifacts != expected_salary_artifacts:
        raise BaselineReportError(
            "not every salary manifest artifact contributed to the salary population"
        )
    projections = _load_projections(
        session,
        slate_id=slate.slate_id,
        site=slate.site,
        salary_artifacts=expected_salary_artifacts,
        projection_artifacts=expected_projection_artifacts,
        as_of=cutoff,
    )
    if projections.artifacts != expected_projection_artifacts:
        raise BaselineReportError(
            "not every projection manifest artifact contributed to the evaluated baseline"
        )
    outcomes = _load_outcomes(
        session,
        slate_id=slate.slate_id,
        site=slate.site,
        salary_artifacts=expected_salary_artifacts,
        expected_season=slate.season,
        expected_week=slate.week,
        evaluation_as_of=label_cutoff,
        as_of=cutoff,
    )
    distributions = _load_distributions(
        session,
        slate_id=slate.slate_id,
        default_season=slate.season,
        default_week=slate.week,
        selected_projection_snapshot_ids={
            player_id: projection.selected_snapshot_ids
            for player_id, projection in projections.values.items()
        },
        as_of=cutoff,
    )
    accumulators = _account(
        salaries.values,
        projections.values,
        outcomes.values,
        expected_projection_sources=frozenset(
            source for source, _ in expected_projection_artifacts
        ),
    )
    _attach_shape_scores(accumulators, outcomes.values, distributions.values)
    if not accumulators:
        raise BaselineReportError(
            "slate has neither point-in-time projections nor bounded result labels"
        )

    cells = tuple(
        _build_cell(
            key,
            accumulator,
            selected_thresholds,
            shape_channel_available=bool(distributions.values),
        )
        for key, accumulator in sorted(accumulators.items())
    )
    return BaselineEvaluationReport(
        as_of=cutoff,
        evaluation_as_of=label_cutoff,
        decision_snapshot_id=snapshot.decision_snapshot_id,
        run_id=snapshot.run_id,
        slate_id=slate.slate_id,
        external_slate_id=slate.external_slate_id,
        site=slate.site,
        season=slate.season,
        week=slate.week,
        minimum_sample_size=selected_thresholds.minimum_sample_size,
        pit_bins=selected_thresholds.pit_bins,
        pit_random_seed=selected_thresholds.pit_random_seed,
        salary_population_n=len(salaries.values),
        salary_sources=tuple(sorted(salaries.sources)),
        salary_file_hashes=tuple(sorted(salaries.hashes)),
        salary_run_ids=tuple(sorted(salaries.run_ids)),
        projection_sources=tuple(sorted(projections.sources)),
        projection_file_hashes=tuple(sorted(projections.hashes)),
        projection_run_ids=tuple(sorted(projections.run_ids)),
        result_sources=tuple(sorted(outcomes.sources)),
        result_file_hashes=tuple(sorted(outcomes.hashes)),
        result_run_ids=tuple(sorted(outcomes.run_ids)),
        distribution_source_set_hashes=tuple(sorted(distributions.source_set_hashes)),
        distribution_run_ids=tuple(sorted(distributions.run_ids)),
        distribution_rows_not_in_frozen_source_set=(distributions.mismatched_source_set_rows),
        shape_channel_available=bool(distributions.values),
        shape_channel_notice=(
            SHAPE_AVAILABLE_NOTICE
            if distributions.values
            else (
                SHAPE_STALE_NOTICE
                if distributions.mismatched_source_set_rows
                else SHAPE_UNAVAILABLE_NOTICE
            )
        ),
        cells=cells,
    )


def render_baseline_report(report: BaselineEvaluationReport) -> str:
    """Render a stable text/CSV report with no missing-accounting ambiguity."""

    output = io.StringIO(newline="")
    output.write("BASELINE EVALUATION REPORT — PURCHASED PROJECTIONS\n")
    output.write(f"as_of={utc_timestamp(report.as_of)}\n")
    output.write(f"evaluation_as_of={utc_timestamp(report.evaluation_as_of)}\n")
    output.write(f"decision_snapshot_id={report.decision_snapshot_id}\n")
    output.write(f"run_id={report.run_id}\n")
    output.write(f"slate_id={report.slate_id}\n")
    output.write(f"external_slate_id={report.external_slate_id}\n")
    output.write(f"site={report.site}\n")
    output.write(f"season={report.season}\n")
    output.write(f"week={report.week}\n")
    output.write(f"minimum_sample_size={report.minimum_sample_size}\n")
    output.write(f"pit_bins={report.pit_bins}\n")
    output.write(f"pit_random_seed={report.pit_random_seed}\n")
    output.write(f"salary_population_n={report.salary_population_n}\n")
    output.write("salary_sources=" + _json_strings(report.salary_sources) + "\n")
    output.write("salary_file_hashes=" + _json_strings(report.salary_file_hashes) + "\n")
    output.write("salary_run_ids=" + _json_strings(report.salary_run_ids) + "\n")
    output.write("projection_sources=" + _json_strings(report.projection_sources) + "\n")
    output.write("projection_file_hashes=" + _json_strings(report.projection_file_hashes) + "\n")
    output.write("projection_run_ids=" + _json_strings(report.projection_run_ids) + "\n")
    output.write("result_sources=" + _json_strings(report.result_sources) + "\n")
    output.write("result_file_hashes=" + _json_strings(report.result_file_hashes) + "\n")
    output.write("result_run_ids=" + _json_strings(report.result_run_ids) + "\n")
    output.write(
        "distribution_source_set_hashes="
        + _json_strings(report.distribution_source_set_hashes)
        + "\n"
    )
    output.write("distribution_run_ids=" + _json_strings(report.distribution_run_ids) + "\n")
    output.write(
        "distribution_rows_not_in_frozen_source_set="
        f"{report.distribution_rows_not_in_frozen_source_set}\n"
    )
    output.write("signed_error_definition=projection_minus_realized_fantasy_points\n")
    output.write("projection_aggregation=" + report.projection_aggregation + "\n")
    output.write("population_definition=" + report.population_definition + "\n")
    output.write("inactive_accounting=" + report.inactive_accounting_notice + "\n")
    output.write(report.shape_channel_notice + "\n\n")

    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (
            "season",
            "week",
            "position",
            "n_scored",
            "n_projected_without_result",
            "n_result_without_projection",
            "n_projected_but_inactive",
            "n_scored_zero_activity_unknown",
            "n_salary_without_projection_or_result",
            "n_projected_partial_source_coverage",
            "signed_mean_error_bias",
            "mae",
            "rmse",
            "spearman_rank_correlation",
            "shape_n_scored",
            "mean_crps",
            "shape_n_log_score_finite",
            "shape_n_log_score_off_support",
            "shape_n_negative_outcomes",
            "shape_n_player_results_without_distribution",
            "mean_log_score",
            "pit_bin_counts",
            "pit_pearson_chi_square",
            "pit_max_abs_frequency_deviation",
        )
    )
    for cell in report.cells:
        shape = cell.shape
        writer.writerow(
            (
                cell.season,
                cell.week,
                cell.position,
                cell.n_scored,
                cell.n_projected_without_result,
                cell.n_result_without_projection,
                cell.n_projected_but_inactive,
                cell.n_scored_zero_activity_unknown,
                cell.n_salary_without_projection_or_result,
                cell.n_projected_partial_source_coverage,
                _metric(cell.signed_mean_error_bias, cell.metric_status),
                _metric(cell.mae, cell.metric_status),
                _metric(cell.rmse, cell.metric_status),
                _spearman(cell),
                "unavailable" if shape is None else shape.n_scored,
                "unavailable" if shape is None else _metric(shape.mean_crps, shape.metric_status),
                "unavailable" if shape is None else shape.n_log_score_finite,
                "unavailable" if shape is None else shape.n_log_score_off_support,
                "unavailable" if shape is None else shape.n_negative_outcomes,
                "unavailable" if shape is None else shape.n_player_results_without_distribution,
                "unavailable"
                if shape is None
                else _metric(shape.mean_log_score, shape.log_score_status),
                "unavailable" if shape is None else _pit_counts(shape, shape.metric_status),
                "unavailable"
                if shape is None
                else _metric(shape.pit_pearson_chi_square, shape.metric_status),
                "unavailable"
                if shape is None
                else _metric(shape.pit_max_abs_frequency_deviation, shape.metric_status),
            )
        )
    return output.getvalue()


def _load_salary_population(
    session: PointInTimeSession,
    *,
    slate_id: int,
    salary_artifacts: frozenset[tuple[str, str]],
    default_season: int,
    default_week: int,
    as_of: datetime,
) -> _SalaryLoad:
    salary_filter, salary_parameters = _artifact_filter(
        "s.source_file_sha256", "s.source", "salary_artifact", salary_artifacts
    )
    rows = session.query(
        f"""
        WITH ranked_salaries AS (
            SELECT s.*,
                   row_number() OVER (
                       PARTITION BY s.player_id
                       ORDER BY rtrim(s.observed_at, 'Z') DESC, s.salary_id DESC
                   ) AS version_rank
            FROM salaries AS s
            WHERE s.slate_id = :slate_id
              {salary_filter}
              AND rtrim(s.observed_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(s.valid_from, 'Z') <= rtrim(:as_of, 'Z')
              AND (
                  s.valid_to IS NULL
                  OR rtrim(s.valid_to, 'Z') > rtrim(:as_of, 'Z')
              )
        )
        SELECT s.player_id, s.game_id AS salary_game_id, s.player_status,
               s.source, s.source_file_sha256, s.run_id,
               p.player_id AS bounded_player_id, p.position,
               g.game_id AS bounded_game_id, g.season, g.week
        FROM ranked_salaries AS s
        LEFT JOIN players AS p
          ON p.player_id = s.player_id
         AND rtrim(p.observed_at, 'Z') <= rtrim(:as_of, 'Z')
         AND rtrim(p.valid_from, 'Z') <= rtrim(:as_of, 'Z')
         AND (
             p.valid_to IS NULL
             OR rtrim(p.valid_to, 'Z') > rtrim(:as_of, 'Z')
         )
        LEFT JOIN games AS g
          ON g.game_id = s.game_id
         AND rtrim(g.observed_at, 'Z') <= rtrim(:as_of, 'Z')
         AND rtrim(g.valid_from, 'Z') <= rtrim(:as_of, 'Z')
         AND (
             g.valid_to IS NULL
             OR rtrim(g.valid_to, 'Z') > rtrim(:as_of, 'Z')
         )
        WHERE s.version_rank = 1
        ORDER BY s.player_id
        """,
        {"slate_id": slate_id, **salary_parameters},
        as_of=as_of,
    )
    if not rows:
        raise BaselineReportError(
            "decision manifest produced an empty point-in-time salary population"
        )

    values: dict[int, _SalaryPlayer] = {}
    artifacts: set[tuple[str, str]] = set()
    sources: set[str] = set()
    hashes: set[str] = set()
    run_ids: set[str] = set()
    for row in rows:
        player_id = int(row["player_id"])
        if row["bounded_player_id"] is None:
            raise BaselineReportError(
                f"salary player {player_id} is unavailable at the decision cutoff"
            )
        if row["salary_game_id"] is not None and row["bounded_game_id"] is None:
            raise BaselineReportError(
                f"player {player_id} salary game is unavailable at the decision cutoff"
            )
        season = default_season if row["season"] is None else int(row["season"])
        week = default_week if row["week"] is None else int(row["week"])
        if (season, week) != (default_season, default_week):
            raise BaselineReportError(
                f"player {player_id} salary game is in {season}-W{week:02d}, "
                f"but the slate is {default_season}-W{default_week:02d}"
            )
        values[player_id] = _SalaryPlayer(
            player_id=player_id,
            season=season,
            week=week,
            position=_position(row["position"]),
            player_status=(None if row["player_status"] is None else str(row["player_status"])),
        )
        source = str(row["source"])
        sha256 = str(row["source_file_sha256"])
        artifacts.add((source, sha256))
        sources.add(source)
        hashes.add(sha256)
        if row["run_id"] is not None:
            run_ids.add(str(row["run_id"]))
    return _SalaryLoad(
        values=values,
        artifacts=frozenset(artifacts),
        sources=frozenset(sources),
        hashes=frozenset(hashes),
        run_ids=frozenset(run_ids),
    )


def _load_projections(
    session: PointInTimeSession,
    *,
    slate_id: int,
    site: str,
    salary_artifacts: frozenset[tuple[str, str]],
    projection_artifacts: frozenset[tuple[str, str]],
    as_of: datetime,
) -> _ProjectionLoad:
    salary_filter, salary_parameters = _artifact_filter(
        "s.source_file_sha256", "s.source", "salary_artifact", salary_artifacts
    )
    projection_filter, projection_parameters = _artifact_filter(
        "ps.source_file_sha256",
        "ps.source",
        "projection_artifact",
        projection_artifacts,
    )
    rows = session.query(
        f"""
        WITH ranked_projections AS (
            SELECT ps.*,
                   row_number() OVER (
                       PARTITION BY ps.player_id, ps.source
                       ORDER BY rtrim(ps.observed_at, 'Z') DESC,
                                ps.projection_snapshot_id DESC
                   ) AS version_rank
            FROM projection_snapshots AS ps
            WHERE ps.slate_id = :slate_id
              AND ps.site = :site
              {projection_filter}
              AND rtrim(ps.observed_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(ps.valid_from, 'Z') <= rtrim(:as_of, 'Z')
              AND (
                  ps.valid_to IS NULL
                  OR rtrim(ps.valid_to, 'Z') > rtrim(:as_of, 'Z')
              )
        ),
        ranked_salaries AS (
            SELECT s.*,
                   row_number() OVER (
                       PARTITION BY s.player_id
                       ORDER BY rtrim(s.observed_at, 'Z') DESC, s.salary_id DESC
                   ) AS version_rank
            FROM salaries AS s
            WHERE s.slate_id = :slate_id
              {salary_filter}
              AND rtrim(s.observed_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(s.valid_from, 'Z') <= rtrim(:as_of, 'Z')
              AND (
                  s.valid_to IS NULL
                  OR rtrim(s.valid_to, 'Z') > rtrim(:as_of, 'Z')
              )
        )
        SELECT ps.projection_snapshot_id, ps.player_id, ps.projection_mean, ps.source,
               ps.source_file_sha256, ps.run_id
        FROM ranked_projections AS ps
        JOIN ranked_salaries AS s
          ON s.player_id = ps.player_id AND s.version_rank = 1
        WHERE ps.version_rank = 1
        ORDER BY ps.player_id, ps.source, ps.projection_snapshot_id
        """,
        {
            "slate_id": slate_id,
            "site": site,
            **salary_parameters,
            **projection_parameters,
        },
        as_of=as_of,
    )
    grouped: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[int(row["player_id"])].append(row)
    values: dict[int, _Projection] = {}
    sources: set[str] = set()
    hashes: set[str] = set()
    run_ids: set[str] = set()
    selected_projection_artifacts: set[tuple[str, str]] = set()
    for player_id, player_rows in grouped.items():
        prediction = math.fsum(float(row["projection_mean"]) for row in player_rows) / len(
            player_rows
        )
        if not math.isfinite(prediction):
            raise BaselineReportError(f"player {player_id} has a non-finite blended projection")
        values[player_id] = _Projection(
            player_id=player_id,
            prediction=prediction,
            selected_snapshot_ids=frozenset(
                int(row["projection_snapshot_id"]) for row in player_rows
            ),
            selected_sources=frozenset(str(row["source"]) for row in player_rows),
        )
        for row in player_rows:
            sources.add(str(row["source"]))
            hashes.add(str(row["source_file_sha256"]))
            selected_projection_artifacts.add((str(row["source"]), str(row["source_file_sha256"])))
            if row["run_id"] is not None:
                run_ids.add(str(row["run_id"]))
    return _ProjectionLoad(
        values=values,
        artifacts=frozenset(selected_projection_artifacts),
        sources=frozenset(sources),
        hashes=frozenset(hashes),
        run_ids=frozenset(run_ids),
    )


def _load_outcomes(
    session: PointInTimeSession,
    *,
    slate_id: int,
    site: str,
    salary_artifacts: frozenset[tuple[str, str]],
    expected_season: int,
    expected_week: int,
    evaluation_as_of: datetime,
    as_of: datetime,
) -> _OutcomeLoad:
    salary_filter, salary_parameters = _artifact_filter(
        "s.source_file_sha256", "s.source", "salary_artifact", salary_artifacts
    )
    rows = session.query(
        f"""
        WITH ranked_salaries AS (
            SELECT s.*,
                   row_number() OVER (
                       PARTITION BY s.player_id
                       ORDER BY rtrim(s.observed_at, 'Z') DESC, s.salary_id DESC
                   ) AS version_rank
            FROM salaries AS s
            WHERE s.slate_id = :slate_id
              AND s.game_id IS NOT NULL
              {salary_filter}
              AND rtrim(s.observed_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(s.valid_from, 'Z') <= rtrim(:as_of, 'Z')
              AND (
                  s.valid_to IS NULL
                  OR rtrim(s.valid_to, 'Z') > rtrim(:as_of, 'Z')
              )
        ),
        ranked_results AS (
            SELECT r.*,
                   row_number() OVER (
                       PARTITION BY r.player_id, r.game_id, r.source
                       ORDER BY rtrim(r.observed_at, 'Z') DESC, r.result_id DESC
                   ) AS version_rank
            FROM results AS r
            JOIN ranked_salaries AS s
              ON s.player_id = r.player_id
             AND s.game_id = r.game_id
             AND s.version_rank = 1
            WHERE r.site = :site
              AND rtrim(r.observed_at, 'Z') <= rtrim(:evaluation_as_of, 'Z')
              AND rtrim(r.valid_from, 'Z') <= rtrim(:evaluation_as_of, 'Z')
              AND (
                  r.valid_to IS NULL
                  OR rtrim(r.valid_to, 'Z') > rtrim(:evaluation_as_of, 'Z')
              )
        )
        SELECT r.*, p.position, g.season, g.week
        FROM ranked_results AS r
        JOIN players AS p ON p.player_id = r.player_id
        JOIN games AS g ON g.game_id = r.game_id
        WHERE r.version_rank = 1
          AND rtrim(p.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(p.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (
              p.valid_to IS NULL
              OR rtrim(p.valid_to, 'Z') > rtrim(:as_of, 'Z')
          )
          AND rtrim(g.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(g.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (
              g.valid_to IS NULL
              OR rtrim(g.valid_to, 'Z') > rtrim(:as_of, 'Z')
          )
        ORDER BY r.player_id, r.game_id, r.source, r.result_id
        """,
        {
            "slate_id": slate_id,
            "site": site,
            "evaluation_as_of": utc_timestamp(evaluation_as_of),
            **salary_parameters,
        },
        as_of=as_of,
    )
    grouped: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[int(row["player_id"])].append(row)
    values: dict[int, _Outcome] = {}
    sources: set[str] = set()
    hashes: set[str] = set()
    run_ids: set[str] = set()
    for player_id, player_rows in grouped.items():
        game_ids = {int(row["game_id"]) for row in player_rows}
        if len(game_ids) != 1:
            raise BaselineReportError(
                f"player {player_id} has result labels for multiple games in one slate"
            )
        realized_values = {float(row["fantasy_points"]) for row in player_rows}
        if len(realized_values) != 1:
            raise BaselineReportError(
                f"conflicting result labels for player {player_id}; refusing to choose a source"
            )
        realized = next(iter(realized_values))
        if not math.isfinite(realized):
            raise BaselineReportError(f"player {player_id} has a non-finite result label")
        activity = {_explicit_activity(row["stat_line_json"]) for row in player_rows}
        known_activity = {value for value in activity if value is not None}
        if len(known_activity) > 1:
            raise BaselineReportError(f"conflicting active/inactive labels for player {player_id}")
        selected_activity = next(iter(known_activity)) if known_activity else None
        if selected_activity is False and realized != 0.0:
            raise BaselineReportError(
                f"inactive result for player {player_id} has nonzero fantasy_points"
            )
        first = player_rows[0]
        season = int(first["season"])
        week = int(first["week"])
        if (season, week) != (expected_season, expected_week):
            raise BaselineReportError(
                f"player {player_id} result game is in {season}-W{week:02d}, "
                f"but the slate is {expected_season}-W{expected_week:02d}"
            )
        values[player_id] = _Outcome(
            player_id=player_id,
            realized=realized,
            season=season,
            week=week,
            position=_position(first["position"]),
            activity=selected_activity,
        )
        for row in player_rows:
            sources.add(str(row["source"]))
            hashes.add(str(row["source_file_sha256"]))
            if row["run_id"] is not None:
                run_ids.add(str(row["run_id"]))
    return _OutcomeLoad(
        values=values,
        sources=frozenset(sources),
        hashes=frozenset(hashes),
        run_ids=frozenset(run_ids),
    )


def _load_distributions(
    session: PointInTimeSession,
    *,
    slate_id: int,
    default_season: int,
    default_week: int,
    selected_projection_snapshot_ids: Mapping[int, frozenset[int]],
    as_of: datetime,
) -> _DistributionLoad:
    rows = session.query(
        """
        WITH ranked_distributions AS (
            SELECT pd.*,
                   row_number() OVER (
                       PARTITION BY pd.player_id, pd.source_set_sha256
                       ORDER BY rtrim(pd.as_of_at, 'Z') DESC,
                                rtrim(pd.observed_at, 'Z') DESC,
                                pd.player_distribution_id DESC
                   ) AS version_rank
            FROM player_distributions AS pd
            WHERE pd.slate_id = :slate_id
              AND rtrim(pd.as_of_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(pd.observed_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(pd.valid_from, 'Z') <= rtrim(:as_of, 'Z')
              AND (
                  pd.valid_to IS NULL
                  OR rtrim(pd.valid_to, 'Z') > rtrim(:as_of, 'Z')
              )
        )
        SELECT pd.*
        FROM ranked_distributions AS pd
        WHERE pd.version_rank = 1
        ORDER BY pd.player_id, pd.source_set_sha256, pd.player_distribution_id
        """,
        {"slate_id": slate_id},
        as_of=as_of,
    )
    values: list[_StoredDistribution] = []
    source_set_hashes: set[str] = set()
    run_ids: set[str] = set()
    mismatched_source_set_rows = 0
    selected_players: set[int] = set()
    for row in rows:
        raw = dict(zip(row.keys(), row, strict=True))
        raw.pop("version_rank", None)
        try:
            stored = PlayerDistributionRow.model_validate(raw)
        except ValueError as error:
            raise BaselineReportError(
                f"stored distribution {row['player_distribution_id']} is invalid"
            ) from error
        frozen_source_set = selected_projection_snapshot_ids.get(stored.player_id)
        stored_source_set = frozenset(
            reference.projection_snapshot_id for reference in stored.source_set_json
        )
        if frozen_source_set is None or stored_source_set != frozen_source_set:
            mismatched_source_set_rows += 1
            continue
        if stored.player_id in selected_players:
            raise BaselineReportError(
                f"player {stored.player_id} has multiple eligible source-specific "
                "distributions; a position/week aggregate cannot count one player twice"
            )
        selected_players.add(stored.player_id)
        values.append(
            _StoredDistribution(
                player_id=stored.player_id,
                season=default_season,
                week=default_week,
                position=_position(stored.position),
                distribution=PlayerOutcomeDistribution(
                    distribution_family=stored.distribution_family,
                    p_active=stored.p_active,
                    p_full_role_given_active=stored.p_full_role_given_active,
                    conditional_location=stored.conditional_location,
                    conditional_scale=stored.conditional_scale,
                    conditional_shape=stored.conditional_shape,
                ),
            )
        )
        source_set_hashes.add(stored.source_set_sha256)
        if stored.run_id is not None:
            run_ids.add(stored.run_id)
    return _DistributionLoad(
        values=tuple(values),
        source_set_hashes=frozenset(source_set_hashes),
        run_ids=frozenset(run_ids),
        mismatched_source_set_rows=mismatched_source_set_rows,
    )


def _account(
    salaries: Mapping[int, _SalaryPlayer],
    projections: Mapping[int, _Projection],
    outcomes: Mapping[int, _Outcome],
    *,
    expected_projection_sources: frozenset[str],
) -> dict[tuple[int, int, str], _CellAccumulator]:
    projection_outside_population = set(projections) - set(salaries)
    result_outside_population = set(outcomes) - set(salaries)
    if projection_outside_population or result_outside_population:
        raise BaselineReportError(
            "projection/result rows escaped the manifest-bound salary population"
        )
    accumulators: dict[tuple[int, int, str], _CellAccumulator] = defaultdict(_CellAccumulator)
    for player_id in sorted(salaries):
        salary = salaries[player_id]
        projection = projections.get(player_id)
        outcome = outcomes.get(player_id)
        salary_key = (salary.season, salary.week, salary.position)
        accumulator = accumulators[salary_key]
        if projection is not None and projection.selected_sources != expected_projection_sources:
            accumulator.n_projected_partial_source_coverage += 1
        if projection is not None and outcome is not None:
            outcome_key = (outcome.season, outcome.week, outcome.position)
            if salary_key != outcome_key:
                raise BaselineReportError(
                    f"salary/result position-week mismatch for player {player_id}: "
                    f"{salary_key!r} != {outcome_key!r}"
                )
            salary_inactive = _inactive_salary_status(salary.player_status)
            if salary_inactive and outcome.activity is True:
                raise BaselineReportError(
                    f"player {player_id} is inactive in the salary input but active "
                    "in the result label"
                )
            if salary_inactive and outcome.realized != 0.0:
                raise BaselineReportError(
                    f"player {player_id} is inactive in the salary input but has a nonzero result"
                )
            accumulator.shape_eligible_player_ids.add(player_id)
            if outcome.activity is False or salary_inactive:
                accumulator.n_projected_but_inactive += 1
            else:
                if outcome.activity is None and outcome.realized == 0.0:
                    accumulator.n_scored_zero_activity_unknown += 1
                accumulator.predictions.append(projection.prediction)
                accumulator.realized.append(outcome.realized)
                accumulator.scored_player_ids.add(player_id)
            continue
        if projection is not None:
            if _inactive_salary_status(salary.player_status):
                accumulator.n_projected_but_inactive += 1
            else:
                accumulator.n_projected_without_result += 1
            continue
        if outcome is not None:
            outcome_key = (outcome.season, outcome.week, outcome.position)
            if salary_key != outcome_key:
                raise BaselineReportError(
                    f"salary/result position-week mismatch for player {player_id}: "
                    f"{salary_key!r} != {outcome_key!r}"
                )
            accumulator.n_result_without_projection += 1
            continue
        accumulator.n_salary_without_projection_or_result += 1
    return dict(accumulators)


def _attach_shape_scores(
    accumulators: dict[tuple[int, int, str], _CellAccumulator],
    outcomes: Mapping[int, _Outcome],
    distributions: Iterable[_StoredDistribution],
) -> None:
    for stored in distributions:
        outcome = outcomes.get(stored.player_id)
        if outcome is None:
            continue
        key = (outcome.season, outcome.week, outcome.position)
        stored_key = (stored.season, stored.week, stored.position)
        if key != stored_key:
            raise BaselineReportError(
                f"distribution/result position-week mismatch for player {stored.player_id}: "
                f"{stored_key!r} != {key!r}"
            )
        accumulator = accumulators[key]
        if stored.player_id not in accumulator.shape_eligible_player_ids:
            continue
        accumulator.shape_distributions.append(stored.distribution)
        accumulator.shape_realized.append(outcome.realized)
        accumulator.shape_player_ids.add(stored.player_id)


def _build_cell(
    key: tuple[int, int, str],
    accumulator: _CellAccumulator,
    thresholds: BaselineThresholds,
    *,
    shape_channel_available: bool,
) -> BaselineEvaluationCell:
    season, week, position = key
    n_scored = len(accumulator.predictions)
    if n_scored != len(accumulator.realized):
        raise BaselineReportError("projection and result metric inputs are misaligned")
    status: MetricStatus = (
        "available" if n_scored >= thresholds.minimum_sample_size else "insufficient_n"
    )
    bias: float | None = None
    mae_value: float | None = None
    rmse_value: float | None = None
    spearman_value: float | None = None
    spearman_status: SpearmanStatus = "insufficient_n"
    if status == "available":
        errors = [
            projection - realized
            for projection, realized in zip(
                accumulator.predictions, accumulator.realized, strict=True
            )
        ]
        bias = math.fsum(errors) / n_scored
        mae_value = math.fsum(abs(error) for error in errors) / n_scored
        rmse_value = math.sqrt(math.fsum(error * error for error in errors) / n_scored)
        if n_scored >= 2:
            spearman_value = _spearman_correlation(
                accumulator.predictions, accumulator.realized
            )
            spearman_status = (
                "available"
                if spearman_value is not None
                else "undefined_constant_rank"
            )
    shape = _build_shape_metrics(
        season,
        week,
        position,
        accumulator,
        thresholds,
        shape_channel_available=shape_channel_available,
    )
    return BaselineEvaluationCell(
        season=season,
        week=week,
        position=position,
        n_scored=n_scored,
        n_projected_without_result=accumulator.n_projected_without_result,
        n_result_without_projection=accumulator.n_result_without_projection,
        n_projected_but_inactive=accumulator.n_projected_but_inactive,
        n_scored_zero_activity_unknown=accumulator.n_scored_zero_activity_unknown,
        n_salary_without_projection_or_result=(accumulator.n_salary_without_projection_or_result),
        n_projected_partial_source_coverage=(accumulator.n_projected_partial_source_coverage),
        metric_status=status,
        spearman_status=spearman_status,
        signed_mean_error_bias=bias,
        mae=mae_value,
        rmse=rmse_value,
        spearman_rank_correlation=spearman_value,
        shape=shape,
    )


def _build_shape_metrics(
    season: int,
    week: int,
    position: str,
    accumulator: _CellAccumulator,
    thresholds: BaselineThresholds,
    *,
    shape_channel_available: bool,
) -> BaselineShapeMetrics | None:
    if not shape_channel_available:
        return None
    distributions = accumulator.shape_distributions
    realized = accumulator.shape_realized
    n_scored = len(distributions)
    if n_scored != len(realized):
        raise BaselineReportError("distribution and result metric inputs are misaligned")
    crps_values = [
        crps(distribution, outcome)
        for distribution, outcome in zip(distributions, realized, strict=True)
    ]
    log_values = [
        log_score(distribution, outcome)
        for distribution, outcome in zip(distributions, realized, strict=True)
    ]
    finite_log_values = [value for value in log_values if math.isfinite(value)]
    off_support = len(log_values) - len(finite_log_values)
    negative = sum(outcome < 0 for outcome in realized)
    metric_status: MetricStatus = (
        "available" if n_scored >= thresholds.minimum_sample_size else "insufficient_n"
    )
    log_status: MetricStatus = (
        "available"
        if len(finite_log_values) >= thresholds.minimum_sample_size
        else "insufficient_n"
    )
    mean_crps = math.fsum(crps_values) / n_scored if metric_status == "available" else None
    mean_log = (
        math.fsum(finite_log_values) / len(finite_log_values) if log_status == "available" else None
    )
    pit_counts: tuple[int, ...] = ()
    pit_chi_square: float | None = None
    pit_max_deviation: float | None = None
    if metric_status == "available":
        calibration = pit_histogram(
            distributions,
            realized,
            bins=thresholds.pit_bins,
            rng=random.Random(f"{thresholds.pit_random_seed}:{season}:{week}:{position}"),
        )
        pit_counts = calibration.bin_counts
        pit_chi_square = calibration.pearson_chi_square
        pit_max_deviation = calibration.max_abs_frequency_deviation
    return BaselineShapeMetrics(
        n_scored=n_scored,
        n_log_score_finite=len(finite_log_values),
        n_log_score_off_support=off_support,
        n_negative_outcomes=negative,
        n_player_results_without_distribution=len(
            accumulator.shape_eligible_player_ids - accumulator.shape_player_ids
        ),
        metric_status=metric_status,
        log_score_status=log_status,
        mean_crps=mean_crps,
        mean_log_score=mean_log,
        pit_bin_counts=pit_counts,
        pit_pearson_chi_square=pit_chi_square,
        pit_max_abs_frequency_deviation=pit_max_deviation,
    )


def _spearman_correlation(predictions: list[float], outcomes: list[float]) -> float | None:
    if len(predictions) < 2:
        return None
    prediction_ranks = _average_ranks(predictions)
    outcome_ranks = _average_ranks(outcomes)
    prediction_mean = math.fsum(prediction_ranks) / len(prediction_ranks)
    outcome_mean = math.fsum(outcome_ranks) / len(outcome_ranks)
    prediction_residuals = [value - prediction_mean for value in prediction_ranks]
    outcome_residuals = [value - outcome_mean for value in outcome_ranks]
    denominator = math.sqrt(
        math.fsum(value * value for value in prediction_residuals)
        * math.fsum(value * value for value in outcome_residuals)
    )
    if denominator == 0.0:
        return None
    correlation = (
        math.fsum(
            left * right
            for left, right in zip(prediction_residuals, outcome_residuals, strict=True)
        )
        / denominator
    )
    return min(1.0, max(-1.0, correlation))


def _average_ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(values)
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end][1] == ordered[start][1]:
            end += 1
        average_rank = ((start + 1) + end) / 2.0
        for index in range(start, end):
            ranks[ordered[index][0]] = average_rank
        start = end
    return ranks


def _explicit_activity(raw: object) -> bool | None:
    if raw is None:
        return None
    try:
        value: Any = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as error:
        raise BaselineReportError("result stat_line_json is invalid JSON") from error
    if not isinstance(value, dict):
        raise BaselineReportError("result stat_line_json must be a JSON object")
    signals: set[bool] = set()
    if "active" in value:
        active = value["active"]
        if isinstance(active, bool):
            signals.add(active)
        else:
            raise BaselineReportError("result stat_line_json active must be boolean")
    if "inactive" in value:
        inactive = value["inactive"]
        if isinstance(inactive, bool):
            signals.add(not inactive)
        else:
            raise BaselineReportError("result stat_line_json inactive must be boolean")
    status = value.get("status")
    if status is not None:
        if not isinstance(status, str):
            raise BaselineReportError("result stat_line_json status must be a string")
        normalized = status.strip().upper().replace(" ", "_")
        if normalized in _INACTIVE_RESULT_STATUSES:
            signals.add(False)
        elif normalized == "ACTIVE":
            signals.add(True)
    if len(signals) > 1:
        raise BaselineReportError("result stat_line_json contains conflicting activity signals")
    return next(iter(signals)) if signals else None


def _inactive_salary_status(value: str | None) -> bool:
    if value is None:
        return False
    normalized = value.strip().upper().replace(" ", "_")
    return normalized in _INACTIVE_SALARY_STATUSES


def _position(value: object) -> str:
    if value is None:
        return "UNKNOWN"
    normalized = str(value).strip().upper()
    if normalized in {"D", "DEF"}:
        return "DST"
    return normalized or "UNKNOWN"


def _metric(value: float | None, status: MetricStatus) -> str:
    if status == "insufficient_n":
        return "insufficient_n"
    if value is None:
        raise BaselineReportError("available metric unexpectedly has no value")
    return f"{value:.6f}"


def _spearman(cell: BaselineEvaluationCell) -> str:
    if cell.spearman_status == "insufficient_n":
        return "insufficient_n"
    if cell.spearman_status == "undefined_constant_rank":
        return "undefined_constant_rank"
    value = cell.spearman_rank_correlation
    if value is None:
        raise BaselineReportError("available Spearman metric unexpectedly has no value")
    return f"{value:.6f}"


def _pit_counts(shape: BaselineShapeMetrics, status: MetricStatus) -> str:
    if status == "insufficient_n":
        return "insufficient_n"
    return json.dumps(shape.pit_bin_counts, separators=(",", ":"))


def _json_strings(values: tuple[str, ...]) -> str:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _manifest_artifacts(
    snapshot: DecisionSnapshotRow, artifact_kind: Literal["salary", "projection"]
) -> frozenset[tuple[str, str]]:
    missing_sources = tuple(
        item.sha256
        for item in snapshot.manifest_hashes_json
        if item.artifact_kind == artifact_kind and (item.source is None or not item.source.strip())
    )
    if missing_sources:
        raise BaselineReportError(
            f"decision manifest {artifact_kind} artifacts have no source: "
            + ", ".join(sorted(missing_sources))
        )
    artifacts = frozenset(
        (str(item.source), item.sha256)
        for item in snapshot.manifest_hashes_json
        if item.artifact_kind == artifact_kind
    )
    if not artifacts:
        raise BaselineReportError(f"decision manifest has no {artifact_kind} artifacts")
    return artifacts


def _artifact_filter(
    hash_column: str,
    source_column: str,
    prefix: str,
    artifacts: frozenset[tuple[str, str]],
) -> tuple[str, dict[str, object]]:
    parameters: dict[str, object] = {}
    predicates: list[str] = []
    for index, (source, sha256) in enumerate(sorted(artifacts)):
        source_key = f"{prefix}_source_{index}"
        hash_key = f"{prefix}_hash_{index}"
        parameters[source_key] = source
        parameters[hash_key] = sha256
        predicates.append(f"({source_column} = :{source_key} AND {hash_column} = :{hash_key})")
    return "AND (" + " OR ".join(predicates) + ")", parameters
