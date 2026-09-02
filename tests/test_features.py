import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from narrative_alpha.features_cli import main as features_main
from narrative_alpha.narrative import (
    FeatureSnapshotConflictError,
    FeatureVersionMismatchError,
    PreparedExtraction,
    ProviderBatchSubmission,
    ProviderResult,
    build_episodes,
    build_features,
    calculate_episode_heat,
    load_batch_pricing,
    load_episode_heats,
    load_feature_rows,
    normalize_item_text,
    run_extraction_batch,
)
from narrative_alpha.store import apply_migrations, connect_database

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "episode_claims.json"
PRICING_PATH = Path("config/model_pricing.toml")
HEAT_CONFIG_PATH = Path("config/heat.toml")
BASE_TIME = datetime.now(UTC).replace(microsecond=0) - timedelta(days=10)


@dataclass(frozen=True)
class FixtureClaim:
    key: str
    source_id: str
    source_family: str
    observed_hours: int
    title: str
    body: str
    player_name: str
    team_refs: tuple[str, ...]
    outcome_direction: str
    roster_behavior_direction: str


@dataclass
class FixtureProvider:
    payload: dict[str, object]

    def submit_batch(
        self,
        requests: tuple[PreparedExtraction, ...],
    ) -> ProviderBatchSubmission:
        assert len(requests) == 1
        return ProviderBatchSubmission(
            provider_batch_id=f"batch-{requests[0].source_item_id}",
            batch_submission_request_id=f"request-{requests[0].source_item_id}",
        )

    def retrieve_batch(
        self,
        requests: tuple[PreparedExtraction, ...],
        submission: ProviderBatchSubmission,
    ) -> tuple[ProviderResult, ...]:
        request = requests[0]
        return (
            ProviderResult(
                custom_id=request.custom_id,
                provider_request_id=None,
                batch_submission_request_id=submission.batch_submission_request_id,
                provider_batch_id=submission.provider_batch_id,
                provider_message_id=f"message-{request.source_item_id}",
                actual_model_id="claude-haiku-4-5-20251001",
                output_json=json.dumps(self.payload),
                content_types=("text",),
                stop_reason="end_turn",
                input_tokens=20,
                output_tokens=10,
                latency_ms=1,
            ),
        )


def test_golden_heat_applies_soft_floor_but_preserves_zero_novelty() -> None:
    expected = 0.15 * (0.15 + 0.85 * 0.5) * 0.15 * math.log(3) * 0.5

    actual = calculate_episode_heat(
        direction=1.0,
        quality=0.0,
        specificity=0.5,
        novelty=1.0,
        independence=0.0,
        reach=2,
        age_hours=12.0,
        half_life_hours=12.0,
        soft_factor_floor=0.15,
    )
    gated = calculate_episode_heat(
        direction=1.0,
        quality=1.0,
        specificity=1.0,
        novelty=0.0,
        independence=1.0,
        reach=100,
        age_hours=0.0,
        half_life_hours=12.0,
        soft_factor_floor=0.15,
    )

    assert actual == pytest.approx(expected)
    assert gated == 0.0


def test_derivative_changes_episode_reach_but_not_event_count(tmp_path: Path) -> None:
    database, player_ids = _feature_database(tmp_path, "origin", "copy")
    as_of = BASE_TIME + timedelta(hours=2)
    with connect_database(database) as connection:
        build_episodes(connection, as_of=as_of, built_at=as_of + timedelta(minutes=5))
        heats = load_episode_heats(
            connection,
            player_id=player_ids[0],
            slate_id=1,
            site="dk",
            as_of=as_of,
        )

    assert len(heats) == 1
    assert heats[0].reach == 2
    assert heats[0].item_count == 2
    assert heats[0].n_events == 1
    expected_quality = 0.15 + 0.85 * ((0.75 + 0.85 + 1.0) / 3.0)
    expected_specificity = 0.15 + 0.85 * 0.8
    expected_heat = (
        expected_quality
        * expected_specificity
        * math.log(3)
        * 2 ** (-2 / 24)
    )
    assert heats[0].heat == pytest.approx(expected_heat)


def test_point_in_time_excludes_future_claim_and_baseline(tmp_path: Path) -> None:
    database, player_ids = _feature_database(tmp_path, "origin", "outside-window")
    as_of = BASE_TIME + timedelta(hours=2)
    with connect_database(database) as connection:
        _seed_ownership(
            connection,
            player_ids[0],
            ownership=0.42,
            observed_at=as_of + timedelta(hours=1),
        )
        report = build_episodes(
            connection, as_of=as_of, built_at=as_of + timedelta(minutes=5)
        )
        build_features(
            connection,
            slate_id=1,
            site="dk",
            as_of=as_of,
            built_at=as_of + timedelta(minutes=10),
        )
        row = load_feature_rows(
            connection,
            slate_id=1,
            site="dk",
            as_of=as_of,
            feature_version="narrative-heat-v1",
        )[0]

    assert report.claims_considered == 1
    assert row.unique_episode_count == 1
    assert row.baseline_ownership is None
    assert row.baseline_ownership_change_6h is None


def test_fixture_slate_standardizes_winsorizes_and_rebuilds_identically(
    tmp_path: Path,
) -> None:
    database, player_ids = _feature_database(tmp_path, "origin", player_count=26)
    as_of = BASE_TIME + timedelta(hours=8)
    with connect_database(database) as connection:
        build_episodes(connection, as_of=as_of, built_at=as_of + timedelta(minutes=5))
        first = build_features(
            connection,
            slate_id=1,
            site="dk",
            as_of=as_of,
            built_at=as_of + timedelta(minutes=10),
        )
        before = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT player_id, input_sha256 FROM narrative_features ORDER BY player_id"
            )
        )
        second = build_features(
            connection,
            slate_id=1,
            site="draftkings",
            as_of=as_of,
            built_at=as_of + timedelta(minutes=20),
        )
        after = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT player_id, input_sha256 FROM narrative_features ORDER BY player_id"
            )
        )
        rows = load_feature_rows(
            connection,
            slate_id=1,
            site="dk",
            as_of=as_of,
            feature_version="narrative-heat-v1",
        )

    by_player = {row.player_id: row for row in rows}
    assert first.features_inserted == len(player_ids) == 26
    assert second.features_inserted == 0 and second.reused_existing
    assert before == after
    assert by_player[player_ids[0]].h_signed_z == 4.0
    assert by_player[player_ids[1]].h_signed_z == pytest.approx(-0.2)
    assert by_player[player_ids[0]].unique_episode_count_z == 4.0
    assert by_player[player_ids[0]].h_velocity_6h != 0.0
    assert by_player[player_ids[0]].h_acceleration != 0.0


def test_aligned_vendor_baseline_change_zeroes_novelty_and_joins_baseline(
    tmp_path: Path,
) -> None:
    database, player_ids = _feature_database(tmp_path, "origin")
    player_id = player_ids[0]
    as_of = BASE_TIME + timedelta(hours=8)
    with connect_database(database) as connection:
        prior_id = _seed_ownership(
            connection,
            player_id,
            ownership=0.1,
            observed_at=BASE_TIME - timedelta(hours=1),
        )
        current_id = _seed_ownership(
            connection,
            player_id,
            ownership=0.2,
            observed_at=BASE_TIME + timedelta(hours=7),
        )
        prior_projection_id = _seed_projection(
            connection,
            player_id,
            projection_mean=10.0,
            observed_at=BASE_TIME - timedelta(hours=1),
        )
        current_projection_id = _seed_projection(
            connection,
            player_id,
            projection_mean=12.5,
            observed_at=BASE_TIME + timedelta(hours=7),
        )
        build_episodes(connection, as_of=as_of, built_at=as_of + timedelta(minutes=5))
        heats = load_episode_heats(
            connection,
            player_id=player_id,
            slate_id=1,
            site="dk",
            as_of=as_of,
        )
        build_features(
            connection,
            slate_id=1,
            site="dk",
            as_of=as_of,
            built_at=as_of + timedelta(minutes=10),
        )
        row = load_feature_rows(
            connection,
            slate_id=1,
            site="dk",
            as_of=as_of,
            feature_version="narrative-heat-v1",
        )[0]

    assert len(heats) == 1
    assert heats[0].novelty == 0.0
    assert heats[0].heat == 0.0
    expected_counterfactual = (
        (0.15 + 0.85 * ((0.75 + 0.85 + 1.0) / 3.0))
        * (0.15 + 0.85 * 0.8)
        * math.log(2)
        * 2 ** (-8 / 24)
    )
    assert heats[0].heat_without_novelty == pytest.approx(expected_counterfactual)
    assert row.h_novelty_share == 0.0
    assert row.baseline_ownership == 0.2
    assert row.baseline_ownership_change_6h == pytest.approx(0.1)
    assert row.baseline_ownership_snapshot_id == current_id
    assert row.baseline_previous_snapshot_id == prior_id
    assert row.ownership_baseline_ids_json == (prior_id, current_id)
    assert row.projection_change_6h == pytest.approx(2.5)
    assert row.projection_snapshot_id == current_projection_id
    assert row.projection_previous_snapshot_id == prior_projection_id


