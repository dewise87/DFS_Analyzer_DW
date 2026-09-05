from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from narrative_alpha.build import BuildResult, build_decision
from narrative_alpha.interface import SlateMemoError, build_slate_memo, render_slate_memo
from narrative_alpha.portfolio import DfsSite, PydfsAdapter
from narrative_alpha.replay import replay_decision
from narrative_alpha.report_cli import BASELINE_NOT_REQUESTED_NOTICE
from narrative_alpha.report_cli import main as report_main
from narrative_alpha.store import apply_migrations, connect_database

DATA_AT = datetime(2026, 9, 13, 12, tzinfo=UTC)
DECISION_AT = datetime(2026, 9, 13, 16, 55, tzinfo=UTC)
EVALUATION_AT = datetime(2026, 9, 14, 12, tzinfo=UTC)
SALARY_HASH = "a" * 64
PROJECTION_HASH = "b" * 64
ODDS_HASH = "c" * 64
WEATHER_HASH = "d" * 64
GOLDEN = Path(__file__).with_name("golden") / "slate_memo.txt"


def test_slate_memo_with_attached_contest_matches_golden(tmp_path: Path) -> None:
    database, artifacts, built = _built_fixture(tmp_path)
    with connect_database(database) as connection:
        _insert_contest(connection, contest_id=1, observed_at=DATA_AT)
        memo = build_slate_memo(built, connection, contest_id=1)

    assert memo.as_of == DECISION_AT
    assert memo.decision_at == DECISION_AT
    assert memo.decision_snapshot_id == built.snapshot.decision_snapshot_id
    assert memo.run_id == built.snapshot.run_id
    assert memo.heuristic_report is not None
    assert memo.attached_contest is not None
    assert memo.attached_contest.contest_id == 1
    assert memo.lineups[0].projected_ownership_sum == pytest.approx(0.9)
    assert {artifact.artifact_kind for artifact in memo.input_artifacts} == {
        "salary",
        "projection",
    }
    assert render_slate_memo(memo) == GOLDEN.read_text(encoding="utf-8")
    assert artifacts.exists()


def test_memo_without_contest_is_explicit_and_tampering_is_refused(
    tmp_path: Path,
) -> None:
    database, _, built = _built_fixture(tmp_path)
    with connect_database(database) as connection:
        memo = build_slate_memo(built, connection)
        rendered = render_slate_memo(memo)
        assert "heuristic_ev_status=unavailable — no contest attached" in rendered
        assert "HEURISTIC ONLY — NOT SIMULATOR-BACKED" in rendered

        player = built.lineups[0].players[0]
        changed_player = player.model_copy(update={"projection": player.projection + 1})
        changed_lineup = built.lineups[0].model_copy(
            update={"players": (changed_player, *built.lineups[0].players[1:])}
        )
        tampered = replace(built, lineups=(changed_lineup,))
        with pytest.raises(SlateMemoError, match="verified replay lineups"):
            build_slate_memo(tampered, connection)


def test_same_hash_under_wrong_source_cannot_enter_memo(tmp_path: Path) -> None:
    database, artifacts, built = _built_fixture(tmp_path)
    with connect_database(database) as connection:
        before = render_slate_memo(build_slate_memo(built, connection))
        original = connection.execute(
            "SELECT * FROM salaries WHERE salary_id = 1"
        ).fetchone()
        assert original is not None
        impostor = dict(original)
        observed_at = _timestamp(DATA_AT + timedelta(hours=1))
        impostor.update(
            {
                "salary_id": 99,
                "salary": 9_999,
                "source": "impostor",
                "observed_at": observed_at,
                "ingested_at": observed_at,
                "valid_from": observed_at,
                "source_version": "impostor-v1",
            }
        )
        _insert(connection, "salaries", impostor)
        after = render_slate_memo(build_slate_memo(built, connection))
        replayed = replay_decision(
            connection,
            decision_snapshot_id=built.snapshot.decision_snapshot_id,
            decision_at=DECISION_AT,
            artifact_root=artifacts,
            adapter=PydfsAdapter(),
        )

    assert after == before
    assert replayed.report.output_matches is True


