import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from narrative_alpha.ingest import (
    ProjectedStat,
    SourceFormatError,
    StatsFileKind,
    load_stokastic_stats_capture,
    parse_stokastic_stats,
    read_derived_projection_means,
)
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.slate_cli import main as slate_main
from narrative_alpha.snapshots import CaptureKind, load_manifest
from narrative_alpha.snapshots.cli import main as snapshot_main
from narrative_alpha.store import apply_migrations, connect_database

GOLDEN = Path("tests/golden")
PASSING = GOLDEN / "stokastic_stats_passing.csv"
RUSHING = GOLDEN / "stokastic_stats_rushing.csv"
RECEIVING = GOLDEN / "stokastic_stats_receiving.csv"
CONFIG = Path("config/derived_scoring.toml")
OBSERVED = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("path", "file_kind", "stats_per_row"),
    [
        (PASSING, StatsFileKind.PASSING, 6),
        (RUSHING, StatsFileKind.RUSHING, 4),
        (RECEIVING, StatsFileKind.RECEIVING, 6),
    ],
)
def test_golden_stats_files_parse_exact_real_headers(
    path: Path, file_kind: StatsFileKind, stats_per_row: int
) -> None:
    result = parse_stokastic_stats(path)

    assert result.file_kind is file_kind
    assert result.rows_seen == 12
    assert all(len(row.stats) == stats_per_row for row in result.rows)
    assert result.receptions_are_vendor_placeholder is (file_kind is StatsFileKind.RECEIVING)


def test_receiving_percent_is_syntax_driven_and_derived_columns_are_not_facts() -> None:
    parsed = parse_stokastic_stats(RECEIVING)
    first = parsed.rows[0]

    assert first.stats[ProjectedStat.TARGET_SHARE] == pytest.approx(0.111)
    assert first.stats[ProjectedStat.RECEPTIONS] == 2.6
    assert set(first.stats) == {
        ProjectedStat.TARGETS,
        ProjectedStat.TARGET_SHARE,
        ProjectedStat.RECEPTIONS,
        ProjectedStat.REC_YDS,
        ProjectedStat.REC_TD,
        ProjectedStat.REC_FUMBLES,
    }


def test_drifted_header_names_missing_and_unexpected_columns(tmp_path: Path) -> None:
    drifted = tmp_path / "drifted.csv"
    drifted.write_text(
        PASSING.read_text(encoding="utf-8").replace("Pass Yds", "Passing Yards", 1),
        encoding="utf-8",
    )

    with pytest.raises(SourceFormatError) as raised:
        parse_stokastic_stats(drifted)

    assert "missing columns: Pass Yds" in str(raised.value)
    assert "unexpected columns: Passing Yards" in str(raised.value)


def test_percentage_without_percent_sign_is_refused_even_when_magnitude_looks_valid(
    tmp_path: Path,
) -> None:
    doctored = tmp_path / "receiving.csv"
    doctored.write_text(
        RECEIVING.read_text(encoding="utf-8").replace("11.1%", "0.111", 1),
        encoding="utf-8",
    )

    with pytest.raises(SourceFormatError, match="explicit % sign"):
        parse_stokastic_stats(doctored)


def test_reception_placeholder_passes_golden_and_refuses_doctored_row(tmp_path: Path) -> None:
    assert parse_stokastic_stats(RECEIVING).receptions_are_vendor_placeholder
    doctored = tmp_path / "receiving.csv"
    doctored.write_text(
        RECEIVING.read_text(encoding="utf-8").replace(
            "3.5,11.1%,2.6,75.0%", "3.5,11.1%,2.0,75.0%", 1
        ),
        encoding="utf-8",
    )

    with pytest.raises(SourceFormatError, match=r"not approximately 0.75 x Tgt"):
        parse_stokastic_stats(doctored)


def test_receiving_yards_placeholder_relationship_is_verified(tmp_path: Path) -> None:
    doctored = tmp_path / "receiving.csv"
    doctored.write_text(
        RECEIVING.read_text(encoding="utf-8").replace("2.6,75.0%,19,7.1", "2.6,75.0%,30,7.1", 1),
        encoding="utf-8",
    )

    with pytest.raises(SourceFormatError, match=r"Rec Yds .* x YPC"):
        parse_stokastic_stats(doctored)