def test_feature_version_reuse_with_changed_config_is_refused(tmp_path: Path) -> None:
    database, _ = _feature_database(tmp_path, "origin")
    as_of = BASE_TIME + timedelta(hours=2)
    changed_config = tmp_path / "heat.toml"
    changed_config.write_text(
        HEAT_CONFIG_PATH.read_text(encoding="utf-8").replace(
            "mainstream = 24.0", "mainstream = 25.0"
        ),
        encoding="utf-8",
    )
    with connect_database(database) as connection:
        build_episodes(connection, as_of=as_of, built_at=as_of + timedelta(minutes=5))
        build_features(
            connection,
            slate_id=1,
            site="dk",
            as_of=as_of,
            built_at=as_of + timedelta(minutes=10),
        )
        with pytest.raises(FeatureVersionMismatchError, match="bump feature_version"):
            build_features(
                connection,
                slate_id=1,
                site="dk",
                as_of=as_of,
                config_path=changed_config,
                built_at=as_of + timedelta(minutes=20),
            )


def test_features_cli_builds_exact_snapshot(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, _ = _feature_database(tmp_path, "origin")
    as_of = BASE_TIME + timedelta(hours=2)
    with connect_database(database) as connection:
        build_episodes(connection, as_of=as_of, built_at=as_of + timedelta(minutes=5))

    assert features_main(
        [
            "build",
            "--database",
            str(database),
            "--slate-id",
            "1",
            "--site",
            "dk",
            "--as-of",
            _timestamp(as_of),
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["features_inserted"] == 1
    assert payload["feature_version"] == "narrative-heat-v1"
    assert payload["site"] == "draftkings"


def _feature_database(
    tmp_path: Path,
    *claim_keys: str,
    player_count: int = 1,
) -> tuple[Path, tuple[int, ...]]:
    database = tmp_path / "features.sqlite3"
    fixtures = {fixture.key: fixture for fixture in _load_fixtures()}
    with connect_database(database) as connection:
        apply_migrations(connection)
        player_ids = _seed_slate_players(connection, player_count)
        for key in claim_keys:
            _seed_extracted_claim(connection, fixtures[key])
    return database, player_ids


def _load_fixtures() -> tuple[FixtureClaim, ...]:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return tuple(
        FixtureClaim(
            key=str(item["key"]),
            source_id=str(item["source_id"]),
            source_family=str(item["source_family"]),
            observed_hours=int(item["observed_hours"]),
            title=str(item["title"]),
            body=str(item["body"]),
            player_name=str(item["player_name"]),
            team_refs=tuple(str(team) for team in item["team_refs"]),
            outcome_direction=str(item["outcome_direction"]),
            roster_behavior_direction=str(item["roster_behavior_direction"]),
        )
        for item in payload
    )


def _seed_slate_players(connection: sqlite3.Connection, count: int) -> tuple[int, ...]:
    observed_at = BASE_TIME - timedelta(days=1)
    timestamp = _timestamp(observed_at)
    connection.execute(
        """
        INSERT INTO teams(
            team_key, abbreviation, canonical_name, league, source, published_at,
            observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES ('CAR', 'CAR', 'Carolina Panthers', 'NFL', 'fixture', NULL,
                  ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (timestamp, timestamp, timestamp),
    )
    team_id = int(connection.execute("SELECT team_id FROM teams").fetchone()[0])
    connection.execute(
        """
        INSERT INTO slates(
            external_slate_id, site, slate_type, season, week, name, starts_at,
            locks_at, source, published_at, observed_at, ingested_at, effective_at,
            valid_from, valid_to, source_version, run_id
        ) VALUES ('fixture-main', 'draftkings', 'classic', 2026, 1, 'Fixture Main',
                  ?, ?, 'fixture', NULL, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (
            _timestamp(BASE_TIME + timedelta(days=2)),
            _timestamp(BASE_TIME + timedelta(days=2)),
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    slate_id = int(connection.execute("SELECT slate_id FROM slates").fetchone()[0])
    player_ids: list[int] = []
    for index in range(count):
        name = "Marcus Bell" if index == 0 else f"Fixture Player {index + 1:02d}"
        cursor = connection.execute(
            """
            INSERT INTO players(
                player_key, canonical_name, position, birth_date, source, published_at,
                observed_at, ingested_at, effective_at, valid_from, valid_to,
                source_version, run_id
            ) VALUES (?, ?, 'WR', NULL, 'fixture', NULL, ?, ?, NULL, ?, NULL,
                      'fixture-v1', NULL)
            """,
            (f"fixture-player-{index + 1}", name, timestamp, timestamp, timestamp),
        )
        assert cursor.lastrowid is not None
        player_id = int(cursor.lastrowid)
        player_ids.append(player_id)
        connection.execute(
            """
            INSERT INTO player_team_history(
                player_id, team, position, roster_status, season, week, source,
                published_at, observed_at, ingested_at, effective_at, valid_from,
                valid_to, source_version, run_id
            ) VALUES (?, 'CAR', 'WR', 'ACT', 2026, 1, 'fixture', NULL, ?, ?, NULL,
                      ?, NULL, 'fixture-v1', NULL)
            """,
            (player_id, timestamp, timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO salaries(
                slate_id, player_id, game_id, team_id, opponent_team_id, site_player_id,
                roster_positions_json, salary, player_status, source_file_sha256,
                source, published_at, observed_at, ingested_at, effective_at, valid_from,
                valid_to, source_version, run_id
            ) VALUES (?, ?, NULL, ?, NULL, ?, '["WR","FLEX"]', ?, NULL, ?,
                      'fixture', NULL, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
            """,
            (
                slate_id,
                player_id,
                team_id,
                f"site-{player_id}",
                5_000 + index,
                hashlib.sha256(f"salary-{index}".encode()).hexdigest(),
                timestamp,
                timestamp,
                timestamp,
            ),
        )
    return tuple(player_ids)


def _seed_extracted_claim(connection: sqlite3.Connection, fixture: FixtureClaim) -> None:
    configured_at = BASE_TIME - timedelta(days=5)
    observed_at = BASE_TIME + timedelta(hours=fixture.observed_hours)
    _seed_source(connection, fixture, configured_at)
    canonical_text = normalize_item_text(fixture.title, fixture.body)
    cursor = connection.execute(
        """
        INSERT INTO source_items(
            source_id, external_item_id, canonical_url, title, raw_content, cleaned_text,
            content_sha256, source, published_at, observed_at, ingested_at, effective_at,
            valid_from, valid_to, source_version, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (
            fixture.source_id,
            fixture.key,
            f"https://example.test/{fixture.key}",
            fixture.title,
            fixture.body.encode("utf-8"),
            fixture.body,
            hashlib.sha256(canonical_text.encode("utf-8")).hexdigest(),
            fixture.source_id,
            _timestamp(observed_at),
            _timestamp(observed_at),
            _timestamp(observed_at),
            _timestamp(observed_at),
        ),
    )
    assert cursor.lastrowid is not None
    item_id = int(cursor.lastrowid)
    evidence_start = canonical_text.index(fixture.player_name)
    evidence = canonical_text[evidence_start:]
    payload: dict[str, object] = {
        "schema_version": "stage1-extraction-v1",
        "prompt_injection_detected": False,
        "claims": [
            {
                "player_refs": [{"name_raw": fixture.player_name}],
                "team_refs": list(fixture.team_refs),
                "claim_type": "usage",
                "claim_dimension": "role",
                "outcome_direction": fixture.outcome_direction,
                "roster_behavior_direction": fixture.roster_behavior_direction,
                "evidence_class": "B",
                "evidence_basis": "beat_report",
                "falsifiable": True,
                "specificity": 0.8,
                "actionability": 0.8,
                "novelty": "new",
                "model_confidence": "high",
                "uncertainty_flags": ["none"],
                "ambiguity_flags": ["none"],
                "suggested_channels": ["mean", "ownership"],
                "disconfirming_context": None,
                "evidence_refs": [
                    {
                        "source_item_id": item_id,
                        "extract_start": evidence_start,
                        "extract_end": evidence_start + len(evidence),
                        "verbatim_extract": evidence,
                    }
                ],
            }
        ],
    }
    report = run_extraction_batch(
        connection,
        window_start=observed_at - timedelta(seconds=1),
        window_end=observed_at + timedelta(seconds=1),
        provider=FixtureProvider(payload),
        pricing=load_batch_pricing(PRICING_PATH),
        run_at=observed_at + timedelta(minutes=1),
        clock=lambda: observed_at + timedelta(minutes=1),
    )
    assert report.claims_stored == 1


def _seed_source(
    connection: sqlite3.Connection,
    fixture: FixtureClaim,
    configured_at: datetime,
) -> None:
    timestamp = _timestamp(configured_at)
    connection.execute("INSERT INTO source_keys(source_id) VALUES (?)", (fixture.source_id,))
    connection.execute(
        """
        INSERT INTO sources(
            source_id, display_name, source_family, collector_kind, feed_url, enabled,
            source, published_at, observed_at, ingested_at, effective_at, valid_from,
            valid_to, source_version, run_id
        ) VALUES (?, ?, ?, 'rss_atom', ?, 1, 'fixture', NULL, ?, ?, NULL, ?,
                  NULL, 'fixture-v1', NULL)
        """,
        (
            fixture.source_id,
            fixture.source_id,
            fixture.source_family,
            f"https://example.test/{fixture.source_id}.xml",
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    connection.execute(
        """
        INSERT INTO source_policies(
            source_id, permitted_use, raw_retention_days, personal_data_fields_allowed,
            must_honor_deletions, redistribution_allowed, third_party_processing_allowed,
            commercial_use_status, terms_reviewed_at, source, published_at, observed_at,
            ingested_at, effective_at, valid_from, valid_to, source_version, run_id
        ) VALUES (?, 'internal analysis', 30, '[]', 1, 0, 1, 'prohibited', ?,
                  'fixture', NULL, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (fixture.source_id, timestamp, timestamp, timestamp, timestamp),
    )


def _seed_ownership(
    connection: sqlite3.Connection,
    player_id: int,
    *,
    ownership: float,
    observed_at: datetime,
) -> int:
    timestamp = _timestamp(observed_at)
    cursor = connection.execute(
        """
        INSERT INTO ownership_baselines(
            slate_id, player_id, site, role, ownership, source_file_sha256, source,
            published_at, observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES (1, ?, 'draftkings', 'classic', ?, ?, 'fixture-vendor', NULL,
                  ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (
            player_id,
            ownership,
            hashlib.sha256(f"{player_id}-{timestamp}".encode()).hexdigest(),
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _seed_projection(
    connection: sqlite3.Connection,
    player_id: int,
    *,
    projection_mean: float,
    observed_at: datetime,
) -> int:
    timestamp = _timestamp(observed_at)
    cursor = connection.execute(
        """
        INSERT INTO projection_snapshots(
            slate_id, player_id, site, projection_mean, projection_floor,
            projection_ceiling, ownership_projection, source_file_sha256, source,
            published_at, observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES (1, ?, 'draftkings', ?, NULL, NULL, NULL, ?, 'fixture-vendor', NULL,
                  ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (
            player_id,
            projection_mean,
            hashlib.sha256(f"projection-{player_id}-{timestamp}".encode()).hexdigest(),
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


# --- Review fixes (2026-09-02) -------------------------------------------------------------


def test_small_baseline_tick_keeps_novelty_and_velocity_measures_decay(tmp_path: Path) -> None:
    # A 0.1-point move from any cause is not "the story is already in the baseline".
    database, player_ids = _feature_database(tmp_path, "origin")
    player_id = player_ids[0]
    as_of = BASE_TIME + timedelta(hours=8)
    with connect_database(database) as connection:
        _seed_ownership(
            connection, player_id, ownership=0.100, observed_at=BASE_TIME - timedelta(hours=1)
        )
        _seed_ownership(
            connection, player_id, ownership=0.101, observed_at=BASE_TIME + timedelta(hours=7)
        )
        build_episodes(connection, as_of=as_of, built_at=as_of + timedelta(minutes=5))
        heats = load_episode_heats(
            connection, player_id=player_id, slate_id=1, site="dk", as_of=as_of
        )
        build_features(
            connection, slate_id=1, site="dk", as_of=as_of, built_at=as_of + timedelta(minutes=10)
        )
        row = load_feature_rows(
            connection, slate_id=1, site="dk", as_of=as_of, feature_version="narrative-heat-v1"
        )[0]

    assert heats[0].novelty == 1.0
    assert heats[0].heat > 0
    # Velocity is decay between t-6h and t on a live episode, not a gate flip.
    assert row.h_velocity_6h == pytest.approx(heats[0].heat - heats[0].heat * 2 ** (6 / 24))
    assert row.h_velocity_6h < 0


def test_changed_input_at_the_same_as_of_conflicts_loudly(tmp_path: Path) -> None:
    database, player_ids = _feature_database(tmp_path, "origin")
    as_of = BASE_TIME + timedelta(hours=8)
    with connect_database(database) as connection:
        build_episodes(connection, as_of=as_of, built_at=as_of + timedelta(minutes=5))
        build_features(
            connection, slate_id=1, site="dk", as_of=as_of, built_at=as_of + timedelta(minutes=10)
        )
        # A baseline observed before as_of but stored afterwards changes the inputs.
        _seed_ownership(
            connection, player_ids[0], ownership=0.3, observed_at=as_of - timedelta(hours=1)
        )
        with pytest.raises(FeatureSnapshotConflictError):
            build_features(
                connection,
                slate_id=1,
                site="dk",
                as_of=as_of,
                built_at=as_of + timedelta(minutes=20),
            )


def test_zero_variance_channels_standardize_to_zero(tmp_path: Path) -> None:
    database, _ = _feature_database(tmp_path, "origin", player_count=3)
    as_of = BASE_TIME + timedelta(hours=8)
    with connect_database(database) as connection:
        build_episodes(connection, as_of=as_of, built_at=as_of + timedelta(minutes=5))
        build_features(
            connection, slate_id=1, site="dk", as_of=as_of, built_at=as_of + timedelta(minutes=10)
        )
        rows = load_feature_rows(
            connection, slate_id=1, site="dk", as_of=as_of, feature_version="narrative-heat-v1"
        )

    assert len(rows) == 3
    # No episode has a DFS or team/fan origin, so those channels are constant across the
    # pool: z must be exactly 0, never NaN.
    assert all(row.h_dfs_z == 0.0 and row.h_team_fan_z == 0.0 for row in rows)
    assert all(math.isfinite(row.h_signed_z) for row in rows)