def test_post_cutoff_contest_cannot_be_attached(tmp_path: Path) -> None:
    database, _, built = _built_fixture(tmp_path)
    with connect_database(database) as connection:
        _insert_contest(
            connection,
            contest_id=2,
            observed_at=DECISION_AT + timedelta(microseconds=1),
        )
        with pytest.raises(SlateMemoError, match="unavailable"):
            build_slate_memo(built, connection, contest_id=2)


def test_exact_physical_contest_version_and_formula_inputs_are_rendered(
    tmp_path: Path,
) -> None:
    database, _, built = _built_fixture(tmp_path)
    with connect_database(database) as connection:
        _insert_contest(
            connection,
            contest_id=1,
            observed_at=DATA_AT,
            external_contest_id="versioned-contest",
            entry_fee_cents=1_000,
        )
        _insert_contest(
            connection,
            contest_id=3,
            observed_at=DATA_AT + timedelta(hours=1),
            external_contest_id="versioned-contest",
            entry_fee_cents=2_000,
        )
        memo = build_slate_memo(built, connection, contest_id=1)

    rendered = render_slate_memo(memo)
    assert "contest_id=1\n" in rendered
    assert "contest_entry_fee_cents=1000\n" in rendered
    assert "contest_observed_at=2026-09-13T12:00:00.000000Z\n" in rendered
    assert "contest_entry_fee_cents=2000\n" not in rendered


