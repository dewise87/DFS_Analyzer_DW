"""Regression checks for the repository review's decision and upload boundaries."""

import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_build import DATA_AT, DECISION_AT, _insert, _pit, _seed_database
from test_optimizer import _request, _showdown_request

from narrative_alpha.build import (
    BuildInputError,
    BuildValidationError,
    build_decision,
)
from narrative_alpha.candidate_selection import select_candidate_scenario
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.interface import build_slate_memo, render_slate_memo
from narrative_alpha.portfolio import (
    CandidatePlayer,
    DfsSite,
    Lineup,
    OptimizationRequest,
    PydfsAdapter,
    validate_lineup,
    validate_portfolio,
)
from narrative_alpha.replay import (
    PointInTimeSession,
    ReplayArtifactError,
    read_frozen_decision,
    replay_decision,
)
from narrative_alpha.store import connect_database, manifest_hash_set_sha256


def _ownership(connection: Any, *, observed_at: Any = DATA_AT, **changes: Any) -> None:
    for row in connection.execute("SELECT player_id FROM players").fetchall():
        _insert(
            connection,
            "ownership_baselines",
            {
                "slate_id": 1,
                "site": "draftkings",
                "player_id": row[0],
                "role": "classic",
                "ownership": 0.1,
                "source_file_sha256": "c" * 64,
                **_pit("dedicated-ownership"),
                "observed_at": utc_timestamp(observed_at),
                "ingested_at": utc_timestamp(observed_at),
                **changes,
            },
        )


def test_dedicated_ownership_is_used_frozen_and_replayed(tmp_path: Path) -> None:
    database = tmp_path / "fixture.sqlite3"
    artifacts = tmp_path / "artifacts"
    _seed_database(database)
    with connect_database(database) as connection:
        # A real vendor may deliver points and ownership in separate exports.
        connection.execute("UPDATE projection_snapshots SET ownership_projection = NULL")
        _ownership(connection)
    built = build_decision(
        database,
        slate_id=1,
        site="draftkings",
        decision_at=DECISION_AT,
        artifact_directory=artifacts,
    )
    assert {
        player.projected_ownership for player in built.request.candidate_player_scenario.players
    } == {0.1}
    assert {
        (item.source, item.sha256)
        for item in built.snapshot.manifest_hashes_json
        if item.artifact_kind == "ownership"
    } == {("dedicated-ownership", "c" * 64)}
    with connect_database(database) as connection:
        memo_before = render_slate_memo(build_slate_memo(built, connection))
        # A later import claiming an earlier observation must not rewrite history.
        _ownership(
            connection,
            observed_at=DECISION_AT - timedelta(seconds=1),
            ingested_at=utc_timestamp(DECISION_AT + timedelta(microseconds=1)),
            ownership=0.9,
            source_file_sha256="d" * 64,
        )
        replayed = replay_decision(
            connection,
            decision_snapshot_id=built.snapshot.decision_snapshot_id,
            decision_at=DECISION_AT,
            artifact_root=artifacts,
            adapter=PydfsAdapter(),
        )
        selected = select_candidate_scenario(
            PointInTimeSession(connection),
            slate_id=1,
            site=DfsSite.DRAFTKINGS,
            as_of=DECISION_AT,
        )
        assert render_slate_memo(build_slate_memo(built, connection)) == memo_before
    assert replayed.report.output_matches
    assert {player.projected_ownership for player in selected.players} == {0.1}


def test_legacy_inline_ownership_replay_ignores_new_dedicated_files(tmp_path: Path) -> None:
    database = tmp_path / "fixture.sqlite3"
    artifacts = tmp_path / "artifacts"
    _seed_database(database)
    built = build_decision(
        database,
        slate_id=1,
        site="draftkings",
        decision_at=DECISION_AT,
        artifact_directory=artifacts,
    )
    with connect_database(database) as connection:
        memo_before = render_slate_memo(build_slate_memo(built, connection))
        _ownership(connection)
        replayed = replay_decision(
            connection,
            decision_snapshot_id=built.snapshot.decision_snapshot_id,
            decision_at=DECISION_AT,
            artifact_root=artifacts,
            adapter=PydfsAdapter(),
        )
        assert render_slate_memo(build_slate_memo(built, connection)) == memo_before
    assert replayed.report.output_matches
    assert replayed.request == built.request


