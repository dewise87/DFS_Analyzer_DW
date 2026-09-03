import math
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.ownership import (
    OwnershipTrainingRow,
    TrainingData,
    apply_governance_cap,
    calibrate_probabilities,
    evaluate_forward_chaining,
    fit_ownership_model,
    load_ownership_config,
    load_training_data,
    persist_evaluation,
    render_evaluation_report,
)
from narrative_alpha.ownership.cli import main as ownership_main
from narrative_alpha.ownership.data import load_latest_fit, persist_fit
from narrative_alpha.store import apply_migrations, connect_database

CONFIG_PATH = Path("config/ownership_model.toml")
BASE = datetime(2026, 9, 1, 12, tzinfo=UTC)


def test_map_fit_recovers_known_slopes_only_through_explicit_synthetic_seam() -> None:
    config = load_ownership_config(CONFIG_PATH)
    expected = np.asarray((0.18, -0.10, 0.07))
    rng = np.random.default_rng(71)
    rows: list[OwnershipTrainingRow] = []
    for week in range(1, 7):
        for player_offset in range(50):
            heat = rng.normal(size=3)
            baseline = float(rng.uniform(0.03, 0.40))
            raw_heat = float(heat @ expected)
            eta = (
                math.log(baseline / (1.0 - baseline))
                + 0.03
                + config.amplitude * math.tanh(raw_heat / config.amplitude)
            )
            probability = 1.0 / (1.0 + math.exp(-eta))
            lineups = 2_000
            roster_count = int(rng.binomial(lineups, probability))
            rows.append(
                _row(
                    week=week,
                    player_id=week * 100 + player_offset,
                    baseline=baseline,
                    heat=tuple(float(value) for value in heat),
                    actual=roster_count / lineups,
                    roster_count=roster_count,
                    lineup_count=lineups,
                )
            )

    with pytest.raises(ValueError, match="allow_synthetic=True"):
        fit_ownership_model(
            rows,
            config=config,
            contest_archetype="single_entry",
            site="draftkings",
        )
    fitted = fit_ownership_model(
        rows,
        config=config,
        contest_archetype="single_entry",
        site="draftkings",
        allow_synthetic=True,
    )

    actual = np.asarray(
        [
            fitted.coefficients["beta_signed"],
            fitted.coefficients["beta_dfs"],
            fitted.coefficients["beta_velocity"],
        ]
    )
    assert actual == pytest.approx(expected, abs=0.02)
    assert np.all(np.linalg.eigvalsh(np.asarray(fitted.covariance)) > 0)