def test_capture_load_joins_qb_is_idempotent_and_derives_hand_scored_dk_mean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot_root = tmp_path / "snapshots"
    exit_code = snapshot_main(
        [
            "capture",
            "--season",
            "2026",
            "--week",
            "1",
            "--kind",
            "stats",
            "--source",
            "stokastic",
            "--root",
            str(snapshot_root),
            str(PASSING),
            str(RUSHING),
            str(RECEIVING),
        ]
    )
    assert exit_code == 0
    capture = next((snapshot_root / "2026" / "week_01").iterdir())
    manifest = load_manifest(capture / "manifest.json")
    assert [record.kind for record in manifest.files] == [CaptureKind.STATS] * 3

    database = tmp_path / "store.sqlite3"
    with connect_database(database) as connection:
        apply_migrations(connection)
        player_ids = _seed_fixture_players(connection)
        first = load_stokastic_stats_capture(
            connection,
            capture,
            season=2026,
            week=1,
            site="dk",
            ingested_at=OBSERVED + timedelta(minutes=1),
        )
        duplicate = load_stokastic_stats_capture(
            connection,
            capture,
            season=2026,
            week=1,
            site="dk",
            ingested_at=OBSERVED + timedelta(minutes=2),
        )
        avery_stats = {
            str(row["stat"]): float(row["value"])
            for row in connection.execute(
                "SELECT stat, value FROM projected_stats WHERE player_id = ?",
                (player_ids[("Avery Archer", "ARI")],),
            )
        }
        derived = read_derived_projection_means(
            connection, season=2026, week=1, site="dk", config_path=CONFIG
        )

    expected = 266 * 0.04 + 1.30 * 4 - 0.52 + 7 * 0.1 + 0.05 * 6
    avery = next(row for row in derived if row.canonical_name == "Avery Archer")
    assert first.files_seen == 3
    assert first.rows_seen == 36
    assert first.players_written == 28
    assert first.stat_rows_inserted == 192
    assert first.receptions_are_vendor_placeholder
    assert not first.held
    assert {"pass_yds", "pass_td", "rush_yds", "rush_td"} <= set(avery_stats)
    assert duplicate.players_written == 0
    assert duplicate.stat_rows_inserted == 0
    assert duplicate.duplicate_stat_rows == 192
    assert avery.projection_mean == pytest.approx(expected)
    assert avery.source == "stokastic-stats-derived"
    assert avery.source_version == hashlib.sha256(CONFIG.read_bytes()).hexdigest()

    assert (
        slate_main(
            [
                "load-stats",
                "--database",
                str(database),
                "--season",
                "2026",
                "--week",
                "1",
                "--site",
                "dk",
                "--root",
                str(snapshot_root),
            ]
        )
        == 0
    )
    load_output = capsys.readouterr().out
    assert "STOKASTIC STATS LOAD — 2026 week 01" in load_output
    assert "0 stat row(s) inserted, 192 already loaded" in load_output
    assert "receptions are a vendor placeholder" in load_output

    assert (
        slate_main(
            [
                "stats",
                "--database",
                str(database),
                "--season",
                "2026",
                "--week",
                "1",
                "--site",
                "dk",
                "--config",
                str(CONFIG),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "stokastic-stats-derived" in output
    assert "Avery Archer" in output
    assert "bonuses        excluded" in output


def test_unresolved_identity_is_queued_and_threshold_holds_entire_capture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    passing = tmp_path / "passing.csv"
    rushing = tmp_path / "rushing.csv"
    receiving = tmp_path / "receiving.csv"
    passing.write_text(
        "Player,Team,Opp,Att,Comp,Pass Yds,TD,INT,Fum\n"
        "Known Quarter,ARI,LAC,30,20,240,1.5,0.5,0.01\n"
        "Mystery Quarter,LAC,ARI,30,20,230,1.2,0.4,0.01\n",
        encoding="utf-8",
    )
    rushing.write_text(
        "Player,Team,Opp,Rush,Rush Yds,TD,Fum\n"
        "Known Quarter,ARI,LAC,4,20,0.2,0.01\n"
        "Mystery Quarter,LAC,ARI,3,15,0.1,0.01\n",
        encoding="utf-8",
    )
    receiving.write_text(
        "Player,Team,Opp,Tgt,Tgt %,Rec,Catch %,Rec Yds,YPC,TD,Fum\n"
        "Known Receiver,ARI,LAC,4.0,12.0%,3.0,75.0%,30,10.0,0.2,0.01\n",
        encoding="utf-8",
    )
    snapshot_root = tmp_path / "snapshots"
    assert (
        snapshot_main(
            [
                "capture",
                "--season",
                "2026",
                "--week",
                "1",
                "--kind",
                "stats",
                "--source",
                "stokastic",
                "--root",
                str(snapshot_root),
                str(passing),
                str(rushing),
                str(receiving),
            ]
        )
        == 0
    )
    capture = next((snapshot_root / "2026" / "week_01").iterdir())

    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        _insert_player(connection, "Known Quarter", "ARI")
        _insert_player(connection, "Known Receiver", "ARI")
        report = load_stokastic_stats_capture(
            connection,
            capture,
            season=2026,
            week=1,
            site="dk",
            ingested_at=OBSERVED + timedelta(minutes=1),
        )
        fact_count = int(connection.execute("SELECT count(*) FROM projected_stats").fetchone()[0])
        queued = connection.execute(
            "SELECT name_raw, team FROM unresolved_player_matches WHERE status = 'pending'"
        ).fetchall()

    exit_code = slate_main(
        [
            "load-stats",
            "--database",
            str(tmp_path / "store.sqlite3"),
            "--season",
            "2026",
            "--week",
            "1",
            "--site",
            "dk",
            "--root",
            str(snapshot_root),
        ]
    )
    cli_output = capsys.readouterr().out

    assert report.held
    assert exit_code == 2
    assert "HELD         33.3% of identities unresolved exceeds the 10% limit" in cli_output
    assert "? unresolved Mystery Quarter LAC — na-crosswalk resolve --unresolved-id" in cli_output
    assert report.unresolved_fraction == pytest.approx(1 / 3)
    assert report.max_unresolved_fraction == 0.10
    assert report.players_written == 0
    assert fact_count == 0
    assert [(str(row["name_raw"]), str(row["team"])) for row in queued] == [
        ("Mystery Quarter", "LAC")
    ]


def test_projected_stats_are_insert_only_and_require_canonical_utc(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        player_id = _insert_player(connection, "Known Quarter", "ARI")
        valid = utc_timestamp(OBSERVED)
        connection.execute(
            """
            INSERT INTO projected_stats(
                source, season, week, player_id, stat, value, file_sha256,
                published_at, observed_at, ingested_at, effective_at, valid_from,
                valid_to, source_version, run_id
            ) VALUES ('stokastic', 2026, 1, ?, 'pass_yds', 240, ?, NULL, ?, ?,
                      NULL, ?, NULL, 'stokastic-stats-v1', NULL)
            """,
            (player_id, "a" * 64, valid, valid, valid),
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE projected_stats SET value = 241")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM projected_stats")
        with pytest.raises(sqlite3.IntegrityError, match="canonical UTC"):
            connection.execute(
                """
                INSERT INTO projected_stats(
                    source, season, week, player_id, stat, value, file_sha256,
                    published_at, observed_at, ingested_at, effective_at, valid_from,
                    valid_to, source_version, run_id
                ) VALUES ('stokastic', 2026, 1, ?, 'pass_td', 1, ?, NULL, ?, ?,
                          NULL, ?, NULL, 'stokastic-stats-v1', NULL)
                """,
                (player_id, "b" * 64, "2026-09-05T12:00:00Z", valid, valid),
            )


def _seed_fixture_players(connection: sqlite3.Connection) -> dict[tuple[str, str], int]:
    identities = {
        (row.name_raw, row.team)
        for path in (PASSING, RUSHING, RECEIVING)
        for row in parse_stokastic_stats(path).rows
    }
    return {
        identity: _insert_player(connection, identity[0], identity[1])
        for identity in sorted(identities)
    }


def _insert_player(connection: sqlite3.Connection, name: str, team: str) -> int:
    stamp = utc_timestamp(OBSERVED - timedelta(days=1))
    cursor = connection.execute(
        """
        INSERT INTO players(
            player_key, canonical_name, position, birth_date, source,
            published_at, observed_at, ingested_at, effective_at, valid_from,
            valid_to, source_version, run_id
        ) VALUES (?, ?, NULL, NULL, 'fixture', NULL, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (f"fixture:{team}:{name}", name, stamp, stamp, stamp),
    )
    assert cursor.lastrowid is not None
    player_id = int(cursor.lastrowid)
    connection.execute(
        """
        INSERT INTO player_team_history(
            player_id, team, position, roster_status, season, week, source,
            published_at, observed_at, ingested_at, effective_at, valid_from,
            valid_to, source_version, run_id
        ) VALUES (?, ?, NULL, NULL, 2026, 1, 'fixture', NULL, ?, ?, NULL, ?,
                  NULL, 'fixture-v1', NULL)
        """,
        (player_id, team, stamp, stamp, stamp),
    )
    return player_id
