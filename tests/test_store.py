import math
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from narrative_alpha.quant import (
    QuantileInterpretation,
    fit_player_distribution_with_diagnostics,
)
from narrative_alpha.store import (
    ContestPayoutRow,
    ContestRow,
    DecisionManifestHash,
    DecisionSnapshotRow,
    MigrationError,
    ModelRunRow,
    PlayerDistributionCreate,
    PlayerDistributionRow,
    PlayerDistributionSourceRef,
    PlayerDistributionStoreError,
    PlayerRow,
    ProjectionSnapshotRow,
    SlateRow,
    TeamRow,
    apply_migrations,
    canonical_distribution_source_set,
    connect_database,
    distribution_source_set_sha256,
    insert_player_distribution,
    manifest_hash_set_sha256,
)
from narrative_alpha.store.models import StoreRow

POINT_IN_TIME_COLUMNS = {
    "published_at",
    "observed_at",
    "ingested_at",
    "effective_at",
    "valid_from",
    "valid_to",
    "source_version",
    "run_id",
}
EXTERNAL_TABLES = {
    "claim_evidence_refs",
    "claim_player_refs",
    "claims",
    "contest_payouts",
    "contests",
    "teams",
    "players",
    "player_aliases",
    "external_player_ids",
    "player_team_history",
    "games",
    "slates",
    "salaries",
    "projection_snapshots",
    "prompt_versions",
    "player_distributions",
    "ownership_baselines",
    "actual_ownership",
    "odds_snapshots",
    "weather_snapshots",
    "results",
    "sources",
    "source_policies",
    "source_items",
    "source_item_extractions",
    "source_item_review_flags",
    "content_tombstones",
    "model_evals",
    "narrative_episodes",
    "episode_claims",
    "narrative_features",
}


def test_store_rows_serialize_utc_timestamps_at_fixed_microsecond_width() -> None:
    instant = datetime(2026, 9, 13, 16, 55, tzinfo=UTC)
    row = PlayerRow(
        player_id=1,
        player_key="player-1",
        canonical_name="Player One",
        position="WR",
        birth_date=None,
        source="fixture",
        published_at=None,
        observed_at=instant,
        ingested_at=instant,
        effective_at=None,
        valid_from=instant,
        valid_to=instant + timedelta(microseconds=1),
        source_version=None,
        run_id=None,
    )

    values = row.db_values()
    assert values["observed_at"] == "2026-09-13T16:55:00.000000Z"
    assert values["valid_to"] == "2026-09-13T16:55:00.000001Z"


def test_migration_runner_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "store.sqlite3"

    with connect_database(database_path) as connection:
        first = apply_migrations(connection)
        second = apply_migrations(connection)
        records = connection.execute(
            "SELECT version, name, sha256 FROM applied_migrations"
        ).fetchall()

    assert [migration.version for migration in first] == [
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
    ]
    assert second == ()
    assert len(records) == 15
    assert records[0][0] == 1
    assert records[0][1] == "0001_phase_0_1_schema.sql"
    assert len(records[0][2]) == 64
    assert records[1][0] == 2
    assert records[1][1] == "0002_identity_crosswalk.sql"
    assert len(records[1][2]) == 64
    assert records[2][0] == 3
    assert records[2][1] == "0003_contests_and_payouts.sql"
    assert len(records[2][2]) == 64
    assert records[3][0] == 4
    assert records[3][1] == "0004_player_distributions.sql"
    assert len(records[3][2]) == 64
    assert records[4][0] == 5
    assert records[4][1] == "0005_narrative_sources.sql"
    assert len(records[4][2]) == 64
    assert records[5][0] == 6
    assert records[5][1] == "0006_versioned_sources.sql"
    assert len(records[5][2]) == 64
    assert records[6][0] == 7
    assert records[6][1] == "0007_stage_1_extraction.sql"
    assert len(records[6][2]) == 64
    assert records[8][0] == 9
    assert records[8][1] == "0009_stage_1_evaluations.sql"
    assert len(records[8][2]) == 64
    assert records[9][0] == 10
    assert records[9][1] == "0010_narrative_episodes.sql"
    assert len(records[9][2]) == 64
    assert records[10][0] == 11
    assert records[10][1] == "0011_narrative_features.sql"
    assert len(records[10][2]) == 64
    assert records[13][0] == 14
    assert records[13][1] == "0014_ops_runs_episodes_step.sql"
    assert len(records[13][2]) == 64
    assert records[14][0] == 15
    assert records[14][1] == "0015_fast_lane_availability.sql"
    assert len(records[14][2]) == 64


