"""The slate lane (`na-ops slate`): step isolation, one cutoff, and the fail-closed gates."""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import NormalDist
from typing import Any

import pytest

from narrative_alpha.build import BuildResult
from narrative_alpha.contests import ManualContest, PayoutBand, add_contest
from narrative_alpha.ingest import (
    OwnershipParseResult,
    ParsedOwnership,
    ParsedProjection,
    ProjectionParseResult,
)
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.ops import (
    SlateDependencies,
    collect_ops_status,
    load_ops_config,
    render_status,
    run_slate,
    status_payload,
)
from narrative_alpha.ops.cli import main as ops_main
from narrative_alpha.quant import QuantileInterpretation, fit_player_distribution_with_diagnostics
from narrative_alpha.replay import read_frozen_decision
from narrative_alpha.simulation import EXPERIMENTAL_NOTICE, SimulationRunError, run_simulation
from narrative_alpha.snapshots import CaptureKind, capture_files
from narrative_alpha.snapshots.core import snapshot_week_path
from narrative_alpha.store import (
    PlayerDistributionSourceRef,
    apply_migrations,
    canonical_distribution_source_set,
    connect_database,
    distribution_source_set_sha256,
)

SEASON = 2026
WEEK = 1
CAPTURED_AT = datetime(2026, 9, 12, 22, 0, tzinfo=UTC)
DECISION_AT = datetime(2026, 9, 13, 16, 0, tzinfo=UTC)
KICKOFF = datetime(2026, 9, 13, 17, 0, tzinfo=UTC)
# The lane ingests as of now, so the decision instant is now: a cutoff before the
# ingest could not see what the ingest just wrote.
NOW = DECISION_AT

# Four real matchups, so the export's `AWAY@HOME` field and the crosswalk both behave as
# they will on a real Sunday.
GAMES = (("GB", "CHI"), ("DAL", "NYG"), ("BUF", "MIA"), ("KC", "DEN"))
ROSTER: tuple[tuple[str, str, str], ...] = tuple(
    [(f"Quarterback {index + 1}", GAMES[index % 4][index % 2], "QB") for index in range(3)]
    + [(f"Runner {index + 1}", GAMES[index % 4][index % 2], "RB") for index in range(6)]
    + [(f"Receiver {index + 1}", GAMES[index % 4][index % 2], "WR") for index in range(8)]
    + [(f"End {index + 1}", GAMES[index % 4][index % 2], "TE") for index in range(4)]
)
SALARIES = {"QB": 7000, "RB": 5600, "WR": 5200, "TE": 3800, "DST": 2800}


class FixtureVendor:
    """A registered vendor adapter, so the lane's registry path is exercised for real."""

    name = "fixture-vendor"

    def parse_projections(self, path: Path) -> ProjectionParseResult:
        rows = _rows(path)
        return ProjectionParseResult(
            rows_seen=len(rows),
            rows=tuple(
                ParsedProjection(
                    name_raw=row["name"],
                    team=row["team"],
                    position=row["position"],
                    external_player_id=row["player_id"],
                    projection_mean=float(row["mean"]),
                    projection_floor=None,
                    projection_ceiling=None,
                    ownership_projection=float(row["ownership"]),
                    source_version="fixture-csv-v1",
                )
                for row in rows
            ),
        )

    def parse_ownership(self, path: Path) -> OwnershipParseResult:
        rows = _rows(path)
        return OwnershipParseResult(
            rows_seen=len(rows),
            rows=tuple(
                ParsedOwnership(
                    name_raw=row["name"],
                    team=row["team"],
                    position=row["position"],
                    external_player_id=row["player_id"],
                    role=row.get("role", "classic"),
                    ownership=float(row["ownership"]),
                    source_version="fixture-csv-v1",
                )
                for row in rows
            ),
        )


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# --------------------------------------------------------------------------------------
# Fixtures: a real captured DraftKings export and a real captured vendor projection file.
# --------------------------------------------------------------------------------------


def _salary_csv() -> str:
    lines = [
        "Position,Name + ID,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame"
    ]
    for index, (name, team, position) in enumerate(ROSTER):
        away, home = next(game for game in GAMES if team in game)
        slot = f"{position}/FLEX" if position in {"RB", "WR", "TE"} else position
        site_id = 1000 + index
        lines.append(
            f"{position},{name} ({site_id}),{name},{site_id},{slot},"
            f"{SALARIES[position] + index * 25},"
            f"{away}@{home} 09/13/2026 01:00PM ET,{team},12.5"
        )
    for index, (away, home) in enumerate(GAMES[:3]):
        site_id = 2000 + index
        lines.append(
            f"DST,{home} Defense ({site_id}),{home} Defense,{site_id},DST,"
            f"{SALARIES['DST'] + index * 25},"
            f"{away}@{home} 09/13/2026 01:00PM ET,{home},7.1"
        )
    return "\n".join(lines) + "\n"


