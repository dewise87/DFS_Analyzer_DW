from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from narrative_alpha.ingest import (
    ContestArchetype,
    ContestMetadata,
    ContestSchemaError,
    ContestStandingsError,
    SalarySite,
    SalarySlateType,
    load_contest_standings,
    parse_contest_standings,
)
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.store import (
    ActualOwnershipRow,
    ResultRow,
    apply_migrations,
    connect_database,
)

GOLDEN = Path(__file__).parent / "golden"
OBSERVED_AT = datetime(2026, 9, 14, 12, tzinfo=UTC)
PRELOCK_AT = datetime(2026, 9, 13, 16, tzinfo=UTC)
CANONICAL_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


@pytest.mark.parametrize(
    ("site", "filename", "field_size", "expected_counts"),
    (
        (
            SalarySite.DRAFTKINGS,
            "draftkings_contest_standings.csv",
            3,
            (2, 2, 2),
        ),
        (
            SalarySite.FANDUEL,
            "fanduel_contest_standings.csv",
            2,
            (1, 2, 2),
        ),
    ),
)
def test_golden_contest_standings_parse_strict_cohort_counts(
    site: SalarySite,
    filename: str,
    field_size: int,
    expected_counts: tuple[int, ...],
) -> None:
    result = parse_contest_standings(
        GOLDEN / filename,
        _metadata(site=site, expected_field_size=field_size),
    )

    assert result.field_size == field_size
    assert result.parse_report.rows_rejected == 0
    assert tuple(row.roster_count for row in result.rows) == expected_counts
    assert all(row.actual_ownership == row.roster_count / field_size for row in result.rows)
    assert {row.role for row in result.rows} == {"classic"}


def test_showdown_roles_remain_separate(tmp_path: Path) -> None:
    standings = tmp_path / "showdown.csv"
    standings.write_text(
        "Rank,EntryId,EntryName,TimeRemaining,Points,Lineup\n"
        "1,e1,One,0,10,CPT Example Passer FLEX Example Passer\n"
        "2,e2,Two,0,8,CPT Other Passer FLEX Example Passer\n"
        "\n"
        "Player,Roster Position,%Drafted,FPTS\n"
        "Example Passer,CPT,50%,24.0\n"
        "Example Passer,FLEX,100%,16.0\n",
        encoding="utf-8",
    )
    metadata = _metadata(
        site=SalarySite.DRAFTKINGS,
        slate_type=SalarySlateType.SHOWDOWN,
        archetype=ContestArchetype.SHOWDOWN,
        expected_field_size=2,
    )

    result = parse_contest_standings(standings, metadata)

    assert [(row.role, row.roster_count) for row in result.rows] == [
        ("captain", 1),
        ("flex", 2),
    ]


def test_schema_drift_is_structured_and_never_guessed(tmp_path: Path) -> None:
    standings = tmp_path / "drifted.csv"
    standings.write_text(
        "Rank,EntryId,EntryName,Points,Lineup,New Column\n"
        "1,e1,One,10,QB Example Passer,x\n\n"
        "Player,Roster Position,%Drafted,FPTS\n"
        "Example Passer,QB,100%,24\n",
        encoding="utf-8",
    )

    with pytest.raises(ContestSchemaError) as raised:
        parse_contest_standings(standings, _metadata(site=SalarySite.DRAFTKINGS))

    assert raised.value.section == "entries"
    assert raised.value.missing_columns == ("timeremaining",)
    assert raised.value.unexpected_columns == ("newcolumn",)


def test_load_is_idempotent_round_trips_typed_rows_and_rejects_cohort_mixing(
    tmp_path: Path,
) -> None:
    database = tmp_path / "results.sqlite3"
    metadata = _metadata(
        site=SalarySite.DRAFTKINGS,
        expected_field_size=3,
        contest_id="dk-probe-1",
    )

    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed_slate(connection)

        first = load_contest_standings(
            connection,
            GOLDEN / "draftkings_contest_standings.csv",
            metadata,
            ingested_at=OBSERVED_AT,
        )
        second = load_contest_standings(
            connection,
            GOLDEN / "draftkings_contest_standings.csv",
            metadata,
            ingested_at=OBSERVED_AT,
        )

        ownership = ActualOwnershipRow.from_db(
            connection.execute(
                "SELECT * FROM actual_ownership ORDER BY actual_ownership_id LIMIT 1"
            ).fetchone()
        )
        result = ResultRow.from_db(
            connection.execute("SELECT * FROM results ORDER BY result_id LIMIT 1").fetchone()
        )

        mixed_cohort = metadata.model_copy(
            update={"contest_archetype": ContestArchetype.THREE_MAX}
        )
        with pytest.raises(ContestStandingsError, match="different cohort metadata"):
            load_contest_standings(
                connection,
                GOLDEN / "draftkings_contest_standings.csv",
                mixed_cohort,
                ingested_at=OBSERVED_AT,
            )

    assert first.ownership_rows_inserted == 3
    assert first.result_rows_inserted == 3
    assert first.ok
    assert second.ownership_rows_inserted == 0
    assert second.result_rows_inserted == 0
    assert second.duplicate_rows == 6
    assert ownership.external_contest_id == "dk-probe-1"
    assert ownership.contest_archetype == "single_entry"
    assert ownership.lineup_count == 3
    assert result.stat_line_json == {
        "contest_id": "dk-probe-1",
        "roster_position": "QB",
    }


def _write_dk_showdown_standings(path: Path, athlete_lines: str) -> None:
    path.write_text(
        "Rank,EntryId,EntryName,TimeRemaining,Points,Lineup\n"
        "1,e1,One,0,40.0,CPT Example Passer FLEX Sample Runner\n"
        "2,e2,Two,0,38.0,CPT Sample Runner FLEX Example Passer\n"
        "\n"
        "Player,Roster Position,%Drafted,FPTS\n" + athlete_lines,
        encoding="utf-8",
    )


def _showdown_metadata(contest_id: str) -> ContestMetadata:
    return _metadata(
        site=SalarySite.DRAFTKINGS,
        slate_type=SalarySlateType.SHOWDOWN,
        archetype=ContestArchetype.SHOWDOWN,
        expected_field_size=2,
        contest_id=contest_id,
    )


def _seed_showdown(connection: sqlite3.Connection) -> None:
    _seed_league(connection)
    _seed_contest_slate(
        connection, slate_id=1, site="draftkings", slate_type="showdown", salary_id_offset=0
    )


def test_showdown_load_stores_base_fpts_and_keeps_per_role_ownership(tmp_path: Path) -> None:
    standings = tmp_path / "showdown.csv"
    _write_dk_showdown_standings(
        standings,
        "Example Passer,CPT,50%,24.00\n"
        "Example Passer,FLEX,50%,16.00\n"
        "Sample Runner,CPT,50%,21.75\n"
        "Sample Runner,FLEX,50%,14.50\n",
    )

    with connect_database(tmp_path / "results.sqlite3") as connection:
        apply_migrations(connection)
        _seed_showdown(connection)
        report = load_contest_standings(
            connection,
            standings,
            _showdown_metadata("dk-showdown-1"),
            ingested_at=OBSERVED_AT,
        )
        results = connection.execute(
            "SELECT player_id, fantasy_points, stat_line_json FROM results ORDER BY player_id"
        ).fetchall()
        ownership = connection.execute(
            "SELECT player_id, role, actual_ownership FROM actual_ownership "
            "ORDER BY player_id, role"
        ).fetchall()

    assert report.errors == ()
    assert report.ok
    assert report.ownership_rows_inserted == 4
    assert report.result_rows_inserted == 2
    assert [(row["player_id"], row["fantasy_points"]) for row in results] == [
        (1, 16.0),
        (2, 14.5),
    ]
    assert all('"roster_position":"FLEX"' in row["stat_line_json"] for row in results)
    assert [(row["player_id"], row["role"], row["actual_ownership"]) for row in ownership] == [
        (1, "captain", 0.5),
        (1, "flex", 0.5),
        (2, "captain", 0.5),
        (2, "flex", 0.5),
    ]


def test_showdown_captain_only_player_is_a_structured_error(tmp_path: Path) -> None:
    standings = tmp_path / "showdown.csv"
    _write_dk_showdown_standings(
        standings,
        "Example Passer,CPT,100%,24.00\nSample Runner,FLEX,50%,14.50\n",
    )

    with connect_database(tmp_path / "results.sqlite3") as connection:
        apply_migrations(connection)
        _seed_showdown(connection)
        report = load_contest_standings(
            connection,
            standings,
            _showdown_metadata("dk-showdown-2"),
            ingested_at=OBSERVED_AT,
        )
        result_players = connection.execute(
            "SELECT player_id FROM results ORDER BY player_id"
        ).fetchall()

    assert report.ok is False
    assert len(report.errors) == 1
    assert "Example Passer" in report.errors[0]
    assert "only at captain" in report.errors[0]
    assert report.ownership_rows_inserted == 2
    assert [row["player_id"] for row in result_players] == [2]


def test_showdown_captain_fpts_not_multiplied_is_a_conflict(tmp_path: Path) -> None:
    standings = tmp_path / "showdown.csv"
    _write_dk_showdown_standings(
        standings,
        "Example Passer,CPT,50%,30.00\nExample Passer,FLEX,50%,16.00\n",
    )

    with connect_database(tmp_path / "results.sqlite3") as connection:
        apply_migrations(connection)
        _seed_showdown(connection)
        report = load_contest_standings(
            connection,
            standings,
            _showdown_metadata("dk-showdown-3"),
            ingested_at=OBSERVED_AT,
        )
        stored_results = connection.execute("SELECT COUNT(*) FROM results").fetchone()

    assert report.ok is False
    assert len(report.errors) == 1
    assert "not 1.5x" in report.errors[0]
    assert report.result_rows_inserted == 0
    assert tuple(stored_results) == (0,)


def test_bare_value_in_percentage_named_column_parses_as_percentage(tmp_path: Path) -> None:
    standings = tmp_path / "large_field.csv"
    entries = "".join(
        f"{rank},e{rank},Entry{rank},0,100,QB Example Passer\n" for rank in range(1, 251)
    )
    standings.write_text(
        "Rank,EntryId,EntryName,TimeRemaining,Points,Lineup\n"
        + entries
        + "\nPlayer,Roster Position,%Drafted,FPTS\nExample Passer,QB,0.8,20.0\n",
        encoding="utf-8",
    )

    result = parse_contest_standings(
        standings,
        _metadata(site=SalarySite.DRAFTKINGS, expected_field_size=250),
    )

    assert result.parse_report.rows_rejected == 0
    row = result.rows[0]
    assert row.reported_ownership == pytest.approx(0.008)
    assert row.roster_count == 2
    assert row.actual_ownership == pytest.approx(0.008)


def test_bare_ownership_value_without_percentage_context_is_rejected(tmp_path: Path) -> None:
    standings = tmp_path / "unnamed_units.csv"
    standings.write_text(
        "Rank,EntryId,EntryName,TimeRemaining,Points,Lineup\n"
        "1,e1,One,0,10,QB Example Passer\n"
        "2,e2,Two,0,9,QB Example Passer\n"
        "\n"
        "Player,Roster Position,Drafted,FPTS\n"
        "Example Passer,QB,0.8,20.0\n",
        encoding="utf-8",
    )

    result = parse_contest_standings(
        standings,
        _metadata(site=SalarySite.DRAFTKINGS, expected_field_size=2),
    )

    assert result.rows == ()
    assert result.parse_report.rows_rejected == 1
    rejected = result.parse_report.rejected[0]
    assert rejected.section == "athletes"
    assert any("refusing to guess" in reason for reason in rejected.reasons)


def test_malformed_entry_rows_do_not_count_toward_field_size(tmp_path: Path) -> None:
    standings = tmp_path / "malformed_entry.csv"
    standings.write_text(
        "Rank,EntryId,EntryName,TimeRemaining,Points,Lineup\n"
        "1,e1,One,0,10,QB Example Passer\n"
        "2,,Two,0,9,QB Example Passer\n"
        "3,e3,Three,0,8,QB Example Passer\n"
        "\n"
        "Player,Roster Position,%Drafted,FPTS\n"
        "Example Passer,QB,100%,20.0\n",
        encoding="utf-8",
    )

    result = parse_contest_standings(
        standings,
        _metadata(site=SalarySite.DRAFTKINGS, expected_field_size=2),
    )

    assert result.field_size == 2
    assert result.parse_report.entry_rows_seen == 3
    assert result.parse_report.rows_rejected == 1
    assert result.parse_report.rejected[0].section == "entries"
    assert result.rows[0].roster_count == 2


def test_reported_ownership_inconsistent_with_field_size_is_rejected(tmp_path: Path) -> None:
    standings = tmp_path / "inconsistent.csv"
    standings.write_text(
        "Rank,EntryId,EntryName,TimeRemaining,Points,Lineup\n"
        "1,e1,One,0,10,QB Example Passer\n"
        "2,e2,Two,0,9,QB Example Passer\n"
        "\n"
        "Player,Roster Position,%Drafted,FPTS\n"
        "Example Passer,QB,10%,20.0\n",
        encoding="utf-8",
    )

    result = parse_contest_standings(
        standings,
        _metadata(site=SalarySite.DRAFTKINGS, expected_field_size=2),
    )

    assert result.rows == ()
    assert result.parse_report.rows_rejected == 1
    rejected = result.parse_report.rejected[0]
    assert any("whole lineup count" in reason for reason in rejected.reasons)


def test_same_external_contest_id_on_both_sites_loads_as_two_cohorts(tmp_path: Path) -> None:
    with connect_database(tmp_path / "results.sqlite3") as connection:
        apply_migrations(connection)
        _seed_slate(connection)
        _seed_contest_slate(
            connection, slate_id=2, site="fanduel", slate_type="classic", salary_id_offset=100
        )

        draftkings = load_contest_standings(
            connection,
            GOLDEN / "draftkings_contest_standings.csv",
            _metadata(
                site=SalarySite.DRAFTKINGS,
                expected_field_size=3,
                contest_id="cross-site-1",
            ),
            ingested_at=OBSERVED_AT,
        )
        fanduel = load_contest_standings(
            connection,
            GOLDEN / "fanduel_contest_standings.csv",
            _metadata(
                site=SalarySite.FANDUEL,
                slate_id=2,
                expected_field_size=2,
                contest_id="cross-site-1",
            ),
            ingested_at=OBSERVED_AT,
        )
        cohorts = connection.execute(
            "SELECT DISTINCT external_contest_id, site, field_size FROM actual_ownership "
            "ORDER BY site"
        ).fetchall()
        ownership_count = connection.execute("SELECT COUNT(*) FROM actual_ownership").fetchone()

    assert draftkings.ok
    assert fanduel.ok
    assert draftkings.ownership_rows_inserted == 3
    assert fanduel.ownership_rows_inserted == 3
    assert fanduel.duplicate_rows == 0
    assert [tuple(row) for row in cohorts] == [
        ("cross-site-1", "draftkings", 3),
        ("cross-site-1", "fanduel", 2),
    ]
    assert tuple(ownership_count) == (6,)


def test_canonical_timestamps_sort_at_same_instant_boundaries(tmp_path: Path) -> None:
    eastern = OBSERVED_AT.astimezone(ZoneInfo("America/New_York"))
    assert utc_timestamp(OBSERVED_AT) == utc_timestamp(eastern)
    assert utc_timestamp(OBSERVED_AT) < utc_timestamp(OBSERVED_AT + timedelta(microseconds=1))
    with pytest.raises(ValueError, match="timezone"):
        utc_timestamp(datetime(2026, 9, 14, 12))

    with connect_database(tmp_path / "results.sqlite3") as connection:
        apply_migrations(connection)
        _seed_slate(connection)
        report = load_contest_standings(
            connection,
            GOLDEN / "draftkings_contest_standings.csv",
            _metadata(
                site=SalarySite.DRAFTKINGS,
                expected_field_size=3,
                contest_id="dk-timestamps-1",
            ),
            ingested_at=OBSERVED_AT,
        )
        written = connection.execute(
            """
            SELECT observed_at, ingested_at, valid_from FROM actual_ownership
            UNION ALL
            SELECT observed_at, ingested_at, valid_from FROM results
            """
        ).fetchall()

    assert report.ok
    assert written
    for row in written:
        for value in tuple(row):
            assert CANONICAL_TIMESTAMP.fullmatch(value)


def _metadata(
    *,
    site: SalarySite,
    slate_id: int = 1,
    slate_type: SalarySlateType = SalarySlateType.CLASSIC,
    archetype: ContestArchetype = ContestArchetype.SINGLE_ENTRY,
    expected_field_size: int | None = None,
    contest_id: str = "fixture-contest",
) -> ContestMetadata:
    return ContestMetadata(
        contest_id=contest_id,
        site=site,
        slate_id=slate_id,
        slate_type=slate_type,
        contest_archetype=archetype,
        entry_limit=1,
        entry_fee_cents=100,
        observed_at=OBSERVED_AT,
        expected_field_size=expected_field_size,
        payout_curve_id="flat-fixture-v1",
    )


_POINT_IN_TIME = (
    "fixture",
    None,
    utc_timestamp(PRELOCK_AT),
    utc_timestamp(PRELOCK_AT),
    None,
    utc_timestamp(PRELOCK_AT),
    None,
    "fixture-v1",
    None,
)
_PLAYERS = (
    (1, "Example Passer", "QB"),
    (2, "Sample Runner", "RB"),
    (3, "Example Receiver", "WR"),
)


def _seed_league(connection: sqlite3.Connection) -> None:
    for team_id, abbreviation in ((1, "AAA"), (2, "BBB")):
        connection.execute(
            """
            INSERT INTO teams(
                team_id, team_key, abbreviation, canonical_name, league,
                source, published_at, observed_at, ingested_at, effective_at,
                valid_from, valid_to, source_version, run_id
            ) VALUES (?, ?, ?, ?, 'NFL', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (team_id, abbreviation, abbreviation, f"Team {abbreviation}", *_POINT_IN_TIME),
        )
    connection.execute(
        """
        INSERT INTO games(
            game_id, external_game_id, season, week, kickoff_at, home_team_id,
            away_team_id, stadium_name, game_status, source, published_at,
            observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES (1, 'game-1', 2026, 1, ?, 1, 2, 'Fixture Stadium', 'scheduled',
                  ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (utc_timestamp(datetime(2026, 9, 13, 17, tzinfo=UTC)), *_POINT_IN_TIME),
    )
    for player_id, name, position in _PLAYERS:
        connection.execute(
            """
            INSERT INTO players(
                player_id, player_key, canonical_name, position, birth_date,
                source, published_at, observed_at, ingested_at, effective_at,
                valid_from, valid_to, source_version, run_id
            ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (player_id, f"player-{player_id}", name, position, *_POINT_IN_TIME),
        )


def _seed_contest_slate(
    connection: sqlite3.Connection,
    *,
    slate_id: int,
    site: str,
    slate_type: str,
    salary_id_offset: int,
) -> None:
    kickoff = utc_timestamp(datetime(2026, 9, 13, 17, tzinfo=UTC))
    connection.execute(
        """
        INSERT INTO slates(
            slate_id, external_slate_id, site, slate_type, season, week, name,
            starts_at, locks_at, source, published_at, observed_at, ingested_at,
            effective_at, valid_from, valid_to, source_version, run_id
        ) VALUES (?, ?, ?, ?, 2026, 1, 'Fixture Slate',
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (slate_id, f"{site}-slate-{slate_id}", site, slate_type, kickoff, kickoff, *_POINT_IN_TIME),
    )
    for player_id, _, position in _PLAYERS:
        if slate_type == "showdown":
            positions = '["CPT","FLEX"]'
        else:
            positions = f'["{position}"' + (',"FLEX"]' if position != "QB" else "]")
        connection.execute(
            """
            INSERT INTO salaries(
                salary_id, slate_id, player_id, game_id, team_id, opponent_team_id,
                site_player_id, roster_positions_json, salary, player_status,
                source_file_sha256, source, published_at, observed_at, ingested_at,
                effective_at, valid_from, valid_to, source_version, run_id
            ) VALUES (?, ?, ?, 1, 1, 2, ?, ?, 5000, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                salary_id_offset + player_id,
                slate_id,
                player_id,
                f"site-{slate_id}-{player_id}",
                positions,
                "a" * 64,
                *_POINT_IN_TIME,
            ),
        )


def _seed_slate(connection: sqlite3.Connection) -> None:
    _seed_league(connection)
    _seed_contest_slate(
        connection, slate_id=1, site="draftkings", slate_type="classic", salary_id_offset=0
    )