def test_source_versioning_migration_preserves_existing_narrative_rows(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "store.sqlite3"
    legacy_migrations = tmp_path / "legacy_migrations"
    legacy_migrations.mkdir()
    migrations_path = Path("src/narrative_alpha/store/migrations")
    for version in range(1, 6):
        source_path = next(migrations_path.glob(f"{version:04d}_*.sql"))
        (legacy_migrations / source_path.name).write_bytes(source_path.read_bytes())

    timestamp = "2026-08-01T12:00:00.000000Z"
    point_in_time_columns = (
        "source, published_at, observed_at, ingested_at, effective_at, "
        "valid_from, valid_to, source_version, run_id"
    )
    point_in_time_values = "'fixture', NULL, :at, :at, NULL, :at, NULL, 'v1', NULL"
    with connect_database(database_path) as connection:
        apply_migrations(connection, legacy_migrations)
        connection.execute(
            f"""
            INSERT INTO sources(
                source_id, display_name, source_family, collector_kind, feed_url,
                enabled, {point_in_time_columns}
            ) VALUES (
                'fixture', 'Fixture', 'official_team', 'rss_atom',
                'https://example.test/feed.xml', 1, {point_in_time_values}
            )
            """,
            {"at": timestamp},
        )
        connection.execute(
            f"""
            INSERT INTO source_policies(
                source_id, permitted_use, raw_retention_days,
                personal_data_fields_allowed, must_honor_deletions,
                redistribution_allowed, third_party_processing_allowed,
                commercial_use_status, terms_reviewed_at, {point_in_time_columns}
            ) VALUES (
                'fixture', 'internal', 7, '[]', 1, 0, 1, 'prohibited', :at,
                {point_in_time_values}
            )
            """,
            {"at": timestamp},
        )
        connection.execute(
            f"""
            INSERT INTO source_items(
                source_id, external_item_id, canonical_url, title, raw_content,
                cleaned_text, content_sha256, {point_in_time_columns}
            ) VALUES (
                'fixture', 'item-1', NULL, 'Title', X'74657874', 'text',
                :content_hash, {point_in_time_values}
            )
            """,
            {"at": timestamp, "content_hash": "a" * 64},
        )
        item_id = int(connection.execute("SELECT source_item_id FROM source_items").fetchone()[0])
        connection.execute(
            f"""
            INSERT INTO content_tombstones(
                source_item_id, source_id, content_sha256, reason, tombstoned_at,
                {point_in_time_columns}
            ) VALUES (
                :item_id, 'fixture', :content_hash, 'platform_deleted', :at,
                {point_in_time_values}
            )
            """,
            {"at": timestamp, "content_hash": "a" * 64, "item_id": item_id},
        )

        applied = apply_migrations(connection)
        counts = {
            table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in (
                "source_keys",
                "sources",
                "source_policies",
                "source_items",
                "content_tombstones",
            )
        }
        retained_text = connection.execute(
            "SELECT title, raw_content, cleaned_text FROM source_items"
        ).fetchone()
        foreign_key_problems = connection.execute("PRAGMA foreign_key_check").fetchall()

        assert [migration.version for migration in applied] == [
            6,
            7,
            8,
            9,
            10,
            11,
            12,
            13,
            14,
            15,
        ]
    assert counts == {
        "source_keys": 1,
        "sources": 1,
        "source_policies": 1,
        "source_items": 1,
        "content_tombstones": 1,
    }
    assert tuple(retained_text) == (None, None, None)
    assert foreign_key_problems == []


def test_stage1_eval_migration_backfills_existing_recovery_parent_timestamps(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "store.sqlite3"
    prior_migrations = tmp_path / "prior_migrations"
    prior_migrations.mkdir()
    migrations_path = Path("src/narrative_alpha/store/migrations")
    for version in range(1, 9):
        source_path = next(migrations_path.glob(f"{version:04d}_*.sql"))
        (prior_migrations / source_path.name).write_bytes(source_path.read_bytes())
    timestamp = "2026-09-02T12:00:00.000000Z"

    with connect_database(database_path) as connection:
        apply_migrations(connection, prior_migrations)
        for run_id in ("parent", "child"):
            connection.execute(
                """
                INSERT INTO model_runs(
                    run_id, run_type, started_at, completed_at, status, code_version,
                    config_sha256, parent_run_id, error_message, created_at
                ) VALUES (?, 'stage_1_extraction', ?, ?, 'succeeded', 'fixture',
                          NULL, NULL, NULL, ?)
                """,
                (run_id, timestamp, timestamp, timestamp),
            )
        connection.execute(
            """
            INSERT INTO model_run_parents(child_run_id, parent_run_id, relationship)
            VALUES ('child', 'parent', 'stage1_recovery')
            """
        )
        connection.commit()

        applied = apply_migrations(connection)
        parent = connection.execute("SELECT * FROM model_run_parents").fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE model_run_parents SET observed_at = ?",
                ("2026-09-02T12:00:01.000000Z",),
            )

        assert [migration.version for migration in applied] == [9, 10, 11, 12, 13, 14, 15]
    assert parent["observed_at"] == timestamp
    assert parent["ingested_at"] == timestamp


@pytest.mark.parametrize(
    ("legacy_content_hash", "legacy_tombstoned_at", "message"),
    [
        ("b" * 64, "2026-08-01T12:00:00.000000Z", "does not match its source item"),
        ("a" * 64, "not-a-date", "non-canonical UTC timestamp"),
    ],
)
def test_stage1_migration_rejects_irreparable_legacy_tombstones(
    tmp_path: Path,
    legacy_content_hash: str,
    legacy_tombstoned_at: str,
    message: str,
) -> None:
    database_path = tmp_path / "store.sqlite3"
    legacy_migrations = tmp_path / "legacy_migrations"
    legacy_migrations.mkdir()
    migrations_path = Path("src/narrative_alpha/store/migrations")
    for version in range(1, 7):
        source_path = next(migrations_path.glob(f"{version:04d}_*.sql"))
        (legacy_migrations / source_path.name).write_bytes(source_path.read_bytes())

    timestamp = "2026-08-01T12:00:00.000000Z"
    with connect_database(database_path) as connection:
        apply_migrations(connection, legacy_migrations)
        connection.execute("INSERT INTO source_keys(source_id) VALUES ('fixture')")
        cursor = connection.execute(
            """
            INSERT INTO source_items(
                source_id, external_item_id, canonical_url, title, raw_content,
                cleaned_text, content_sha256, source, published_at, observed_at,
                ingested_at, effective_at, valid_from, valid_to, source_version, run_id
            ) VALUES (
                'fixture', 'legacy-item', NULL, 'Legacy title', X'74657874', 'text',
                ?, 'fixture', NULL, ?, ?, NULL, ?, NULL, 'v1', NULL
            )
            """,
            ("a" * 64, timestamp, timestamp, timestamp),
        )
        assert cursor.lastrowid is not None
        connection.execute(
            """
            INSERT INTO content_tombstones(
                source_item_id, source_id, content_sha256, reason, tombstoned_at,
                source, published_at, observed_at, ingested_at, effective_at,
                valid_from, valid_to, source_version, run_id
            ) VALUES (?, 'fixture', ?, 'platform_deleted', ?, 'fixture', NULL,
                      ?, ?, NULL, ?, NULL, 'v1', NULL)
            """,
            (
                int(cursor.lastrowid),
                legacy_content_hash,
                legacy_tombstoned_at,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        with pytest.raises(MigrationError, match=message):
            apply_migrations(connection)
        applied_versions = {
            int(row[0]) for row in connection.execute("SELECT version FROM applied_migrations")
        }

    assert applied_versions == {1, 2, 3, 4, 5, 6}


def test_each_migration_is_transactional(tmp_path: Path) -> None:
    database_path = tmp_path / "store.sqlite3"
    migrations_path = tmp_path / "migrations"
    migrations_path.mkdir()
    (migrations_path / "0001_broken.sql").write_text(
        "CREATE TABLE should_roll_back(id INTEGER PRIMARY KEY);\n"
        "INSERT INTO table_that_does_not_exist VALUES (1);\n",
        encoding="utf-8",
    )

    with connect_database(database_path) as connection:
        with pytest.raises(MigrationError, match=r"0001_broken\.sql failed"):
            apply_migrations(connection, migrations_path)
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'should_roll_back'"
        ).fetchone()
        applied_count = connection.execute("SELECT count(*) FROM applied_migrations").fetchone()[0]

    assert table is None
    assert applied_count == 0


def test_connection_enables_wal_foreign_keys_and_recursive_triggers(
    tmp_path: Path,
) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 1


def test_store_interval_guards_preserve_microseconds_and_require_canonical_utc(
    tmp_path: Path,
) -> None:
    database = tmp_path / "store.sqlite3"
    observed_at = "2026-01-01T00:00:00.000000Z"
    one_microsecond_later = "2026-01-01T00:00:00.000001Z"
    insert_source = """
        INSERT INTO sources(
            source_id, display_name, source_family, collector_kind, feed_url,
            enabled, source, published_at, observed_at, ingested_at, effective_at,
            valid_from, valid_to, source_version, run_id
        ) VALUES (?, 'Fixture', 'official_team', 'rss_atom', ?, 1, 'fixture',
                  NULL, ?, ?, NULL, ?, ?, 'v1', NULL)
    """
    with connect_database(database) as connection:
        apply_migrations(connection)
        connection.executemany(
            "INSERT INTO source_keys(source_id) VALUES (?)",
            [("micro-close",), ("offset-close",), ("reversed",)],
        )
        connection.execute(
            insert_source,
            (
                "micro-close",
                "https://example.test/micro-close.xml",
                observed_at,
                observed_at,
                observed_at,
                None,
            ),
        )
        # A one-microsecond interval closes cleanly: lexical order on canonical text is exact.
        connection.execute(
            "UPDATE sources SET valid_to = ? WHERE source_id = 'micro-close'",
            (one_microsecond_later,),
        )
        # Non-canonical spellings (offsets, compact fractions) are refused at insert so the
        # UNIQUE(source_id, observed_at) and interval checks can never see two spellings of
        # one instant.
        for spelling in ("2026-01-02T00:00:00+14:00", "2026-01-02T00:00:00Z", "2026-01-02"):
            with pytest.raises(sqlite3.IntegrityError, match="canonical UTC"):
                connection.execute(
                    insert_source,
                    (
                        "offset-close",
                        "https://example.test/offset-close.xml",
                        observed_at,
                        observed_at,
                        spelling,
                        None,
                    ),
                )
        with pytest.raises(sqlite3.IntegrityError, match="valid_to must be later"):
            connection.execute(
                insert_source,
                (
                    "reversed",
                    "https://example.test/reversed.xml",
                    observed_at,
                    observed_at,
                    one_microsecond_later,
                    observed_at,
                ),
            )
        closed = connection.execute(
            "SELECT valid_to FROM sources WHERE source_id = 'micro-close'"
        ).fetchone()[0]

    assert closed == one_microsecond_later


def test_migration_runner_disables_legacy_alter_table_and_keeps_tombstone_keys(
    tmp_path: Path,
) -> None:
    connection = sqlite3.connect(tmp_path / "raw.sqlite3")
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA legacy_alter_table = ON")
        apply_migrations(connection)
        assert connection.execute("PRAGMA legacy_alter_table").fetchone()[0] == 0
        # No registered SQL function is required: a bare sqlite3 connection can write.
        connection.execute("INSERT INTO source_keys(source_id) VALUES ('bare')")
        tombstone_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(content_tombstones)"
        ).fetchall()
        assert {str(row[2]) for row in tombstone_foreign_keys} >= {
            "model_runs",
            "source_items",
            "source_keys",
        }
        assert all(str(row[2]) != "source_items_stage1" for row in tombstone_foreign_keys)
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE sql LIKE '%na_timestamp_after%'"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_stage1_migration_rejects_utc_reversed_legacy_interval(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "store.sqlite3"
    legacy_migrations = tmp_path / "legacy_migrations"
    legacy_migrations.mkdir()
    migrations_path = Path("src/narrative_alpha/store/migrations")
    for version in range(1, 7):
        source_path = next(migrations_path.glob(f"{version:04d}_*.sql"))
        (legacy_migrations / source_path.name).write_bytes(source_path.read_bytes())

    observed_at = "2026-01-01T00:00:00.000000Z"
    with connect_database(database_path) as connection:
        apply_migrations(connection, legacy_migrations)
        connection.execute("INSERT INTO source_keys(source_id) VALUES ('fixture')")
        connection.execute(
            """
            INSERT INTO sources(
                source_id, display_name, source_family, collector_kind, feed_url,
                enabled, source, published_at, observed_at, ingested_at, effective_at,
                valid_from, valid_to, source_version, run_id
            ) VALUES (
                'fixture', 'Fixture', 'official_team', 'rss_atom',
                'https://example.test/feed.xml', 1, 'fixture', NULL, ?, ?, NULL,
                '2026-08-01T00:00:00-12:00', '2026-08-01T23:00:00+14:00',
                'v1', NULL
            )
            """,
            (observed_at, observed_at),
        )

        with pytest.raises(
            MigrationError,
            match=r"source configuration.*invalid timestamp",
        ):
            apply_migrations(connection)


def test_foreign_key_enforcement_rejects_orphan(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                """
                INSERT INTO decision_snapshots(
                    decision_snapshot_id, slate_id, decision_at, created_at,
                    manifest_schema_version, manifest_hashes_json,
                    manifest_hash_set_sha256, run_id, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "orphan",
                    999,
                    "2026-09-13T16:55:00Z",
                    "2026-09-13T16:55:00Z",
                    "1.0",
                    "[]",
                    "a" * 64,
                    None,
                    None,
                ),
            )


def test_external_tables_have_point_in_time_columns(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        for table in EXTERNAL_TABLES:
            columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            assert columns >= POINT_IN_TIME_COLUMNS, table


def test_typed_rows_round_trip_for_identity_projection_and_decision_snapshot(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "store.sqlite3"
    observed_at = datetime(2026, 9, 10, 14, 0, tzinfo=UTC)
    decision_at = datetime(2026, 9, 13, 16, 55, tzinfo=UTC)
    point_in_time = {
        "source": "fixture",
        "published_at": None,
        "observed_at": observed_at,
        "ingested_at": observed_at,
        "effective_at": None,
        "valid_from": observed_at,
        "valid_to": None,
        "source_version": "fixture-v1",
        "run_id": "run-1",
    }
    model_run = ModelRunRow(
        run_id="run-1",
        run_type="fixture",
        started_at=observed_at,
        completed_at=observed_at,
        status="succeeded",
        code_version="test",
        config_sha256=None,
        parent_run_id=None,
        error_message=None,
        created_at=observed_at,
    )
    team = TeamRow(
        team_id=1,
        team_key="GB",
        abbreviation="GB",
        canonical_name="Green Bay Packers",
        **point_in_time,
    )
    player = PlayerRow(
        player_id=1,
        player_key="player-1",
        canonical_name="Example Player",
        position="WR",
        birth_date=None,
        **point_in_time,
    )
    slate = SlateRow(
        slate_id=1,
        external_slate_id="dk-main-1",
        site="draftkings",
        slate_type="classic",
        season=2026,
        week=1,
        name="Sunday Main",
        starts_at=datetime(2026, 9, 13, 17, 0, tzinfo=UTC),
        locks_at=datetime(2026, 9, 13, 17, 0, tzinfo=UTC),
        **point_in_time,
    )
    projection = ProjectionSnapshotRow(
        projection_snapshot_id=1,
        slate_id=1,
        player_id=1,
        site="draftkings",
        projection_mean=11.331484530668263,
        projection_floor=5.268835182960364,
        projection_ceiling=18.979527073347107,
        ownership_projection=0.17,
        source_file_sha256="b" * 64,
        **{**point_in_time, "source": "Fixture"},
    )
    distribution_source_set = (
        PlayerDistributionSourceRef(
            projection_snapshot_id=1,
            source=" FIXTURE ",
            source_file_sha256="b" * 64,
        ),
    )
    quantile_interpretation = QuantileInterpretation(0.1, 0.9)
    fit_result = fit_player_distribution_with_diagnostics(
        source="fixture",
        position="WR",
        mean=11.331484530668263,
        floor=5.268835182960364,
        ceiling=18.979527073347107,
        p_active=0.93,
        p_full_role_given_active=0.82,
        quantile_configuration={("fixture", "WR"): quantile_interpretation},
        tolerance=1e-6,
    )
    distribution_create = PlayerDistributionCreate(
        slate_id=1,
        player_id=1,
        source_set_json=distribution_source_set,
        as_of_at=observed_at,
        **point_in_time,
    )
    create_values = distribution_create.model_dump(mode="python")
    with pytest.raises(ValueError, match="as_of_at must not be later"):
        PlayerDistributionCreate.model_validate(
            {**create_values, "as_of_at": observed_at + timedelta(minutes=1)}
        )
    contest = ContestRow(
        contest_id=1,
        external_contest_id="dk-contest-1",
        site="draftkings",
        slate_id=1,
        archetype="single_entry",
        field_size=100,
        entry_limit=1,
        entry_fee_cents=100,
        total_prizes_cents=9_000,
        payout_curve_id="dk-contest-1-payouts",
        **point_in_time,
    )
    payout = ContestPayoutRow(
        contest_payout_id=1,
        payout_curve_id="dk-contest-1-payouts",
        rank_from=1,
        rank_to=3,
        prize_cents=3_000,
        **point_in_time,
    )
    manifest_hashes = (
        DecisionManifestHash(
            artifact_kind="generated_lineups",
            sha256="d" * 64,
            path="lineups/upload.csv",
            source="optimizer",
        ),
        DecisionManifestHash(
            artifact_kind="salary",
            sha256="c" * 64,
            path="salaries/dk.csv",
            source="draftkings",
        ),
    )
    decision_snapshot = DecisionSnapshotRow(
        decision_snapshot_id="decision-1",
        slate_id=1,
        decision_at=decision_at,
        created_at=decision_at,
        manifest_schema_version="1.0",
        manifest_hashes_json=manifest_hashes,
        manifest_hash_set_sha256=manifest_hash_set_sha256(manifest_hashes),
        run_id="run-1",
        note="fixture snapshot",
    )

    with connect_database(database_path) as connection:
        apply_migrations(connection)
        _insert_row(connection, "model_runs", model_run)
        _insert_row(connection, "teams", team)
        _insert_row(connection, "players", player)
        _insert_row(connection, "slates", slate)
        _insert_row(connection, "projection_snapshots", projection)

        player_values = player.model_dump(mode="python")
        future_player = PlayerRow.model_validate(
            {
                **player_values,
                "player_id": 3,
                "player_key": "player-3",
                "observed_at": observed_at + timedelta(minutes=1),
                "ingested_at": observed_at + timedelta(minutes=1),
                "valid_from": observed_at + timedelta(minutes=1),
            }
        )
        expired_player = PlayerRow.model_validate(
            {
                **player_values,
                "player_id": 4,
                "player_key": "player-4",
                "observed_at": observed_at - timedelta(hours=2),
                "valid_from": observed_at - timedelta(hours=2),
                "valid_to": observed_at,
            }
        )
        _insert_row(connection, "players", future_player)
        _insert_row(connection, "players", expired_player)

        slate_values = slate.model_dump(mode="python")
        second_slate = SlateRow.model_validate(
            {
                **slate_values,
                "slate_id": 2,
                "external_slate_id": "dk-main-2",
            }
        )
        future_slate = SlateRow.model_validate(
            {
                **slate_values,
                "slate_id": 3,
                "external_slate_id": "dk-main-3",
                "observed_at": observed_at + timedelta(minutes=1),
                "ingested_at": observed_at + timedelta(minutes=1),
                "valid_from": observed_at + timedelta(minutes=1),
            }
        )
        expired_slate = SlateRow.model_validate(
            {
                **slate_values,
                "slate_id": 4,
                "external_slate_id": "dk-main-4",
                "observed_at": observed_at - timedelta(hours=2),
                "valid_from": observed_at - timedelta(hours=2),
                "valid_to": observed_at,
            }
        )
        _insert_row(connection, "slates", second_slate)
        _insert_row(connection, "slates", future_slate)
        _insert_row(connection, "slates", expired_slate)

        projection_values = projection.model_dump(mode="python")
        expired_projection = ProjectionSnapshotRow.model_validate(
            {
                **projection_values,
                "projection_snapshot_id": 2,
                "source_file_sha256": "d" * 64,
                "observed_at": observed_at - timedelta(hours=2),
                "valid_from": observed_at - timedelta(hours=2),
                "valid_to": observed_at,
            }
        )
        future_valid_projection = ProjectionSnapshotRow.model_validate(
            {
                **projection_values,
                "projection_snapshot_id": 3,
                "source_file_sha256": "f" * 64,
                "observed_at": observed_at - timedelta(hours=1),
                "valid_from": observed_at + timedelta(minutes=1),
                "valid_to": None,
            }
        )
        mismatched_projection = ProjectionSnapshotRow.model_validate(
            {
                **projection_values,
                "projection_snapshot_id": 4,
                "projection_mean": 12.0,
                "source_file_sha256": "1" * 64,
                "observed_at": observed_at - timedelta(hours=3),
                "valid_from": observed_at - timedelta(hours=3),
            }
        )
        wrong_site_projection = ProjectionSnapshotRow.model_validate(
            {
                **projection_values,
                "projection_snapshot_id": 5,
                "site": "fanduel",
                "source_file_sha256": "2" * 64,
            }
        )
        future_observed_projection = ProjectionSnapshotRow.model_validate(
            {
                **projection_values,
                "projection_snapshot_id": 6,
                "source_file_sha256": "3" * 64,
                "observed_at": observed_at + timedelta(minutes=1),
                "ingested_at": observed_at + timedelta(minutes=1),
                "valid_from": observed_at + timedelta(minutes=1),
            }
        )
        _insert_row(connection, "projection_snapshots", expired_projection)
        _insert_row(connection, "projection_snapshots", future_valid_projection)
        _insert_row(connection, "projection_snapshots", mismatched_projection)
        _insert_row(connection, "projection_snapshots", wrong_site_projection)
        _insert_row(connection, "projection_snapshots", future_observed_projection)
        inserted_distribution = insert_player_distribution(
            connection,
            distribution_create,
            fit_result=fit_result,
        )
        assert inserted_distribution.player_distribution_id == 1
        assert inserted_distribution.source == "fixture"

        distribution_values = inserted_distribution.model_dump(mode="python")
        with pytest.raises(ValueError, match="source_set_sha256 does not match"):
            PlayerDistributionRow.model_validate(
                {**distribution_values, "source_set_sha256": "f" * 64}
            )
        with pytest.raises(ValueError, match="floor_quantile must be below"):
            PlayerDistributionRow.model_validate(
                {
                    **distribution_values,
                    "floor_quantile": 0.95,
                    "ceiling_quantile": 0.05,
                }
            )
        with pytest.raises(ValueError, match="as_of_at must not be later"):
            PlayerDistributionRow.model_validate(
                {
                    **distribution_values,
                    "as_of_at": observed_at + timedelta(minutes=1),
                }
            )
        with pytest.raises(ValueError):
            PlayerDistributionRow.model_validate({**distribution_values, "conditional_scale": 0.0})
        with pytest.raises(ValueError):
            PlayerDistributionRow.model_validate(
                {**distribution_values, "conditional_shape": math.inf}
            )
        with pytest.raises(ValueError, match="fit provenance is internally inconsistent"):
            PlayerDistributionRow.model_validate(
                {**distribution_values, "fit_config_sha256": "e" * 64}
            )
        with pytest.raises(ValueError, match="fit provenance is internally inconsistent"):
            PlayerDistributionRow.model_validate(
                {**distribution_values, "fitter_version": "forged-fitter"}
            )
        with pytest.raises(ValueError, match="fit provenance is internally inconsistent"):
            PlayerDistributionRow.model_validate(
                {
                    **distribution_values,
                    "fit_max_relative_error": fit_result.fit_tolerance,
                }
            )

        forged_fit_result = fit_result.model_copy(
            update={
                "fit_config_sha256": "e" * 64,
                "fit_max_relative_error": 0.0,
                "fitter_version": "forged-fitter",
            }
        )
        with pytest.raises(
            PlayerDistributionStoreError,
            match="validated fit result is internally inconsistent",
        ):
            insert_player_distribution(
                connection,
                distribution_create,
                fit_result=forged_fit_result,
            )
        assert connection.execute("SELECT count(*) FROM player_distributions").fetchone()[0] == 1
        forged_create = distribution_create.model_copy(
            update={"as_of_at": observed_at + timedelta(minutes=1)}
        )
        with pytest.raises(
            PlayerDistributionStoreError,
            match="create metadata is internally inconsistent",
        ):
            insert_player_distribution(
                connection,
                forged_create,
                fit_result=fit_result,
            )
        assert connection.execute("SELECT count(*) FROM player_distributions").fetchone()[0] == 1

        nonexistent_reference = (
            PlayerDistributionSourceRef(
                projection_snapshot_id=999,
                source="fixture",
                source_file_sha256="b" * 64,
            ),
        )
        with pytest.raises(PlayerDistributionStoreError, match="does not exist"):
            insert_player_distribution(
                connection,
                _distribution_create_variant(
                    distribution_create,
                    source_set=nonexistent_reference,
                ),
                fit_result=fit_result,
            )

        wrong_source_reference = (
            PlayerDistributionSourceRef(
                projection_snapshot_id=1,
                source="wrong-source",
                source_file_sha256="b" * 64,
            ),
        )
        with pytest.raises(PlayerDistributionStoreError, match="source does not match"):
            insert_player_distribution(
                connection,
                _distribution_create_variant(
                    distribution_create,
                    source_set=wrong_source_reference,
                ),
                fit_result=fit_result,
            )

        wrong_hash_reference = (
            PlayerDistributionSourceRef(
                projection_snapshot_id=1,
                source="fixture",
                source_file_sha256="c" * 64,
            ),
        )
        with pytest.raises(PlayerDistributionStoreError, match="file hash does not match"):
            insert_player_distribution(
                connection,
                _distribution_create_variant(
                    distribution_create,
                    source_set=wrong_hash_reference,
                ),
                fit_result=fit_result,
            )

        mismatched_values_reference = (
            PlayerDistributionSourceRef(
                projection_snapshot_id=4,
                source="fixture",
                source_file_sha256="1" * 64,
            ),
        )
        with pytest.raises(PlayerDistributionStoreError, match="values do not match"):
            insert_player_distribution(
                connection,
                _distribution_create_variant(
                    distribution_create,
                    source_set=mismatched_values_reference,
                ),
                fit_result=fit_result,
            )

        wrong_site_reference = (
            PlayerDistributionSourceRef(
                projection_snapshot_id=5,
                source="fixture",
                source_file_sha256="2" * 64,
            ),
        )
        with pytest.raises(PlayerDistributionStoreError, match="site 'fanduel'"):
            insert_player_distribution(
                connection,
                _distribution_create_variant(
                    distribution_create,
                    source_set=wrong_site_reference,
                ),
                fit_result=fit_result,
            )

        future_observed_reference = (
            PlayerDistributionSourceRef(
                projection_snapshot_id=6,
                source="fixture",
                source_file_sha256="3" * 64,
            ),
        )
        with pytest.raises(
            PlayerDistributionStoreError,
            match="projection snapshot 6 was observed after",
        ):
            insert_player_distribution(
                connection,
                _distribution_create_variant(
                    distribution_create,
                    source_set=future_observed_reference,
                ),
                fit_result=fit_result,
            )

        with pytest.raises(PlayerDistributionStoreError, match="belongs to slate 1, not 2"):
            insert_player_distribution(
                connection,
                _distribution_create_variant(distribution_create, slate_id=2),
                fit_result=fit_result,
            )
        with pytest.raises(PlayerDistributionStoreError, match="player 2 does not exist"):
            insert_player_distribution(
                connection,
                _distribution_create_variant(distribution_create, player_id=2),
                fit_result=fit_result,
            )
        with pytest.raises(
            PlayerDistributionStoreError,
            match="player 3 was observed after",
        ):
            insert_player_distribution(
                connection,
                _distribution_create_variant(distribution_create, player_id=3),
                fit_result=fit_result,
            )
        with pytest.raises(PlayerDistributionStoreError, match="player 4 had expired"):
            insert_player_distribution(
                connection,
                _distribution_create_variant(distribution_create, player_id=4),
                fit_result=fit_result,
            )
        with pytest.raises(
            PlayerDistributionStoreError,
            match="slate 3 was observed after",
        ):
            insert_player_distribution(
                connection,
                _distribution_create_variant(distribution_create, slate_id=3),
                fit_result=fit_result,
            )
        with pytest.raises(PlayerDistributionStoreError, match="slate 4 had expired"):
            insert_player_distribution(
                connection,
                _distribution_create_variant(distribution_create, slate_id=4),
                fit_result=fit_result,
            )

        wrong_position_fit = fit_player_distribution_with_diagnostics(
            source="fixture",
            position="TE",
            mean=11.331484530668263,
            floor=5.268835182960364,
            ceiling=18.979527073347107,
            p_active=0.93,
            p_full_role_given_active=0.82,
            quantile_configuration={("fixture", "TE"): quantile_interpretation},
            tolerance=1e-6,
        )
        with pytest.raises(PlayerDistributionStoreError, match="fit position"):
            insert_player_distribution(
                connection,
                distribution_create,
                fit_result=wrong_position_fit,
            )
        with pytest.raises(PlayerDistributionStoreError, match="metadata source"):
            insert_player_distribution(
                connection,
                _distribution_create_variant(
                    distribution_create,
                    source="another-vendor",
                ),
                fit_result=fit_result,
            )
        with pytest.raises(PlayerDistributionStoreError, match="observed after"):
            insert_player_distribution(
                connection,
                _distribution_create_variant(
                    distribution_create,
                    as_of_at=observed_at - timedelta(minutes=1),
                ),
                fit_result=fit_result,
            )

        expired_reference = (
            PlayerDistributionSourceRef(
                projection_snapshot_id=2,
                source="fixture",
                source_file_sha256="d" * 64,
            ),
        )
        with pytest.raises(PlayerDistributionStoreError, match="had expired"):
            insert_player_distribution(
                connection,
                _distribution_create_variant(
                    distribution_create,
                    source_set=expired_reference,
                ),
                fit_result=fit_result,
            )

        future_valid_reference = (
            PlayerDistributionSourceRef(
                projection_snapshot_id=3,
                source="fixture",
                source_file_sha256="f" * 64,
            ),
        )
        with pytest.raises(PlayerDistributionStoreError, match="was not valid"):
            insert_player_distribution(
                connection,
                _distribution_create_variant(
                    distribution_create,
                    source_set=future_valid_reference,
                ),
                fit_result=fit_result,
            )

        with pytest.raises(
            PlayerDistributionStoreError,
            match="requires exactly one projection snapshot",
        ):
            insert_player_distribution(
                connection,
                _distribution_create_variant(
                    distribution_create,
                    source_set=(distribution_source_set[0], expired_reference[0]),
                ),
                fit_result=fit_result,
            )

        invalid_probability = inserted_distribution.db_values()
        invalid_probability["player_distribution_id"] = 8
        invalid_probability["as_of_at"] = "2026-09-10T13:59:00Z"
        invalid_probability["p_active"] = 1.1
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _insert_values(connection, "player_distributions", invalid_probability)

        invalid_source_set = inserted_distribution.db_values()
        invalid_source_set["player_distribution_id"] = 9
        invalid_source_set["as_of_at"] = "2026-09-10T13:58:00Z"
        invalid_source_set["source_set_json"] = "{}"
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _insert_values(connection, "player_distributions", invalid_source_set)

        duplicate_distribution = inserted_distribution.db_values()
        duplicate_distribution["player_distribution_id"] = 10
        duplicate_distribution["fit_config_sha256"] = "e" * 64
        duplicate_distribution["fitter_version"] = "alternate-fitter"
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            _insert_values(connection, "player_distributions", duplicate_distribution)

        _insert_row(connection, "contests", contest)
        _insert_row(connection, "contest_payouts", payout)
        _insert_row(connection, "decision_snapshots", decision_snapshot)

        restored_player = PlayerRow.from_db(
            connection.execute("SELECT * FROM players WHERE player_id = 1").fetchone()
        )
        restored_projection = ProjectionSnapshotRow.from_db(
            connection.execute(
                "SELECT * FROM projection_snapshots WHERE projection_snapshot_id = 1"
            ).fetchone()
        )
        restored_distribution = PlayerDistributionRow.from_db(
            connection.execute(
                "SELECT * FROM player_distributions WHERE player_distribution_id = 1"
            ).fetchone()
        )
        restored_contest = ContestRow.from_db(
            connection.execute("SELECT * FROM contests WHERE contest_id = 1").fetchone()
        )
        restored_payout = ContestPayoutRow.from_db(
            connection.execute(
                "SELECT * FROM contest_payouts WHERE contest_payout_id = 1"
            ).fetchone()
        )
        restored_decision = DecisionSnapshotRow.from_db(
            connection.execute(
                "SELECT * FROM decision_snapshots WHERE decision_snapshot_id = 'decision-1'"
            ).fetchone()
        )

    assert restored_player == player
    assert restored_projection == projection
    assert restored_distribution == inserted_distribution
    assert restored_contest == contest
    assert restored_payout == payout
    assert restored_decision == decision_snapshot

    rollback_create = PlayerDistributionCreate.model_validate(
        {
            **distribution_create.model_dump(mode="python"),
            "as_of_at": observed_at + timedelta(minutes=1),
            "ingested_at": observed_at + timedelta(minutes=1),
        }
    )
    with (
        pytest.raises(RuntimeError, match="force surrounding rollback"),
        connect_database(database_path) as connection,
    ):
        apply_migrations(connection)
        assert not connection.in_transaction
        insert_player_distribution(
            connection,
            rollback_create,
            fit_result=fit_result,
        )
        assert connection.in_transaction
        raise RuntimeError("force surrounding rollback")

    with connect_database(database_path) as connection:
        apply_migrations(connection)
        distribution_count = connection.execute(
            "SELECT count(*) FROM player_distributions"
        ).fetchone()[0]
    assert distribution_count == 1


def test_distribution_source_set_is_canonical_and_rejects_duplicate_snapshots() -> None:
    first = PlayerDistributionSourceRef(
        projection_snapshot_id=2,
        source="vendor-b",
        source_file_sha256="b" * 64,
    )
    second = PlayerDistributionSourceRef(
        projection_snapshot_id=1,
        source="vendor-a",
        source_file_sha256="a" * 64,
    )

    assert canonical_distribution_source_set((first, second)) == (
        canonical_distribution_source_set((second, first))
    )
    assert distribution_source_set_sha256((first, second)) == (
        distribution_source_set_sha256((second, first))
    )
    with pytest.raises(ValueError, match="duplicate projection snapshots"):
        distribution_source_set_sha256((first, first))


def _distribution_create_variant(
    distribution: PlayerDistributionCreate,
    *,
    source_set: tuple[PlayerDistributionSourceRef, ...] | None = None,
    as_of_at: datetime | None = None,
    slate_id: int | None = None,
    player_id: int | None = None,
    source: str | None = None,
) -> PlayerDistributionCreate:
    values = distribution.model_dump(mode="python")
    if source_set is not None:
        values["source_set_json"] = source_set
    if as_of_at is not None:
        values["as_of_at"] = as_of_at
    if slate_id is not None:
        values["slate_id"] = slate_id
    if player_id is not None:
        values["player_id"] = player_id
    if source is not None:
        values["source"] = source
    return PlayerDistributionCreate.model_validate(values)


def _insert_row(connection: sqlite3.Connection, table: str, row: StoreRow) -> None:
    _insert_values(connection, table, row.db_values())


def _insert_values(
    connection: sqlite3.Connection,
    table: str,
    values: Mapping[str, object],
) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )
