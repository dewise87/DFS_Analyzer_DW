"""Slate input readiness: the counts, the thresholds, and the build guard they drive.

Every fixture here is synthetic and every store is a temporary file. Nothing touches the
operator's database, and no vendor export is needed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_build import (
    DATA_AT,
    DECISION_AT,
    ODDS_HASH,
    _insert,
    _pit,
    _players,
    _seed_candidate_pool,
)

from narrative_alpha.build import BuildInputError, BuildReadinessError, build_decision
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.interface import build_slate_memo, render_slate_memo
from narrative_alpha.ops.status import collect_ops_status, render_status, status_payload
from narrative_alpha.portfolio import DfsSite, PydfsAdapter
from narrative_alpha.readiness import (
    ODDS_COVERAGE,
    OWNERSHIP_COVERAGE,
    PROJECTION_AGE,
    PROJECTION_COVERAGE,
    WEATHER_COVERAGE,
    ReadinessConfig,
    ReadinessConfigError,
    collect_slate_readiness,
    load_readiness_config,
    load_readiness_config_bytes,
    readiness_payload,
    render_readiness,
)
from narrative_alpha.replay import ReplayArtifactError, replay_decision
from narrative_alpha.report_cli import load_build_result
from narrative_alpha.store import DecisionSnapshotRow, apply_migrations, connect_database

SHIPPED = load_readiness_config()


def _database(tmp_path: Path, name: str = "readiness.sqlite3") -> Path:
    database = tmp_path / name
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed_candidate_pool(connection, _players())
    return database


def _read(database: Path, *, as_of: datetime = DECISION_AT, config: Any = None) -> Any:
    with connect_database(database) as connection:
        return collect_slate_readiness(
            connection, slate_id=1, as_of=as_of, config=config or SHIPPED
        )


def _check(readiness: Any, name: str) -> Any:
    return next(check for check in readiness.checks if check.name == name)


def _tuned(**changes: Any) -> ReadinessConfig:
    """The shipped thresholds with one value moved, so a test states what it is testing."""

    values = {
        "config_version": SHIPPED.config_version,
        "config_sha256": SHIPPED.config_sha256,
        "minimum_projection_coverage": SHIPPED.minimum_projection_coverage,
        "minimum_ownership_coverage": SHIPPED.minimum_ownership_coverage,
        "maximum_projection_age_minutes": SHIPPED.maximum_projection_age_minutes,
        "maximum_projection_age_minutes_showdown": (
            SHIPPED.maximum_projection_age_minutes_showdown
        ),
        "odds_required": SHIPPED.odds_required,
        "weather_required": SHIPPED.weather_required,
        "weather_outdoor_only": SHIPPED.weather_outdoor_only,
        "raw_bytes": SHIPPED.raw_bytes,
    }
    return ReadinessConfig(**{**values, **changes})


def _strict_market(tmp_path: Path) -> tuple[Path, ReadinessConfig]:
    """The shipped file with odds and weather required, so a game-level miss can be provoked.

    Nothing ingests odds or weather yet, so the shipped defaults do not require them; these
    tests exercise the required path on a written file so the frozen artifact's hash stays
    honest about the bytes it was judged by.
    """

    text = (
        SHIPPED.raw_bytes.decode("utf-8")
        .replace("odds_required = false", "odds_required = true")
        .replace("weather_required = false", "weather_required = true")
    )
    path = tmp_path / "readiness-strict.toml"
    path.write_text(text, encoding="utf-8")
    return path, load_readiness_config(path)


# --------------------------------------------------------------------------------------
# What the pool is
# --------------------------------------------------------------------------------------


def test_a_complete_slate_is_ready_and_names_every_passing_threshold(tmp_path: Path) -> None:
    readiness = _read(_database(tmp_path))

    assert readiness.ready
    assert readiness.summary_line == "READY"
    assert readiness.active_players == 24
    assert readiness.projections.covered == 24
    assert readiness.projections.missing == 0
    assert readiness.projections.sources == ("fixture-projection",)
    assert readiness.odds.covered == 4 and readiness.odds.missing == 0
    assert readiness.weather.covered == 4 and readiness.weather.missing == 0
    # A met threshold is named too, with the number that met it.
    assert {check.name for check in readiness.checks} == {
        PROJECTION_COVERAGE,
        PROJECTION_AGE,
        OWNERSHIP_COVERAGE,
        ODDS_COVERAGE,
        WEATHER_COVERAGE,
    }
    assert all(check.passed for check in readiness.checks)
    assert _check(readiness, PROJECTION_COVERAGE).observed == "100.00% (24 of 24)"
    assert _check(readiness, PROJECTION_AGE).observed == "4h55m"


def test_coverage_excludes_inactive_and_officially_ruled_out_players(tmp_path: Path) -> None:
    """The denominator is the pool the build would use, so a dropped player is not a gap."""

    database = _database(tmp_path)
    with connect_database(database) as connection:
        connection.execute("UPDATE salaries SET player_status = 'OUT' WHERE player_id = 1")
        connection.execute("DELETE FROM projection_snapshots WHERE player_id IN (1, 2)")
        _insert(
            connection,
            "player_availability",
            {
                "availability_id": "availability-2",
                "slate_id": 1,
                "player_id": 2,
                "season": 2026,
                "week": 1,
                "site": "draftkings",
                "availability_status": "unavailable",
                "rule_id": "fixture-rule",
                "rules_version": "fixture-rules-v1",
                "source_file_sha256": "e" * 64,
                **_pit("fixture-availability"),
            },
        )

    readiness = _read(database)

    assert readiness.salaried_players == 24
    assert readiness.inactive_salary_players == 1
    assert readiness.ruled_out_players == 2
    assert readiness.active_players == 22
    # Neither missing projection counts against coverage: neither player can be a candidate.
    assert readiness.projections.covered == 22
    assert readiness.projections.missing == 0
    assert readiness.unprojected_players == ()
    assert _check(readiness, PROJECTION_COVERAGE).passed


def test_an_official_availability_decision_overrides_an_inactive_salary_label(
    tmp_path: Path,
) -> None:
    """`_candidate_from_rows` lets an official decision win in both directions; so does this."""

    database = _database(tmp_path)
    with connect_database(database) as connection:
        connection.execute("UPDATE salaries SET player_status = 'OUT' WHERE player_id = 1")
        _insert(
            connection,
            "player_availability",
            {
                "availability_id": "availability-1",
                "slate_id": 1,
                "player_id": 1,
                "season": 2026,
                "week": 1,
                "site": "draftkings",
                "availability_status": "available",
                "rule_id": "fixture-rule",
                "rules_version": "fixture-rules-v1",
                "source_file_sha256": "e" * 64,
                **_pit("fixture-availability"),
            },
        )

    readiness = _read(database)

    assert readiness.inactive_salary_players == 1
    assert readiness.ruled_out_players == 0
    assert readiness.active_players == 24


def test_the_players_the_build_would_drop_are_named_most_expensive_first(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with connect_database(database) as connection:
        connection.execute("DELETE FROM projection_snapshots WHERE player_id IN (1, 2, 3)")

    readiness = _read(database)

    assert readiness.projections.covered == 21
    assert readiness.projections.missing == 3
    assert readiness.unprojected_players_total == 3
    salaries = [line.salary for line in readiness.unprojected_players]
    assert salaries == sorted(salaries, reverse=True)
    # The three quarterbacks, dearest first: 6050, 6025, 6000.
    assert [line.player_id for line in readiness.unprojected_players] == [3, 2, 1]
    assert "QB Player 1" in render_readiness(readiness)


def test_a_projection_ingested_after_the_instant_does_not_count(tmp_path: Path) -> None:
    """Observation time alone never admits a row: the ingestion stamp bounds it too."""

    database = _database(tmp_path)
    late = utc_timestamp(DECISION_AT + timedelta(microseconds=1))
    with connect_database(database) as connection:
        connection.execute(
            "UPDATE projection_snapshots SET ingested_at = ? WHERE player_id <= 4", (late,)
        )

    readiness = _read(database)

    assert readiness.projections.covered == 20
    assert readiness.projections.missing == 4
    assert not _check(readiness, PROJECTION_COVERAGE).passed
    # One microsecond earlier and the same rows are in.
    at_the_boundary = _read(database, as_of=DECISION_AT + timedelta(microseconds=1))
    assert at_the_boundary.projections.covered == 24


def test_per_source_coverage_is_reported_source_by_source(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with connect_database(database) as connection:
        for player_id in (1, 2, 3):
            _insert(
                connection,
                "projection_snapshots",
                {
                    "projection_snapshot_id": 500 + player_id,
                    "slate_id": 1,
                    "player_id": player_id,
                    "site": "draftkings",
                    "projection_mean": 12.5,
                    "projection_floor": None,
                    "projection_ceiling": None,
                    "ownership_projection": None,
                    "source_file_sha256": "f" * 64,
                    **_pit("second-vendor", source_version="second-v1"),
                },
            )

    readiness = _read(database)

    by_source = {item.source: item for item in readiness.projections.by_source}
    assert by_source["fixture-projection"].covered == 24
    assert by_source["second-vendor"].covered == 3
    assert by_source["second-vendor"].missing == 21
    assert readiness.projections.covered == 24


# --------------------------------------------------------------------------------------
# Ownership: which of the two numbers each player would get
# --------------------------------------------------------------------------------------


def test_a_player_with_only_embedded_ownership_is_reported_as_embedded(
    tmp_path: Path,
) -> None:
    """The build's precedence rule mixes the two sources, so the report separates them."""

    database = _database(tmp_path)
    with connect_database(database) as connection:
        for player_id in range(1, 21):
            _insert(
                connection,
                "ownership_baselines",
                {
                    "slate_id": 1,
                    "player_id": player_id,
                    "site": "draftkings",
                    "role": "classic",
                    "ownership": 0.11,
                    "source_file_sha256": "c" * 64,
                    **_pit("dedicated-ownership"),
                },
            )

    readiness = _read(database)
    classic = readiness.ownership[0]

    assert classic.role == "classic"
    assert classic.embedded_is_fallback
    assert classic.dedicated == 20
    assert classic.embedded == 4
    assert classic.missing == 0
    assert classic.covered == 24
    assert classic.dedicated_sources == ("dedicated-ownership",)
    assert classic.embedded_sources == ("fixture-projection",)
    # The four on the fallback are named, most expensive first, not just counted.
    assert {line.player_id for line in classic.embedded_players} == {21, 22, 23, 24}
    fallback_salaries = [line.salary for line in classic.embedded_players]
    assert fallback_salaries == sorted(fallback_salaries, reverse=True)
    assert classic.embedded_players_total == 4
    assert "would use embedded ownership: 4" in render_readiness(readiness)


