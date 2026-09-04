from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import NormalDist
from typing import Any

import pytest

from narrative_alpha.evaluation import (
    SHAPE_STALE_NOTICE,
    SHAPE_UNAVAILABLE_NOTICE,
    BaselineReportError,
    BaselineThresholds,
    build_baseline_report,
    render_baseline_report,
)
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.quant import (
    QuantileInterpretation,
    fit_player_distribution_with_diagnostics,
)
from narrative_alpha.store import (
    DecisionManifestHash,
    DecisionSnapshotRow,
    PlayerDistributionCreate,
    PlayerDistributionSourceRef,
    apply_migrations,
    connect_database,
    insert_player_distribution,
    manifest_hash_set_sha256,
)

DATA_AT = datetime(2026, 9, 13, 12, tzinfo=UTC)
DECISION_AT = datetime(2026, 9, 13, 16, 55, tzinfo=UTC)
EVALUATION_AT = datetime(2026, 9, 14, 12, tzinfo=UTC)
SALARY_HASH = "a" * 64
PROJECTION_A_HASH = "b" * 64
PROJECTION_B_HASH = "c" * 64
RESULT_A_HASH = "d" * 64
RESULT_B_HASH = "e" * 64
NON_MANIFEST_HASH = "f" * 64
REQUEST_HASH = "1" * 64
LINEUP_HASH = "2" * 64
SNAPSHOT_ID = "decision-baseline-fixture"
GOLDEN = Path(__file__).with_name("golden") / "baseline_report.txt"


def test_accounting_hand_metrics_shape_unavailable_and_golden(tmp_path: Path) -> None:
    database = tmp_path / "baseline.sqlite3"
    _seed_baseline(database)

    with connect_database(database) as connection:
        report = build_baseline_report(
            connection,
            decision_snapshot_id=SNAPSHOT_ID,
            decision_at=DECISION_AT,
            evaluation_as_of=EVALUATION_AT,
            thresholds=BaselineThresholds(minimum_sample_size=3),
        )

    wr = _cell(report, "WR")
    assert wr.n_scored == 3
    assert wr.n_projected_without_result == 1
    assert wr.n_result_without_projection == 1
    assert wr.n_projected_but_inactive == 1
    assert wr.n_projected_partial_source_coverage == 0
    assert wr.signed_mean_error_bias == pytest.approx(0.0)
    assert wr.mae == pytest.approx(40 / 3)
    assert wr.rmse == pytest.approx(math.sqrt(200))
    assert wr.spearman_rank_correlation == pytest.approx(-0.5)

    # A stored active zero is scored, never inferred inactive from its value.
    te = _cell(report, "TE")
    assert te.n_scored == 1
    assert te.n_projected_but_inactive == 0
    assert te.metric_status == "insufficient_n"

    # Explicit result metadata is the other supported inactive-evidence path.
    rb = _cell(report, "RB")
    assert rb.n_scored == 0
    assert rb.n_projected_but_inactive == 1
    qb = _cell(report, "QB")
    assert qb.n_scored == 1
    assert qb.n_scored_zero_activity_unknown == 1
    assert qb.n_salary_without_projection_or_result == 1
    assert report.salary_population_n == 10
    assert report.shape_channel_notice == SHAPE_UNAVAILABLE_NOTICE
    assert report.projection_sources == ("fixture-a", "fixture-b")
    assert report.projection_file_hashes == (
        PROJECTION_A_HASH,
        PROJECTION_B_HASH,
    )
    assert render_baseline_report(report) == GOLDEN.read_text(encoding="utf-8")


def test_unknown_activity_zero_is_scored_without_inactive_inference(
    tmp_path: Path,
) -> None:
    database = tmp_path / "unknown-zero.sqlite3"
    _seed_baseline(database)
    with connect_database(database) as connection:
        report = build_baseline_report(
            connection,
            decision_snapshot_id=SNAPSHOT_ID,
            decision_at=DECISION_AT,
            evaluation_as_of=EVALUATION_AT,
            thresholds=BaselineThresholds(minimum_sample_size=1),
        )

    qb = _cell(report, "QB")
    assert qb.n_scored == 1
    assert qb.n_projected_but_inactive == 0
    assert qb.n_scored_zero_activity_unknown == 1
    assert qb.signed_mean_error_bias == pytest.approx(5.0)
    assert qb.mae == pytest.approx(5.0)
    assert qb.rmse == pytest.approx(5.0)
    assert qb.spearman_status == "insufficient_n"


