import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from narrative_alpha.store import (
    ContestPayoutRow,
    ContestRow,
    DecisionManifestHash,
    DecisionSnapshotRow,
    MigrationError,
    ModelRunRow,
    PlayerRow,
    ProjectionSnapshotRow,
    SlateRow,
    TeamRow,
    apply_migrations,
    connect_database,
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
    "ownership_baselines",
    "actual_ownership",
    "odds_snapshots",
    "weather_snapshots",
    "results",
}


def test_migration_runner_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "store.sqlite3"

    with connect_database(database_path) as connection:
        first = apply_migrations(connection)
        second = apply_migrations(connection)
        records = connection.execute(
            "SELECT version, name, sha256 FROM applied_migrations"
        ).fetchall()

    assert [migration.version for migration in first] == [1, 2, 3]
    assert second == ()
    assert len(records) == 3
    assert records[0][0] == 1
    assert records[0][1] == "0001_phase_0_1_schema.sql"
    assert len(records[0][2]) == 64
    assert records[1][0] == 2
    assert records[1][1] == "0002_identity_crosswalk.sql"
    assert len(records[1][2]) == 64
    assert records[2][0] == 3
    assert records[2][1] == "0003_contests_and_payouts.sql"
    assert len(records[2][2]) == 64


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


def test_connection_enables_wal_and_foreign_keys(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


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
        projection_mean=18.5,
        projection_floor=9.0,
        projection_ceiling=30.0,
        ownership_projection=0.17,
        source_file_sha256="b" * 64,
        **point_in_time,
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

    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        _insert_row(connection, "model_runs", model_run)
        _insert_row(connection, "teams", team)
        _insert_row(connection, "players", player)
        _insert_row(connection, "slates", slate)
        _insert_row(connection, "projection_snapshots", projection)
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
    assert restored_contest == contest
    assert restored_payout == payout
    assert restored_decision == decision_snapshot


def _insert_row(connection: sqlite3.Connection, table: str, row: StoreRow) -> None:
    values = row.db_values()
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )
