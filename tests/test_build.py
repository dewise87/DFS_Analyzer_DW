from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import narrative_alpha.build as build_module
import narrative_alpha.replay as replay_module
from narrative_alpha.build import BuildSelfVerificationError, build_decision
from narrative_alpha.identity import CrosswalkError
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.portfolio import CandidatePlayer, DfsSite, PydfsAdapter
from narrative_alpha.replay import replay_decision
from narrative_alpha.store import (
    DecisionSnapshotRow,
    apply_migrations,
    canonical_manifest_hashes,
    connect_database,
)

DATA_AT = datetime(2026, 9, 13, 12, tzinfo=UTC)
DECISION_AT = datetime(2026, 9, 13, 16, 55, tzinfo=UTC)
SALARY_HASH = "a" * 64
PROJECTION_HASH = "b" * 64


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
    assert build_module.select_candidate_scenario is replay_module.select_candidate_scenario


def _seed_database(database: Path) -> None:
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed_candidate_pool(connection, _players())


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
                    game_start=datetime(
                        2026, 9, 13, 17 + team_index // 2, tzinfo=UTC
                    ),
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
                "kickoff_at": utc_timestamp(
                    datetime(2026, 9, 13, 17 + game_index, tzinfo=UTC)
                ),
                "home_team_id": team_ids[home],
                "away_team_id": team_ids[away],
                "stadium_name": "Fixture Stadium",
                "game_status": "scheduled",
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