@pytest.mark.parametrize(
    "table,key,field,value",
    [
        ("projection_snapshots", "projection_snapshot_id", "projection_mean", 1000),
        ("salaries", "salary_id", "salary", 1000),
    ],
)
def test_late_ingested_revision_cannot_enter_an_earlier_decision(
    tmp_path: Path,
    table: str,
    key: str,
    field: str,
    value: int,
) -> None:
    database = tmp_path / "fixture.sqlite3"
    _seed_database(database)
    with connect_database(database) as connection:
        session = PointInTimeSession(connection)
        before = select_candidate_scenario(
            session,
            slate_id=1,
            site=DfsSite.DRAFTKINGS,
            as_of=DECISION_AT,
        )
        revision = dict(connection.execute(f"SELECT * FROM {table} WHERE player_id = 1").fetchone())
        del revision[key]
        revision.update(
            {
                field: value,
                "observed_at": utc_timestamp(DECISION_AT - timedelta(seconds=1)),
                "ingested_at": utc_timestamp(DECISION_AT + timedelta(microseconds=1)),
                "source_file_sha256": "f" * 64,
            }
        )
        _insert(connection, table, revision)
        after = select_candidate_scenario(
            session,
            slate_id=1,
            site=DfsSite.DRAFTKINGS,
            as_of=DECISION_AT,
        )
    assert after == before


@pytest.mark.parametrize(
    "status,unavailable",
    [
        ("O", True),
        ("OUT", True),
        ("IR", True),
        ("QUESTIONABLE", False),
        ("D", False),
    ],
)
def test_explicit_salary_inactivity_reaches_the_optimizer(
    tmp_path: Path,
    status: str,
    unavailable: bool,
) -> None:
    database = tmp_path / "fixture.sqlite3"
    _seed_database(database)
    with connect_database(database) as connection:
        connection.execute("UPDATE salaries SET player_status = ? WHERE player_id = 1", (status,))
    built = build_decision(
        database,
        slate_id=1,
        site="draftkings",
        decision_at=DECISION_AT,
        artifact_directory=tmp_path / "artifacts",
    )
    player = next(p for p in built.request.candidate_player_scenario.players if p.player_id == 1)
    assert player.is_injured is unavailable
    if unavailable:
        assert all(p.player_id != 1 for lineup in built.lineups for p in lineup.players)


@pytest.mark.parametrize("minutes", [5, 6])
def test_full_slate_build_refuses_at_and_after_lock(tmp_path: Path, minutes: int) -> None:
    database = tmp_path / "fixture.sqlite3"
    _seed_database(database)
    with pytest.raises(BuildInputError, match="must precede slate lock"):
        build_decision(
            database,
            slate_id=1,
            site="draftkings",
            decision_at=DECISION_AT + timedelta(minutes=minutes),
            artifact_directory=tmp_path / "artifacts",
        )


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("salary", 1, "player_salary"),
        ("projection", 1000, "player_projection"),
        ("projected_ownership", 0.99, "player_ownership"),
        ("team", "FAKE", "candidate_metadata"),
        ("game_id", "fake-game", "candidate_metadata"),
    ],
)
def test_independent_validation_does_not_trust_solver_metadata(
    field: str,
    value: Any,
    code: str,
) -> None:
    request = _request(DfsSite.DRAFTKINGS)
    lineup = PydfsAdapter().build_lineups(request)[0]
    players = (lineup.players[0].model_copy(update={field: value}), *lineup.players[1:])
    corrupted = lineup.model_copy(
        update={
            "players": players,
            "total_salary": sum(p.salary for p in players),
        }
    )
    assert code in {issue.code for issue in validate_lineup(corrupted, request).errors}


