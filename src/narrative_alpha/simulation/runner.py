"""Point-in-time orchestration and deterministic text persistence for simulations."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import numpy as np

from narrative_alpha.contests import load_contest_payouts
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.portfolio import OptimizationRequest
from narrative_alpha.replay import PointInTimeSession, read_frozen_decision
from narrative_alpha.report_cli import DEFAULT_REPORT_DIRECTORY, write_report_atomic
from narrative_alpha.simulation.config import (
    DEFAULT_SIMULATION_CONFIG_PATH,
    SimulationConfig,
    load_simulation_config,
)
from narrative_alpha.simulation.evaluation import evaluate_contest
from narrative_alpha.simulation.field import FieldGenerationResult, generate_field
from narrative_alpha.simulation.models import (
    EXPERIMENTAL_NOTICE,
    LineupSimulationResult,
    OwnershipMarginal,
    PlayerSimulationInput,
    PortfolioSimulationResult,
    SimulationReport,
    SimulationRunResult,
)
from narrative_alpha.simulation.outcomes import draw_player_outcomes
from narrative_alpha.store import ContestPayoutRow, ContestRow, PlayerDistributionRow

#: The largest contest field the settlement loop finishes on a Sunday (measured at
#: roughly 25 ms per draw per 5,000 entries); a Slice 43b vectorization raises it.
MAX_SIMULATED_FIELD_SIZE = 10_000


class SimulationRunError(RuntimeError):
    """Raised when a frozen decision lacks any required point-in-time input."""


def run_simulation(
    connection: sqlite3.Connection,
    *,
    decision_snapshot_id: str,
    contest_external_id: str,
    artifact_root: Path,
    report_directory: Path = DEFAULT_REPORT_DIRECTORY,
    config_path: Path = DEFAULT_SIMULATION_CONFIG_PATH,
    draws: int | None = None,
    seed: int | None = None,
    independent: bool = False,
    run_at: datetime | None = None,
) -> SimulationRunResult:
    """Simulate one frozen decision, write one report, and append its run record."""

    config = load_simulation_config(config_path)
    draw_count = config.default_draws if draws is None else draws
    selected_seed = config.default_seed if seed is None else seed
    if draw_count < 1:
        raise SimulationRunError("draws must be positive")
    if selected_seed < 0:
        raise SimulationRunError("seed must be non-negative")

    raw_snapshot = connection.execute(
        "SELECT * FROM decision_snapshots WHERE decision_snapshot_id = ?",
        (decision_snapshot_id,),
    ).fetchone()
    if raw_snapshot is None:
        raise SimulationRunError(f"decision snapshot {decision_snapshot_id!r} does not exist")
    from narrative_alpha.store import DecisionSnapshotRow

    snapshot = DecisionSnapshotRow.from_db(raw_snapshot)
    cutoff = snapshot.decision_at
    frozen = read_frozen_decision(
        connection,
        decision_snapshot_id=decision_snapshot_id,
        decision_at=cutoff,
        artifact_root=artifact_root.resolve(),
    )
    if frozen.request.slate_type.value != "classic":
        raise SimulationRunError("first-season simulation supports classic nine-player slates only")
    slate = PointInTimeSession(connection).slate(snapshot.slate_id, as_of=cutoff)
    contest = load_contest_for_decision(
        connection,
        external_contest_id=contest_external_id,
        slate_id=snapshot.slate_id,
        site=frozen.request.site.value,
        as_of=cutoff,
    )
    payouts = _load_payouts_for_decision(connection, contest=contest, as_of=cutoff)
    simulation_players = load_player_distributions_for_decision(
        connection,
        request=frozen.request,
        as_of=cutoff,
        projection_artifacts={
            (str(item.source), item.sha256)
            for item in snapshot.manifest_hashes_json
            if item.artifact_kind == "projection" and item.source is not None
        },
    )
    ownership_source, scenario_run_id, ownership = load_ownership_for_decision(
        connection,
        decision_snapshot_id=decision_snapshot_id,
        request=frozen.request,
        as_of=cutoff,
    )

    if len(frozen.lineups) > contest.entry_limit:
        raise SimulationRunError(
            f"the decision carries {len(frozen.lineups)} lineups but contest "
            f"{contest.external_contest_id} allows {contest.entry_limit} entr"
            f"{'y' if contest.entry_limit == 1 else 'ies'}; a simulated ROI for entries the "
            "contest would not accept is not a number"
        )
    if contest.field_size > MAX_SIMULATED_FIELD_SIZE:
        # Settlement is a Python loop over entries and payout bands per draw; above this
        # size it does not finish on a Sunday. Refusing states the limit rather than
        # simulating a smaller field and calling it the contest.
        raise SimulationRunError(
            f"contest {contest.external_contest_id} has field_size {contest.field_size}, "
            f"above the {MAX_SIMULATED_FIELD_SIZE} this simulator can settle in useful time; "
            "the shadow simulation is not run for it"
        )
    opponent_count = contest.field_size - len(frozen.lineups)
    if opponent_count < 1:
        raise SimulationRunError(
            f"contest field_size {contest.field_size} leaves no opponent field after "
            f"the decision's {len(frozen.lineups)} portfolio entries"
        )
    seed_sequence = np.random.SeedSequence(selected_seed)
    field_seed, outcome_seed = seed_sequence.spawn(2)
    field = generate_field(
        frozen.request,
        ownership,
        lineup_count=opponent_count,
        rng=np.random.default_rng(field_seed),
        config=config.field,
    )
    outcomes = draw_player_outcomes(
        simulation_players,
        draws=draw_count,
        rng=np.random.default_rng(outcome_seed),
        dependence=config.dependence,
        independent=independent,
    )
    evaluation = evaluate_contest(
        outcomes,
        player_ids=tuple(item.player.player_id for item in simulation_players),
        portfolio_lineups=frozen.lineups,
        field_lineups=field.lineups,
        payout_bands=payouts,
        entry_fee_cents=contest.entry_fee_cents,
        score_quantiles=config.calibration.score_quantiles,
        score_sample_limit=config.calibration.score_sample_limit,
    )
    report = _report(
        config=config,
        request=frozen.request,
        contest=contest,
        season=slate.season,
        week=slate.week,
        decision_snapshot_id=decision_snapshot_id,
        draws=draw_count,
        seed=selected_seed,
        independent=independent,
        ownership_source=ownership_source,
        scenario_run_id=scenario_run_id,
        field=field,
        lineup_results=evaluation.lineup_results,
        portfolio_result=evaluation.portfolio_result,
        score_quantiles=evaluation.score_quantiles,
        field_duplication_distribution=_field_duplication_distribution(field.lineups),
    )
    report_bytes = render_simulation_report(report).encode("utf-8")
    stamp = ensure_utc(run_at or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%fZ")
    report_path = simulation_report_path(
        report_directory,
        season=slate.season,
        week=slate.week,
        decision_snapshot_id=decision_snapshot_id,
        stamp=stamp,
    )
    write_report_atomic(report_path, report_bytes.decode("utf-8"))
    run_id = _record_run(
        connection,
        report=report,
        report_path=report_path,
        report_bytes=report_bytes,
        created_at=ensure_utc(run_at or datetime.now(UTC)),
    )
    return SimulationRunResult(
        report=report,
        report_path=report_path,
        report_bytes=report_bytes,
        simulation_run_id=run_id,
    )


def load_contest_for_decision(
    connection: sqlite3.Connection,
    *,
    external_contest_id: str,
    slate_id: int,
    site: str,
    as_of: datetime,
) -> ContestRow:
    stamp = utc_timestamp(as_of)
    rows = connection.execute(
        """
        SELECT * FROM contests
        WHERE external_contest_id = ? AND slate_id = ? AND site = ?
          AND rtrim(observed_at, 'Z') <= rtrim(?, 'Z')
          AND rtrim(ingested_at, 'Z') <= rtrim(?, 'Z')
          AND rtrim(valid_from, 'Z') <= rtrim(?, 'Z')
          AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(?, 'Z'))
        ORDER BY rtrim(observed_at, 'Z') DESC, contest_id DESC
        LIMIT 1
        """,
        (external_contest_id, slate_id, site, stamp, stamp, stamp, stamp),
    ).fetchall()
    if not rows:
        raise SimulationRunError(
            f"contest {external_contest_id!r} was not available for slate {slate_id} as of {stamp}"
        )
    contest = ContestRow.from_db(rows[0])
    if contest.payout_curve_id is None:
        raise SimulationRunError(f"contest {external_contest_id!r} has no payout_curve_id")
    return contest


def load_player_distributions_for_decision(
    connection: sqlite3.Connection,
    *,
    request: OptimizationRequest,
    as_of: datetime,
    projection_artifacts: set[tuple[str, str]],
) -> tuple[PlayerSimulationInput, ...]:
    """Select one newest eligible stored marginal per frozen candidate; never impute."""

    stamp = utc_timestamp(as_of)
    rows = connection.execute(
        """
        SELECT * FROM player_distributions
        WHERE slate_id = ?
          AND rtrim(as_of_at, 'Z') <= rtrim(?, 'Z')
          AND rtrim(observed_at, 'Z') <= rtrim(?, 'Z')
          AND rtrim(ingested_at, 'Z') <= rtrim(?, 'Z')
          AND rtrim(valid_from, 'Z') <= rtrim(?, 'Z')
          AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(?, 'Z'))
        ORDER BY player_id, rtrim(as_of_at, 'Z') DESC,
                 rtrim(observed_at, 'Z') DESC, player_distribution_id DESC
        """,
        (request.slate_id, stamp, stamp, stamp, stamp, stamp),
    ).fetchall()
    eligible: dict[int, list[PlayerDistributionRow]] = defaultdict(list)
    for row in rows:
        restored = PlayerDistributionRow.from_db(row)
        if all(
            (reference.source, reference.source_file_sha256) in projection_artifacts
            for reference in restored.source_set_json
        ):
            eligible[restored.player_id].append(restored)

    output: list[PlayerSimulationInput] = []
    missing: list[int] = []
    from narrative_alpha.quant import PlayerOutcomeDistribution

    for player in request.candidate_player_scenario.players:
        matches = eligible.get(player.player_id, [])
        if not matches:
            missing.append(player.player_id)
            continue
        row = matches[0]
        output.append(
            PlayerSimulationInput(
                player=player,
                distribution=PlayerOutcomeDistribution(
                    distribution_family=row.distribution_family,
                    p_active=row.p_active,
                    p_full_role_given_active=row.p_full_role_given_active,
                    conditional_location=row.conditional_location,
                    conditional_scale=row.conditional_scale,
                    conditional_shape=row.conditional_shape,
                ),
                player_distribution_id=row.player_distribution_id,
                distribution_source=row.source,
            )
        )
    if missing:
        listed = ", ".join(str(value) for value in missing[:20])
        suffix = "" if len(missing) <= 20 else f", +{len(missing) - 20} more"
        raise SimulationRunError(
            "no point-in-time player_distribution tied to the decision's projection "
            f"artifacts for player IDs: {listed}{suffix}; simulation refuses to impute"
        )
    return tuple(output)


def load_ownership_for_decision(
    connection: sqlite3.Connection,
    *,
    decision_snapshot_id: str,
    request: OptimizationRequest,
    as_of: datetime,
) -> tuple[Literal["scenario_model", "vendor_baseline"], str | None, dict[int, float]]:
    """Read the recorded Stage 4 route, then only the source it says was applied."""

    route = connection.execute(
        "SELECT * FROM decision_ownership_routing WHERE decision_snapshot_id = ?",
        (decision_snapshot_id,),
    ).fetchone()
    if route is None:
        raise SimulationRunError(
            "decision has no decision_ownership_routing row; ownership must not be recomputed"
        )
    player_ids = {player.player_id for player in request.candidate_player_scenario.players}
    stamp = utc_timestamp(as_of)
    if bool(route["applied"]):
        run_id = str(route["scenario_run_id"])
        rows = connection.execute(
            """
            SELECT player_id, applied_ownership FROM ownership_scenarios
            WHERE run_id = ? AND role = 'classic'
              AND rtrim(observed_at, 'Z') <= rtrim(?, 'Z')
              AND rtrim(created_at, 'Z') <= rtrim(?, 'Z')
            ORDER BY player_id
            """,
            (run_id, stamp, stamp),
        ).fetchall()
        values = {int(row["player_id"]): float(row["applied_ownership"]) for row in rows}
        _require_complete_ownership(values, player_ids, "routed applied ownership")
        return "scenario_model", run_id, values

    rows = connection.execute(
        """
        WITH ranked AS (
            SELECT ob.*,
                   row_number() OVER (
                       PARTITION BY ob.source, ob.player_id
                       ORDER BY rtrim(ob.observed_at, 'Z') DESC,
                                ob.ownership_baseline_id DESC
                   ) AS version_rank
            FROM ownership_baselines AS ob
            WHERE ob.slate_id = ? AND ob.site = ? AND ob.role = 'classic'
              AND rtrim(ob.observed_at, 'Z') <= rtrim(?, 'Z')
              AND rtrim(ob.ingested_at, 'Z') <= rtrim(?, 'Z')
              AND rtrim(ob.valid_from, 'Z') <= rtrim(?, 'Z')
              AND (ob.valid_to IS NULL OR rtrim(ob.valid_to, 'Z') > rtrim(?, 'Z'))
        )
        SELECT player_id, ownership FROM ranked WHERE version_rank = 1
        ORDER BY player_id, source
        """,
        (request.slate_id, request.site.value, stamp, stamp, stamp, stamp),
    ).fetchall()
    grouped: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        grouped[int(row["player_id"])].append(float(row["ownership"]))
    values = {
        player_id: sum(items) / len(items)
        for player_id, items in grouped.items()
        if player_id in player_ids
    }
    _require_complete_ownership(values, player_ids, "vendor ownership baseline")
    return "vendor_baseline", None, values


def render_simulation_report(report: SimulationReport) -> str:
    """Render stable bytes: no wall-clock value or database identity enters the body."""

    output = io.StringIO(newline="")
    if report.notice is not None:
        output.write(report.notice + "\n")
    output.write("NARRATIVE ALPHA CONTEST SIMULATION\n")
    output.write(f"decision_snapshot_id={report.decision_snapshot_id}\n")
    output.write(f"contest_external_id={report.contest_external_id}\n")
    output.write(f"site={report.site}\n")
    output.write(f"season={report.season}\n")
    output.write(f"week={report.week:02d}\n")
    output.write(f"draws={report.draws}\n")
    output.write(f"seed={report.seed}\n")
    output.write(f"independent={str(report.independent).lower()}\n")
    output.write(f"config_version={report.config_version}\n")
    output.write(f"config_sha256={report.config_sha256}\n")
    output.write("assumption_status=first-season assumptions\n")
    output.write(f"game_factor_loading={report.game_factor_loading:.6f}\n")
    output.write(f"team_factor_loading={report.team_factor_loading:.6f}\n")
    output.write(
        f"within_position_negative_loading={report.within_position_negative_loading:.6f}\n"
    )
    output.write(f"configured_stack_rate={report.configured_stack_rate:.6f}\n")
    output.write(f"ownership_source={report.ownership_source}\n")
    output.write(f"ownership_scenario_run_id={report.ownership_scenario_run_id or ''}\n")
    output.write(f"field_lineup_count={report.field_lineup_count}\n")
    output.write(f"field_stack_rate={report.field_stack_rate:.6f}\n")
    output.write(f"ownership_tolerance={report.ownership_tolerance:.6f}\n")

    writer = csv.writer(output, lineterminator="\n")
    output.write("\nOWNERSHIP MARGINALS\n")
    writer.writerow(
        (
            "player_id",
            "name",
            "position",
            "source_target",
            "calibrated_target",
            "achieved",
            "absolute_error",
        )
    )
    for marginal_row in report.ownership_marginals:
        writer.writerow(
            (
                marginal_row.player_id,
                marginal_row.name,
                marginal_row.position,
                f"{marginal_row.source_target:.6f}",
                f"{marginal_row.calibrated_target:.6f}",
                f"{marginal_row.achieved:.6f}",
                f"{marginal_row.absolute_error:.6f}",
            )
        )

    output.write("\nPORTFOLIO LINEUPS\n")
    if report.notice is not None:
        output.write(report.notice + "\n")
    writer.writerow(
        (
            "lineup_id",
            "expected_payout_cents",
            "expected_roi",
            "cash_probability",
            "top_1_percent_probability",
            "duplication_distribution",
            "downside_p5_payout_cents",
        )
    )
    for lineup_result in report.lineup_results:
        writer.writerow(_metric_cells(lineup_result, lineup_id=lineup_result.lineup_id))

    portfolio = report.portfolio_result
    output.write("\nPORTFOLIO\n")
    if report.notice is not None:
        output.write(report.notice + "\n")
    writer.writerow(
        (
            "expected_payout_cents",
            "expected_roi",
            "cash_probability",
            "top_1_percent_probability",
            "duplication_distribution",
            "downside_p5_payout_cents",
            "mean_pairwise_outcome_correlation",
        )
    )
    writer.writerow(
        (
            f"{portfolio.expected_payout_cents:.6f}",
            _optional_float(portfolio.expected_roi),
            f"{portfolio.cash_probability:.6f}",
            f"{portfolio.top_one_percent_probability:.6f}",
            _duplication(portfolio.duplication_distribution),
            f"{portfolio.downside_p5_payout_cents:.6f}",
            _optional_float(portfolio.mean_pairwise_outcome_correlation),
        )
    )
    output.write("\nPORTFOLIO OUTCOME CORRELATION\n")
    if report.notice is not None:
        output.write(report.notice + "\n")
    writer.writerow(("lineup_id", *(row.lineup_id for row in report.lineup_results)))
    for lineup, correlations in zip(
        report.lineup_results, portfolio.outcome_correlation_matrix, strict=True
    ):
        writer.writerow((lineup.lineup_id, *(f"{value:.6f}" for value in correlations)))
    output.write("\nSIMULATED FIELD SCORE QUANTILES\n")
    writer.writerow(("quantile", "score"))
    for quantile, score in report.simulated_score_quantiles:
        writer.writerow((f"{quantile:.6f}", f"{score:.6f}"))
    output.write("\nSIMULATED FIELD DUPLICATION DISTRIBUTION\n")
    writer.writerow(("duplicates_excluding_self", "probability"))
    for duplicates, probability in report.simulated_field_duplication_distribution:
        writer.writerow((duplicates, f"{probability:.6f}"))
    return output.getvalue()


def simulation_report_path(
    directory: Path,
    *,
    season: int,
    week: int,
    decision_snapshot_id: str,
    stamp: str,
) -> Path:
    safe_decision = re.sub(r"[^A-Za-z0-9._-]+", "_", decision_snapshot_id).strip("._")
    return (
        directory
        / str(season)
        / f"week_{week:02d}"
        / f"simulation-{safe_decision or 'decision'}-{stamp}.txt"
    )


def _load_payouts_for_decision(
    connection: sqlite3.Connection, *, contest: ContestRow, as_of: datetime
) -> tuple[ContestPayoutRow, ...]:
    assert contest.payout_curve_id is not None
    stamp = utc_timestamp(as_of)
    version = connection.execute(
        """
        SELECT max(observed_at) AS observed_at FROM contest_payouts
        WHERE payout_curve_id = ?
          AND rtrim(observed_at, 'Z') <= rtrim(?, 'Z')
          AND rtrim(ingested_at, 'Z') <= rtrim(?, 'Z')
          AND rtrim(valid_from, 'Z') <= rtrim(?, 'Z')
          AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(?, 'Z'))
        """,
        (contest.payout_curve_id, stamp, stamp, stamp, stamp),
    ).fetchone()
    if version is None or version["observed_at"] is None:
        raise SimulationRunError(
            f"contest payout curve {contest.payout_curve_id!r} was unavailable at the decision"
        )
    version_at = datetime.fromisoformat(str(version["observed_at"]).replace("Z", "+00:00"))
    payouts = load_contest_payouts(
        connection, payout_curve_id=contest.payout_curve_id, as_of=version_at
    )
    if not payouts or any(
        payout.ingested_at > as_of
        or payout.valid_from > as_of
        or (payout.valid_to is not None and payout.valid_to <= as_of)
        for payout in payouts
    ):
        raise SimulationRunError(
            f"contest payout curve {contest.payout_curve_id!r} has no complete "
            "point-in-time version"
        )
    return payouts


def _report(
    *,
    config: SimulationConfig,
    request: OptimizationRequest,
    contest: ContestRow,
    season: int,
    week: int,
    decision_snapshot_id: str,
    draws: int,
    seed: int,
    independent: bool,
    ownership_source: Literal["scenario_model", "vendor_baseline"],
    scenario_run_id: str | None,
    field: FieldGenerationResult,
    lineup_results: tuple[LineupSimulationResult, ...],
    portfolio_result: PortfolioSimulationResult,
    score_quantiles: tuple[tuple[float, float], ...],
    field_duplication_distribution: tuple[tuple[int, float], ...],
) -> SimulationReport:
    players = {player.player_id: player for player in request.candidate_player_scenario.players}
    marginals = tuple(
        OwnershipMarginal(
            player_id=player_id,
            name=players[player_id].name,
            position=players[player_id].position,
            source_target=field.source_targets[player_id],
            calibrated_target=target,
            achieved=field.achieved_marginals[player_id],
            absolute_error=abs(field.achieved_marginals[player_id] - target),
        )
        for player_id, target in sorted(field.calibrated_targets.items())
    )
    return SimulationReport(
        notice=None if config.calibrated_against_real_contest else EXPERIMENTAL_NOTICE,
        decision_snapshot_id=decision_snapshot_id,
        contest_external_id=contest.external_contest_id,
        contest_id=contest.contest_id,
        site=request.site.value,
        season=season,
        week=week,
        draws=draws,
        seed=seed,
        independent=independent,
        config_version=config.config_version,
        config_sha256=config.sha256,
        game_factor_loading=config.dependence.game_loading,
        team_factor_loading=config.dependence.team_loading,
        within_position_negative_loading=(config.dependence.within_position_negative_loading),
        configured_stack_rate=config.field.stack_rate,
        ownership_source=ownership_source,
        ownership_scenario_run_id=scenario_run_id,
        field_lineup_count=len(field.lineups),
        field_stack_rate=field.stack_rate,
        ownership_tolerance=config.field.ownership_tolerance,
        ownership_marginals=marginals,
        lineup_results=lineup_results,
        portfolio_result=portfolio_result,
        simulated_score_quantiles=score_quantiles,
        simulated_field_duplication_distribution=field_duplication_distribution,
    )


def _record_run(
    connection: sqlite3.Connection,
    *,
    report: SimulationReport,
    report_path: Path,
    report_bytes: bytes,
    created_at: datetime,
) -> int:
    metrics = json.dumps(report.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    cursor = connection.execute(
        """
        INSERT INTO simulation_runs(
            decision_snapshot_id, contest_id, created_at, report_path, report_sha256,
            config_version, config_sha256, draw_count, seed, independent,
            ownership_source, ownership_scenario_run_id, metrics_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            report.decision_snapshot_id,
            report.contest_id,
            utc_timestamp(created_at),
            str(report_path.resolve()),
            hashlib.sha256(report_bytes).hexdigest(),
            report.config_version,
            report.config_sha256,
            report.draws,
            report.seed,
            int(report.independent),
            report.ownership_source,
            report.ownership_scenario_run_id,
            metrics,
        ),
    )
    if cursor.lastrowid is None:
        raise sqlite3.DatabaseError("simulation_runs insert returned no row id")
    return int(cursor.lastrowid)


def _require_complete_ownership(
    values: Mapping[int, float], expected_ids: set[int], label: str
) -> None:
    missing = sorted(expected_ids - set(values))
    extra = sorted(set(values) - expected_ids)
    if missing or extra:
        raise SimulationRunError(
            f"{label} does not exactly cover the decision candidates; "
            f"missing={missing}, extra={extra}"
        )


def _metric_cells(row: object, *, lineup_id: str) -> tuple[object, ...]:
    from narrative_alpha.simulation.models import LineupSimulationResult

    metric = LineupSimulationResult.model_validate(row)
    return (
        lineup_id,
        f"{metric.expected_payout_cents:.6f}",
        _optional_float(metric.expected_roi),
        f"{metric.cash_probability:.6f}",
        f"{metric.top_one_percent_probability:.6f}",
        _duplication(metric.duplication_distribution),
        f"{metric.downside_p5_payout_cents:.6f}",
    )


def _optional_float(value: float | None) -> str:
    return "undefined" if value is None else f"{value:.6f}"


def _duplication(values: tuple[tuple[int, float], ...]) -> str:
    return ";".join(f"{count}:{probability:.6f}" for count, probability in values)


def _field_duplication_distribution(
    lineups: Sequence[Sequence[int]],
) -> tuple[tuple[int, float], ...]:
    from collections import Counter

    counts = Counter(tuple(lineup) for lineup in lineups)
    frequencies = Counter(counts[tuple(lineup)] - 1 for lineup in lineups)
    total = len(lineups)
    return tuple((duplicates, count / total) for duplicates, count in sorted(frequencies.items()))