def test_cli_gate_names_week_count_and_rejects_fixture_labels(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ownership.sqlite3"
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed_label_weeks(connection, weeks=2, source="draftkings-contest-standings")

    result = ownership_main(
        ["fit", "--database", str(database), "--archetype", "single_entry", "--site", "dk"]
    )
    assert result == 2
    assert "found 2" in capsys.readouterr().err

    fixture_database = tmp_path / "fixture.sqlite3"
    with connect_database(fixture_database) as connection:
        apply_migrations(connection)
        _seed_label_weeks(connection, weeks=3, source="fixture")

    result = ownership_main(
        [
            "fit",
            "--database",
            str(fixture_database),
            "--archetype",
            "single_entry",
            "--site",
            "dk",
        ]
    )
    assert result == 2
    assert "fixture/test" in capsys.readouterr().err


def test_forward_chaining_uses_only_strictly_earlier_weeks_and_renders_baseline() -> None:
    config = replace(load_ownership_config(CONFIG_PATH), posterior_draws=200)
    rows = tuple(
        _row(
            week=week,
            player_id=week * 10 + player,
            baseline=0.10 + player * 0.01,
            heat=(float(player - 2), float(2 - player) / 2, float(player) / 3),
            actual=min(0.95, 0.10 + player * 0.01 + (player - 2) * 0.012),
            roster_count=max(0, round(1_000 * (0.10 + player * 0.01 + (player - 2) * 0.012))),
            lineup_count=1_000,
        )
        for week in range(1, 5)
        for player in range(6)
    )
    report = evaluate_forward_chaining(
        TrainingData(rows=rows, missing=(), decision_snapshot_ids=()),
        config=config,
        site="draftkings",
        contest_archetype="single_entry",
        allow_synthetic=True,
        evaluated_at=BASE,
    )

    for fold in report.folds:
        assert all(
            training_week < (fold.test_season, fold.test_week)
            for training_week in fold.training_weeks
        )
    rendered = render_evaluation_report(report)
    assert "metric,model,vendor_baseline" in rendered
    assert "mae_percentage_points" in rendered
    assert "OUT-OF-WEEK: model beat untouched vendor baseline" in rendered


def test_logistic_ipf_matches_each_position_total() -> None:
    values = calibrate_probabilities(
        (0.10, 0.30, 0.20, 0.40, 0.25),
        ("QB", "QB", "WR", "WR", "WR"),
        {"QB": 1.0, "WR": 2.0},
    )
    assert sum(values[:2]) == pytest.approx(1.0, abs=1e-10)
    assert sum(values[2:]) == pytest.approx(2.0, abs=1e-10)


def test_training_join_uses_each_frozen_decision_not_a_later_baseline(tmp_path: Path) -> None:
    database = tmp_path / "training.sqlite3"
    config = load_ownership_config(CONFIG_PATH)
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed_label_weeks(connection, weeks=1, source="draftkings-contest-standings")
        _seed_feature_at_decision(connection, config.feature_version)
        data = load_training_data(
            connection,
            site="dk",
            contest_archetype="single_entry",
            feature_version=config.feature_version,
            as_of=BASE + timedelta(days=10),
        )

    assert not data.missing
    assert len(data.rows) == 1
    assert data.rows[0].baseline_ownership == 0.20
    assert data.rows[0].h_signed_z == 0.75


def test_a_fast_lane_refreeze_without_features_does_not_hide_the_week(tmp_path: Path) -> None:
    """`na-fast inactives` freezes a later decision with no feature rows of its own; the
    label must still join the decision that froze features, not go missing."""

    database = tmp_path / "training.sqlite3"
    config = load_ownership_config(CONFIG_PATH)
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed_label_weeks(connection, weeks=1, source="draftkings-contest-standings")
        _seed_feature_at_decision(connection, config.feature_version)
        refreeze_at = utc_timestamp(BASE + timedelta(days=1, minutes=30))
        connection.execute(
            """
            INSERT INTO decision_snapshots(
                decision_snapshot_id, slate_id, decision_at, created_at,
                manifest_schema_version, manifest_hashes_json,
                manifest_hash_set_sha256, run_id, note
            ) VALUES ('decision-1-fast', 1, ?, ?, 'decision-v1', '[]', ?, NULL,
                      'na-fast official inactives; base=decision-1')
            """,
            (refreeze_at, refreeze_at, "9" * 64),
        )
        data = load_training_data(
            connection,
            site="dk",
            contest_archetype="single_entry",
            feature_version=config.feature_version,
            as_of=BASE + timedelta(days=10),
        )

    assert not data.missing
    assert data.decision_snapshot_ids == ("decision-1",)
    assert data.rows[0].decision_snapshot_id == "decision-1"


def test_overdispersed_labels_widen_the_posterior_and_the_factor_is_stored(
    tmp_path: Path,
) -> None:
    """Rows sharing slates and stories vary more than a binomial says; the fit must not
    report intervals a pure binomial would give on 20,000-lineup contests."""

    config = load_ownership_config(CONFIG_PATH)
    rng = np.random.default_rng(5)
    tight: list[OwnershipTrainingRow] = []
    noisy: list[OwnershipTrainingRow] = []
    for week in range(1, 5):
        for player_offset in range(40):
            heat = rng.normal(size=3)
            baseline = float(rng.uniform(0.05, 0.35))
            eta = math.log(baseline / (1.0 - baseline)) + 0.1 * float(heat[0])
            lineups = 20_000
            for rows, extra in ((tight, 0.0), (noisy, float(rng.normal(0.0, 0.6)))):
                probability = 1.0 / (1.0 + math.exp(-(eta + extra)))
                roster_count = int(rng.binomial(lineups, probability))
                rows.append(
                    _row(
                        week=week,
                        player_id=week * 100 + player_offset,
                        baseline=baseline,
                        heat=tuple(float(value) for value in heat),
                        actual=roster_count / lineups,
                        roster_count=roster_count,
                        lineup_count=lineups,
                    )
                )
    fit_tight = fit_ownership_model(
        tight, config=config, contest_archetype="single_entry", site="draftkings",
        allow_synthetic=True,
    )
    fit_noisy = fit_ownership_model(
        noisy, config=config, contest_archetype="single_entry", site="draftkings",
        allow_synthetic=True,
    )

    assert fit_tight.dispersion < 2.0
    assert fit_noisy.dispersion > 10.0
    assert fit_noisy.covariance[0][0] > fit_tight.covariance[0][0] * 5

    database = tmp_path / "fit.sqlite3"
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed_label_weeks(connection, weeks=1, source="draftkings-contest-standings")
        _seed_feature_at_decision(connection, config.feature_version)
        data = load_training_data(
            connection,
            site="dk",
            contest_archetype="single_entry",
            feature_version=config.feature_version,
            as_of=BASE + timedelta(days=10),
        )
        model = replace(fit_noisy, training_weeks=((2026, 1), (2026, 2), (2026, 3)))
        stored = persist_fit(
            connection, model, data, config=config, fitted_at=BASE + timedelta(days=10)
        )
        connection.commit()
        loaded = load_latest_fit(
            connection,
            site="dk",
            contest_archetype="single_entry",
            config=config,
            as_of=BASE + timedelta(days=11),
        )
    assert stored.run_id == loaded.run_id
    assert loaded.dispersion == pytest.approx(fit_noisy.dispersion)


def test_evaluation_artifact_and_model_eval_row_keep_baseline_beside_model(
    tmp_path: Path,
) -> None:
    config = replace(load_ownership_config(CONFIG_PATH), posterior_draws=100)
    rows = tuple(
        _row(
            week=week,
            player_id=week * 10 + player,
            baseline=0.15 + player * 0.02,
            heat=(float(player - 1), 0.0, 0.0),
            actual=0.15 + player * 0.025,
            roster_count=round(1_000 * (0.15 + player * 0.025)),
            lineup_count=1_000,
        )
        for week in range(1, 4)
        for player in range(3)
    )
    report = evaluate_forward_chaining(
        TrainingData(rows=rows, missing=(), decision_snapshot_ids=()),
        config=config,
        site="draftkings",
        contest_archetype="single_entry",
        evaluated_at=BASE,
        allow_synthetic=True,
    )
    database = tmp_path / "evaluation.sqlite3"
    with connect_database(database) as connection:
        apply_migrations(connection)
        connection.execute(
            """
            INSERT INTO narrative_feature_versions(
                feature_version, formula_version, config_sha256, config_json,
                registered_at, source
            ) VALUES (?, 'test-v1', ?, '{}', ?, 'test')
            """,
            (config.feature_version, "f" * 64, utc_timestamp(BASE)),
        )
        stored_report = persist_evaluation(
            connection, report, report_directory=tmp_path / "reports"
        )
        stored = connection.execute(
            "SELECT evaluation_kind, beat_baseline, metrics_json FROM model_evals"
        ).fetchone()

    assert stored_report.report_path is not None
    rendered = stored_report.report_path.read_text(encoding="utf-8")
    assert "metric,model,vendor_baseline" in rendered
    assert stored["evaluation_kind"] == "ownership"
    assert stored["beat_baseline"] == int(report.beat_baseline)
    assert '"forward_chaining"' in stored["metrics_json"]


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        ("UNVALIDATED", 0.52),
        ("TESTING", 0.55),
        ("PROVISIONAL", 0.60),
        ("VALIDATED", 0.60),
    ),
)
def test_classic_probability_caps_bind_at_every_status(status: str, expected: float) -> None:
    config = load_ownership_config(CONFIG_PATH)
    assert apply_governance_cap(
        0.50,
        1.0,
        status=status,  # type: ignore[arg-type]
        slate_kind="classic",
        config=config,
    ) == pytest.approx(expected)


def _row(
    *,
    week: int,
    player_id: int,
    baseline: float,
    heat: tuple[float, float, float],
    actual: float,
    roster_count: int,
    lineup_count: int,
) -> OwnershipTrainingRow:
    return OwnershipTrainingRow(
        player_id=player_id,
        season=2026,
        week=week,
        slate_id=week,
        decision_snapshot_id=f"decision-{week}",
        decision_at=utc_timestamp(BASE + timedelta(days=week)),
        site="draftkings",
        contest_archetype="single_entry",
        role="classic",
        position="WR",
        baseline_ownership=baseline,
        h_signed_z=heat[0],
        h_dfs_z=heat[1],
        h_velocity_z=heat[2],
        actual_ownership=actual,
        roster_count=roster_count,
        lineup_count=lineup_count,
        label_source="synthetic",
    )


def _seed_label_weeks(connection: sqlite3.Connection, *, weeks: int, source: str) -> None:
    stamp = utc_timestamp(BASE - timedelta(days=1))
    connection.execute(
        """
        INSERT INTO players(
            player_id, player_key, canonical_name, position, birth_date, source,
            published_at, observed_at, ingested_at, effective_at, valid_from,
            valid_to, source_version, run_id
        ) VALUES (1, 'player-1', 'Player One', 'WR', NULL, 'seed', NULL, ?, ?, NULL,
                  ?, NULL, 'seed-v1', NULL)
        """,
        (stamp, stamp, stamp),
    )
    for week in range(1, weeks + 1):
        slate_at = utc_timestamp(BASE + timedelta(days=week))
        connection.execute(
            """
            INSERT INTO slates(
                slate_id, external_slate_id, site, slate_type, season, week, name,
                starts_at, locks_at, source, published_at, observed_at, ingested_at,
                effective_at, valid_from, valid_to, source_version, run_id
            ) VALUES (?, ?, 'draftkings', 'classic', 2026, ?, ?, ?, ?, 'seed', NULL,
                      ?, ?, NULL, ?, NULL, 'seed-v1', NULL)
            """,
            (
                week,
                f"slate-{week}",
                week,
                f"Week {week}",
                slate_at,
                slate_at,
                stamp,
                stamp,
                stamp,
            ),
        )
        observed = utc_timestamp(BASE + timedelta(days=week, hours=6))
        connection.execute(
            """
            INSERT INTO actual_ownership(
                external_contest_id, site, slate_id, contest_archetype, field_size,
                entry_limit, entry_fee_cents, payout_curve_id, player_id, role,
                lineup_count, roster_count, actual_ownership, source_file_sha256,
                source, published_at, observed_at, ingested_at, effective_at,
                valid_from, valid_to, source_version, run_id
            ) VALUES (?, 'draftkings', ?, 'single_entry', 100, 1, 100, NULL, 1,
                      'classic', 100, 20, 0.20, ?, ?, NULL, ?, ?, NULL, ?, NULL,
                      'labels-v1', NULL)
            """,
            (f"contest-{week}", week, "a" * 64, source, observed, observed, observed),
        )


def _seed_feature_at_decision(connection: sqlite3.Connection, feature_version: str) -> None:
    before = utc_timestamp(BASE - timedelta(days=1))
    decision = utc_timestamp(BASE + timedelta(days=1))
    built = utc_timestamp(BASE + timedelta(days=1, hours=1))
    future = utc_timestamp(BASE + timedelta(days=1, hours=2))
    connection.execute(
        """
        INSERT INTO teams(
            team_id, team_key, abbreviation, canonical_name, league, source,
            published_at, observed_at, ingested_at, effective_at, valid_from,
            valid_to, source_version, run_id
        ) VALUES (1, 'AAA', 'AAA', 'Team AAA', 'NFL', 'seed', NULL, ?, ?, NULL,
                  ?, NULL, 'seed-v1', NULL)
        """,
        (before, before, before),
    )
    connection.execute(
        """
        INSERT INTO salaries(
            salary_id, slate_id, player_id, game_id, team_id, opponent_team_id,
            site_player_id, roster_positions_json, salary, player_status,
            source_file_sha256, source, published_at, observed_at, ingested_at,
            effective_at, valid_from, valid_to, source_version, run_id
        ) VALUES (1, 1, 1, NULL, 1, NULL, 'site-1', '["WR"]', 5000, NULL, ?,
                  'salary', NULL, ?, ?, NULL, ?, NULL, 'salary-v1', NULL)
        """,
        ("b" * 64, before, before, before),
    )
    connection.execute(
        """
        INSERT INTO ownership_baselines(
            ownership_baseline_id, slate_id, player_id, site, role, ownership,
            source_file_sha256, source, published_at, observed_at, ingested_at,
            effective_at, valid_from, valid_to, source_version, run_id
        ) VALUES (1, 1, 1, 'draftkings', 'classic', 0.20, ?, 'vendor', NULL,
                  ?, ?, NULL, ?, NULL, 'vendor-v1', NULL)
        """,
        ("c" * 64, before, before, before),
    )
    connection.execute(
        """
        INSERT INTO ownership_baselines(
            ownership_baseline_id, slate_id, player_id, site, role, ownership,
            source_file_sha256, source, published_at, observed_at, ingested_at,
            effective_at, valid_from, valid_to, source_version, run_id
        ) VALUES (2, 1, 1, 'draftkings', 'classic', 0.80, ?, 'vendor', NULL,
                  ?, ?, NULL, ?, NULL, 'vendor-v2', NULL)
        """,
        ("d" * 64, future, future, future),
    )
    connection.execute(
        """
        INSERT INTO decision_snapshots(
            decision_snapshot_id, slate_id, decision_at, created_at,
            manifest_schema_version, manifest_hashes_json,
            manifest_hash_set_sha256, run_id, note
        ) VALUES ('decision-1', 1, ?, ?, 'decision-v1', '[]', ?, NULL, NULL)
        """,
        (decision, built, "e" * 64),
    )
    connection.execute(
        """
        INSERT INTO narrative_feature_versions(
            feature_version, formula_version, config_sha256, config_json,
            registered_at, source
        ) VALUES (?, 'feature-formula-v1', ?, '{}', ?, 'seed')
        """,
        (feature_version, "f" * 64, built),
    )
    connection.execute(
        """
        INSERT INTO model_runs(
            run_id, run_type, started_at, completed_at, status, code_version,
            config_sha256, parent_run_id, error_message, created_at
        ) VALUES ('feature-run', 'stage_3_features', ?, NULL, 'running', 'test', ?,
                  NULL, NULL, ?)
        """,
        (built, "f" * 64, built),
    )
    connection.execute(
        """
        INSERT INTO narrative_features(
            feature_id, player_id, slate_id, contest_archetype, site, role, as_of,
            baseline_ownership, baseline_ownership_change_6h, projection_change_6h,
            salary, value_rank, position_scarcity, alternative_quality_index,
            h_signed, h_absolute, h_mainstream, h_dfs, h_team_fan, h_velocity_6h,
            h_acceleration, h_consensus, h_source_entropy, h_novelty_share,
            h_signed_z, h_absolute_z, h_mainstream_z, h_dfs_z, h_team_fan_z,
            h_velocity_6h_z, h_acceleration_z, h_consensus_z, h_source_entropy_z,
            h_novelty_share_z, unique_episode_count, unique_source_count,
            unique_author_count, source_overlap_index, unique_episode_count_z,
            unique_source_count_z, unique_author_count_z, source_overlap_index_z,
            model_version, feature_version, formula_version, feature_config_sha256,
            episode_method_version, episode_ids_json, ownership_baseline_ids_json,
            baseline_ownership_snapshot_id, baseline_previous_snapshot_id,
            projection_snapshot_id, projection_previous_snapshot_id, salary_id,
            input_sha256, source, published_at, observed_at, ingested_at, effective_at,
            valid_from, valid_to, source_version, run_id
        ) VALUES (
            'feature-1', 1, 1, NULL, 'draftkings', 'classic', ?, 0.20, NULL, NULL,
            5000, NULL, NULL, NULL, 0.5, 0.5, 0.0, 0.2, 0.0, 0.1, 0.0, 1.0,
            0.0, 1.0, 0.75, 0.75, 0.0, -0.25, 0.0, 0.50, 0.0, 1.0, 0.0, 1.0,
            0, 0, NULL, 0.0, 0.0, 0.0, NULL, 0.0, NULL, ?, 'feature-formula-v1',
            ?, 'episodes-v1', '[]', '[1]', 1, NULL, NULL, NULL, 1, ?, 'features',
            NULL, ?, ?, ?, ?, NULL, ?, 'feature-run'
        )
        """,
        (
            decision,
            feature_version,
            "f" * 64,
            "1" * 64,
            built,
            built,
            decision,
            built,
            feature_version,
        ),
    )
