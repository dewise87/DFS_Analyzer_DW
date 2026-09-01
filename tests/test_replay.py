from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from narrative_alpha.portfolio import (
    CandidatePlayer,
    CandidatePlayerScenario,
    ContestArchetype,
    DfsSite,
    OptimizationRequest,
    PydfsAdapter,
    SlateType,
    UploadEntry,
)
from narrative_alpha.replay import (
    MissingAsOfBound,
    PointInTimeSession,
    ReplayArtifactError,
    UnboundedReplayQuery,
    replay_decision,
)
from narrative_alpha.store import (
    DecisionManifestHash,
    DecisionSnapshotRow,
    apply_migrations,
    connect_database,
    manifest_hash_set_sha256,
)

DATA_AT = datetime(2026, 9, 13, 12, tzinfo=UTC)
DECISION_AT = datetime(2026, 9, 13, 16, 55, tzinfo=UTC)
SALARY_HASH = "a" * 64
PROJECTION_HASH = "b" * 64


def test_point_in_time_session_refuses_missing_or_unbounded_reads(tmp_path: Path) -> None:
    with connect_database(tmp_path / "replay.sqlite3") as connection:
        apply_migrations(connection)
        session = PointInTimeSession(connection)

        with pytest.raises(MissingAsOfBound):
            session.query("SELECT :as_of", as_of=None)
        with pytest.raises(UnboundedReplayQuery):
            session.query("SELECT 1", as_of=DECISION_AT)


def test_replay_is_byte_stable_and_ignores_post_lock_projection(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    request_path = artifact_root / "optimizer" / "request.json"
    request_path.parent.mkdir(parents=True)
    request = _request()
    request_bytes = request.model_dump_json(indent=2).encode("utf-8")
    request_path.write_bytes(request_bytes)
    adapter = PydfsAdapter()
    expected_output = adapter.export_upload_csv(
        adapter.build_lineups(request), request.site, request.upload_entries
    )
    output_path = artifact_root / "lineups" / "upload.csv"
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(expected_output)
    manifest = (
        DecisionManifestHash(
            artifact_kind="salary",
            sha256=SALARY_HASH,
            path="salary/draftkings.csv",
            source="draftkings",
        ),
        DecisionManifestHash(
            artifact_kind="projection",
            sha256=PROJECTION_HASH,
            path="projections/fixture.csv",
            source="fixture-projection",
        ),
        DecisionManifestHash(
            artifact_kind="optimizer_request",
            sha256=hashlib.sha256(request_bytes).hexdigest(),
            path="optimizer/request.json",
            source="narrative-alpha",
        ),
        DecisionManifestHash(
            artifact_kind="generated_lineups",
            sha256=hashlib.sha256(expected_output).hexdigest(),
            path="lineups/upload.csv",
            source="narrative-alpha",
        ),
    )
    snapshot = DecisionSnapshotRow(
        decision_snapshot_id="decision-fixture-1",
        slate_id=1,
        decision_at=DECISION_AT,
        created_at=DECISION_AT,
        manifest_schema_version="1.0",
        manifest_hashes_json=manifest,
        manifest_hash_set_sha256=manifest_hash_set_sha256(manifest),
        run_id=None,
        note="byte-stable replay fixture",
    )

    with connect_database(tmp_path / "replay.sqlite3") as connection:
        apply_migrations(connection)
        _seed_candidate_pool(connection, request.candidate_player_scenario.players)
        _insert(connection, "decision_snapshots", snapshot.db_values())

        first = replay_decision(
            connection,
            decision_snapshot_id=snapshot.decision_snapshot_id,
            decision_at=DECISION_AT,
            artifact_root=artifact_root,
            adapter=adapter,
        )
        _insert_post_lock_projection(connection)
        second = replay_decision(
            connection,
            decision_snapshot_id=snapshot.decision_snapshot_id,
            decision_at=DECISION_AT,
            artifact_root=artifact_root,
            adapter=adapter,
        )
        _insert_prelock_projection_drift(connection)
        with pytest.raises(ReplayArtifactError, match="candidate values differ"):
            replay_decision(
                connection,
                decision_snapshot_id=snapshot.decision_snapshot_id,
                decision_at=DECISION_AT,
                artifact_root=artifact_root,
                adapter=adapter,
            )

    assert first.output_bytes == expected_output
    assert second.output_bytes == first.output_bytes
    assert first.report.actual_output_sha256 == second.report.actual_output_sha256
    assert first.report.output_matches
    assert second.report.output_matches


def _request() -> OptimizationRequest:
    return OptimizationRequest(
        site=DfsSite.DRAFTKINGS,
        slate_id=1,
        slate_type=SlateType.CLASSIC,
        contest_archetype=ContestArchetype.CASH,
        salary_cap=50_000,
        candidate_player_scenario=CandidatePlayerScenario(
            scenario_id="prelock-fixture",
            players=_players(),
            projection_source_versions=(
                f"fixture-projection:projection-v1:{PROJECTION_HASH}",
            ),
        ),
        number_of_lineups=1,
        upload_entries=(
            UploadEntry(
                entry_id="entry-1",
                contest_id="contest-1",
                contest_name="Replay Fixture",
                entry_fee="$1.00",
            ),
        ),
    )


def _players() -> tuple[CandidatePlayer, ...]:
    position_counts = (("QB", 4), ("RB", 8), ("WR", 10), ("TE", 4), ("DST", 4))
    salaries = {"QB": 6200, "RB": 4700, "WR": 4300, "TE": 3500, "DST": 2800}
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
                "kickoff_at": _timestamp(
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
            "starts_at": _timestamp(datetime(2026, 9, 13, 17, tzinfo=UTC)),
            "locks_at": _timestamp(datetime(2026, 9, 13, 17, tzinfo=UTC)),
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


def _insert_post_lock_projection(connection: sqlite3.Connection) -> None:
    post_lock = DECISION_AT + timedelta(minutes=1)
    _insert(
        connection,
        "projection_snapshots",
        {
            "projection_snapshot_id": 10_001,
            "slate_id": 1,
            "player_id": 30,
            "site": "draftkings",
            "projection_mean": 999.0,
            "projection_floor": None,
            "projection_ceiling": None,
            "ownership_projection": 0.99,
            "source_file_sha256": PROJECTION_HASH,
            **_pit(
                "fixture-projection",
                observed_at=post_lock,
                source_version="projection-post-lock",
            ),
        },
    )


def _insert_prelock_projection_drift(connection: sqlite3.Connection) -> None:
    _insert(
        connection,
        "projection_snapshots",
        {
            "projection_snapshot_id": 10_002,
            "slate_id": 1,
            "player_id": 30,
            "site": "draftkings",
            "projection_mean": 998.0,
            "projection_floor": None,
            "projection_ceiling": None,
            "ownership_projection": 0.98,
            "source_file_sha256": PROJECTION_HASH,
            **_pit(
                "fixture-projection",
                observed_at=DATA_AT + timedelta(microseconds=1),
                source_version="projection-drift",
            ),
        },
    )


def _pit(
    source: str,
    *,
    observed_at: datetime = DATA_AT,
    source_version: str = "fixture-v1",
) -> dict[str, Any]:
    timestamp = _timestamp(observed_at)
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


def _insert(
    connection: sqlite3.Connection, table: str, values: dict[str, Any]
) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