def _vendor_csv(*, unknown: bool = False) -> str:
    lines = ["name,player_id,team,position,mean,ownership"]
    for index, (name, team, position) in enumerate(ROSTER):
        lines.append(f"{name},{1000 + index},{team},{position},{20 - index * 0.3},0.10")
    for index, (name, team, position) in enumerate(DEFENCES):
        lines.append(f"{name},{2000 + index},{team},{position},7.0,0.08")
    if unknown:
        lines.append("Nobody At All,9999,GB,WR,11.1,0.04")
    return "\n".join(lines) + "\n"


def _showdown_roster() -> tuple[tuple[str, int, str, str], ...]:
    players = tuple(
        (name, 1000 + index, team, position)
        for index, (name, team, position) in enumerate(ROSTER)
        if team == "GB"
    )
    return (*players, ("CHI Defense", 2000, "CHI", "DST"))


def _showdown_salary_csv() -> str:
    lines = [
        "Position,Name + ID,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame"
    ]
    for index, (name, site_id, team, position) in enumerate(_showdown_roster()):
        lines.append(
            f"{position},{name} ({site_id}),{name},{site_id},CPT/FLEX,"
            f"{SALARIES[position] + index * 25},"
            f"GB@CHI 09/13/2026 01:00PM ET,{team},12.5"
        )
    return "\n".join(lines) + "\n"


def _showdown_projection_csv() -> str:
    lines = ["name,player_id,team,position,mean,ownership"]
    for index, (name, site_id, team, position) in enumerate(_showdown_roster()):
        lines.append(f"{name},{site_id},{team},{position},{20 - index * 0.3},0.10")
    return "\n".join(lines) + "\n"


def _showdown_ownership_csv() -> str:
    lines = ["name,player_id,team,position,mean,ownership,role"]
    roster = _showdown_roster()
    for role, ownership in (("captain", 1 / len(roster)), ("flex", 5 / len(roster))):
        for name, site_id, team, position in roster:
            lines.append(f"{name},{site_id},{team},{position},0,{ownership:.12f},{role}")
    return "\n".join(lines) + "\n"


def _capture(
    snapshots: Path,
    tmp_path: Path,
    *,
    kind: CaptureKind,
    source: str,
    filename: str,
    text: str,
    observed_at: datetime = CAPTURED_AT,
) -> Path:
    staged = tmp_path / "staged" / f"{observed_at:%Y%m%d%H%M%S%f}-{filename}"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(text, encoding="utf-8")
    return capture_files(snapshots, SEASON, WEEK, kind, source, [staged], observed_at=observed_at)


DEFENCES: tuple[tuple[str, str, str], ...] = tuple(
    (f"{home} Defense", home, "DST") for _, home in GAMES[:3]
)


def _seed_players(connection: sqlite3.Connection) -> None:
    """The nflverse roster the crosswalk resolves against.

    The defences are seeded under the same ``dst:<code>`` keys the salary loader uses, so
    the loader reuses them. They are seeded with a roster row on purpose: the projection
    loader has no team-defence path of its own, so a vendor's DST rows resolve only by
    name and team. Until a real vendor adapter lands (Slice 9) that path is untested
    against real files, and it is recorded as an open item rather than guessed at here.
    """

    stamp = utc_timestamp(CAPTURED_AT - timedelta(days=7))
    for name, team, position in DEFENCES:
        cursor = connection.execute(
            """
            INSERT INTO players(
                player_key, canonical_name, position, birth_date, source, published_at,
                observed_at, ingested_at, effective_at, valid_from, valid_to,
                source_version, run_id
            ) VALUES (?, ?, ?, NULL, 'fixture', NULL, ?, ?, NULL, ?, NULL,
                      'fixture-v1', NULL)
            """,
            (f"dst:{team}", name, position, stamp, stamp, stamp),
        )
        assert cursor.lastrowid is not None
        connection.execute(
            """
            INSERT INTO player_team_history(
                player_id, team, position, roster_status, season, week, source,
                published_at, observed_at, ingested_at, effective_at, valid_from,
                valid_to, source_version, run_id
            ) VALUES (?, ?, ?, 'ACT', ?, ?, 'fixture', NULL, ?, ?, NULL, ?, NULL,
                      'fixture-v1', NULL)
            """,
            (int(cursor.lastrowid), team, position, SEASON, WEEK, stamp, stamp, stamp),
        )
    for index, (name, team, position) in enumerate(ROSTER):
        cursor = connection.execute(
            """
            INSERT INTO players(
                player_key, canonical_name, position, birth_date, source, published_at,
                observed_at, ingested_at, effective_at, valid_from, valid_to,
                source_version, run_id
            ) VALUES (?, ?, ?, NULL, 'fixture', NULL, ?, ?, NULL, ?, NULL,
                      'fixture-v1', NULL)
            """,
            (f"player-{index}", name, position, stamp, stamp, stamp),
        )
        assert cursor.lastrowid is not None
        connection.execute(
            """
            INSERT INTO player_team_history(
                player_id, team, position, roster_status, season, week, source,
                published_at, observed_at, ingested_at, effective_at, valid_from,
                valid_to, source_version, run_id
            ) VALUES (?, ?, ?, 'ACT', ?, ?, 'fixture', NULL, ?, ?, NULL, ?, NULL,
                      'fixture-v1', NULL)
            """,
            (int(cursor.lastrowid), team, position, SEASON, WEEK, stamp, stamp, stamp),
        )


def _write_config(tmp_path: Path) -> Path:
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
    return path


@pytest.fixture
def week(tmp_path: Path) -> Any:
    """A captured week and a seeded store: exactly what a Saturday leaves behind."""

    config = load_ops_config(_write_config(tmp_path))
    _capture(
        config.snapshot_root,
        tmp_path,
        kind=CaptureKind.SALARIES,
        source="draftkings",
        filename="DKSalaries.csv",
        text=_salary_csv(),
    )
    _capture(
        config.snapshot_root,
        tmp_path,
        kind=CaptureKind.PROJECTIONS,
        source="fixture-vendor",
        filename="projections.csv",
        text=_vendor_csv(),
        observed_at=CAPTURED_AT + timedelta(minutes=1),
    )
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed_players(connection)
    return config


def _run(
    config: Any,
    *,
    tmp_path: Path,
    dependencies: SlateDependencies | None = None,
    **overrides: Any,
) -> Any:
    arguments: dict[str, Any] = {
        "season": SEASON,
        "week": WEEK,
        "site": "dk",
        "decision_at": DECISION_AT,
        "number_of_lineups": 1,
        "artifact_directory": tmp_path / "decisions",
        "report_directory": tmp_path / "reports",
        "dependencies": dependencies or SlateDependencies(source_formats=(FixtureVendor(),)),
        "now": NOW,
    }
    arguments |= overrides
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        return run_slate(connection, config=config, database=config.database, **arguments)


# --------------------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------------------


def test_lane_goes_from_captures_to_upload_csv_and_memo(week: Any, tmp_path: Path) -> None:
    report = _run(week, tmp_path=tmp_path)

    assert report.ok, [step.error_text for step in report.steps if not step.ok]
    assert [step.step for step in report.steps] == [
        "slate_salaries",
        "slate_projections",
        "slate_episodes",
        "slate_features",
        "slate_build",
        "slate_memo",
    ]
    assert report.slate_id is not None
    assert report.decision_snapshot_id is not None
    assert report.upload_csv_path is not None and report.upload_csv_path.is_file()
    assert report.memo_path is not None and report.memo_path.is_file()
    assert "SLATE DECISION MEMO" in report.memo_path.read_text(encoding="utf-8")
    assert report.replay_command is not None
    assert report.decision_snapshot_id in report.replay_command
    # The upload CSV is the frozen artifact, not a second rendering of it.
    assert report.upload_csv_path.name == "generated_lineups.csv"

    with connect_database(week.database) as connection:
        recorded = {
            str(row["step"]): (str(row["status"]), json.loads(str(row["summary_json"])))
            for row in connection.execute("SELECT step, status, summary_json FROM ops_runs")
        }
        snapshots = connection.execute(
            "SELECT decision_snapshot_id, decision_at FROM decision_snapshots"
        ).fetchall()

    assert set(recorded) == {
        "slate_salaries",
        "slate_projections",
        "slate_episodes",
        "slate_features",
        "slate_build",
        "slate_memo",
    }
    assert all(status == "succeeded" for status, _ in recorded.values())
    # decision_at is written into *every* step's summary, so the run replays from one cutoff.
    assert {summary["decision_at"] for _, summary in recorded.values()} == {
        utc_timestamp(DECISION_AT)
    }
    assert len(snapshots) == 1
    assert str(snapshots[0]["decision_at"]) == utc_timestamp(DECISION_AT)


def test_lane_builds_showdown_without_new_operator_flags(tmp_path: Path) -> None:
    config = load_ops_config(_write_config(tmp_path))
    _capture(
        config.snapshot_root,
        tmp_path,
        kind=CaptureKind.SALARIES,
        source="draftkings",
        filename="DKSalariesShowdown.csv",
        text=_showdown_salary_csv(),
    )
    _capture(
        config.snapshot_root,
        tmp_path,
        kind=CaptureKind.PROJECTIONS,
        source="fixture-vendor",
        filename="showdown-projections.csv",
        text=_showdown_projection_csv(),
        observed_at=CAPTURED_AT + timedelta(minutes=1),
    )
    _capture(
        config.snapshot_root,
        tmp_path,
        kind=CaptureKind.OWNERSHIP,
        source="fixture-vendor",
        filename="showdown-ownership.csv",
        text=_showdown_ownership_csv(),
        observed_at=CAPTURED_AT + timedelta(minutes=2),
    )
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed_players(connection)

    report = _run(config, tmp_path=tmp_path)

    assert report.ok, [step.error_text for step in report.steps if not step.ok]
    assert report.upload_csv_path is not None
    assert report.upload_csv_path.read_text(encoding="utf-8").startswith("CPT,FLEX")
    assert report.memo_path is not None
    memo = report.memo_path.read_text(encoding="utf-8")
    assert "slate_type=showdown" in memo
    assert "contest_archetype=showdown" in memo
    assert "CAPTAIN CHOICES\n" in memo


def test_optional_simulation_step_skips_when_distributions_are_absent(
    week: Any, tmp_path: Path
) -> None:
    report = _run(week, tmp_path=tmp_path, simulate=True)

    step = report.step("slate_simulate")
    assert step is not None
    assert step.status == "skipped"
    assert "no player_distributions" in str(step.error_text)
    assert report.simulation_path is None


def test_simulation_run_is_byte_stable_and_appends_its_provenance(
    week: Any, tmp_path: Path
) -> None:
    lane = _run(week, tmp_path=tmp_path)
    assert lane.decision_snapshot_id is not None
    with connect_database(week.database) as connection:
        apply_migrations(connection)
        frozen = read_frozen_decision(
            connection,
            decision_snapshot_id=lane.decision_snapshot_id,
            decision_at=DECISION_AT,
            artifact_root=tmp_path / "decisions",
        )
        _seed_simulation_inputs(connection, frozen.request.candidate_player_scenario.players)
        contest = add_contest(
            connection,
            ManualContest(
                external_contest_id="simulation-contest",
                site=frozen.request.site,
                slate_id=frozen.request.slate_id,
                archetype=frozen.request.contest_archetype,
                field_size=501,
                entry_limit=1,
                entry_fee_cents=1_000,
                payout_curve_id="simulation-curve",
                observed_at=DECISION_AT,
            ),
            (
                PayoutBand(rank_from=1, rank_to=1, prize_cents=10_000),
                PayoutBand(rank_from=2, rank_to=501, prize_cents=0),
            ),
            ingested_at=DECISION_AT,
        ).contest
        first = run_simulation(
            connection,
            decision_snapshot_id=lane.decision_snapshot_id,
            contest_external_id=contest.external_contest_id,
            artifact_root=tmp_path / "decisions",
            report_directory=tmp_path / "reports",
            draws=20,
            seed=42,
            run_at=DECISION_AT + timedelta(days=1),
        )
        second = run_simulation(
            connection,
            decision_snapshot_id=lane.decision_snapshot_id,
            contest_external_id=contest.external_contest_id,
            artifact_root=tmp_path / "decisions",
            report_directory=tmp_path / "reports",
            draws=20,
            seed=42,
            run_at=DECISION_AT + timedelta(days=1, seconds=1),
        )
        rows = connection.execute(
            "SELECT config_sha256, draw_count, seed, ownership_source FROM simulation_runs "
            "ORDER BY simulation_run_id"
        ).fetchall()

    assert first.report_bytes == second.report_bytes
    assert first.report_path != second.report_path
    assert first.report_path.read_bytes() == first.report_bytes
    assert first.report_bytes.startswith((EXPERIMENTAL_NOTICE + "\n").encode())
    assert len(rows) == 2
    assert rows[0]["config_sha256"] == first.report.config_sha256
    assert tuple(rows[0][key] for key in ("draw_count", "seed", "ownership_source")) == (
        20,
        42,
        "vendor_baseline",
    )


def test_a_simulator_error_skips_the_shadow_step_and_the_lane_still_succeeds(
    week: Any, tmp_path: Path
) -> None:
    """The simulator is experimental: an unlucky field or a bad store is a stated gap on
    the shadow step, never "one or more steps FAILED" on a Sunday."""

    lane = _run(week, tmp_path=tmp_path)
    assert lane.decision_snapshot_id is not None
    with connect_database(week.database) as connection:
        apply_migrations(connection)
        frozen = read_frozen_decision(
            connection,
            decision_snapshot_id=lane.decision_snapshot_id,
            decision_at=DECISION_AT,
            artifact_root=tmp_path / "decisions",
        )
        _seed_simulation_inputs(connection, frozen.request.candidate_player_scenario.players)
        add_contest(
            connection,
            ManualContest(
                external_contest_id="simulation-contest",
                site=frozen.request.site,
                slate_id=frozen.request.slate_id,
                archetype=frozen.request.contest_archetype,
                field_size=501,
                entry_limit=1,
                entry_fee_cents=1_000,
                payout_curve_id="simulation-curve",
                observed_at=DECISION_AT,
            ),
            (
                PayoutBand(rank_from=1, rank_to=1, prize_cents=10_000),
                PayoutBand(rank_from=2, rank_to=501, prize_cents=0),
            ),
            ingested_at=DECISION_AT,
        )
        connection.commit()

    def unlucky_field(*args: Any, **kwargs: Any) -> Any:
        raise SimulationRunError("could not generate a legal field lineup after 300 attempts")

    # A later instant: the seeded inputs make this a new decision, not a rebuild of the
    # first one, so the only thing under test is the shadow step's behaviour.
    report = _run(
        week,
        tmp_path=tmp_path,
        simulate=True,
        decision_at=DECISION_AT + timedelta(minutes=5),
        dependencies=SlateDependencies(
            source_formats=(FixtureVendor(),), run_simulation=unlucky_field
        ),
    )

    step = report.step("slate_simulate")
    assert step is not None
    assert step.status == "skipped"
    assert "shadow simulation did not run" in str(step.error_text)
    assert "300 attempts" in str(step.error_text)
    assert report.ok, [step.error_text for step in report.steps if not step.ok]


def _seed_simulation_inputs(connection: sqlite3.Connection, players: tuple[Any, ...]) -> None:
    stamp = utc_timestamp(DECISION_AT)
    normal = NormalDist()
    position_counts: dict[str, int] = {}
    for player in players:
        position_counts[player.position] = position_counts.get(player.position, 0) + 1
    position_totals = {"QB": 1.0, "RB": 2.4, "WR": 3.5, "TE": 1.1, "DST": 1.0}
    for player in players:
        projection = connection.execute(
            """
            SELECT * FROM projection_snapshots
            WHERE slate_id = ? AND player_id = ?
            ORDER BY projection_snapshot_id DESC LIMIT 1
            """,
            (1, player.player_id),
        ).fetchone()
        assert projection is not None
        source = str(projection["source"])
        reference = PlayerDistributionSourceRef(
            projection_snapshot_id=int(projection["projection_snapshot_id"]),
            source=source,
            source_file_sha256=str(projection["source_file_sha256"]),
        )
        source_set = (reference,)
        mean = float(player.projection)
        shape = 0.35
        scale = mean * math.exp(-0.5 * shape**2)
        floor = scale * math.exp(shape * normal.inv_cdf(0.25))
        ceiling = scale * math.exp(shape * normal.inv_cdf(0.75))
        fit = fit_player_distribution_with_diagnostics(
            source=source,
            position=player.position,
            mean=mean,
            floor=floor,
            ceiling=ceiling,
            p_active=1,
            p_full_role_given_active=1,
            quantile_configuration={(source, player.position): QuantileInterpretation(0.25, 0.75)},
        )
        distribution = fit.distribution
        connection.execute(
            """
            INSERT INTO player_distributions(
                slate_id, player_id, position, source_set_json, source_set_sha256,
                as_of_at, distribution_family, p_active, p_full_role_given_active,
                conditional_location, conditional_scale, conditional_shape, input_mean,
                input_floor, input_ceiling, floor_quantile, ceiling_quantile,
                fit_tolerance, fit_max_relative_error, fit_config_sha256, fitter_version,
                source, published_at, observed_at, ingested_at, effective_at, valid_from,
                valid_to, source_version, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      NULL, ?, ?, NULL, ?, NULL, ?, NULL)
            """,
            (
                1,
                player.player_id,
                player.position,
                canonical_distribution_source_set(source_set),
                distribution_source_set_sha256(source_set),
                stamp,
                distribution.distribution_family,
                distribution.p_active,
                distribution.p_full_role_given_active,
                distribution.conditional_location,
                distribution.conditional_scale,
                distribution.conditional_shape,
                fit.input_mean,
                fit.input_floor,
                fit.input_ceiling,
                fit.floor_quantile,
                fit.ceiling_quantile,
                fit.fit_tolerance,
                fit.fit_max_relative_error,
                fit.fit_config_sha256,
                fit.fitter_version,
                source,
                stamp,
                stamp,
                stamp,
                fit.fitter_version,
            ),
        )
        ownership = position_totals[player.position] / position_counts[player.position]
        connection.execute(
            """
            INSERT INTO ownership_baselines(
                slate_id, player_id, site, role, ownership, source_file_sha256,
                source, published_at, observed_at, ingested_at, effective_at,
                valid_from, valid_to, source_version, run_id
            ) VALUES (1, ?, 'draftkings', 'classic', ?, ?, 'fixture-vendor', NULL,
                      ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
            """,
            (player.player_id, ownership, "c" * 64, stamp, stamp, stamp),
        )


def test_one_decision_instant_reaches_every_stage(week: Any, tmp_path: Path) -> None:
    seen: dict[str, datetime] = {}
    base = SlateDependencies(source_formats=(FixtureVendor(),))

    def episodes(connection: sqlite3.Connection, **kwargs: Any) -> Any:
        seen["episodes"] = kwargs["as_of"]
        return base.build_episodes(connection, **kwargs)

    def features(connection: sqlite3.Connection, **kwargs: Any) -> Any:
        seen["features"] = kwargs["as_of"]
        return base.build_features(connection, **kwargs)

    def build(database: Path, **kwargs: Any) -> BuildResult:
        seen["build"] = kwargs["decision_at"]
        return base.build_decision(database, **kwargs)

    report = _run(
        week,
        tmp_path=tmp_path,
        dependencies=replace(
            base,
            build_episodes=episodes,
            build_features=features,
            build_decision=build,
        ),
    )

    assert report.ok, [step.error_text for step in report.steps if not step.ok]
    assert seen == {
        "episodes": DECISION_AT,
        "features": DECISION_AT,
        "build": DECISION_AT,
    }


def test_second_run_at_the_same_instant_writes_nothing_new(week: Any, tmp_path: Path) -> None:
    first = _run(week, tmp_path=tmp_path)
    assert first.ok, [step.error_text for step in first.steps if not step.ok]

    with connect_database(week.database) as connection:
        before = _snapshot_counts(connection)

    second = _run(week, tmp_path=tmp_path)

    with connect_database(week.database) as connection:
        after = _snapshot_counts(connection)

    assert second.ok, [step.error_text for step in second.steps if not step.ok]
    assert after == before
    assert second.decision_snapshot_id == first.decision_snapshot_id
    for name in ("slate_features", "slate_build"):
        step = second.step(name)
        assert step is not None
        assert step.summary["reused_existing"] is True
    # No Stage 1 claim exists in this fixture, so the episode build is empty rather than
    # reused — but it still persists nothing on either run.
    episodes = second.step("slate_episodes")
    assert episodes is not None
    assert (episodes.summary["episodes_inserted"], episodes.summary["episode_count"]) == (0, 0)
    salaries = second.step("slate_salaries")
    assert salaries is not None and salaries.summary["salary_rows_inserted"] == 0
    vendors = second.step("slate_projections")
    assert vendors is not None and vendors.summary["projection_rows_inserted"] == 0


def _snapshot_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        for table in (
            "salaries",
            "projection_snapshots",
            "narrative_episodes",
            "narrative_features",
            "decision_snapshots",
        )
    }


# --------------------------------------------------------------------------------------
# Fail closed
# --------------------------------------------------------------------------------------


def test_missing_projection_capture_stops_the_build_and_names_the_kind(
    tmp_path: Path,
) -> None:
    config = load_ops_config(_write_config(tmp_path))
    _capture(
        config.snapshot_root,
        tmp_path,
        kind=CaptureKind.SALARIES,
        source="draftkings",
        filename="DKSalaries.csv",
        text=_salary_csv(),
    )
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed_players(connection)

    report = _run(config, tmp_path=tmp_path)

    assert not report.ok
    salaries = report.step("slate_salaries")
    assert salaries is not None and salaries.status == "succeeded"
    vendors = report.step("slate_projections")
    assert vendors is not None and vendors.status == "skipped"
    assert "manifests a projections or ownership file" in str(vendors.error_text)
    build = report.step("slate_build")
    assert build is not None and build.status == "failed"
    assert "projections: NOT CAPTURED for this week" in str(build.error_text)
    assert "ownership: NOT CAPTURED for this week" in str(build.error_text)
    assert build.summary["projection_rows_available"] == 0
    memo = report.step("slate_memo")
    assert memo is not None and memo.status == "skipped"
    assert report.upload_csv_path is None
    # Earlier steps still recorded, and episodes are slate-independent so they still ran.
    episodes = report.step("slate_episodes")
    assert episodes is not None and episodes.status == "succeeded"


def test_unresolved_players_stop_the_build_with_the_resolve_command(
    tmp_path: Path,
) -> None:
    config = load_ops_config(_write_config(tmp_path))
    _capture(
        config.snapshot_root,
        tmp_path,
        kind=CaptureKind.SALARIES,
        source="draftkings",
        filename="DKSalaries.csv",
        text=_salary_csv(),
    )
    _capture(
        config.snapshot_root,
        tmp_path,
        kind=CaptureKind.PROJECTIONS,
        source="fixture-vendor",
        filename="projections.csv",
        text=_vendor_csv(unknown=True),
        observed_at=CAPTURED_AT + timedelta(minutes=1),
    )
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed_players(connection)

    report = _run(config, tmp_path=tmp_path)

    assert not report.ok
    vendors = report.step("slate_projections")
    assert vendors is not None and vendors.status == "failed"
    assert vendors.summary["unresolved_rows"] == 1
    build = report.step("slate_build")
    assert build is not None and build.status == "failed"
    assert build.summary["unresolved_identities"] == 1
    assert "na-crosswalk resolve --unresolved-id" in str(build.error_text)
    assert "--player-id <player_id>" in str(build.error_text)
    with connect_database(config.database) as connection:
        assert connection.execute("SELECT count(*) FROM decision_snapshots").fetchone()[0] == 0


def test_vendor_without_an_adapter_is_named_and_the_lane_continues(
    week: Any, tmp_path: Path
) -> None:
    _capture(
        week.snapshot_root,
        tmp_path,
        kind=CaptureKind.OWNERSHIP,
        source="stokastic",
        filename="ownership.csv",
        text=_vendor_csv(),
        observed_at=CAPTURED_AT + timedelta(minutes=2),
    )

    report = _run(week, tmp_path=tmp_path)

    vendors = report.step("slate_projections")
    assert vendors is not None and vendors.status == "failed"
    assert vendors.summary["missing_adapter_vendors"] == ["stokastic"]
    assert "no SourceFormat adapter is registered for vendor(s) stokastic" in str(
        vendors.error_text
    )
    # Nothing was guessed, and the registered vendor's capture still loaded.
    assert vendors.summary["captures_loaded"] == 1
    assert int(vendors.summary["projection_rows_inserted"]) > 0
    # The lane continues: the decision is still built and the memo still written.
    build = report.step("slate_build")
    assert build is not None and build.status == "succeeded"
    assert report.memo_path is not None and report.memo_path.is_file()
    with connect_database(week.database) as connection:
        assert connection.execute("SELECT count(*) FROM ownership_baselines").fetchone()[0] == 0


def test_two_slates_for_the_site_refuse_to_guess_which_one_to_play(tmp_path: Path) -> None:
    """One Saturday capture can hold the classic and the showdown export together."""

    config = load_ops_config(_write_config(tmp_path))
    showdown = (
        "Position,Name + ID,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,"
        "AvgPointsPerGame\n"
        "QB,Quarterback 1 (1000),Quarterback 1,1000,CPT,10800,"
        "GB@CHI 09/13/2026 05:00PM ET,GB,12.5\n"
    )
    staged = tmp_path / "staged"
    staged.mkdir(parents=True, exist_ok=True)
    classic_path = staged / "DKSalaries.csv"
    classic_path.write_text(_salary_csv(), encoding="utf-8")
    showdown_path = staged / "DKSalariesShowdown.csv"
    showdown_path.write_text(showdown, encoding="utf-8")
    capture_files(
        config.snapshot_root,
        SEASON,
        WEEK,
        CaptureKind.SALARIES,
        "draftkings",
        [classic_path, showdown_path],
        observed_at=CAPTURED_AT,
    )
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed_players(connection)

    report = _run(config, tmp_path=tmp_path)

    for name in ("slate_projections", "slate_features", "slate_build", "slate_memo"):
        step = report.step(name)
        assert step is not None and step.status == "skipped"
        assert "rerun with `--slate-id`" in str(step.error_text)
    episodes = report.step("slate_episodes")
    assert episodes is not None and episodes.status == "succeeded"
    assert report.slate_id is None

    # Naming the slate unblocks exactly the steps that needed it.
    salaries = report.step("slate_salaries")
    assert salaries is not None
    chosen = min(int(value) for value in salaries.summary["slate_ids"])  # type: ignore[call-overload]
    named = _run(config, tmp_path=tmp_path, slate_id=chosen)
    assert named.slate_id == chosen
    assert not any("--slate-id" in str(step.error_text) for step in named.steps)
    features = named.step("slate_features")
    assert features is not None and features.status == "succeeded"


def test_a_step_that_fails_is_isolated_and_the_next_safe_step_still_runs(
    week: Any, tmp_path: Path
) -> None:
    base = SlateDependencies(source_formats=(FixtureVendor(),))

    def exploding_episodes(connection: sqlite3.Connection, **kwargs: Any) -> Any:
        raise ValueError("stage 2 refused this cutoff")

    report = _run(
        week,
        tmp_path=tmp_path,
        dependencies=replace(base, build_episodes=exploding_episodes),
    )

    episodes = report.step("slate_episodes")
    assert episodes is not None and episodes.status == "failed"
    assert "stage 2 refused this cutoff" in str(episodes.error_text)
    # The lane keeps going, and the failed step still carries the run-wide facts.
    assert episodes.summary["decision_at"] == utc_timestamp(DECISION_AT)
    assert episodes.summary["week"] == WEEK
    build = report.step("slate_build")
    assert build is not None and build.status == "succeeded"
    assert not report.ok
    with connect_database(week.database) as connection:
        statuses = dict(
            (str(row["step"]), str(row["status"]))
            for row in connection.execute("SELECT step, status FROM ops_runs")
        )
    assert statuses["slate_episodes"] == "failed"
    assert statuses["slate_build"] == "succeeded"


# --------------------------------------------------------------------------------------
# Status and CLI
# --------------------------------------------------------------------------------------


