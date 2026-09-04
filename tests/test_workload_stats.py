"""Slice 37: pinned nflverse workload files as gradable `results` stat lines."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import Any

import httpx
import pytest
from test_grading import LOCK, _seed_claim, _seed_world

from narrative_alpha.grading import grade_week
from narrative_alpha.identity.crosswalk import PlayerCrosswalk
from narrative_alpha.identity.pins import PinHashError, archive_bytes
from narrative_alpha.ingest.nflverse_stats import (
    SNAP_COUNTS_LABEL,
    WEEKLY_STATS_LABEL,
    WORKLOAD_STATS_SOURCE,
    PinnedStatsRelease,
    StatsSchemaError,
    UnpinnedStatsError,
    fetch_pinned_stats,
    load_workload_stats,
    pinned_stats_release,
    refresh_stats_release,
)
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.ops import (
    ResultsDependencies,
    collect_ops_status,
    load_ops_config,
    render_status,
    run_results,
    status_payload,
)
from narrative_alpha.store import apply_migrations, connect_database

SEASON = 2026
WEEK = 3
REVIEWED = date(2026, 9, 22)
OBSERVED = datetime(2026, 9, 22, 12, tzinfo=UTC)
GRADED = datetime(2026, 9, 22, 12, tzinfo=UTC)
WEEKLY_URL = "https://example.test/stats_player_week_2026.csv"
SNAPS_URL = "https://example.test/snap_counts_2026.csv"

_WEEKLY_HEADER = (
    "player_id,player_display_name,position,team,season,week,carries,receptions,"
    "target_share,fantasy_points_ppr"
)
_SNAP_HEADER = (
    "season,week,player,pfr_player_id,position,team,offense_snaps,offense_pct,"
    "defense_snaps,st_snaps"
)

# Example Player's three weeks: the reference for week 3 is weeks 1-2 only.
_WEEKLY_ROWS = (
    "00-0001,Example Player,WR,ABC,2026,1,0,5,0.20,12.0",
    "00-0002,Other Player,RB,ABC,2026,1,10,2,0.05,15.0",
    "00-0001,Example Player,WR,ABC,2026,2,0,3,0.15,8.0",
    "00-0002,Other Player,RB,ABC,2026,2,12,1,0.04,18.0",
    "00-0001,Example Player,WR,ABC,2026,3,0,9,0.35,25.0",
    "00-0002,Other Player,RB,ABC,2026,3,8,1,0.03,11.0",
)
_SNAP_ROWS = (
    "2026,1,Example Player,ExamP00,WR,ABC,30,0.50,0,2",
    "2026,1,Other Player,OtherP0,RB,ABC,40,0.66,0,1",
    "2026,2,Example Player,ExamP00,WR,ABC,36,0.60,0,2",
    "2026,2,Other Player,OtherP0,RB,ABC,44,0.70,0,1",
    "2026,3,Example Player,ExamP00,WR,ABC,55,0.85,0,3",
    "2026,3,Other Player,OtherP0,RB,ABC,38,0.62,0,1",
)


def _csv(header: str, rows: tuple[str, ...]) -> bytes:
    return ("\n".join((header, *rows)) + "\n").encode("utf-8")


def _pin(
    archive: Path,
    *,
    weekly: bytes,
    snaps: bytes,
    reviewed_at: date = REVIEWED,
    season: int = SEASON,
) -> tuple[PinnedStatsRelease, Any]:
    """Archive both files under their own hashes and return the pin plus a pin table."""

    weekly_sha256 = hashlib.sha256(weekly).hexdigest()
    snaps_sha256 = hashlib.sha256(snaps).hexdigest()
    archive_bytes(archive, weekly, weekly_sha256, label=WEEKLY_STATS_LABEL)
    archive_bytes(archive, snaps, snaps_sha256, label=SNAP_COUNTS_LABEL)
    release = PinnedStatsRelease(
        season=season,
        reviewed_at=reviewed_at,
        weekly_url=WEEKLY_URL,
        weekly_sha256=weekly_sha256,
        snaps_url=SNAPS_URL,
        snaps_sha256=snaps_sha256,
    )
    return release, MappingProxyType({season: (release,)})


def _link_nflverse(connection: sqlite3.Connection, player_id: int, gsis_id: str) -> None:
    """The mapping the roster seed writes: an nflverse GSIS id to a canonical player."""

    stamp = utc_timestamp(LOCK - timedelta(days=10))
    connection.execute(
        """
        INSERT INTO external_player_ids(
            player_id, source, site, external_player_id, published_at, observed_at,
            ingested_at, effective_at, valid_from, valid_to, source_version, run_id,
            match_method, match_confidence, manual_override
        ) VALUES (?, 'nflverse', NULL, ?, NULL, ?, ?, NULL, ?, NULL, 'roster-v1', NULL,
                  'seed', 1.0, 0)
        """,
        (player_id, gsis_id, stamp, stamp, stamp),
    )


def _world(
    connection: sqlite3.Connection,
    *,
    link: bool = True,
    week: int = WEEK,
) -> None:
    """The grading fixture world, moved to `week` and given the roster's nflverse link."""

    _seed_world(connection)
    connection.execute("UPDATE games SET week = ? WHERE game_id = 1", (week,))
    connection.execute("UPDATE slates SET week = ? WHERE slate_id = 1", (week,))
    stamp = utc_timestamp(LOCK - timedelta(days=10))
    # A canonical teammate who is never priced: the team's touch denominator is real, and
    # the step counts them as not salaried rather than writing a row with no game.
    connection.execute(
        """
        INSERT INTO players(
            player_id, player_key, canonical_name, position, birth_date, source,
            published_at, observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES (2, 'player-2', 'Other Player', 'RB', NULL, 'fixture', NULL, ?, ?, NULL,
                  ?, NULL, 'fixture-v1', NULL)
        """,
        (stamp, stamp, stamp),
    )
    if link:
        _link_nflverse(connection, 1, "00-0001")
        _link_nflverse(connection, 2, "00-0002")
    connection.commit()


def _load(
    connection: sqlite3.Connection,
    archive: Path,
    releases: Any,
    *,
    week: int = WEEK,
    site: str = "draftkings",
    observed_at: datetime = OBSERVED,
) -> Any:
    return load_workload_stats(
        connection,
        season=SEASON,
        week=week,
        site=site,
        archive_dir=archive,
        observed_at=observed_at,
        releases=releases,
    )


def _stat_line(connection: sqlite3.Connection, player_id: int = 1) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT stat_line_json FROM results
        WHERE source = ? AND player_id = ?
        ORDER BY result_id DESC LIMIT 1
        """,
        (WORKLOAD_STATS_SOURCE, player_id),
    ).fetchone()
    assert row is not None
    return dict(json.loads(str(row["stat_line_json"])))


def test_pinned_stats_fetch_refuses_drifted_bytes(tmp_path: Path) -> None:
    weekly = _csv(_WEEKLY_HEADER, _WEEKLY_ROWS)
    snaps = _csv(_SNAP_HEADER, _SNAP_ROWS)
    drifted = PinnedStatsRelease(
        season=SEASON,
        reviewed_at=REVIEWED,
        weekly_url=WEEKLY_URL,
        weekly_sha256=hashlib.sha256(weekly).hexdigest(),
        snaps_url=SNAPS_URL,
        # The reviewed snap-counts hash no longer describes what upstream serves.
        snaps_sha256="0" * 64,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = weekly if request.url.path.endswith("stats_player_week_2026.csv") else snaps
        return httpx.Response(200, content=body, request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(PinHashError, match="hash mismatch"),
    ):
        fetch_pinned_stats(drifted, tmp_path / "archive", client=client)

    archived = tuple(path for path in (tmp_path / "archive").rglob("*") if path.is_file())
    assert [path.name for path in archived] == [f"{drifted.weekly_sha256}.csv"]


def test_pin_selection_never_looks_ahead_and_says_when_nothing_is_reviewed(
    tmp_path: Path,
) -> None:
    _, releases = _pin(
        tmp_path / "archive",
        weekly=_csv(_WEEKLY_HEADER, _WEEKLY_ROWS),
        snaps=_csv(_SNAP_HEADER, _SNAP_ROWS),
    )

    chosen = pinned_stats_release(SEASON, REVIEWED, releases=releases)
    assert chosen.reviewed_at == REVIEWED
    with pytest.raises(UnpinnedStatsError, match="review and add its hashes"):
        pinned_stats_release(SEASON, REVIEWED - timedelta(days=1), releases=releases)
    with pytest.raises(UnpinnedStatsError):
        pinned_stats_release(SEASON + 1, REVIEWED, releases=releases)


def test_baseline_is_the_prior_games_only_and_is_absent_in_the_first_game(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive"
    _, releases = _pin(
        archive,
        weekly=_csv(_WEEKLY_HEADER, _WEEKLY_ROWS),
        snaps=_csv(_SNAP_HEADER, _SNAP_ROWS),
    )
    database = tmp_path / "stats.sqlite3"
    with connect_database(database) as connection:
        apply_migrations(connection)
        _world(connection, week=1)
        first = _load(connection, archive, releases, week=1)
        first_line = _stat_line(connection)
        connection.execute("UPDATE games SET week = 3 WHERE game_id = 1")
        connection.execute("UPDATE slates SET week = 3 WHERE slate_id = 1")
        third = _load(
            connection,
            archive,
            releases,
            week=3,
            observed_at=OBSERVED + timedelta(days=14),
        )
        third_line = _stat_line(connection)

    # Week 1 is the player's first game of the season: every reference is absent, so the
    # usage rule reports ungradable rather than comparing the game with itself.
    assert first.players_written == 1
    assert first.players_without_baseline == 1
    assert not [key for key in first_line if key.endswith("_baseline")]
    assert first_line["snap_share"] == 0.5

    assert third.players_written == 1
    assert third.players_without_baseline == 0
    assert third_line["snap_share"] == 0.85
    # Weeks 1 and 2 only — (0.50 + 0.60) / 2, never the 0.85 being graded.
    assert third_line["snap_share_baseline"] == 0.55
    assert third_line["target_share"] == 0.35
    assert third_line["target_share_baseline"] == 0.175
    assert third_line["touch_share"] == round(9 / 18, 6)
    # The mean is taken over the stored six-decimal shares, so a rerun of the same pin
    # re-derives the identical byte string.
    assert third_line["touch_share_baseline"] == round((round(5 / 17, 6) + round(3 / 16, 6)) / 2, 6)
    assert third_line["played"] is True
    # nflverse's weekly file carries no routes column, so the key is simply absent.
    assert "route_share" not in third_line
    assert "route_share_baseline" not in third_line


def test_route_share_is_written_when_the_pinned_file_carries_routes(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    header = f"{_WEEKLY_HEADER},routes"
    rows = tuple(
        f"{row},{routes}" for row, routes in zip(_WEEKLY_ROWS, (20, 5, 25, 5, 30, 10), strict=True)
    )
    _, releases = _pin(archive, weekly=_csv(header, rows), snaps=_csv(_SNAP_HEADER, _SNAP_ROWS))
    with connect_database(tmp_path / "stats.sqlite3") as connection:
        apply_migrations(connection)
        _world(connection)
        _load(connection, archive, releases)
        line = _stat_line(connection)

    assert line["route_share"] == round(30 / 40, 6)
    assert line["route_share_baseline"] == round((round(20 / 25, 6) + round(25 / 30, 6)) / 2, 6)


def test_an_unresolved_player_holds_the_row_and_enters_the_queue(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _, releases = _pin(
        archive,
        weekly=_csv(_WEEKLY_HEADER, _WEEKLY_ROWS),
        snaps=_csv(_SNAP_HEADER, _SNAP_ROWS),
    )
    with connect_database(tmp_path / "stats.sqlite3") as connection:
        apply_migrations(connection)
        _world(connection, link=False)
        report = _load(connection, archive, releases)
        connection.commit()
        rows = connection.execute(
            "SELECT count(*) FROM results WHERE source = ?", (WORKLOAD_STATS_SOURCE,)
        ).fetchone()[0]
        pending = PlayerCrosswalk(connection).list_unresolved()

    assert report.players_written == 0
    assert report.players_held == 2
    assert rows == 0
    assert report.unresolved_ids and len(report.unresolved_ids) == 2
    assert {item.reason for item in report.held} == {
        "no canonical player resolves this nflverse id"
    }
    assert {row.external_player_id for row in pending} == {"00-0001", "00-0002"}
    assert {row.source for row in pending} == {"nflverse"}


def test_rerunning_the_step_inserts_nothing_new(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _, releases = _pin(
        archive,
        weekly=_csv(_WEEKLY_HEADER, _WEEKLY_ROWS),
        snaps=_csv(_SNAP_HEADER, _SNAP_ROWS),
    )
    with connect_database(tmp_path / "stats.sqlite3") as connection:
        apply_migrations(connection)
        _world(connection)
        first = _load(connection, archive, releases)
        # A later Tuesday, a new clock: the content is identical, so nothing is appended.
        second = _load(connection, archive, releases, observed_at=OBSERVED + timedelta(days=7))
        connection.commit()
        rows = connection.execute(
            "SELECT count(*) FROM results WHERE source = ?", (WORKLOAD_STATS_SOURCE,)
        ).fetchone()[0]

    assert (first.players_written, first.players_unchanged) == (1, 0)
    assert (second.players_written, second.players_unchanged) == (0, 1)
    assert rows == 1


def test_a_repinned_file_that_changes_a_number_appends_a_new_observation(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive"
    _, first_releases = _pin(
        archive,
        weekly=_csv(_WEEKLY_HEADER, _WEEKLY_ROWS),
        snaps=_csv(_SNAP_HEADER, _SNAP_ROWS),
    )
    corrected = tuple(
        row.replace("55,0.85", "58,0.90") if ",3,Example Player," in row else row
        for row in _SNAP_ROWS
    )
    _, second_releases = _pin(
        archive,
        weekly=_csv(_WEEKLY_HEADER, _WEEKLY_ROWS),
        snaps=_csv(_SNAP_HEADER, corrected),
        reviewed_at=REVIEWED + timedelta(days=1),
    )
    with connect_database(tmp_path / "stats.sqlite3") as connection:
        apply_migrations(connection)
        _world(connection)
        _load(connection, archive, first_releases)
        _load(connection, archive, second_releases, observed_at=OBSERVED + timedelta(days=1))
        connection.commit()
        shares = [
            json.loads(str(row["stat_line_json"]))["snap_share"]
            for row in connection.execute(
                "SELECT stat_line_json FROM results WHERE source = ? ORDER BY result_id",
                (WORKLOAD_STATS_SOURCE,),
            ).fetchall()
        ]

    assert shares == [0.85, 0.9]


def test_a_usage_claim_grades_end_to_end_from_a_written_stats_row(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _, releases = _pin(
        archive,
        weekly=_csv(_WEEKLY_HEADER, _WEEKLY_ROWS),
        snaps=_csv(_SNAP_HEADER, _SNAP_ROWS),
    )
    with connect_database(tmp_path / "stats.sqlite3") as connection:
        apply_migrations(connection)
        _world(connection)
        _seed_claim(connection, "usage-up", "usage", "snap_share", "increase", True)
        _seed_claim(connection, "usage-down", "usage", "snap_share", "decrease", True)
        connection.execute("DELETE FROM results")
        connection.commit()
        report = _load(connection, archive, releases)
        connection.commit()
        result_id = int(
            connection.execute(
                "SELECT result_id FROM results WHERE source = ?", (WORKLOAD_STATS_SOURCE,)
            ).fetchone()[0]
        )
        grade_report = grade_week(
            connection,
            season=SEASON,
            week=WEEK,
            site="draftkings",
            grading_run_id="grade-stats-1",
            graded_at=GRADED,
        )
        grades = {
            str(row["claim_id"]): row
            for row in connection.execute(
                "SELECT claim_id, verdict, result_id, outcome_json FROM claim_grades"
            ).fetchall()
        }

    assert report.players_written == 1
    assert grade_report.verdict_counts["correct"] == 1
    assert grade_report.verdict_counts["incorrect"] == 1
    assert str(grades["usage-up"]["verdict"]) == "correct"
    assert str(grades["usage-down"]["verdict"]) == "incorrect"
    # The graded outcome is the row this module wrote, not a standings row.
    assert int(grades["usage-up"]["result_id"]) == result_id
    outcome = json.loads(str(grades["usage-up"]["outcome_json"]))
    assert (outcome["value"], outcome["reference"]) == (0.85, 0.55)
    assert outcome["classified_direction"] == "increase"


def test_a_zero_snap_player_is_not_played_and_a_dnp_claim_grades_correct(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive"
    weekly = tuple(
        "00-0001,Example Player,WR,ABC,2026,3,0,0,0.00,0.0"
        if row.startswith("00-0001,Example Player,WR,ABC,2026,3")
        else row
        for row in _WEEKLY_ROWS
    )
    snaps = tuple(
        "2026,3,Example Player,ExamP00,WR,ABC,0,0.00,0,0"
        if row.startswith("2026,3,Example Player")
        else row
        for row in _SNAP_ROWS
    )
    _, releases = _pin(
        archive, weekly=_csv(_WEEKLY_HEADER, weekly), snaps=_csv(_SNAP_HEADER, snaps)
    )
    with connect_database(tmp_path / "stats.sqlite3") as connection:
        apply_migrations(connection)
        _world(connection)
        _seed_claim(connection, "dnp", "availability", "active_status", "decrease", True)
        _seed_claim(connection, "active", "availability", "active_status", "increase", True)
        # The fixture's only official row is pre-lock, which grading never uses as an
        # outcome; the played fact on the stat line is what decides these two claims.
        connection.execute("DELETE FROM results")
        connection.commit()
        _load(connection, archive, releases)
        connection.commit()
        line = _stat_line(connection)
        grade_week(
            connection,
            season=SEASON,
            week=WEEK,
            site="draftkings",
            grading_run_id="grade-dnp-1",
            graded_at=GRADED,
        )
        verdicts = {
            str(row["claim_id"]): str(row["verdict"])
            for row in connection.execute("SELECT claim_id, verdict FROM claim_grades").fetchall()
        }

    assert line["played"] is False
    assert line["snap_share"] == 0.0
    assert verdicts == {"dnp": "correct", "active": "incorrect"}


def test_header_drift_refuses_with_the_missing_columns_named(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    drifted_header = _WEEKLY_HEADER.replace("target_share", "tgt_share")
    _, releases = _pin(
        archive,
        weekly=_csv(drifted_header, _WEEKLY_ROWS),
        snaps=_csv(_SNAP_HEADER, _SNAP_ROWS),
    )
    with connect_database(tmp_path / "stats.sqlite3") as connection:
        apply_migrations(connection)
        _world(connection)
        with pytest.raises(StatsSchemaError, match="missing required columns: target_share"):
            _load(connection, archive, releases)


def test_a_snap_row_that_cannot_be_matched_holds_the_player(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    snaps = tuple(row for row in _SNAP_ROWS if not row.startswith("2026,3,Example Player"))
    _, releases = _pin(
        archive,
        weekly=_csv(_WEEKLY_HEADER, _WEEKLY_ROWS),
        snaps=_csv(_SNAP_HEADER, snaps),
    )
    with connect_database(tmp_path / "stats.sqlite3") as connection:
        apply_migrations(connection)
        _world(connection)
        report = _load(connection, archive, releases)

    assert report.players_written == 0
    assert [item.reason for item in report.held] == [
        "no nflverse snap-count row for 'Example Player' on ABC in week 03, so snaps and "
        "the played fact are unknown"
    ]


def test_refresh_hashes_both_files_and_renders_a_paste_ready_entry(tmp_path: Path) -> None:
    weekly = _csv(_WEEKLY_HEADER, _WEEKLY_ROWS)
    snaps = _csv(_SNAP_HEADER, _SNAP_ROWS)

    def handler(request: httpx.Request) -> httpx.Response:
        body = weekly if "stats_player_week" in request.url.path else snaps
        return httpx.Response(200, content=body, request=request)

    archive = tmp_path / "archive"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = refresh_stats_release(
            SEASON, archive, reviewed_at=REVIEWED, client=client, today=REVIEWED
        )

    rendered = report.render()
    assert report.weekly_sha256 == hashlib.sha256(weekly).hexdigest()
    assert report.snaps_sha256 == hashlib.sha256(snaps).hexdigest()
    assert (report.weekly_rows, report.snap_rows) == (6, 6)
    assert report.matches_pin is False
    assert "PinnedStatsRelease(" in rendered
    assert "reviewed_at=date(2026, 9, 22)" in rendered
    # Both files are archived under their own hashes, so the pasted entry is fetchable
    # offline after upstream overwrites the rolling assets.
    archived = {path.stem for path in archive.rglob("*.csv")}
    assert archived == {report.weekly_sha256, report.snaps_sha256}


def _ops_config(tmp_path: Path) -> Any:
    path = tmp_path / "ops.toml"
    path.write_text(
        f"""
timezone = "America/New_York"
season = {SEASON}
monthly_llm_budget_usd = "50.00"
keychain_service = "narrative-alpha-anthropic"

[batch]
weekdays = ["wed"]
local_time = "09:30"

[paths]
database = "{tmp_path / "store.sqlite3"}"
snapshot_root = "{tmp_path / "snapshots"}"
nflverse_archive = "{tmp_path / "archive"}"
log_directory = "{tmp_path / "logs"}"
""".lstrip(),
        encoding="utf-8",
    )
    return load_ops_config(path)


def _run_lane(config: Any, standings: Path, releases: Any, *, site: str = "dk") -> Any:
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        return run_results(
            connection,
            config=config,
            season=SEASON,
            week=WEEK,
            site=site,
            standings_files=[standings],
            artifact_directory=config.database.parent / "decisions",
            report_directory=config.database.parent / "reports",
            dependencies=ResultsDependencies(
                load_workload_stats=partial(load_workload_stats, releases=releases)
            ),
            now=OBSERVED,
        )


def test_the_lane_writes_stat_lines_before_it_grades(tmp_path: Path) -> None:
    config = _ops_config(tmp_path)
    standings = tmp_path / "contest-standings-missing.csv"
    standings.write_bytes(b"rank,entryid\n")
    _, releases = _pin(
        tmp_path / "archive",
        weekly=_csv(_WEEKLY_HEADER, _WEEKLY_ROWS),
        snaps=_csv(_SNAP_HEADER, _SNAP_ROWS),
    )
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _world(connection)

    report = _run_lane(config, standings, releases)

    steps = [step.step for step in report.steps]
    assert steps.index("results_stats") < steps.index("results_grade")
    stats = report.step("results_stats")
    assert stats is not None and stats.status == "succeeded"
    assert stats.summary["players_written"] == 1
    assert stats.summary["reviewed_at"] == REVIEWED.isoformat()
    assert stats.summary["scoring_column"] == "fantasy_points_ppr"
    # The standings ingest failed on a stub export; the workload step does not read it.
    ingest = report.step("results_ingest")
    assert ingest is not None and ingest.status == "failed"


def test_the_lane_skips_a_site_nflverse_does_not_score(tmp_path: Path) -> None:
    config = _ops_config(tmp_path)
    standings = tmp_path / "contest-standings-missing.csv"
    standings.write_bytes(b"rank,entryid\n")
    _, releases = _pin(
        tmp_path / "archive",
        weekly=_csv(_WEEKLY_HEADER, _WEEKLY_ROWS),
        snaps=_csv(_SNAP_HEADER, _SNAP_ROWS),
    )
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _world(connection)

    report = _run_lane(config, standings, releases, site="fd")

    stats = report.step("results_stats")
    assert stats is not None and stats.status == "skipped"
    assert "no fantasy-point column for fanduel" in str(stats.error_text)


def test_the_lane_states_the_gap_when_nothing_is_pinned(tmp_path: Path) -> None:
    config = _ops_config(tmp_path)
    standings = tmp_path / "contest-standings-missing.csv"
    standings.write_bytes(b"rank,entryid\n")
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _world(connection)

    report = _run_lane(config, standings, MappingProxyType({}))

    stats = report.step("results_stats")
    assert stats is not None and stats.status == "skipped"
    assert "na-crosswalk nflverse-stats-refresh" in str(stats.error_text)


def test_status_shows_the_newest_stats_pin_and_its_age(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _ops_config(tmp_path)
    release, releases = _pin(
        tmp_path / "archive",
        weekly=_csv(_WEEKLY_HEADER, _WEEKLY_ROWS),
        snaps=_csv(_SNAP_HEADER, _SNAP_ROWS),
    )
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        unpinned = collect_ops_status(
            connection, config=config, database=config.database, now=OBSERVED
        )
        monkeypatch.setattr(
            "narrative_alpha.ops.status.pinned_stats_release",
            partial(pinned_stats_release, releases=releases),
        )
        pinned = collect_ops_status(
            connection,
            config=config,
            database=config.database,
            now=OBSERVED + timedelta(days=5),
        )

    assert unpinned.workload_stats_pin is None
    assert any("nflverse-stats-refresh" in action for action in unpinned.manual_actions)
    assert "none reviewed" in render_status(unpinned)

    pin = pinned.workload_stats_pin
    assert pin is not None
    assert (pin.season, pin.reviewed_at, pin.age_days) == (SEASON, REVIEWED, 5)
    assert pin.weekly_sha256 == release.weekly_sha256
    payload = status_payload(pinned)
    assert payload["workload_stats_pin"] == {  # type: ignore[comparison-overlap]
        "season": SEASON,
        "reviewed_at": REVIEWED.isoformat(),
        "age_days": 5,
        "weekly_sha256": release.weekly_sha256,
        "snap_counts_sha256": release.snaps_sha256,
    }
    assert "WORKLOAD STATS PIN" in render_status(pinned)