def test_portfolio_validation_enforces_count_exclusions_and_pins() -> None:
    request = _request(DfsSite.DRAFTKINGS).model_copy(update={"number_of_lineups": 2})
    lineups = PydfsAdapter().build_lineups(request)
    assert not validate_portfolio((), request).valid
    assert not validate_portfolio(lineups[:1], request).valid
    excluded = request.model_copy(
        update={
            "excluded_lineup_player_ids": (tuple(p.player_id for p in lineups[0].players),),
        }
    )
    assert "excluded_lineup" in {e.code for e in validate_portfolio(lineups, excluded).errors}
    pinned = request.model_copy(update={"pinned_lineups": (lineups[0],)})
    assert "pinned_lineups" in {e.code for e in validate_portfolio(lineups[::-1], pinned).errors}


def test_build_rejects_an_adapter_that_returns_incomplete_output(tmp_path: Path) -> None:
    class EmptyAdapter(PydfsAdapter):
        def build_lineups(self, request: OptimizationRequest) -> tuple[Lineup, ...]:
            return ()

    database = tmp_path / "fixture.sqlite3"
    artifacts = tmp_path / "artifacts"
    _seed_database(database)
    with pytest.raises(BuildValidationError, match="expected 1 lineups, got 0"):
        build_decision(
            database,
            slate_id=1,
            site="draftkings",
            decision_at=DECISION_AT,
            artifact_directory=artifacts,
            adapter=EmptyAdapter(),
        )
    assert not artifacts.exists()
    with connect_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM decision_snapshots").fetchone()[0] == 0


def test_current_fanduel_format_allows_defense_in_flex_and_five_one_stack() -> None:
    request = _showdown_request(DfsSite.FANDUEL)
    pool = request.candidate_player_scenario.players
    players = tuple(
        p.model_copy(
            update={
                "team": "AAA" if i < 5 else "BBB",
                "opponent": "BBB" if i < 5 else "AAA",
            }
        )
        for i, p in enumerate(pool)
    )
    request = request.model_copy(
        update={
            "candidate_player_scenario": request.candidate_player_scenario.model_copy(
                update={"players": players}
            ),
        }
    )
    lineup = PydfsAdapter().build_lineups(request)[0]
    assert len(lineup.players) == 6
    assert sum(p.slot == "FLEX" for p in lineup.players) == 5
    assert any(p.position == "DST" and p.slot == "FLEX" for p in lineup.players)
    assert sum(p.team == "AAA" for p in lineup.players) == 5
    assert validate_lineup(lineup, request).valid


@pytest.mark.parametrize(
    "changes", [{"projection": float("inf")}, {"eligible_roster_slots": (" ",)}]
)
def test_nonfinite_or_empty_candidate_inputs_are_rejected(changes: dict[str, Any]) -> None:
    player = _request(DfsSite.DRAFTKINGS).candidate_player_scenario.players[0]
    with pytest.raises(ValidationError):
        CandidatePlayer.model_validate({**player.model_dump(), **changes})


def test_frozen_decision_reader_refuses_hash_valid_but_illegal_output(tmp_path: Path) -> None:
    database = tmp_path / "fixture.sqlite3"
    artifacts = tmp_path / "artifacts"
    _seed_database(database)
    built = build_decision(
        database,
        slate_id=1,
        site="draftkings",
        decision_at=DECISION_AT,
        artifact_directory=artifacts,
    )
    # A matching hash proves byte integrity, not roster/portfolio legality.
    output = built.generated_lineups_path.read_bytes()
    output += output.splitlines(keepends=True)[1]
    built.generated_lineups_path.write_bytes(output)
    manifest = tuple(
        item.model_copy(update={"sha256": hashlib.sha256(output).hexdigest()})
        if item.artifact_kind == "generated_lineups"
        else item
        for item in built.snapshot.manifest_hashes_json
    )
    bad_snapshot = built.snapshot.model_copy(
        update={
            "decision_snapshot_id": "illegal-output",
            "manifest_hashes_json": manifest,
            "manifest_hash_set_sha256": manifest_hash_set_sha256(manifest),
        }
    )
    with connect_database(database) as connection:
        _insert(connection, "decision_snapshots", bad_snapshot.db_values())
        with pytest.raises(ReplayArtifactError, match="expected 1 lineups, got 2"):
            read_frozen_decision(
                connection,
                decision_snapshot_id="illegal-output",
                decision_at=DECISION_AT,
                artifact_root=artifacts,
            )