def test_a_player_with_neither_ownership_source_is_missing_not_embedded(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with connect_database(database) as connection:
        connection.execute(
            "UPDATE projection_snapshots SET ownership_projection = NULL WHERE player_id <= 5"
        )

    readiness = _read(database)
    classic = readiness.ownership[0]

    assert (classic.dedicated, classic.embedded, classic.missing) == (0, 19, 5)
    assert classic.missing_players_total == 5
    assert not _check(readiness, OWNERSHIP_COVERAGE).passed


# --------------------------------------------------------------------------------------
# Games: odds and weather are covered games, not covered players
# --------------------------------------------------------------------------------------


def test_a_missing_game_is_named_not_counted(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with connect_database(database) as connection:
        connection.execute("DELETE FROM odds_snapshots WHERE game_id IN (2, 3)")

    readiness = _read(database, config=_tuned(odds_required=True))

    assert readiness.odds.required == 4
    assert readiness.odds.covered == 2
    assert readiness.odds.missing == 2
    assert {game.external_game_id for game in readiness.odds.missing_games} == {
        "game-2",
        "game-3",
    }
    detail = _check(readiness, ODDS_COVERAGE).detail
    assert "DDD@CCC" in detail and "FFF@EEE" in detail


def test_weather_is_required_only_for_venues_that_are_not_domes(tmp_path: Path) -> None:
    """An unrecognized venue is not evidence of a roof, so it still needs a forecast."""

    database = _database(tmp_path, "weather.sqlite3")
    with connect_database(database) as connection:
        connection.execute("DELETE FROM weather_snapshots")
        # Ford Field is indoor in the shipped stadium table; "Fixture Stadium" is unknown.
        connection.execute("UPDATE games SET stadium_name = 'Ford Field' WHERE game_id IN (1, 2)")

    readiness = _read(database, config=_tuned(weather_required=True))

    roofs = {game.external_game_id: game.roof for game in readiness.games}
    assert roofs == {
        "game-1": "indoor",
        "game-2": "indoor",
        "game-3": "unknown",
        "game-4": "unknown",
    }
    assert readiness.weather.required == 2
    assert readiness.weather.missing == 2
    assert not _check(readiness, WEATHER_COVERAGE).passed

    # Not required (the shipped default), the same store passes and says why.
    relaxed = _read(database)
    check = _check(relaxed, WEATHER_COVERAGE)
    assert check.passed
    assert check.threshold == "not required"


def test_projected_stats_coverage_is_informational_and_never_a_threshold(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with connect_database(database) as connection:
        for player_id in (1, 2):
            _insert(
                connection,
                "projected_stats",
                {
                    "source": "stokastic-stats",
                    "season": 2026,
                    "week": 1,
                    "player_id": player_id,
                    "stat": "rush_yds",
                    "value": 42.0,
                    "file_sha256": "a" * 64,
                    **_pit("stokastic-stats", source_version="stats-v1"),
                },
            )

    readiness = _read(database)

    assert readiness.projected_stats.covered == 2
    assert readiness.projected_stats.missing == 22
    assert readiness.projected_stats.sources == ("stokastic-stats",)
    assert readiness.ready
    assert "projected_stats" not in {check.name for check in readiness.checks}


# --------------------------------------------------------------------------------------
# Thresholds: each fails and passes at its own boundary
# --------------------------------------------------------------------------------------


def test_projection_coverage_fails_and_passes_at_its_boundary(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with connect_database(database) as connection:
        # 23 of 24 is 95.83%; 22 of 24 is 91.67%.
        connection.execute("DELETE FROM projection_snapshots WHERE player_id = 1")
    assert _check(_read(database), PROJECTION_COVERAGE).passed

    with connect_database(database) as connection:
        connection.execute("DELETE FROM projection_snapshots WHERE player_id = 2")
    failed = _check(_read(database), PROJECTION_COVERAGE)
    assert not failed.passed
    assert failed.observed.startswith("91.67% (22 of 24)")
    assert failed.threshold == "95.00%"

    # Exactly at the floor is a pass, never a miss.
    assert _check(
        _read(database, config=_tuned(minimum_projection_coverage=22 / 24)), PROJECTION_COVERAGE
    ).passed


def test_ownership_coverage_fails_and_passes_at_its_boundary(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with connect_database(database) as connection:
        # 22 of 24 is 91.67%, above the 90% floor; 21 of 24 is 87.5%, below it.
        connection.execute(
            "UPDATE projection_snapshots SET ownership_projection = NULL WHERE player_id <= 2"
        )
    assert _check(_read(database), OWNERSHIP_COVERAGE).passed

    with connect_database(database) as connection:
        connection.execute(
            "UPDATE projection_snapshots SET ownership_projection = NULL WHERE player_id = 3"
        )
    failed = _check(_read(database), OWNERSHIP_COVERAGE)
    assert not failed.passed
    assert failed.observed.startswith("87.50% (21 of 24)")


def test_projection_age_fails_and_passes_at_its_boundary(tmp_path: Path) -> None:
    """Six hours on a main slate, three on a showdown, both measured at the instant."""

    database = _database(tmp_path)
    # DATA_AT is 12:00 and DECISION_AT is 16:55, so the pool is 4h55m old.
    assert _check(_read(database), PROJECTION_AGE).passed
    exactly_six = DATA_AT + timedelta(hours=6)
    assert _check(_read(database, as_of=exactly_six), PROJECTION_AGE).passed
    just_over = _check(_read(database, as_of=exactly_six + timedelta(minutes=1)), PROJECTION_AGE)
    assert not just_over.passed
    assert just_over.observed == "6h01m"
    assert just_over.threshold == "6h00m"

    with connect_database(database) as connection:
        connection.execute("UPDATE slates SET slate_type = 'showdown' WHERE slate_id = 1")
    showdown = _check(_read(database), PROJECTION_AGE)
    assert not showdown.passed
    assert showdown.threshold == "3h00m"
    assert "showdown bound" in showdown.detail


def test_odds_are_reported_but_not_required_until_an_ingest_can_satisfy_them(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with connect_database(database) as connection:
        connection.execute("DELETE FROM odds_snapshots WHERE game_id = 1")

    shipped = _read(database)
    assert shipped.odds.missing == 1  # still measured and named
    assert _check(shipped, ODDS_COVERAGE).passed
    assert _check(shipped, ODDS_COVERAGE).threshold == "not required"
    assert not _check(_read(database, config=_tuned(odds_required=True)), ODDS_COVERAGE).passed


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


def test_the_shipped_defaults_are_the_ones_the_slice_documents() -> None:
    assert SHIPPED.minimum_projection_coverage == 0.95
    assert SHIPPED.minimum_ownership_coverage == 0.90
    assert SHIPPED.maximum_projection_age(  # a main slate
        "classic"
    ) == timedelta(hours=6)
    assert SHIPPED.maximum_projection_age("showdown") == timedelta(hours=3)
    # Nothing ingests odds or weather into the store yet (Slice 2 captures raw files), so
    # neither can be required without making every real build an excuse.
    assert SHIPPED.odds_required is False
    assert SHIPPED.weather_required is False
    assert SHIPPED.weather_outdoor_only is True


@pytest.mark.parametrize(
    "text,message",
    [
        ("config_version = 1\n", "config_version"),
        ('config_version = "v1"\n', "coverage"),
        (
            'config_version = "v1"\n[coverage]\nminimum_projection_coverage = 1.5\n'
            "minimum_ownership_coverage = 0.9\n[freshness]\n"
            "maximum_projection_age_minutes = 360\n"
            "maximum_projection_age_minutes_showdown = 180\n"
            "[market]\nodds_required = true\nweather_required = true\n"
            "weather_outdoor_only = true\n",
            "minimum_projection_coverage",
        ),
        (
            'config_version = "v1"\n[coverage]\nminimum_projection_coverage = 0.95\n'
            "minimum_ownership_coverage = 0.9\n[freshness]\n"
            "maximum_projection_age_minutes = 0\n"
            "maximum_projection_age_minutes_showdown = 180\n"
            "[market]\nodds_required = true\nweather_required = true\n"
            "weather_outdoor_only = true\n",
            "maximum_projection_age_minutes",
        ),
    ],
)
def test_an_unusable_threshold_file_is_refused_by_name(text: str, message: str) -> None:
    with pytest.raises(ReadinessConfigError, match=message):
        load_readiness_config_bytes(text.encode("utf-8"), source="fixture")


def test_the_report_carries_the_exact_configuration_bytes_it_was_judged_by(
    tmp_path: Path,
) -> None:
    readiness = _read(_database(tmp_path))

    assert readiness.config_sha256 == SHIPPED.config_sha256
    assert readiness.config_version == SHIPPED.config_version
    assert readiness_payload(readiness)["config_sha256"] == SHIPPED.config_sha256


# --------------------------------------------------------------------------------------
# The build consults it
# --------------------------------------------------------------------------------------


def test_the_build_refuses_a_failing_threshold_and_writes_nothing(tmp_path: Path) -> None:
    database = _database(tmp_path)
    artifacts = tmp_path / "artifacts"
    strict_path, _ = _strict_market(tmp_path)
    with connect_database(database) as connection:
        connection.execute("DELETE FROM odds_snapshots WHERE game_id IN (1, 2)")

    with pytest.raises(BuildReadinessError) as raised:
        build_decision(
            database,
            slate_id=1,
            site=DfsSite.DRAFTKINGS,
            decision_at=DECISION_AT,
            artifact_directory=artifacts,
            readiness_config_path=strict_path,
        )

    assert "odds_coverage" in str(raised.value)
    assert "--accept-readiness odds_coverage" in str(raised.value)
    assert raised.value.structured()["code"] == "readiness_refused"
    # A refusal writes nothing: no artifact directory, no run, no snapshot.
    assert not artifacts.exists()
    with connect_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM decision_snapshots").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM model_runs").fetchone()[0] == 0


def test_an_accepted_failure_builds_and_the_manifest_records_the_acceptance(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    artifacts = tmp_path / "artifacts"
    strict_path, strict = _strict_market(tmp_path)
    with connect_database(database) as connection:
        connection.execute("DELETE FROM odds_snapshots WHERE game_id IN (1, 2)")

    built = build_decision(
        database,
        slate_id=1,
        site=DfsSite.DRAFTKINGS,
        decision_at=DECISION_AT,
        artifact_directory=artifacts,
        accepted_readiness_failures=("odds_coverage",),
        readiness_config_path=strict_path,
    )

    assert built.accepted_readiness_failures == ("odds_coverage",)
    assert built.readiness is not None and not built.readiness.ready
    artifact = next(
        item for item in built.snapshot.manifest_hashes_json if item.artifact_kind == "readiness"
    )
    assert artifact.source == strict.config_version
    assert built.readiness_path is not None
    frozen = json.loads(built.readiness_path.read_bytes())
    assert frozen["accepted_failures"] == ["odds_coverage"]
    assert frozen["readiness"]["failed_checks"] == ["odds_coverage"]
    assert frozen["config"]["config_sha256"] == strict.config_sha256
    # The frozen bytes are the artifact the manifest hash covers.
    assert built.replay.readiness is not None
    assert built.replay.readiness.accepted_failures == ("odds_coverage",)
    assert built.replay.readiness.store_matches


def test_accepting_a_check_that_does_not_exist_is_refused_before_anything_runs(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)

    with pytest.raises(BuildInputError, match="projeciton_coverage"):
        build_decision(
            database,
            slate_id=1,
            site=DfsSite.DRAFTKINGS,
            decision_at=DECISION_AT,
            artifact_directory=tmp_path / "artifacts",
            accepted_readiness_failures=("projeciton_coverage",),
        )


def test_replay_of_an_accepted_decision_reproduces_the_same_readiness_summary(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    artifacts = tmp_path / "artifacts"
    strict_path, _ = _strict_market(tmp_path)
    with connect_database(database) as connection:
        connection.execute("DELETE FROM weather_snapshots WHERE game_id = 4")

    built = build_decision(
        database,
        slate_id=1,
        site=DfsSite.DRAFTKINGS,
        decision_at=DECISION_AT,
        artifact_directory=artifacts,
        accepted_readiness_failures=("weather_coverage",),
        readiness_config_path=strict_path,
    )

    with connect_database(database) as connection:
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
        memo = render_slate_memo(build_slate_memo(built, connection))

    assert replayed.report.output_matches
    assert replayed.readiness is not None
    assert replayed.readiness.accepted_failures == ("weather_coverage",)
    assert replayed.readiness.failed_checks == ("weather_coverage",)
    assert replayed.readiness.summary == built.readiness.summary_line  # type: ignore[union-attr]
    assert replayed.readiness.store_matches
    assert "accepted_failures=weather_coverage" in memo
    assert "store_still_measures_the_same=yes" in memo


def test_a_row_backfilled_after_the_decision_is_named_not_used_to_refuse_replay(
    tmp_path: Path,
) -> None:
    """The decision is unchanged; what changed is the store, and the memo says so."""

    database = _database(tmp_path)
    artifacts = tmp_path / "artifacts"
    built = build_decision(
        database,
        slate_id=1,
        site=DfsSite.DRAFTKINGS,
        decision_at=DECISION_AT,
        artifact_directory=artifacts,
    )
    with connect_database(database) as connection:
        # Backdated into an instant the decision predates: candidate selection is pinned to
        # its own manifest artifacts and never sees it, so the lineups cannot move.
        _insert(
            connection,
            "odds_snapshots",
            {
                "game_id": 1,
                "sportsbook": "late-book",
                "home_spread": -1.0,
                "away_spread": 1.0,
                "total": 40.0,
                "response_file_sha256": ODDS_HASH,
                **_pit("late-odds"),
            },
        )
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
        # The memo an operator would regenerate today, from the frozen decision rather
        # than from the build's own in-memory result.
        reloaded = load_build_result(
            connection,
            decision_snapshot_id=snapshot.decision_snapshot_id,
            decision_at=DECISION_AT,
            artifact_root=artifacts,
        )
        memo = render_slate_memo(build_slate_memo(reloaded, connection))

    assert replayed.report.output_matches
    assert replayed.request == built.request
    assert replayed.readiness is not None
    assert not replayed.readiness.store_matches
    # The decision's own readiness is still what it was frozen as; only the store moved.
    assert replayed.readiness.summary == "READY"
    assert "store_still_measures_the_same=NO" in memo


def test_a_tampered_readiness_artifact_is_refused_by_its_manifest_hash(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    artifacts = tmp_path / "artifacts"
    built = build_decision(
        database,
        slate_id=1,
        site=DfsSite.DRAFTKINGS,
        decision_at=DECISION_AT,
        artifact_directory=artifacts,
    )
    assert built.readiness_path is not None
    built.readiness_path.write_bytes(b'{"accepted_failures":[],"config":{},"readiness":{}}')

    with connect_database(database) as connection:
        snapshot = DecisionSnapshotRow.from_db(
            connection.execute("SELECT * FROM decision_snapshots").fetchone()
        )
        with pytest.raises(ReplayArtifactError, match=r"artifact hash mismatch.*readiness\.json"):
            replay_decision(
                connection,
                decision_snapshot_id=snapshot.decision_snapshot_id,
                decision_at=DECISION_AT,
                artifact_root=artifacts,
                adapter=PydfsAdapter(),
            )


def test_two_decisions_differing_only_in_an_acceptance_are_different_decisions(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with connect_database(database) as connection:
        connection.execute("DELETE FROM odds_snapshots WHERE game_id = 1")

    accepted = build_decision(
        database,
        slate_id=1,
        site=DfsSite.DRAFTKINGS,
        decision_at=DECISION_AT,
        artifact_directory=tmp_path / "one",
        accepted_readiness_failures=("odds_coverage",),
    )
    both = build_decision(
        database,
        slate_id=1,
        site=DfsSite.DRAFTKINGS,
        decision_at=DECISION_AT,
        artifact_directory=tmp_path / "two",
        accepted_readiness_failures=("odds_coverage", "weather_coverage"),
    )

    assert accepted.snapshot.decision_snapshot_id != both.snapshot.decision_snapshot_id


# --------------------------------------------------------------------------------------
# Surfaces
# --------------------------------------------------------------------------------------


def test_the_status_screen_shows_one_readiness_line_per_slate(ops_week: Any) -> None:
    database = ops_week.database
    with connect_database(database) as connection:
        connection.execute("DELETE FROM projection_snapshots")

    with connect_database(database) as connection:
        status = collect_ops_status(connection, config=ops_week, database=database, now=DECISION_AT)
    rendered = render_status(status)
    payload = status_payload(status)

    assert status.slate is not None
    row = status.slate.slates[0]
    assert row.readiness is not None
    assert not row.readiness.ready
    assert row.readiness_line.startswith("NOT READY — projection_coverage")
    assert f"readiness {row.readiness_line}" in rendered
    slate_payload = payload["slate"]["slates"][0]  # type: ignore[index,call-overload]
    assert slate_payload["readiness_summary"] == row.readiness_line
    assert slate_payload["readiness"]["ready"] is False
    assert slate_payload["readiness"]["slate_id"] == 1
    assert any("is not ready to build" in action for action in status.manual_actions)


def test_a_ready_slate_reads_ready_on_the_status_screen(ops_week: Any) -> None:
    with connect_database(ops_week.database) as connection:
        status = collect_ops_status(
            connection, config=ops_week, database=ops_week.database, now=DECISION_AT
        )

    assert status.slate is not None
    assert status.slate.slates[0].readiness_line == "READY"
    assert "readiness READY" in render_status(status)
    assert not any("is not ready to build" in action for action in status.manual_actions)


def test_the_dashboard_page_renders_the_same_payload_and_the_status_page_links_to_it(
    ops_week: Any,
) -> None:
    from narrative_alpha.ops.dashboard import DashboardContext, _readiness_page, _status_page

    context = DashboardContext(
        config=ops_week,
        database=ops_week.database,
        artifact_directory=ops_week.database.parent / "decisions",
        report_directory=ops_week.database.parent / "reports",
        runner=_LaneRunner(),
        clock=lambda: DECISION_AT,
    )

    index = _readiness_page(context, {})
    page = _readiness_page(context, {"slate_id": ["1"]})
    missing = _readiness_page(context, {"slate_id": ["99"]})
    status_page = _status_page(context)

    assert index.startswith("<!doctype html>")
    assert 'href="/readiness?slate_id=1"' in index
    assert "Slate 1 — READY" in page
    assert "unprojected players" in page.casefold()
    assert "No slate <code>99</code>" in missing
    # The status page carries the same sentence and a link to the full report.
    assert 'href="/readiness?slate_id=1"' in status_page
    assert "READY" in status_page


class _LaneRunner:
    """The dashboard's runner contract, reduced to what a read-only page touches."""

    any_running = False

    def state(self, lane: str) -> Any:  # pragma: no cover - unused by the read pages
        raise NotImplementedError

    def states(self) -> tuple[Any, ...]:
        return ()


def test_the_readiness_command_prints_the_report_and_exits_nonzero_when_not_ready(
    ops_week: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    from narrative_alpha.ops.cli import main as ops_main

    exit_code = ops_main(
        [
            "--config",
            str(ops_week.path),
            "readiness",
            "--slate-id",
            "1",
            "--as-of",
            utc_timestamp(DECISION_AT),
        ]
    )
    text = capsys.readouterr().out

    assert exit_code == 0
    assert "SLATE INPUT READINESS" in text
    assert "READY" in text
    assert "PLAYERS THE BUILD WOULD DROP" in text
    assert "PROJECTED STATS (informational" in text

    with connect_database(ops_week.database) as connection:
        connection.execute("DELETE FROM projection_snapshots WHERE player_id <= 5")
    exit_code = ops_main(
        [
            "--config",
            str(ops_week.path),
            "readiness",
            "--slate-id",
            "1",
            "--as-of",
            utc_timestamp(DECISION_AT),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["ready"] is False
    # Deleting the projection rows takes their embedded ownership with them, so both
    # coverage thresholds miss and both are named.
    assert payload["failed_checks"] == ["projection_coverage", "ownership_coverage"]
    assert payload["unprojected_players_total"] == 5


@pytest.fixture
def ops_week(tmp_path: Path) -> Any:
    """An operator config, one initialized snapshot week, and one complete slate."""

    from narrative_alpha.ops.config import load_ops_config
    from narrative_alpha.snapshots import CaptureKind, capture_files

    path = tmp_path / "ops.toml"
    path.write_text(
        f"""
timezone = "UTC"
season = 2026
monthly_llm_budget_usd = "50.00"
keychain_service = "narrative-alpha-anthropic"

[batch]
weekdays = ["wed"]
local_time = "09:30"
max_items_per_run = 20

[paths]
database = "{tmp_path / "store.sqlite3"}"
snapshot_root = "{tmp_path / "snapshots"}"
nflverse_archive = "{tmp_path / "archive"}"
log_directory = "{tmp_path / "logs"}"
""".lstrip(),
        encoding="utf-8",
    )
    config = load_ops_config(path)
    staged = tmp_path / "staged" / "DKSalaries.csv"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text("Position,Name,Salary\nQB,Fixture Quarterback,7000\n", encoding="utf-8")
    capture_files(
        config.snapshot_root,
        2026,
        1,
        CaptureKind.SALARIES,
        "draftkings",
        [staged],
        observed_at=DATA_AT,
    )
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed_candidate_pool(connection, _players())
    return config