def test_status_renders_the_slate_section_on_an_empty_store(tmp_path: Path) -> None:
    config = load_ops_config(_write_config(tmp_path))
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        status = collect_ops_status(connection, config=config, database=config.database, now=NOW)
    rendered = render_status(status)
    payload = status_payload(status)

    assert "SLATE LANE (`na-ops slate`)" in rendered
    assert "slate_salaries     last success never" in rendered
    assert "no snapshot week is initialized, so no slate week can be shown" in rendered
    assert status.slate is None
    assert payload["slate"] is None
    assert [step["step"] for step in payload["slate_steps"]] == [  # type: ignore[index]
        "slate_salaries",
        "slate_projections",
        "slate_episodes",
        "slate_features",
        "slate_build",
        "slate_memo",
        "slate_simulate",
    ]


def test_status_shows_captures_ingested_features_and_the_decision(
    week: Any, tmp_path: Path
) -> None:
    report = _run(week, tmp_path=tmp_path)
    assert report.ok, [step.error_text for step in report.steps if not step.ok]

    with connect_database(week.database) as connection:
        status = collect_ops_status(connection, config=week, database=week.database, now=NOW)
    rendered = render_status(status)
    payload = status_payload(status)

    assert status.slate is not None
    captures = {capture.kind: capture for capture in status.slate.captures}
    assert (captures["salaries"].files_captured, captures["salaries"].files_ingested) == (1, 1)
    assert (
        captures["projections"].files_captured,
        captures["projections"].files_ingested,
    ) == (1, 1)
    assert captures["ownership"].files_captured == 0
    assert status.slate.decision_at == DECISION_AT
    slate = status.slate.slates[0]
    assert slate.slate_id == report.slate_id
    assert slate.decision_snapshot_id == report.decision_snapshot_id
    assert slate.contest_policy_version == "contest-policy-v2"
    assert slate.feature_rows_at_decision > 0
    assert slate.unresolved_count == 0
    assert f"{SEASON} week {WEEK:02d}" in rendered
    assert "1 of 1 file(s) ingested" in rendered
    assert "none captured" in rendered
    assert str(report.decision_snapshot_id) in rendered
    assert "policy contest-policy-v2" in rendered
    assert payload["slate"] is not None
    assert payload["slate"]["slates"][0]["contest_policy_version"] == "contest-policy-v2"  # type: ignore[index]


def test_cli_runs_the_lane_and_prints_the_paths(
    week: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = ops_main(
        [
            "--config",
            str(tmp_path / "ops.toml"),
            "slate",
            "--season",
            str(SEASON),
            "--week",
            str(WEEK),
            "--site",
            "dk",
            "--decision-at",
            utc_timestamp(DECISION_AT),
            "--artifact-directory",
            str(tmp_path / "decisions"),
            "--report-directory",
            str(tmp_path / "reports"),
            "--json",
        ],
        slate_dependencies=SlateDependencies(source_formats=(FixtureVendor(),)),
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["decision_at"] == utc_timestamp(DECISION_AT)
    assert Path(payload["upload_csv"]).is_file()
    assert Path(payload["memo"]).is_file()
    assert payload["replay_command"].startswith("na-replay --database ")
    assert [step["step"] for step in payload["steps"]] == [
        "slate_salaries",
        "slate_projections",
        "slate_episodes",
        "slate_features",
        "slate_build",
        "slate_memo",
    ]


def test_cli_exits_nonzero_when_a_step_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = load_ops_config(_write_config(tmp_path))
    _capture(
        config.snapshot_root,
        tmp_path,
        kind=CaptureKind.SALARIES,
        source="draftkings",
        filename="DKSalaries.csv",
        text=_salary_csv(),
    )
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed_players(connection)

    exit_code = ops_main(
        [
            "--config",
            str(tmp_path / "ops.toml"),
            "slate",
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
        ],
        slate_dependencies=SlateDependencies(source_formats=(FixtureVendor(),)),
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "one or more steps FAILED" in output
    assert "none — the step that produces it did not succeed" in output


def test_a_cutoff_before_the_run_is_refused_before_anything_is_recorded(
    week: Any, tmp_path: Path
) -> None:
    """The lane ingests as of now; a past cutoff could not see what it just loaded."""

    with pytest.raises(ValueError, match="before this run began"):
        _run(week, tmp_path=tmp_path, decision_at=DECISION_AT - timedelta(hours=1))

    with connect_database(week.database) as connection:
        assert connection.execute("SELECT count(*) FROM ops_runs").fetchone()[0] == 0


def test_no_salary_capture_is_a_recorded_refusal_not_a_crash(tmp_path: Path) -> None:
    config = load_ops_config(_write_config(tmp_path))
    snapshot_week_path(config.snapshot_root, SEASON, WEEK).mkdir(parents=True)

    report = _run(config, tmp_path=tmp_path)

    salaries = report.step("slate_salaries")
    assert salaries is not None and salaries.status == "failed"
    assert "na-snapshot capture --kind salaries" in str(salaries.error_text)
    assert salaries.summary["decision_at"] == utc_timestamp(DECISION_AT)
    assert not report.ok
