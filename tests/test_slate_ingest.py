from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from narrative_alpha.identity import PlayerCrosswalk
from narrative_alpha.ingest.slates import (
    SlateIngestError,
    list_slates,
    load_salary_capture,
    newest_salary_capture,
    normalize_site,
    render_slates,
)
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.slate_cli import main as slate_main
from narrative_alpha.snapshots import CaptureKind, capture_files
from narrative_alpha.store import SalaryRow, SlateRow, apply_migrations, connect_database

GOLDEN_PATH = Path(__file__).with_name("golden")
OBSERVED = datetime(2026, 9, 12, 22, 0, tzinfo=UTC)
DK_KICKOFF = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)
DK_SLATE_ID = "draftkings:2026:w01:classic:20260913T170000Z"

DRAFTKINGS_ROSTER = (
    ("Example Passer", "GB", "QB"),
    ("Sample Runner", "CHI", "RB"),
    ("Green Bay Defense", "GB", "DST"),
)
FANDUEL_ROSTER = (
    ("Example Thrower", "DAL", "QB"),
    ("Sample Catcher", "NYG", "WR"),
    ("New York Defense", "NYG", "DST"),
)
SHOWDOWN_ROSTER = (
    ("Example Quarterback", "BUF", "QB"),
    ("Sample Receiver", "MIA", "WR"),
    ("Example Kicker", "BUF", "K"),
)


def _seed_players(
    connection: sqlite3.Connection,
    roster: tuple[tuple[str, str, str], ...],
    *,
    observed_at: datetime = OBSERVED - timedelta(days=7),
) -> dict[str, int]:
    stamp = utc_timestamp(observed_at)
    player_ids: dict[str, int] = {}
    for name, team, position in roster:
        cursor = connection.execute(
            """
            INSERT INTO players(
                player_key, canonical_name, position, birth_date, source,
                published_at, observed_at, ingested_at, effective_at, valid_from,
                valid_to, source_version, run_id
            ) VALUES (?, ?, ?, NULL, 'fixture', NULL, ?, ?, NULL, ?, NULL,
                      'fixture-v1', NULL)
            """,
            (name.lower().replace(" ", "-"), name, position, stamp, stamp, stamp),
        )
        assert cursor.lastrowid is not None
        player_id = int(cursor.lastrowid)
        player_ids[name] = player_id
        connection.execute(
            """
            INSERT INTO player_team_history(
                player_id, team, position, roster_status, season, week, source,
                published_at, observed_at, ingested_at, effective_at, valid_from,
                valid_to, source_version, run_id
            ) VALUES (?, ?, ?, 'ACT', 2026, 1, 'fixture', NULL, ?, ?, NULL, ?, NULL,
                      'fixture-v1', NULL)
            """,
            (player_id, team, position, stamp, stamp, stamp),
        )
    return player_ids


def _capture(
    tmp_path: Path,
    golden: str,
    *,
    season: int = 2026,
    week: int = 1,
    source: str = "draftkings",
    observed_at: datetime = OBSERVED,
    root: Path | None = None,
    text: str | None = None,
) -> Path:
    staged = tmp_path / "staged" / golden
    staged.parent.mkdir(parents=True, exist_ok=True)
    if text is None:
        staged.write_bytes((GOLDEN_PATH / golden).read_bytes())
    else:
        staged.write_text(text, encoding="utf-8")
    return capture_files(
        root or (tmp_path / "snapshots"),
        season,
        week,
        CaptureKind.SALARIES,
        source,
        [staged],
        observed_at=observed_at,
    )


def _store(tmp_path: Path, name: str = "store.sqlite3") -> Path:
    return tmp_path / name


def test_draftkings_classic_capture_writes_slate_salaries_teams_and_games(
    tmp_path: Path,
) -> None:
    capture = _capture(tmp_path, "draftkings_classic.csv")

    with connect_database(_store(tmp_path)) as connection:
        apply_migrations(connection)
        _seed_players(connection, DRAFTKINGS_ROSTER)
        report = load_salary_capture(
            connection,
            capture,
            season=2026,
            week=1,
            site="dk",
            ingested_at=OBSERVED + timedelta(minutes=5),
        )
        slate = SlateRow.from_db(connection.execute("SELECT * FROM slates").fetchone())
        salaries = tuple(
            SalaryRow.from_db(row)
            for row in connection.execute("SELECT * FROM salaries ORDER BY salary_id")
        )
        teams = tuple(
            str(row[0])
            for row in connection.execute("SELECT abbreviation FROM teams ORDER BY abbreviation")
        )
        games = connection.execute(
            "SELECT external_game_id, kickoff_at FROM games"
        ).fetchall()

    assert report.ok
    assert report.salary_rows_inserted == 3
    assert slate.external_slate_id == DK_SLATE_ID
    assert slate.name == DK_SLATE_ID
    assert slate.slate_type == "classic"
    assert slate.site == "draftkings"
    assert (slate.starts_at, slate.locks_at) == (DK_KICKOFF, DK_KICKOFF)
    # Never "now": the slate and its salaries carry the capture's observation time.
    assert slate.observed_at == OBSERVED
    assert {row.observed_at for row in salaries} == {OBSERVED}
    assert teams == ("CHI", "GB")
    assert len(games) == 1
    assert tuple(games[0]) == ("2026:w01:CHI-GB", utc_timestamp(DK_KICKOFF))
    assert {row.salary for row in salaries} == {7200, 6400, 3100}
    assert all(row.game_id is not None for row in salaries)
    assert all(row.slate_id == slate.slate_id for row in salaries)


def test_home_and_away_come_from_the_export_not_the_key_order(tmp_path: Path) -> None:
    """The key is alphabetical; ``GB@CHI`` still means CHI hosts."""

    capture = _capture(tmp_path, "draftkings_classic.csv")

    with connect_database(_store(tmp_path)) as connection:
        apply_migrations(connection)
        _seed_players(connection, DRAFTKINGS_ROSTER)
        load_salary_capture(connection, capture, season=2026, week=1, site="dk")
        game = connection.execute(
            """
            SELECT home.abbreviation AS home, away.abbreviation AS away
            FROM games AS g
            JOIN teams AS home ON home.team_id = g.home_team_id
            JOIN teams AS away ON away.team_id = g.away_team_id
            """
        ).fetchone()

    assert (str(game["away"]), str(game["home"])) == ("GB", "CHI")


def test_ingested_rows_satisfy_the_candidate_pool_joins(tmp_path: Path) -> None:
    """The point of the slice: a build can actually reach these rows."""

    capture = _capture(tmp_path, "draftkings_classic.csv")
    with connect_database(_store(tmp_path)) as connection:
        apply_migrations(connection)
        _seed_players(connection, DRAFTKINGS_ROSTER)
        load_salary_capture(connection, capture, season=2026, week=1, site="dk")
        joined = connection.execute(
            """
            SELECT count(*) FROM salaries AS s
            JOIN players AS p ON p.player_id = s.player_id
            JOIN teams AS team ON team.team_id = s.team_id
            JOIN teams AS opponent ON opponent.team_id = s.opponent_team_id
            JOIN games AS g ON g.game_id = s.game_id
            """
        ).fetchone()[0]

    assert joined == 3


def test_fanduel_export_without_kickoff_refuses_until_starts_at_is_given(
    tmp_path: Path,
) -> None:
    capture = _capture(tmp_path, "fanduel_classic.csv", source="fanduel")
    starts_at = datetime(2026, 9, 13, 20, 25, tzinfo=UTC)

    with connect_database(_store(tmp_path)) as connection:
        apply_migrations(connection)
        _seed_players(connection, FANDUEL_ROSTER)
        refused = load_salary_capture(
            connection, capture, season=2026, week=1, site="fd"
        )
        assert refused.slates == ()
        assert any("--starts-at" in error for error in refused.errors)
        assert connection.execute("SELECT count(*) FROM slates").fetchone()[0] == 0

        accepted = load_salary_capture(
            connection,
            capture,
            season=2026,
            week=1,
            site="fd",
            slate_name="FD Sunday Main",
            starts_at=starts_at,
        )
        salaries = tuple(
            SalaryRow.from_db(row) for row in connection.execute("SELECT * FROM salaries")
        )

    assert accepted.ok
    assert len(accepted.slates) == 1
    loaded = accepted.slates[0]
    assert loaded.name == "FD Sunday Main"
    assert loaded.external_slate_id == "fanduel:2026:w01:classic:20260913T202500Z"
    assert loaded.locks_at == starts_at
    # FanDuel omits the kickoff but still writes AWAY@HOME, so the label is not a guess.
    assert loaded.matchups_without_kickoff == ("DAL@NYG",)
    # No kickoff in the export means no game row was invented.
    assert all(row.game_id is None for row in salaries)


def test_showdown_export_produces_a_showdown_slate(tmp_path: Path) -> None:
    capture = _capture(tmp_path, "draftkings_showdown.csv", week=2)

    with connect_database(_store(tmp_path)) as connection:
        apply_migrations(connection)
        _seed_players(connection, SHOWDOWN_ROSTER)
        report = load_salary_capture(connection, capture, season=2026, week=2, site="dk")

    assert report.ok
    slate = report.slates[0]
    assert slate.slate_type == "showdown"
    assert slate.external_slate_id == "draftkings:2026:w02:showdown:20260918T001500Z"
    assert slate.salary_rows_inserted == 3


def test_unresolved_player_is_queued_and_named_but_the_slate_is_written(
    tmp_path: Path,
) -> None:
    capture = _capture(tmp_path, "draftkings_classic.csv")

    with connect_database(_store(tmp_path)) as connection:
        apply_migrations(connection)
        _seed_players(connection, DRAFTKINGS_ROSTER[1:])
        report = load_salary_capture(connection, capture, season=2026, week=1, site="dk")
        pending = connection.execute(
            "SELECT name_raw, site, status FROM unresolved_player_matches"
        ).fetchall()
        with pytest.raises(Exception, match="unresolved player identity"):
            PlayerCrosswalk(connection).require_all_resolved(site="draftkings")

    assert report.ok is False
    assert report.unresolved_rows == 1
    assert report.salary_rows_inserted == 2
    assert len(report.slates) == 1
    unresolved = report.slates[0].unresolved[0]
    assert (unresolved.name_raw, unresolved.team, unresolved.position) == (
        "Example Passer",
        "GB",
        "QB",
    )
    assert unresolved.unresolved_id is not None
    assert tuple(pending[0]) == ("Example Passer", "draftkings", "pending")


def test_capture_file_changed_after_the_manifest_is_refused(tmp_path: Path) -> None:
    capture = _capture(tmp_path, "draftkings_classic.csv")
    (capture / "salaries" / "draftkings_classic.csv").write_text("tampered\n", encoding="utf-8")

    with connect_database(_store(tmp_path)) as connection:
        apply_migrations(connection)
        _seed_players(connection, DRAFTKINGS_ROSTER)
        with pytest.raises(SlateIngestError, match="hash mismatch"):
            load_salary_capture(connection, capture, season=2026, week=1, site="dk")
        assert connection.execute("SELECT count(*) FROM slates").fetchone()[0] == 0


def test_reloading_the_same_capture_inserts_nothing_and_says_so(tmp_path: Path) -> None:
    capture = _capture(tmp_path, "draftkings_classic.csv")

    with connect_database(_store(tmp_path)) as connection:
        apply_migrations(connection)
        _seed_players(connection, DRAFTKINGS_ROSTER)
        first = load_salary_capture(connection, capture, season=2026, week=1, site="dk")
        second = load_salary_capture(connection, capture, season=2026, week=1, site="dk")
        counts = connection.execute(
            "SELECT (SELECT count(*) FROM slates), (SELECT count(*) FROM salaries), "
            "(SELECT count(*) FROM teams), (SELECT count(*) FROM games)"
        ).fetchone()

    assert first.slates[0].slate_inserted is True
    assert second.slates[0].slate_inserted is False
    assert second.salary_rows_inserted == 0
    assert second.duplicate_rows == 3
    assert second.slates[0].teams_inserted == 0
    assert second.slates[0].games_inserted == 0
    assert tuple(counts) == (1, 3, 2, 1)


def test_a_later_capture_versions_salaries_and_reports_the_change(tmp_path: Path) -> None:
    snapshots = tmp_path / "snapshots"
    saturday = _capture(tmp_path, "draftkings_classic.csv", root=snapshots)
    sunday_text = (GOLDEN_PATH / "draftkings_classic.csv").read_text(encoding="utf-8").replace(
        ",6400,", ",6100,"
    )
    sunday = _capture(
        tmp_path,
        "draftkings_classic.csv",
        root=snapshots,
        observed_at=OBSERVED + timedelta(hours=14),
        text=sunday_text,
    )

    with connect_database(_store(tmp_path)) as connection:
        apply_migrations(connection)
        _seed_players(connection, DRAFTKINGS_ROSTER)
        load_salary_capture(connection, saturday, season=2026, week=1, site="dk")
        later = load_salary_capture(connection, sunday, season=2026, week=1, site="dk")
        rows = connection.execute(
            "SELECT salary, observed_at FROM salaries WHERE site_player_id = '1002' "
            "ORDER BY observed_at"
        ).fetchall()
        slate_count = connection.execute("SELECT count(*) FROM slates").fetchone()[0]

    # Same slate identity, a second observation, and nothing updated in place.
    assert slate_count == 1
    assert later.slates[0].slate_inserted is False
    assert later.salary_rows_inserted == 3
    assert [int(row[0]) for row in rows] == [6400, 6100]
    change = later.slates[0].salary_changes
    assert len(change) == 1
    assert (change[0].site_player_id, change[0].previous_salary, change[0].salary) == (
        "1002",
        6400,
        6100,
    )
    assert change[0].previous_observed_at == OBSERVED


def test_another_sites_export_in_the_same_capture_is_skipped(tmp_path: Path) -> None:
    staged = tmp_path / "staged"
    staged.mkdir()
    for golden in ("draftkings_classic.csv", "fanduel_classic.csv"):
        (staged / golden).write_bytes((GOLDEN_PATH / golden).read_bytes())
    capture = capture_files(
        tmp_path / "snapshots",
        2026,
        1,
        CaptureKind.SALARIES,
        "manual",
        sorted(staged.iterdir()),
        observed_at=OBSERVED,
    )

    with connect_database(_store(tmp_path)) as connection:
        apply_migrations(connection)
        _seed_players(connection, DRAFTKINGS_ROSTER)
        report = load_salary_capture(connection, capture, season=2026, week=1, site="dk")

    assert report.files_seen == 2
    assert report.files_skipped == ("salaries/fanduel_classic.csv",)
    assert len(report.slates) == 1
    assert report.slates[0].site == "draftkings"


def test_capture_from_another_week_is_refused(tmp_path: Path) -> None:
    capture = _capture(tmp_path, "draftkings_classic.csv", week=1)

    with connect_database(_store(tmp_path)) as connection:
        apply_migrations(connection)
        with pytest.raises(SlateIngestError, match="not the requested season"):
            load_salary_capture(connection, capture, season=2026, week=2, site="dk")


def test_newest_salary_capture_is_the_default_and_ignores_other_kinds(
    tmp_path: Path,
) -> None:
    snapshots = tmp_path / "snapshots"
    _capture(tmp_path, "draftkings_classic.csv", root=snapshots)
    newest = _capture(
        tmp_path,
        "draftkings_classic.csv",
        root=snapshots,
        observed_at=OBSERVED + timedelta(hours=14),
    )
    projections = tmp_path / "staged" / "projections.csv"
    projections.write_text("name\n", encoding="utf-8")
    capture_files(
        snapshots,
        2026,
        1,
        CaptureKind.PROJECTIONS,
        "vendor",
        [projections],
        observed_at=OBSERVED + timedelta(hours=20),
    )

    capture_files(
        snapshots,
        2026,
        2,
        CaptureKind.PROJECTIONS,
        "vendor",
        [projections],
        observed_at=OBSERVED + timedelta(hours=20),
    )

    assert newest_salary_capture(snapshots, 2026, 1) == newest
    with pytest.raises(SlateIngestError, match="no capture"):
        newest_salary_capture(snapshots, 2026, 2)
    with pytest.raises(SlateIngestError, match="snapshot week does not exist"):
        newest_salary_capture(snapshots, 2026, 3)


def test_list_shows_ids_counts_and_the_latest_observation_times(tmp_path: Path) -> None:
    capture = _capture(tmp_path, "draftkings_classic.csv")

    with connect_database(_store(tmp_path)) as connection:
        apply_migrations(connection)
        assert list_slates(connection, season=2026, week=1) == ()
        empty = render_slates((), season=2026, week=1)

        _seed_players(connection, DRAFTKINGS_ROSTER[1:])
        report = load_salary_capture(connection, capture, season=2026, week=1, site="dk")
        summaries = list_slates(connection, season=2026, week=1, site="dk")
        rendered = render_slates(summaries, season=2026, week=1)
        other_site = list_slates(connection, season=2026, week=1, site="fd")

    assert "none ingested" in empty
    assert other_site == ()
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.slate_id == report.slates[0].slate_id
    assert summary.player_count == 2
    assert summary.unresolved_count == 1
    assert summary.latest_salary_at == OBSERVED
    assert summary.latest_projection_at is None
    assert summary.latest_ownership_at is None
    assert str(summary.slate_id) in rendered
    assert "draftkings" in rendered
    assert "MISSING" in rendered


def test_cli_ingest_then_list_uses_the_newest_capture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshots = tmp_path / "snapshots"
    _capture(tmp_path, "draftkings_classic.csv", root=snapshots)
    database = _store(tmp_path)
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed_players(connection, DRAFTKINGS_ROSTER)

    common = ["--database", str(database), "--season", "2026", "--week", "1"]
    ingest_code = slate_main(["ingest", *common, "--site", "dk", "--root", str(snapshots)])
    ingest_output = capsys.readouterr().out
    list_code = slate_main(["list", *common])
    list_output = capsys.readouterr().out

    assert (ingest_code, list_code) == (0, 0)
    assert DK_SLATE_ID in ingest_output
    assert "3 inserted" in ingest_output
    assert DK_SLATE_ID in list_output
    assert utc_timestamp(DK_KICKOFF) in list_output


def test_cli_reports_unresolved_players_with_a_nonzero_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshots = tmp_path / "snapshots"
    capture = _capture(tmp_path, "draftkings_classic.csv", root=snapshots)
    database = _store(tmp_path)
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed_players(connection, DRAFTKINGS_ROSTER[1:])

    code = slate_main(
        [
            "ingest",
            "--database",
            str(database),
            "--season",
            "2026",
            "--week",
            "1",
            "--site",
            "dk",
            "--capture",
            str(capture),
        ]
    )
    output = capsys.readouterr().out

    assert code == 1
    assert "unresolved Example Passer" in output
    assert "na-crosswalk resolve --unresolved-id" in output


def test_cli_refuses_a_missing_capture_with_exit_code_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = slate_main(
        [
            "ingest",
            "--database",
            str(_store(tmp_path)),
            "--season",
            "2026",
            "--week",
            "1",
            "--site",
            "dk",
            "--root",
            str(tmp_path / "snapshots"),
        ]
    )

    assert code == 2
    assert "ERROR:" in capsys.readouterr().err


def test_site_aliases_normalize_and_anything_else_is_refused() -> None:
    assert normalize_site("dk") is normalize_site("DraftKings")
    assert normalize_site("fd") is normalize_site("fanduel")
    with pytest.raises(SlateIngestError, match="site must be"):
        normalize_site("yahoo")


# --- Review fixes (2026-09-02) -------------------------------------------------------------


def test_team_defenses_resolve_to_one_canonical_row_per_franchise(tmp_path: Path) -> None:
    # No roster carries a defense, and each site names it differently; a queue entry for
    # every team every week would block the lineup build until resolved by hand.
    dk_capture = _capture(tmp_path, "draftkings_classic.csv")
    fd_capture = _capture(
        tmp_path / "fd", "fanduel_classic.csv", source="fanduel", observed_at=OBSERVED
    )
    with connect_database(_store(tmp_path)) as connection:
        apply_migrations(connection)
        _seed_players(
            connection,
            tuple(entry for entry in DRAFTKINGS_ROSTER if entry[2] not in {"DST", "D"}),
        )
        dk = load_salary_capture(connection, dk_capture, season=2026, week=1, site="dk")
        fd = load_salary_capture(
            connection,
            fd_capture,
            season=2026,
            week=1,
            site="fd",
            starts_at=datetime(2026, 9, 13, 17, 0, tzinfo=UTC),
        )
        defenses = connection.execute(
            "SELECT player_key, canonical_name, position FROM players "
            "WHERE player_key LIKE 'dst:%' ORDER BY player_key"
        ).fetchall()
        queued = connection.execute(
            "SELECT count(*) FROM unresolved_player_matches WHERE position IN ('DST', 'D')"
        ).fetchone()[0]

    assert not any(row.position in {"DST", "D"} for slate in dk.slates for row in slate.unresolved)
    assert not any(row.position in {"DST", "D"} for slate in fd.slates for row in slate.unresolved)
    assert [tuple(row) for row in defenses] == [
        ("dst:GB", "GB DST", "DST"),
        ("dst:NYG", "NYG DST", "DST"),
    ]
    assert queued == 0