def test_na_report_reconstructs_after_build_and_stdout_matches_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, artifacts, built = _built_fixture(tmp_path)
    with connect_database(database) as connection:
        _insert_contest(connection, contest_id=1, observed_at=DATA_AT)
    output = tmp_path / "report.txt"

    exit_code = report_main(
        [
            "--database",
            str(database),
            "--decision-snapshot-id",
            built.snapshot.decision_snapshot_id,
            "--decision-at",
            DECISION_AT.isoformat(),
            "--evaluation-as-of",
            EVALUATION_AT.isoformat(),
            "--artifact-root",
            str(artifacts),
            "--output",
            str(output),
            "--contest-id",
            "1",
            "--minimum-sample-size",
            "2",
            "--pit-bins",
            "7",
            "--pit-random-seed",
            "42",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert captured.out == output.read_text(encoding="utf-8")
    assert "SLATE DECISION MEMO" in captured.out
    assert "BASELINE EVALUATION REPORT — PURCHASED PROJECTIONS" in captured.out
    assert "minimum_sample_size=2\n" in captured.out
    assert "pit_bins=7\n" in captured.out
    assert "pit_random_seed=42\n" in captured.out


def test_na_report_without_evaluation_cutoff_renders_memo_and_says_baseline_skipped(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The pre-kickoff Saturday flow: no results exist yet, so the memo stands alone."""

    database, artifacts, built = _built_fixture(tmp_path)
    output = tmp_path / "memo-only.txt"

    exit_code = report_main(
        [
            "--database",
            str(database),
            "--decision-snapshot-id",
            built.snapshot.decision_snapshot_id,
            "--decision-at",
            DECISION_AT.isoformat(),
            "--artifact-root",
            str(artifacts),
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert captured.out == output.read_text(encoding="utf-8")
    assert "SLATE DECISION MEMO" in captured.out
    # The absence of a baseline is stated, never silent, and never a measured zero.
    assert BASELINE_NOT_REQUESTED_NOTICE in captured.out
    assert "BASELINE EVALUATION REPORT — PURCHASED PROJECTIONS" not in captured.out


def _built_fixture(tmp_path: Path) -> tuple[Path, Path, BuildResult]:
    database = tmp_path / "memo.sqlite3"
    artifacts = tmp_path / "artifacts"
    _seed_build_store(database)
    built = build_decision(
        database,
        slate_id=1,
        site=DfsSite.DRAFTKINGS,
        decision_at=DECISION_AT,
        artifact_directory=artifacts,
    )
    return database, artifacts, built


def _seed_build_store(database: Path) -> None:
    with connect_database(database) as connection:
        apply_migrations(connection)
        teams = ("AAA", "BBB", "CCC", "DDD")
        for team_id, team in enumerate(teams, start=1):
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
        for game_id, (home, away) in enumerate(((1, 2), (3, 4)), start=1):
            _insert(
                connection,
                "games",
                {
                    "game_id": game_id,
                    "external_game_id": f"game-{game_id}",
                    "season": 2026,
                    "week": 1,
                    "kickoff_at": _timestamp(datetime(2026, 9, 13, 16 + game_id, tzinfo=UTC)),
                    "home_team_id": home,
                    "away_team_id": away,
                    "stadium_name": "Fixture Stadium",
                    "game_status": "scheduled",
                    **_pit("fixture"),
                },
            )
            # Odds and weather are slate readiness inputs (Slice 47), so a fixture that
            # omits them is a slate the build refuses. Both games carry both.
            _insert(
                connection,
                "odds_snapshots",
                {
                    "game_id": game_id,
                    "sportsbook": "fixture-book",
                    "home_spread": -3.0,
                    "away_spread": 3.0,
                    "total": 44.5,
                    "response_file_sha256": ODDS_HASH,
                    **_pit("fixture-odds"),
                },
            )
            _insert(
                connection,
                "weather_snapshots",
                {
                    "game_id": game_id,
                    "stadium_name": "Fixture Stadium",
                    "forecast_model": "fixture-model",
                    "forecast_run_at": _timestamp(DATA_AT),
                    "forecast_for_at": _timestamp(
                        datetime(2026, 9, 13, 16 + game_id, tzinfo=UTC)
                    ),
                    "lead_time_seconds": 3600,
                    "temperature_c": 18.0,
                    "precipitation_probability": 0.1,
                    "wind_speed_kph": 12.0,
                    "wind_gust_kph": 20.0,
                    "weather_code": 1,
                    "response_file_sha256": WEATHER_HASH,
                    **_pit("fixture-weather"),
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
        player_specs = (
            (1, "QB", 1, 2, 1, 6_000, 24.0),
            (2, "RB", 1, 2, 1, 5_000, 20.0),
            (3, "RB", 2, 1, 1, 5_000, 19.0),
            (4, "RB", 3, 4, 2, 5_000, 18.0),
            (5, "WR", 1, 2, 1, 4_500, 17.0),
            (6, "WR", 2, 1, 1, 4_500, 16.0),
            (7, "WR", 3, 4, 2, 4_500, 15.0),
            (8, "TE", 4, 3, 2, 3_500, 14.0),
            (9, "DST", 4, 3, 2, 2_500, 12.0),
        )
        for player_id, position, team_id, opponent_id, game_id, salary, projection in player_specs:
            _insert(
                connection,
                "players",
                {
                    "player_id": player_id,
                    "player_key": f"player-{player_id}",
                    "canonical_name": f"{position} Player {player_id}",
                    "position": position,
                    "birth_date": None,
                    **_pit("fixture"),
                },
            )
            slots = [position, "FLEX"] if position in {"RB", "WR", "TE"} else [position]
            _insert(
                connection,
                "salaries",
                {
                    "salary_id": player_id,
                    "slate_id": 1,
                    "player_id": player_id,
                    "game_id": game_id,
                    "team_id": team_id,
                    "opponent_team_id": opponent_id,
                    "site_player_id": str(10_000 + player_id),
                    "roster_positions_json": json.dumps(slots),
                    "salary": salary,
                    "player_status": None,
                    "source_file_sha256": SALARY_HASH,
                    **_pit("draftkings", source_version="salary-v1"),
                },
            )
            _insert(
                connection,
                "projection_snapshots",
                {
                    "projection_snapshot_id": player_id,
                    "slate_id": 1,
                    "player_id": player_id,
                    "site": "draftkings",
                    "projection_mean": projection,
                    "projection_floor": None,
                    "projection_ceiling": None,
                    "ownership_projection": 0.1,
                    "source_file_sha256": PROJECTION_HASH,
                    **_pit("fixture-projection", source_version="projection-v1"),
                },
            )


def _insert_contest(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    observed_at: datetime,
    external_contest_id: str | None = None,
    entry_fee_cents: int = 1_000,
) -> None:
    _insert(
        connection,
        "contests",
        {
            "contest_id": contest_id,
            "external_contest_id": external_contest_id or f"dk-contest-{contest_id}",
            "site": "draftkings",
            "slate_id": 1,
            "archetype": "single_entry",
            "field_size": 100,
            "entry_limit": 1,
            "entry_fee_cents": entry_fee_cents,
            "total_prizes_cents": 1_000_000,
            "payout_curve_id": f"payout-{contest_id}",
            **_pit("manual-site-lobby", observed_at=observed_at),
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


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _insert(connection: sqlite3.Connection, table: str, values: dict[str, Any]) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )
