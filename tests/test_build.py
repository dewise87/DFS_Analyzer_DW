from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import narrative_alpha.build as build_module
import narrative_alpha.replay as replay_module
from narrative_alpha.build import (
    BuildDataError,
    BuildReadinessError,
    BuildSelfVerificationError,
    build_decision,
)
from narrative_alpha.identity import CrosswalkError
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.interface import build_slate_memo, render_slate_memo
from narrative_alpha.ops.backup import create_backup, restore_backup
from narrative_alpha.portfolio import (
    CandidatePlayer,
    ContestArchetype,
    DfsSite,
    PydfsAdapter,
)
from narrative_alpha.replay import replay_decision
from narrative_alpha.store import (
    DecisionSnapshotRow,
    apply_migrations,
    canonical_manifest_hashes,
    connect_database,
)

DATA_AT = datetime(2026, 9, 13, 12, tzinfo=UTC)
DECISION_AT = datetime(2026, 9, 13, 16, 55, tzinfo=UTC)
SHOWDOWN_DATA_AT = datetime(2026, 9, 13, 15, 55, tzinfo=UTC)
SALARY_HASH = "a" * 64
PROJECTION_HASH = "b" * 64
ODDS_HASH = "c" * 64
WEATHER_HASH = "d" * 64


def test_build_then_replay_is_byte_identical_and_commits_run(tmp_path: Path) -> None:
    database = tmp_path / "build.sqlite3"
    artifacts = tmp_path / "artifacts"
    _seed_database(database)

    built = build_decision(
        database,
        slate_id=1,
        site=DfsSite.DRAFTKINGS,
        decision_at=DECISION_AT,
        artifact_directory=artifacts,
    )

    assert built.replay.report.output_matches
    assert built.replay.output_bytes == built.generated_lineups_path.read_bytes()
    assert built.optimizer_request_path.read_bytes().startswith(b'{"bring_back_rules"')
    assert built.manifest_path.read_bytes() == canonical_manifest_hashes(
        built.snapshot.manifest_hashes_json
    ).encode("utf-8")
    policy_artifact = next(
        item
        for item in built.snapshot.manifest_hashes_json
        if item.artifact_kind == "contest_policy"
    )
    assert policy_artifact.source == built.contest_policy.policy_version
    assert policy_artifact.sha256 == built.contest_policy.sha256
    assert built.contest_policy_path.read_bytes() == built.contest_policy.raw_bytes
    assert {
        item.sha256
        for item in built.snapshot.manifest_hashes_json
        if item.artifact_kind == "salary"
    } == {SALARY_HASH}
    assert {
        item.sha256
        for item in built.snapshot.manifest_hashes_json
        if item.artifact_kind == "projection"
    } == {PROJECTION_HASH}

    with connect_database(database) as connection:
        run = connection.execute(
            "SELECT status, started_at, completed_at, created_at FROM model_runs"
        ).fetchone()
        snapshot = DecisionSnapshotRow.from_db(
            connection.execute("SELECT * FROM decision_snapshots").fetchone()
        )
        replayed = replay_decision(
            connection,
            decision_snapshot_id=snapshot.decision_snapshot_id,
            decision_at=DECISION_AT,
            artifact_root=artifacts,
            adapter=PydfsAdapter(),
        )

    expected_timestamp = utc_timestamp(DECISION_AT)
    assert tuple(run) == (
        "succeeded",
        expected_timestamp,
        expected_timestamp,
        expected_timestamp,
    )
    assert replayed.report.output_matches
    assert replayed.output_bytes == built.replay.output_bytes


@pytest.mark.parametrize("site", [DfsSite.DRAFTKINGS, DfsSite.FANDUEL])
def test_showdown_build_uses_both_ownership_roles_and_replays_identically(
    tmp_path: Path,
    site: DfsSite,
) -> None:
    database = tmp_path / "showdown.sqlite3"
    artifacts = tmp_path / "artifacts"
    _seed_showdown_database(database, site=site)
    captain_slot = "CPT" if site is DfsSite.DRAFTKINGS else "MVP"

    built = build_decision(
        database,
        slate_id=1,
        site=site,
        decision_at=DECISION_AT,
        artifact_directory=artifacts,
        contest_archetype=ContestArchetype.SHOWDOWN,
    )

    assert built.replay.report.output_matches
    assert built.generated_lineups_path.read_bytes() == built.replay.output_bytes
    assert (
        built.generated_lineups_path.read_text(encoding="utf-8").splitlines()[0].split(",")[0]
        == captain_slot
    )
    assert all(
        player.projected_ownership == pytest.approx(5 / 6)
        and player.projected_ownership_captain == pytest.approx(1 / 6)
        for player in built.request.candidate_player_scenario.players
    )
    assert len(built.lineups[0].players) == 6
    captain = next(player for player in built.lineups[0].players if player.slot == captain_slot)
    candidate = next(
        player
        for player in built.request.candidate_player_scenario.players
        if player.player_id == captain.player_id
    )
    assert captain.salary == round(candidate.salary * 1.5)
    assert captain.projection == pytest.approx(candidate.projection * 1.5)

    with connect_database(database) as connection:
        replayed = replay_decision(
            connection,
            decision_snapshot_id=built.snapshot.decision_snapshot_id,
            decision_at=DECISION_AT,
            artifact_root=artifacts,
            adapter=PydfsAdapter(),
        )
        memo = render_slate_memo(build_slate_memo(built, connection))

    assert replayed.output_bytes == built.generated_lineups_path.read_bytes()
    assert "CAPTAIN CHOICES\n" in memo
    assert ",0.833333,0.166667\n" in memo


def test_showdown_build_refuses_a_missing_ownership_role(tmp_path: Path) -> None:
    database = tmp_path / "showdown.sqlite3"
    _seed_showdown_database(database)
    with connect_database(database) as connection:
        connection.execute(
            "DELETE FROM ownership_baselines WHERE player_id = 6 AND role = 'captain'"
        )

    # Readiness sees the same hole first and refuses on its own threshold. Accepting
    # that named failure proves the deeper guard is not merely shadowed by it: candidate
    # selection still refuses a showdown pool missing a role baseline.
    with pytest.raises(BuildReadinessError, match=r"ownership_coverage_captain"):
        build_decision(
            database,
            slate_id=1,
            site=DfsSite.DRAFTKINGS,
            decision_at=DECISION_AT,
            artifact_directory=tmp_path / "artifacts",
            contest_archetype=ContestArchetype.SHOWDOWN,
        )
    with pytest.raises(BuildDataError, match=r"player 6 captain"):
        build_decision(
            database,
            slate_id=1,
            site=DfsSite.DRAFTKINGS,
            decision_at=DECISION_AT,
            artifact_directory=tmp_path / "artifacts",
            contest_archetype=ContestArchetype.SHOWDOWN,
            accepted_readiness_failures=("ownership_coverage_captain",),
        )


def test_backup_restore_drill_replays_decision_byte_identically(tmp_path: Path) -> None:
    """The recovery proof is a real build, online backup, out-of-place restore, and replay."""

    database = tmp_path / "live-fixture" / "store.sqlite3"
    artifacts = tmp_path / "live-fixture" / "decisions"
    reports = tmp_path / "live-fixture" / "reports"
    pins = tmp_path / "live-fixture" / "pins"
    snapshots = tmp_path / "live-fixture" / "snapshots"
    backups = tmp_path / "backups"
    for directory in (reports, pins, snapshots):
        directory.mkdir(parents=True)
    _seed_database(database)
    built = build_decision(
        database,
        slate_id=1,
        site=DfsSite.DRAFTKINGS,
        decision_at=DECISION_AT,
        artifact_directory=artifacts,
    )
    backup = create_backup(
        database=database,
        artifact_directory=artifacts,
        report_directory=reports,
        pin_archive=pins,
        snapshot_root=snapshots,
        backup_directory=backups,
        now=DECISION_AT + timedelta(hours=1),
    )
    restored = restore_backup(
        backup=backup.stamp,
        into=tmp_path / "restored-fixture",
        backup_directory=backups,
    )
    with connect_database(restored.database) as connection:
        replayed = replay_decision(
            connection,
            decision_snapshot_id=built.snapshot.decision_snapshot_id,
            decision_at=DECISION_AT,
            artifact_root=restored.artifact_directory,
            adapter=PydfsAdapter(),
        )
    assert replayed.report.output_matches
    assert replayed.output_bytes == built.generated_lineups_path.read_bytes()


def test_self_verify_artifact_corruption_is_failure_and_rolls_back_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "build.sqlite3"
    artifacts = tmp_path / "artifacts"
    _seed_database(database)
    real_replay = build_module.replay_decision

    def corrupt_then_replay(*args: Any, **kwargs: Any) -> Any:
        artifact_root = Path(kwargs["artifact_root"])
        request_path = next(artifact_root.rglob("optimizer_request.json"))
        request_path.write_bytes(b"{}")
        return real_replay(*args, **kwargs)

    monkeypatch.setattr(build_module, "replay_decision", corrupt_then_replay)

    with pytest.raises(BuildSelfVerificationError, match="immediate replay failed"):
        build_decision(
            database,
            slate_id=1,
            site=DfsSite.DRAFTKINGS,
            decision_at=DECISION_AT,
            artifact_directory=artifacts,
        )

    with connect_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM model_runs").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM decision_snapshots").fetchone()[0] == 0


def test_build_refuses_pending_player_identity(tmp_path: Path) -> None:
    database = tmp_path / "build.sqlite3"
    _seed_database(database)
    with connect_database(database) as connection:
        _insert(
            connection,
            "unresolved_player_matches",
            {
                "identity_key": "c" * 64,
                "source": "fixture-projection",
                "site": "draftkings",
                "external_player_id": "missing-player",
                "name_raw": "Missing Player",
                "normalized_name": "missing player",
                "team": "AAA",
                "opponent": "BBB",
                "position": "WR",
                "roster_status": None,
                "birth_date": None,
                "eligible_positions_json": '["WR"]',
                "candidates_json": "[]",
                "source_file_sha256": PROJECTION_HASH,
                "first_observed_at": utc_timestamp(DATA_AT),
                "last_observed_at": utc_timestamp(DATA_AT),
                "occurrences": 1,
                "status": "pending",
                "resolved_player_id": None,
                "resolved_at": None,
                "resolution_note": None,
                "match_method": None,
                "match_confidence": None,
                "manual_override": 0,
                "run_id": None,
            },
        )

    with pytest.raises(CrosswalkError, match="lineup generation must stop"):
        build_decision(
            database,
            slate_id=1,
            site=DfsSite.DRAFTKINGS,
            decision_at=DECISION_AT,
            artifact_directory=tmp_path / "artifacts",
        )


def test_build_and_replay_share_candidate_selection_function() -> None:
    assert (
        build_module.select_routed_candidate_scenario
        is replay_module.select_routed_candidate_scenario
    )


def _seed_database(database: Path) -> None:
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed_candidate_pool(connection, _players())


def _seed_showdown_database(
    database: Path,
    *,
    player_count: int = 6,
    site: DfsSite = DfsSite.DRAFTKINGS,
) -> None:
    players = _showdown_players(player_count=player_count)
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed_candidate_pool(connection, players)
        if site is DfsSite.FANDUEL:
            connection.execute("UPDATE slates SET site = 'fanduel'")
            connection.execute("UPDATE projection_snapshots SET site = 'fanduel'")
            connection.execute('UPDATE salaries SET roster_positions_json = \'["MVP","FLEX"]\'')
        connection.execute(
            "UPDATE slates SET slate_type = 'showdown', name = 'AAA at BBB Showdown' "
            "WHERE slate_id = 1"
        )
        # A showdown slate's projections may be at most three hours old at the decision
        # instant (`config/readiness.toml`), so this fixture's are an hour old rather than
        # the classic fixture's five.
        fresh = utc_timestamp(SHOWDOWN_DATA_AT)
        connection.execute(
            "UPDATE projection_snapshots SET observed_at = ?, ingested_at = ?, "
            "valid_from = ? WHERE slate_id = 1",
            (fresh, fresh, fresh),
        )
        for player in players:
            for role, ownership in (("captain", 1 / 6), ("flex", 5 / 6)):
                _insert(
                    connection,
                    "ownership_baselines",
                    {
                        "slate_id": 1,
                        "player_id": player.player_id,
                        "site": site.value,
                        "role": role,
                        "ownership": ownership,
                        "source_file_sha256": (
                            f"{player.player_id:02x}{'c' if role == 'captain' else 'f'}"
                        ).ljust(64, "0"),
                        **_pit("fixture-ownership", source_version="ownership-v1"),
                    },
                )


def _showdown_players(*, player_count: int = 6) -> tuple[CandidatePlayer, ...]:
    positions = ("QB", "RB", "WR", "TE", "K", "DST", "WR", "RB")[:player_count]
    return tuple(
        CandidatePlayer(
            player_id=index,
            site_player_id=str(20_000 + index),
            name=f"Showdown Player {index}",
            team="AAA" if index % 2 else "BBB",
            opponent="BBB" if index % 2 else "AAA",
            position=position,
            eligible_roster_slots=("CPT", "FLEX"),
            salary=5_900 + index * 100,
            projection=round(24.0 - index, 4),
            projected_ownership=0.01,
            game_id="game-1",
            game_start=datetime(2026, 9, 13, 17, tzinfo=UTC),
        )
        for index, position in enumerate(positions, start=1)
    )


def _players() -> tuple[CandidatePlayer, ...]:
    position_counts = (("QB", 3), ("RB", 6), ("WR", 8), ("TE", 4), ("DST", 3))
    salaries = {"QB": 6000, "RB": 4800, "WR": 4400, "TE": 3600, "DST": 2800}
    teams = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH")
    players: list[CandidatePlayer] = []
    player_id = 1
    for position, count in position_counts:
        for index in range(count):
            team_index = (player_id - 1) % len(teams)
            slots = (position, "FLEX") if position in {"RB", "WR", "TE"} else (position,)
            players.append(
                CandidatePlayer(
                    player_id=player_id,
                    site_player_id=str(10_000 + player_id),
                    name=f"{position} Player {index + 1}",
                    team=teams[team_index],
                    opponent=teams[team_index ^ 1],
                    position=position,
                    eligible_roster_slots=slots,
                    salary=salaries[position] + index * 25,
                    projection=round(30 - player_id * 0.3, 4),
                    projected_ownership=0.05 + (index % 4) * 0.02,
                    game_id=f"game-{team_index // 2 + 1}",
                    game_start=datetime(2026, 9, 13, 17 + team_index // 2, tzinfo=UTC),
                )
            )
            player_id += 1
    return tuple(players)


def _seed_candidate_pool(
    connection: sqlite3.Connection, players: tuple[CandidatePlayer, ...]
) -> None:
    teams = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH")
    team_ids = {team: index for index, team in enumerate(teams, start=1)}
    for team, team_id in team_ids.items():
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
    for game_index in range(4):
        home = teams[game_index * 2]
        away = teams[game_index * 2 + 1]
        _insert(
            connection,
            "games",
            {
                "game_id": game_index + 1,
                "external_game_id": f"game-{game_index + 1}",
                "season": 2026,
                "week": 1,
                "kickoff_at": utc_timestamp(datetime(2026, 9, 13, 17 + game_index, tzinfo=UTC)),
                "home_team_id": team_ids[home],
                "away_team_id": team_ids[away],
                "stadium_name": "Fixture Stadium",
                "game_status": "scheduled",
                **_pit("fixture"),
            },
        )
        # Odds and weather are readiness inputs (Slice 47): a fixture slate that omits
        # them is an incomplete slate, and the build now says so. Every game gets both, so
        # these fixtures stand for a slate whose inputs are actually all present.
        _insert(
            connection,
            "odds_snapshots",
            {
                "game_id": game_index + 1,
                "sportsbook": "fixture-book",
                "home_spread": -3.0,
                "away_spread": 3.0,
                "total": 44.5,
                "response_file_sha256": ODDS_HASH,
                **_pit("fixture-odds", source_version="odds-v1"),
            },
        )
        _insert(
            connection,
            "weather_snapshots",
            {
                "game_id": game_index + 1,
                "stadium_name": "Fixture Stadium",
                "forecast_model": "fixture-model",
                "forecast_run_at": utc_timestamp(DATA_AT),
                "forecast_for_at": utc_timestamp(
                    datetime(2026, 9, 13, 17 + game_index, tzinfo=UTC)
                ),
                "lead_time_seconds": 3600,
                "temperature_c": 18.0,
                "precipitation_probability": 0.1,
                "wind_speed_kph": 12.0,
                "wind_gust_kph": 20.0,
                "weather_code": 1,
                "response_file_sha256": WEATHER_HASH,
                **_pit("fixture-weather", source_version="weather-v1"),
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
    for player in players:
        _insert(
            connection,
            "players",
            {
                "player_id": player.player_id,
                "player_key": f"player-{player.player_id}",
                "canonical_name": player.name,
                "position": player.position,
                "birth_date": None,
                **_pit("fixture"),
            },
        )
        _insert(
            connection,
            "salaries",
            {
                "salary_id": player.player_id,
                "slate_id": 1,
                "player_id": player.player_id,
                "game_id": int(player.game_id.rsplit("-", 1)[1]),
                "team_id": team_ids[player.team],
                "opponent_team_id": team_ids[player.opponent],
                "site_player_id": player.site_player_id,
                "roster_positions_json": json.dumps(player.eligible_roster_slots),
                "salary": player.salary,
                "player_status": None,
                "source_file_sha256": SALARY_HASH,
                **_pit("draftkings", source_version="salary-v1"),
            },
        )
        _insert(
            connection,
            "projection_snapshots",
            {
                "projection_snapshot_id": player.player_id,
                "slate_id": 1,
                "player_id": player.player_id,
                "site": "draftkings",
                "projection_mean": player.projection,
                "projection_floor": None,
                "projection_ceiling": None,
                "ownership_projection": player.projected_ownership,
                "source_file_sha256": PROJECTION_HASH,
                **_pit("fixture-projection", source_version="projection-v1"),
            },
        )


def _pit(source: str, *, source_version: str = "fixture-v1") -> dict[str, Any]:
    timestamp = utc_timestamp(DATA_AT)
    return {
        "source": source,
        "published_at": None,
        "observed_at": timestamp,
        "ingested_at": timestamp,
        "effective_at": None,
        "valid_from": timestamp,
        "valid_to": None,
        "source_version": source_version,
        "run_id": None,
    }


def _insert(connection: sqlite3.Connection, table: str, values: dict[str, Any]) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )


def test_failed_build_removes_artifacts_so_an_identical_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "build.sqlite3"
    artifacts = tmp_path / "artifacts"
    _seed_database(database)
    real_replay = build_module.replay_decision

    def corrupt_then_replay(*args: Any, **kwargs: Any) -> Any:
        artifact_root = Path(kwargs["artifact_root"])
        request_path = next(artifact_root.rglob("optimizer_request.json"))
        request_path.write_bytes(b"{}")
        return real_replay(*args, **kwargs)

    monkeypatch.setattr(build_module, "replay_decision", corrupt_then_replay)
    with pytest.raises(BuildSelfVerificationError):
        build_decision(
            database,
            slate_id=1,
            site=DfsSite.DRAFTKINGS,
            decision_at=DECISION_AT,
            artifact_directory=artifacts,
        )
    assert list(artifacts.rglob("optimizer_request.json")) == []

    monkeypatch.setattr(build_module, "replay_decision", real_replay)
    result = build_decision(
        database,
        slate_id=1,
        site=DfsSite.DRAFTKINGS,
        decision_at=DECISION_AT,
        artifact_directory=artifacts,
    )
    assert result.replay.report.output_matches


def test_rebuilding_the_identical_decision_is_a_structured_error(tmp_path: Path) -> None:
    database = tmp_path / "build.sqlite3"
    artifacts = tmp_path / "artifacts"
    _seed_database(database)
    build_decision(
        database,
        slate_id=1,
        site=DfsSite.DRAFTKINGS,
        decision_at=DECISION_AT,
        artifact_directory=artifacts,
    )

    with pytest.raises(build_module.BuildInputError, match="already exists"):
        build_decision(
            database,
            slate_id=1,
            site=DfsSite.DRAFTKINGS,
            decision_at=DECISION_AT,
            artifact_directory=artifacts,
        )