def test_post_cutoff_and_non_manifest_revisions_do_not_move_report(
    tmp_path: Path,
) -> None:
    database = tmp_path / "lookahead.sqlite3"
    _seed_baseline(database)
    with connect_database(database) as connection:
        before = build_baseline_report(
            connection,
            decision_snapshot_id=SNAPSHOT_ID,
            decision_at=DECISION_AT,
            evaluation_as_of=EVALUATION_AT,
            thresholds=BaselineThresholds(minimum_sample_size=3),
        )
        _insert_projection(
            connection,
            projection_id=900,
            player_id=1,
            source="fixture-a",
            source_hash=PROJECTION_A_HASH,
            mean=999.0,
            observed_at=DECISION_AT + timedelta(microseconds=1),
        )
        # A later-ingested backfill can claim a pre-cutoff observed_at. The immutable
        # decision manifest, not the claimed timestamp alone, keeps it out.
        _insert_projection(
            connection,
            projection_id=901,
            player_id=1,
            source="fixture-a",
            source_hash=NON_MANIFEST_HASH,
            mean=777.0,
            observed_at=datetime(2026, 9, 13, 13, tzinfo=UTC),
        )
        # Hash identity is not enough: source and hash must match the frozen pair.
        _insert_projection(
            connection,
            projection_id=905,
            player_id=1,
            source="impostor",
            source_hash=PROJECTION_A_HASH,
            mean=666.0,
            observed_at=DATA_AT,
        )
        _insert_result(
            connection,
            result_id=902,
            player_id=1,
            points=888.0,
            source="results-a",
            source_hash=RESULT_A_HASH,
            observed_at=EVALUATION_AT + timedelta(microseconds=1),
        )
        after = build_baseline_report(
            connection,
            decision_snapshot_id=SNAPSHOT_ID,
            decision_at=DECISION_AT,
            evaluation_as_of=EVALUATION_AT,
            thresholds=BaselineThresholds(minimum_sample_size=3),
        )

    assert after == before
    assert render_baseline_report(after) == render_baseline_report(before)


def test_exact_cutoff_is_included_and_legacy_compact_timestamp_is_read(
    tmp_path: Path,
) -> None:
    database = tmp_path / "exact-cutoff.sqlite3"
    _seed_baseline(database)
    with connect_database(database) as connection:
        connection.execute(
            "UPDATE decision_snapshots SET decision_at = ? WHERE decision_snapshot_id = ?",
            ("2026-09-13T16:55:00Z", SNAPSHOT_ID),
        )
        _insert_projection(
            connection,
            projection_id=899,
            player_id=1,
            source="fixture-a",
            source_hash=PROJECTION_A_HASH,
            mean=13.0,
            observed_at=DECISION_AT,
        )
        report = build_baseline_report(
            connection,
            decision_snapshot_id=SNAPSHOT_ID,
            decision_at=DECISION_AT,
            evaluation_as_of=EVALUATION_AT,
            thresholds=BaselineThresholds(minimum_sample_size=3),
        )

    assert _cell(report, "WR").signed_mean_error_bias == pytest.approx(2 / 3)


def test_microsecond_version_ordering_beats_legacy_compact_timestamp(
    tmp_path: Path,
) -> None:
    database = tmp_path / "microsecond-order.sqlite3"
    _seed_baseline(database)
    with connect_database(database) as connection:
        connection.execute(
            """
            UPDATE projection_snapshots
            SET observed_at = ?, ingested_at = ?, valid_from = ?
            WHERE projection_snapshot_id = 11
            """,
            ("2026-09-13T12:00:00Z",) * 3,
        )
        _insert_projection(
            connection,
            projection_id=898,
            player_id=1,
            source="fixture-a",
            source_hash=PROJECTION_A_HASH,
            mean=13.0,
            observed_at=DATA_AT + timedelta(microseconds=1),
        )
        report = build_baseline_report(
            connection,
            decision_snapshot_id=SNAPSHOT_ID,
            decision_at=DECISION_AT,
            evaluation_as_of=EVALUATION_AT,
            thresholds=BaselineThresholds(minimum_sample_size=3),
        )

    assert _cell(report, "WR").signed_mean_error_bias == pytest.approx(2 / 3)


