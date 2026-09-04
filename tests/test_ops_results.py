"""The Tuesday results lane: immutable capture, labels, replay proof, and reports."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import narrative_alpha.ops.results as results_module
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.ops import (
    RESULTS_STEPS,
    ResultsDependencies,
    collect_ops_status,
    load_ops_config,
    render_status,
    run_results,
    status_payload,
)
from narrative_alpha.ops.cli import main as ops_main
from narrative_alpha.store import apply_migrations, connect_database

SEASON = 2026
WEEK = 1
PRELOCK = datetime(2026, 9, 13, 16, tzinfo=UTC)
FIRST_RESULTS = datetime(2026, 9, 15, 12, tzinfo=UTC)
GOLDEN = Path(__file__).parent / "golden" / "draftkings_contest_standings.csv"
CONTEST_ID = "dk-probe-1"


def _config(tmp_path: Path) -> Any:
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


def _standings(tmp_path: Path, *, name: str = f"contest-standings-{CONTEST_ID}.csv") -> Path:
    path = tmp_path / name
    path.write_bytes(GOLDEN.read_bytes())
    return path


def _seed(connection: sqlite3.Connection, *, contest: bool = True, decision: bool = False) -> None:
    stamp = utc_timestamp(PRELOCK - timedelta(days=1))
    for team_id, code in ((1, "AAA"), (2, "BBB")):
        connection.execute(
            """
            INSERT INTO teams(
                team_id, team_key, abbreviation, canonical_name, league, source,
                published_at, observed_at, ingested_at, effective_at, valid_from,
                valid_to, source_version, run_id
            ) VALUES (?, ?, ?, ?, 'NFL', 'fixture', NULL, ?, ?, NULL, ?, NULL,
                      'fixture-v1', NULL)
            """,
            (team_id, code, code, f"Team {code}", stamp, stamp, stamp),
        )
    connection.execute(
        """
        INSERT INTO games(
            game_id, external_game_id, season, week, kickoff_at, home_team_id,
            away_team_id, stadium_name, game_status, source, published_at,
            observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES (1, 'game-1', ?, ?, ?, 1, 2, 'Fixture', 'final', 'fixture', NULL,
                  ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (SEASON, WEEK, utc_timestamp(PRELOCK + timedelta(hours=1)), stamp, stamp, stamp),
    )
    connection.execute(
        """
        INSERT INTO slates(
            slate_id, external_slate_id, site, slate_type, season, week, name,
            starts_at, locks_at, source, published_at, observed_at, ingested_at,
            effective_at, valid_from, valid_to, source_version, run_id
        ) VALUES (1, 'slate-1', 'draftkings', 'classic', ?, ?, 'Main', ?, ?,
                  'fixture', NULL, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (SEASON, WEEK, utc_timestamp(PRELOCK), utc_timestamp(PRELOCK), stamp, stamp, stamp),
    )
    players = (
        (1, "Example Passer", "QB"),
        (2, "Sample Runner", "RB"),
        (3, "Example Receiver", "WR"),
    )
    for player_id, name, position in players:
        connection.execute(
            """
            INSERT INTO players(
                player_id, player_key, canonical_name, position, birth_date, source,
                published_at, observed_at, ingested_at, effective_at, valid_from,
                valid_to, source_version, run_id
            ) VALUES (?, ?, ?, ?, NULL, 'fixture', NULL, ?, ?, NULL, ?, NULL,
                      'fixture-v1', NULL)
            """,
            (player_id, f"player-{player_id}", name, position, stamp, stamp, stamp),
        )
        connection.execute(
            """
            INSERT INTO salaries(
                slate_id, player_id, game_id, team_id, opponent_team_id,
                site_player_id, roster_positions_json, salary, player_status,
                source_file_sha256, source, published_at, observed_at, ingested_at,
                effective_at, valid_from, valid_to, source_version, run_id
            ) VALUES (1, ?, 1, 1, 2, ?, ?, 5000, NULL, ?, 'draftkings', NULL,
                      ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
            """,
            (
                player_id,
                str(player_id),
                json.dumps([position]),
                "a" * 64,
                stamp,
                stamp,
                stamp,
            ),
        )
    if contest:
        connection.execute(
            """
            INSERT INTO contests(
                external_contest_id, site, slate_id, archetype, field_size,
                entry_limit, entry_fee_cents, total_prizes_cents, payout_curve_id,
                source, published_at, observed_at, ingested_at, effective_at,
                valid_from, valid_to, source_version, run_id
            ) VALUES (?, 'draftkings', 1, 'single_entry', 3, 1, 100, NULL, NULL,
                      'fixture', NULL, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
            """,
            (CONTEST_ID, stamp, stamp, stamp),
        )
    if decision:
        connection.execute(
            """
            INSERT INTO decision_snapshots(
                decision_snapshot_id, slate_id, decision_at, created_at,
                manifest_schema_version, manifest_hashes_json,
                manifest_hash_set_sha256, run_id, note
            ) VALUES ('decision-1', 1, ?, ?, '1.0', '[]', ?, NULL, NULL)
            """,
            (utc_timestamp(PRELOCK), utc_timestamp(PRELOCK), "b" * 64),
        )
    connection.commit()


def _run(
    config: Any,
    standings: Path,
    *,
    now: datetime = FIRST_RESULTS,
    dependencies: ResultsDependencies | None = None,
) -> Any:
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        return run_results(
            connection,
            config=config,
            season=SEASON,
            week=WEEK,
            site="dk",
            standings_files=[standings],
            artifact_directory=config.database.parent / "decisions",
            report_directory=config.database.parent / "reports",
            dependencies=dependencies or ResultsDependencies(),
            now=now,
        )


def test_lane_captures_ingests_idempotently_and_counts_labels(tmp_path: Path) -> None:
    config = _config(tmp_path)
    standings = _standings(tmp_path)
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed(connection)

    first = _run(config, standings)
    second = _run(config, standings, now=FIRST_RESULTS + timedelta(hours=1))

    assert first.ok and second.ok
    assert [step.step for step in first.steps] == list(RESULTS_STEPS)
    grade = first.step("results_grade")
    assert grade is not None and grade.status == "succeeded"
    assert grade.summary["grades_inserted"] == 0
    assert grade.summary["verdicts"] == {
        "correct": 0,
        "incorrect": 0,
        "indeterminate": 0,
        "ungradable": 0,
    }
    first_capture = first.step("results_capture")
    second_capture = second.step("results_capture")
    assert first_capture is not None and first_capture.summary["files_new"] == 1
    assert second_capture is not None and second_capture.summary["files_new"] == 0
    second_ingest = second.step("results_ingest")
    assert second_ingest is not None
    assert second_ingest.summary["ownership_rows_inserted"] == 0
    assert second_ingest.summary["result_rows_inserted"] == 0
    assert second_ingest.summary["duplicate_rows"] == 6
    assert second_ingest.summary["source_observed_at"] == [utc_timestamp(FIRST_RESULTS)]

    with connect_database(config.database) as connection:
        captures = list(config.snapshot_root.glob("2026/week_01/*/manifest.json"))
        ownership = connection.execute(
            "SELECT count(*), min(observed_at), max(observed_at) FROM actual_ownership"
        ).fetchone()
        status = collect_ops_status(
            connection, config=config, database=config.database, now=FIRST_RESULTS
        )
    assert len(captures) == 1
    assert tuple(ownership) == (3, utc_timestamp(FIRST_RESULTS), utc_timestamp(FIRST_RESULTS))
    assert status.labels.weeks_with_labels == 1
    assert status.grading is not None
    assert (status.grading.graded, status.grading.ungradable) == (0, 0)
    cohort = status.labels.by_week_and_archetype[0]
    assert (cohort.label_rows, cohort.distinct_contests) == (3, 1)
    payload = status_payload(status)
    assert payload["labels"]["weeks_with_labels"] == 1  # type: ignore[index]
    assert payload["grading"]["graded"] == 0  # type: ignore[index]
    assert [step["step"] for step in payload["results_steps"]] == list(  # type: ignore[index]
        RESULTS_STEPS
    )
    assert "RESULT LABELS" in render_status(status)


def test_missing_contest_is_recorded_with_the_operator_remedy(tmp_path: Path) -> None:
    config = _config(tmp_path)
    standings = _standings(tmp_path, name="contest-standings-not-added.csv")
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed(connection, contest=False)

    report = _run(config, standings)

    ingest = report.step("results_ingest")
    assert ingest is not None and ingest.status == "failed"
    assert "`na-contest add`" in str(ingest.error_text)
    baseline = report.step("results_report")
    assert baseline is not None and baseline.status == "skipped"


def test_schema_drift_records_the_header_row(tmp_path: Path) -> None:
    config = _config(tmp_path)
    standings = _standings(tmp_path)
    standings.write_text(
        "Rank,EntryId,EntryName,Points,Lineup,New Column\n"
        "1,e1,One,10,QB Example Passer,x\n\n"
        "Player,Roster Position,%Drafted,FPTS\nExample Passer,QB,100%,24\n",
        encoding="utf-8",
    )
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed(connection)

    report = _run(config, standings)

    ingest = report.step("results_ingest")
    assert ingest is not None and ingest.status == "failed"
    assert "header row: Rank,EntryId,EntryName,Points,Lineup,New Column" in str(ingest.error_text)


def test_replay_mismatch_records_both_hashes_and_suppresses_report(tmp_path: Path) -> None:
    config = _config(tmp_path)
    standings = _standings(tmp_path)
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed(connection, decision=True)

    def mismatch(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(
            report=SimpleNamespace(
                expected_output_sha256="c" * 64,
                actual_output_sha256="d" * 64,
                output_matches=False,
            )
        )

    report = _run(
        config,
        standings,
        dependencies=replace(ResultsDependencies(), replay_decision=mismatch),
    )

    replay = report.step("results_replay")
    assert replay is not None and replay.status == "failed"
    detail = replay.summary["replays"][0]  # type: ignore[index]
    assert detail["expected_output_sha256"] == "c" * 64
    assert detail["actual_output_sha256"] == "d" * 64
    baseline = report.step("results_report")
    assert baseline is not None and baseline.status == "skipped"


def test_newest_decision_writes_the_baseline_report_at_lane_cutoff(tmp_path: Path) -> None:
    config = _config(tmp_path)
    standings = _standings(tmp_path)
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed(connection, decision=True)

    def matching(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(
            report=SimpleNamespace(
                expected_output_sha256="e" * 64,
                actual_output_sha256="e" * 64,
                output_matches=True,
            )
        )

    seen: dict[str, object] = {}

    def build(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return SimpleNamespace(decision_snapshot_id=kwargs["decision_snapshot_id"])

    dependencies = replace(
        ResultsDependencies(),
        replay_decision=matching,
        build_baseline_report=build,
        render_baseline_report=lambda report: "BASELINE FIXTURE\n",
    )
    report = _run(config, standings, dependencies=dependencies)

    assert report.ok
    assert seen["decision_snapshot_id"] == "decision-1"
    assert seen["decision_at"] == PRELOCK
    assert seen["evaluation_as_of"] == FIRST_RESULTS
    assert report.report_path is not None and report.report_path.is_file()
    assert report.report_path.parent == tmp_path / "reports" / "2026" / "week_01"
    assert report.report_path.name.startswith("results-dk-")
    rendered = report.report_path.read_text(encoding="utf-8")
    assert rendered.startswith("BASELINE FIXTURE\n")
    assert "ENTRY RECEIPTS — REALIZED, NOT PROJECTED" in rendered


def test_cli_runs_the_results_lane_and_returns_step_exit_codes(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    config = _config(tmp_path)
    standings = _standings(tmp_path)
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed(connection)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return FIRST_RESULTS

    monkeypatch.setattr(results_module, "datetime", FrozenDateTime)
    exit_code = ops_main(
        [
            "--config",
            str(config.path),
            "results",
            "--season",
            str(SEASON),
            "--week",
            str(WEEK),
            "--site",
            "dk",
            "--artifact-directory",
            str(tmp_path / "decisions"),
            "--report-directory",
            str(tmp_path / "reports"),
            "--json",
            str(standings),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["ok"] is True
    assert [step["step"] for step in payload["steps"]] == list(RESULTS_STEPS)