def test_partial_projection_source_coverage_is_visible(tmp_path: Path) -> None:
    database = tmp_path / "partial-source.sqlite3"
    _seed_baseline(database)
    with connect_database(database) as connection:
        connection.execute("DELETE FROM projection_snapshots WHERE projection_snapshot_id = 42")
        report = build_baseline_report(
            connection,
            decision_snapshot_id=SNAPSHOT_ID,
            decision_at=DECISION_AT,
            evaluation_as_of=EVALUATION_AT,
        )

    assert _cell(report, "WR").n_projected_partial_source_coverage == 1


def test_below_threshold_renders_every_point_metric_as_insufficient_n(
    tmp_path: Path,
) -> None:
    database = tmp_path / "threshold.sqlite3"
    _seed_baseline(database)
    with connect_database(database) as connection:
        report = build_baseline_report(
            connection,
            decision_snapshot_id=SNAPSHOT_ID,
            decision_at=DECISION_AT,
            evaluation_as_of=EVALUATION_AT,
            thresholds=BaselineThresholds(minimum_sample_size=4),
        )

    wr = _cell(report, "WR")
    assert wr.metric_status == "insufficient_n"
    assert wr.spearman_status == "insufficient_n"
    assert wr.signed_mean_error_bias is None
    wr_line = next(
        line
        for line in render_baseline_report(report).splitlines()
        if line.startswith("2026,1,WR,")
    )
    assert wr_line.count("insufficient_n") == 4


def test_conflicting_result_sources_fail_loudly(tmp_path: Path) -> None:
    database = tmp_path / "conflict.sqlite3"
    _seed_baseline(database)
    with connect_database(database) as connection:
        _insert_result(
            connection,
            result_id=903,
            player_id=2,
            points=99.0,
            source="results-conflict",
            source_hash="9" * 64,
            observed_at=datetime(2026, 9, 14, 11, tzinfo=UTC),
        )
        with pytest.raises(BaselineReportError, match="conflicting result labels"):
            build_baseline_report(
                connection,
                decision_snapshot_id=SNAPSHOT_ID,
                decision_at=DECISION_AT,
                evaluation_as_of=EVALUATION_AT,
            )


def test_a_workload_stats_row_is_not_a_second_opinion_on_the_label(tmp_path: Path) -> None:
    """nflverse's PPR points beside the standings' DraftKings points are not a conflict:
    the workload row is a grading fact and the evaluation never reads it as a label."""

    from narrative_alpha.ingest.nflverse_stats import WORKLOAD_STATS_SOURCE

    database = tmp_path / "workload.sqlite3"
    _seed_baseline(database)
    with connect_database(database) as connection:
        _insert_result(
            connection,
            result_id=904,
            player_id=2,
            points=99.0,
            source=WORKLOAD_STATS_SOURCE,
            source_hash="8" * 64,
            observed_at=datetime(2026, 9, 14, 11, tzinfo=UTC),
            stat_line={"played": True, "snap_share": 0.8, "scoring": "nflverse:fantasy_points_ppr"},
        )
        report = build_baseline_report(
            connection,
            decision_snapshot_id=SNAPSHOT_ID,
            decision_at=DECISION_AT,
            evaluation_as_of=EVALUATION_AT,
        )
    assert report is not None


@pytest.mark.parametrize(
    "stat_line",
    (
        {"active": True, "inactive": True},
        {"active": True, "status": "OUT"},
    ),
)
def test_conflicting_activity_signals_fail_loudly(
    tmp_path: Path,
    stat_line: dict[str, object],
) -> None:
    database = tmp_path / "activity-conflict.sqlite3"
    _seed_baseline(database)
    with connect_database(database) as connection:
        _insert_result(
            connection,
            result_id=904,
            player_id=10,
            points=0.0,
            source="results-activity-conflict",
            source_hash="8" * 64,
            observed_at=datetime(2026, 9, 14, 11, tzinfo=UTC),
            stat_line=stat_line,
        )
        with pytest.raises(BaselineReportError, match="conflicting activity signals"):
            build_baseline_report(
                connection,
                decision_snapshot_id=SNAPSHOT_ID,
                decision_at=DECISION_AT,
                evaluation_as_of=EVALUATION_AT,
            )


def test_inactive_nonzero_result_fails_loudly(tmp_path: Path) -> None:
    database = tmp_path / "inactive-nonzero.sqlite3"
    _seed_baseline(database)
    with connect_database(database) as connection:
        _insert_result(
            connection,
            result_id=906,
            player_id=8,
            points=1.0,
            source="results-a",
            source_hash=RESULT_A_HASH,
            observed_at=datetime(2026, 9, 14, 11, tzinfo=UTC),
            stat_line={"active": False},
        )
        with pytest.raises(BaselineReportError, match=r"inactive result.*nonzero"):
            build_baseline_report(
                connection,
                decision_snapshot_id=SNAPSHOT_ID,
                decision_at=DECISION_AT,
                evaluation_as_of=EVALUATION_AT,
            )


def test_shape_scores_exact_selected_distribution_and_counts_off_support(
    tmp_path: Path,
) -> None:
    database = tmp_path / "shape.sqlite3"
    _seed_baseline(database, first_result=-1.0, include_projection_b=False)
    with connect_database(database) as connection:
        _insert_distribution(connection, player_id=1, projection_id=11, position="WR")
        _insert_distribution(connection, player_id=8, projection_id=81, position="RB")
        report = build_baseline_report(
            connection,
            decision_snapshot_id=SNAPSHOT_ID,
            decision_at=DECISION_AT,
            evaluation_as_of=EVALUATION_AT,
            thresholds=BaselineThresholds(minimum_sample_size=1, pit_bins=2),
        )

    shape = _cell(report, "WR").shape
    assert shape is not None
    assert shape.n_scored == 1
    assert shape.n_log_score_finite == 0
    assert shape.n_log_score_off_support == 1
    assert shape.n_negative_outcomes == 1
    assert shape.n_player_results_without_distribution == 2
    assert shape.mean_crps is not None
    assert shape.mean_log_score is None
    assert shape.log_score_status == "insufficient_n"
    assert sum(shape.pit_bin_counts) == 1

    # Shape scores target the unconditional inactive/active mixture. The explicit
    # inactive zero is excluded from point error but included in CRPS/log/PIT.
    rb = _cell(report, "RB")
    assert rb.n_projected_but_inactive == 1
    assert rb.n_scored == 0
    assert rb.shape is not None
    assert rb.shape.n_scored == 1
    assert rb.shape.n_log_score_finite == 1
    assert sum(rb.shape.pit_bin_counts) == 1


def test_source_specific_distribution_for_blend_is_reported_stale(
    tmp_path: Path,
) -> None:
    database = tmp_path / "stale-shape.sqlite3"
    _seed_baseline(database)
    with connect_database(database) as connection:
        _insert_distribution(connection, player_id=1, projection_id=11, position="WR")
        report = build_baseline_report(
            connection,
            decision_snapshot_id=SNAPSHOT_ID,
            decision_at=DECISION_AT,
            evaluation_as_of=EVALUATION_AT,
        )

    assert report.shape_channel_available is False
    assert report.shape_channel_notice == SHAPE_STALE_NOTICE
    assert report.distribution_rows_not_in_frozen_source_set == 1


def _seed_baseline(
    database: Path,
    *,
    first_result: float = 30.0,
    include_projection_b: bool = True,
) -> None:
    with connect_database(database) as connection:
        apply_migrations(connection)
        _insert(
            connection,
            "model_runs",
            {
                "run_id": "baseline-run",
                "run_type": "decision_build",
                "started_at": utc_timestamp(DECISION_AT),
                "completed_at": utc_timestamp(DECISION_AT),
                "status": "succeeded",
                "code_version": "test",
                "config_sha256": REQUEST_HASH,
                "parent_run_id": None,
                "error_message": None,
                "created_at": utc_timestamp(DECISION_AT),
            },
        )
        for team_id, team in ((1, "AAA"), (2, "BBB")):
            _insert(
                connection,
                "teams",
                {
                    "team_id": team_id,
                    "team_key": team,
                    "abbreviation": team,
                    "canonical_name": f"Team {team}",
                    "league": "NFL",
                    **_pit("fixture"),
                },
            )
        _insert(
            connection,
            "games",
            {
                "game_id": 1,
                "external_game_id": "game-1",
                "season": 2026,
                "week": 1,
                "kickoff_at": utc_timestamp(datetime(2026, 9, 13, 17, tzinfo=UTC)),
                "home_team_id": 1,
                "away_team_id": 2,
                "stadium_name": "Fixture Stadium",
                "game_status": "final",
                **_pit("fixture"),
            },
        )
        _insert(
            connection,
            "slates",
            {
                "slate_id": 1,
                "external_slate_id": "dk-main",
                "site": "draftkings",
                "slate_type": "classic",
                "season": 2026,
                "week": 1,
                "name": "Sunday Main",
                "starts_at": utc_timestamp(datetime(2026, 9, 13, 17, tzinfo=UTC)),
                "locks_at": utc_timestamp(datetime(2026, 9, 13, 17, tzinfo=UTC)),
                **_pit("fixture"),
            },
        )
        positions = (
            "WR",
            "WR",
            "WR",
            "WR",
            "WR",
            "WR",
            "TE",
            "RB",
            "QB",
            "QB",
        )
        for player_id, position in enumerate(positions, start=1):
            _insert(
                connection,
                "players",
                {
                    "player_id": player_id,
                    "player_key": f"player-{player_id}",
                    "canonical_name": f"Player {player_id}",
                    "position": position,
                    "birth_date": None,
                    **_pit("fixture"),
                },
            )
            _insert(
                connection,
                "salaries",
                {
                    "salary_id": player_id,
                    "slate_id": 1,
                    "player_id": player_id,
                    "game_id": 1,
                    "team_id": 1 if player_id % 2 else 2,
                    "opponent_team_id": 2 if player_id % 2 else 1,
                    "site_player_id": str(10_000 + player_id),
                    "roster_positions_json": json.dumps([position]),
                    "salary": 5_000,
                    "player_status": "OUT" if player_id == 6 else None,
                    "source_file_sha256": SALARY_HASH,
                    **_pit("draftkings"),
                },
            )

        means = {
            1: 10.0,
            2: 20.0,
            3: 30.0,
            4: 40.0,
            6: 15.0,
            7: 0.0,
            8: 12.0,
            10: 5.0,
        }
        for player_id, mean in means.items():
            projection_sources = [("fixture-a", PROJECTION_A_HASH, -1.0)]
            if include_projection_b:
                projection_sources.append(("fixture-b", PROJECTION_B_HASH, 1.0))
            for source_index, (source, source_hash, offset) in enumerate(projection_sources):
                source_mean = mean if mean == 0 else mean + offset
                floor, ceiling = _distribution_inputs(source_mean, player_id, source)
                _insert_projection(
                    connection,
                    projection_id=player_id * 10 + source_index + 1,
                    player_id=player_id,
                    source=source,
                    source_hash=source_hash,
                    mean=source_mean,
                    observed_at=DATA_AT,
                    floor=floor,
                    ceiling=ceiling,
                )

        for result_id, player_id, points, stat_line in (
            (1, 1, first_result, None),
            (2, 2, 10.0, None),
            (3, 3, 20.0, None),
            (4, 5, 5.0, None),
            (5, 7, 0.0, {"active": True}),
            (6, 8, 0.0, {"active": False}),
            (8, 10, 0.0, None),
        ):
            _insert_result(
                connection,
                result_id=result_id,
                player_id=player_id,
                points=points,
                source="results-a",
                source_hash=RESULT_A_HASH,
                observed_at=datetime(2026, 9, 14, 10, tzinfo=UTC),
                stat_line=stat_line,
            )
        # An identical label from a second source remains one scored player.
        _insert_result(
            connection,
            result_id=7,
            player_id=1,
            points=first_result,
            source="results-b",
            source_hash=RESULT_B_HASH,
            observed_at=datetime(2026, 9, 14, 11, tzinfo=UTC),
        )
        projection_manifest = [
            DecisionManifestHash(
                artifact_kind="projection",
                sha256=PROJECTION_A_HASH,
                path=f"store/projection/{PROJECTION_A_HASH}",
                source="fixture-a",
            )
        ]
        if include_projection_b:
            projection_manifest.append(
                DecisionManifestHash(
                    artifact_kind="projection",
                    sha256=PROJECTION_B_HASH,
                    path=f"store/projection/{PROJECTION_B_HASH}",
                    source="fixture-b",
                )
            )
        manifest = (
            DecisionManifestHash(
                artifact_kind="salary",
                sha256=SALARY_HASH,
                path=f"store/salary/{SALARY_HASH}",
                source="draftkings",
            ),
            *projection_manifest,
            DecisionManifestHash(
                artifact_kind="optimizer_request",
                sha256=REQUEST_HASH,
                path="decision/optimizer_request.json",
                source="narrative-alpha",
            ),
            DecisionManifestHash(
                artifact_kind="generated_lineups",
                sha256=LINEUP_HASH,
                path="decision/generated_lineups.csv",
                source="narrative-alpha",
            ),
        )
        snapshot = DecisionSnapshotRow(
            decision_snapshot_id=SNAPSHOT_ID,
            slate_id=1,
            decision_at=DECISION_AT,
            created_at=DECISION_AT,
            manifest_schema_version="1.0",
            manifest_hashes_json=manifest,
            manifest_hash_set_sha256=manifest_hash_set_sha256(manifest),
            run_id="baseline-run",
            note="fixture",
        )
        _insert(connection, "decision_snapshots", snapshot.db_values())


def _distribution_inputs(
    mean: float, player_id: int, source: str
) -> tuple[float | None, float | None]:
    if player_id not in {1, 8} or source != "fixture-a":
        return None, None
    interpretation = QuantileInterpretation(0.1, 0.9)
    shape = 0.4
    scale = mean / math.exp(0.5 * shape**2)
    normal = NormalDist()
    return (
        scale * math.exp(shape * normal.inv_cdf(interpretation.floor_quantile)),
        scale * math.exp(shape * normal.inv_cdf(interpretation.ceiling_quantile)),
    )


def _insert_distribution(
    connection: sqlite3.Connection,
    *,
    player_id: int,
    projection_id: int,
    position: str,
) -> None:
    projection = connection.execute(
        "SELECT * FROM projection_snapshots WHERE projection_snapshot_id = ?",
        (projection_id,),
    ).fetchone()
    assert projection is not None
    fit = fit_player_distribution_with_diagnostics(
        source="fixture-a",
        position=position,
        mean=float(projection["projection_mean"]),
        floor=float(projection["projection_floor"]),
        ceiling=float(projection["projection_ceiling"]),
        p_active=0.95,
        p_full_role_given_active=0.9,
        quantile_configuration={("fixture-a", position): QuantileInterpretation(0.1, 0.9)},
        tolerance=1e-10,
    )
    create = PlayerDistributionCreate(
        slate_id=1,
        player_id=player_id,
        source_set_json=(
            PlayerDistributionSourceRef(
                projection_snapshot_id=projection_id,
                source="fixture-a",
                source_file_sha256=PROJECTION_A_HASH,
            ),
        ),
        as_of_at=DECISION_AT,
        source="fixture-a",
        published_at=None,
        observed_at=DATA_AT,
        ingested_at=DECISION_AT,
        effective_at=None,
        valid_from=DATA_AT,
        valid_to=None,
        source_version="distribution-v1",
        run_id="baseline-run",
    )
    insert_player_distribution(connection, create, fit_result=fit)


def _insert_projection(
    connection: sqlite3.Connection,
    *,
    projection_id: int,
    player_id: int,
    source: str,
    source_hash: str,
    mean: float,
    observed_at: datetime,
    floor: float | None = None,
    ceiling: float | None = None,
) -> None:
    _insert(
        connection,
        "projection_snapshots",
        {
            "projection_snapshot_id": projection_id,
            "slate_id": 1,
            "player_id": player_id,
            "site": "draftkings",
            "projection_mean": mean,
            "projection_floor": floor,
            "projection_ceiling": ceiling,
            "ownership_projection": 0.1,
            "source_file_sha256": source_hash,
            **_pit(source, observed_at=observed_at),
        },
    )


def _insert_result(
    connection: sqlite3.Connection,
    *,
    result_id: int,
    player_id: int,
    points: float,
    source: str,
    source_hash: str,
    observed_at: datetime,
    stat_line: dict[str, object] | None = None,
) -> None:
    _insert(
        connection,
        "results",
        {
            "result_id": result_id,
            "game_id": 1,
            "player_id": player_id,
            "site": "draftkings",
            "fantasy_points": points,
            "stat_line_json": None if stat_line is None else json.dumps(stat_line),
            "source_file_sha256": source_hash,
            **_pit(source, observed_at=observed_at),
        },
    )


def _pit(source: str, *, observed_at: datetime = DATA_AT) -> dict[str, Any]:
    timestamp = utc_timestamp(observed_at)
    return {
        "source": source,
        "published_at": None,
        "observed_at": timestamp,
        "ingested_at": timestamp,
        "effective_at": None,
        "valid_from": timestamp,
        "valid_to": None,
        "source_version": "fixture-v1",
        "run_id": None,
    }


def _insert(connection: sqlite3.Connection, table: str, values: dict[str, Any]) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )


def _cell(report: Any, position: str) -> Any:
    return next(cell for cell in report.cells if cell.position == position)
