"""Real response schema goldens; synthetic games/capture metadata are labeled explicitly."""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_build import _insert
from test_ops_slate import (
    CAPTURED_AT,
    DECISION_AT,
    GAMES,
    KICKOFF,
    _run,
)
from test_ops_slate import (
    week as seeded_week,  # noqa: F401 - shared capture fixture
)

from narrative_alpha.identity.normalization import TEAM_CODES_BY_NAME, team_code_from_name
from narrative_alpha.ingest.game_inputs import GameInputIngestError, newest_game_input_capture
from narrative_alpha.ingest.odds import load_odds_capture
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.ingest.weather import load_weather_capture, parse_weather
from narrative_alpha.ops.status import collect_ops_status, status_payload
from narrative_alpha.readiness import collect_slate_readiness
from narrative_alpha.slate_cli import main
from narrative_alpha.snapshots import CaptureKind
from narrative_alpha.snapshots.core import CapturePayload, capture_payloads
from narrative_alpha.snapshots.fetch import ODDS_SOURCE, WEATHER_SOURCE
from narrative_alpha.snapshots.models import SnapshotRequest
from narrative_alpha.snapshots.stadiums import find_stadium_for_team
from narrative_alpha.store import apply_migrations, connect_database

GOLDEN = Path(__file__).parent / "golden"
OBSERVED = datetime(2026, 9, 1, 13, tzinfo=UTC)
# The real body ends September 8. This synthetic request selects an actual available hour.
WEATHER_KICKOFF = datetime(2026, 9, 2, 17, 25, tzinfo=UTC)


def _odds() -> Any:
    return json.loads((GOLDEN / "the_odds_api_two_games.json").read_bytes())


def _weather() -> Any:
    return json.loads((GOLDEN / "open_meteo_one_game.json").read_bytes())


def _capture(
    root: Path,
    kind: CaptureKind,
    body: Any,
    *,
    kickoff: datetime = WEATHER_KICKOFF,
    observed: datetime = OBSERVED,
    stadium: str = "Lumen Field",
    filename: str = "body.json",
) -> Path:
    source = ODDS_SOURCE if kind is CaptureKind.ODDS else WEATHER_SOURCE
    run = datetime(2026, 9, 1, 6, tzinfo=UTC)
    requests = []
    if kind is CaptureKind.WEATHER:
        requests.append(
            SnapshotRequest(
                source=source,
                url="https://single-runs-api.open-meteo.com/v1/forecast",
                observed_at=observed,
                attempts=1,
                status_code=200,
                file_path=f"weather/{filename}",
                stadium=stadium,
                stadium_table_version="2026-09-01.1",
                kickoff_at=kickoff,
                forecast_model_run_at=run,
                forecast_lead_time_seconds=int((kickoff - run).total_seconds()),
            )
        )
    return capture_payloads(
        root,
        2026,
        1,
        kind,
        [
            CapturePayload(
                filename,
                json.dumps(body).encode(),
                observed,
                source,
            )
        ],
        requests=requests,
        captured_at=observed,
    )


def seed_games(connection: sqlite3.Connection, events: Any) -> None:
    """Seed only identities from the exact captured event teams/kickoffs; no production writes."""
    pit = dict(
        source="fixture",
        observed_at=utc_timestamp(OBSERVED),
        ingested_at=utc_timestamp(OBSERVED),
        valid_from=utc_timestamp(OBSERVED),
    )
    teams: dict[str, int] = {}
    for event in events:
        for name in (event["home_team"], event["away_team"]):
            if name not in teams:
                code = team_code_from_name(name)
                _insert(
                    connection,
                    "teams",
                    dict(
                        team_key=code,
                        abbreviation=code,
                        canonical_name=name,
                        league="NFL",
                        **pit,
                    ),
                )
                teams[name] = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        _insert(
            connection,
            "games",
            dict(
                external_game_id=event["id"],
                season=2026,
                week=1,
                kickoff_at=utc_timestamp(datetime.fromisoformat(event["commence_time"])),
                home_team_id=teams[event["home_team"]],
                away_team_id=teams[event["away_team"]],
                stadium_name=None,
                game_status="scheduled",
                **pit,
            ),
        )


@pytest.fixture
def store(tmp_path: Path) -> Path:
    database = tmp_path / "scratch.sqlite3"
    with connect_database(database) as connection:
        apply_migrations(connection)
        seed_games(connection, _odds())
        # A synthetic Seattle game in the body's horizon, distinct from the real odds event.
        event = copy.deepcopy(_odds()[0])
        event["id"] = "synthetic-weather-game"
        event["commence_time"] = utc_timestamp(WEATHER_KICKOFF)
        connection.execute(
            "INSERT INTO games(external_game_id, season, week, kickoff_at, home_team_id, "
            "away_team_id, game_status, source, observed_at, ingested_at, valid_from) "
            "SELECT ?, season, week, ?, home_team_id, away_team_id, game_status, source, "
            "observed_at, ingested_at, valid_from FROM games WHERE game_id = 1",
            (event["id"], event["commence_time"]),
        )
    return database


def test_both_real_goldens_and_reload(store: Path, tmp_path: Path) -> None:
    odds = _capture(tmp_path / "odds", CaptureKind.ODDS, _odds())
    weather = _capture(tmp_path / "weather", CaptureKind.WEATHER, _weather())
    with connect_database(store) as connection:
        first = load_odds_capture(connection, odds, season=2026, week=1, ingested_at=OBSERVED)
        assert first.ok and first.games_matched == 2 and first.rows_inserted == 4
        first_weather = load_weather_capture(
            connection, weather, season=2026, week=1, ingested_at=OBSERVED
        )
        assert first_weather.ok and first_weather.rows_inserted == 1
        assert "stored NULL" in first_weather.notes[0]
        row = connection.execute("SELECT * FROM weather_snapshots").fetchone()
        assert row["forecast_for_at"] == "2026-09-02T17:00:00.000000Z"
        assert row["forecast_run_at"] == "2026-09-01T06:00:00.000000Z"
        assert row["lead_time_seconds"] == 127500
        assert row["precipitation_probability"] is None
        for loader, capture, count in (
            (load_odds_capture, odds, 4),
            (load_weather_capture, weather, 1),
        ):
            again = loader(
                connection, capture, season=2026, week=1, ingested_at=OBSERVED + timedelta(days=1)
            )
            assert again.ok and again.rows_inserted == 0 and again.duplicate_rows == count
        row = connection.execute(
            "SELECT * FROM odds_snapshots ORDER BY odds_snapshot_id"
        ).fetchone()
        assert row["sportsbook"] == "draftkings"
        assert row["home_spread"] == -3.5 and row["away_spread"] == 3.5
        assert row["home_spread_price"] == -105
        assert row["published_at"] == "2026-09-01T13:18:19.000000Z"
        assert row["source"] == "the-odds-api"


def test_unmatched_event_is_named_and_bad_bookmaker_is_skipped(store: Path, tmp_path: Path) -> None:
    body = _odds()
    body[0]["commence_time"] = "2026-09-10T01:15:00Z"
    body[1]["bookmakers"][0]["markets"][0]["outcomes"][0]["point"] = 7
    capture = _capture(tmp_path, CaptureKind.ODDS, body)
    with connect_database(store) as connection:
        report = load_odds_capture(connection, capture, season=2026, week=1)
    # A league-wide feed always carries games this slate-scoped store never ingested;
    # that is a counted note, not a skip. A bad bookmaker is a skip.
    assert report.rows_inserted == 1 and report.rejected_rows == 1
    assert report.unmatched_rows == 1
    assert report.notes == ("1 event(s) matched no ingested game for 2026 week 1",)
    assert "draftkings" in report.errors[0] and "spreads disagree" in report.errors[0]
    assert not report.ok


@pytest.mark.parametrize("percent,fraction", [(50, 0.5), (0.5, 0.005), (1, 0.01), (100, 1)])
def test_probability_uses_explicit_percent(percent: float, fraction: float) -> None:
    body = _weather()
    body["hourly_units"]["precipitation_probability"] = "%"
    body["hourly"]["precipitation_probability"] = [percent] * len(body["hourly"]["time"])
    _, parsed = parse_weather(body, WEATHER_KICKOFF)
    assert parsed.precipitation_probability == fraction
    body["hourly_units"]["precipitation_probability"] = "fraction"
    with pytest.raises(ValueError, match="units"):
        parse_weather(body, WEATHER_KICKOFF)


@pytest.mark.parametrize("field,unit", [("temperature_2m", "°F"), ("wind_speed_10m", "mph")])
def test_wrong_units_refused(field: str, unit: str) -> None:
    body = _weather()
    body["hourly_units"][field] = unit
    with pytest.raises(ValueError, match="units"):
        parse_weather(body, WEATHER_KICKOFF)


def test_real_weather_horizon_does_not_cover_real_kickoff() -> None:
    with pytest.raises(ValueError, match="0 forecast values"):
        parse_weather(_weather(), datetime(2026, 9, 10, 0, 15, tzinfo=UTC))


@pytest.mark.parametrize("kind", [CaptureKind.ODDS, CaptureKind.WEATHER])
def test_hash_tampering_refuses_and_cli_returns_two(
    store: Path, tmp_path: Path, kind: CaptureKind, capsys: Any
) -> None:
    capture = _capture(tmp_path, kind, _odds() if kind is CaptureKind.ODDS else _weather())
    (capture / kind.value / "body.json").write_text("[]")
    loader = load_odds_capture if kind is CaptureKind.ODDS else load_weather_capture
    with connect_database(store) as connection, pytest.raises(GameInputIngestError, match="hash"):
        loader(connection, capture, season=2026, week=1)
    assert (
        main(
            [
                f"load-{kind.value}",
                "--database",
                str(store),
                "--season",
                "2026",
                "--week",
                "1",
                "--capture",
                str(capture),
            ]
        )
        == 2
    )
    assert "hash mismatch" in capsys.readouterr().err


def test_both_cli_commands_default_and_explicit_capture(
    store: Path, tmp_path: Path, capsys: Any
) -> None:
    for kind, body in ((CaptureKind.ODDS, _odds()), (CaptureKind.WEATHER, _weather())):
        root = tmp_path / kind.value
        capture = _capture(root, kind, body)
        assert newest_game_input_capture(root, 2026, 1, kind) == capture
        args = [f"load-{kind.value}", "--database", str(store), "--season", "2026", "--week", "1"]
        assert main([*args, "--root", str(root)]) == 0
        assert "games matched:" in capsys.readouterr().out
        assert main([*args, "--capture", str(capture)]) == 0
        assert "rows inserted: 0" in capsys.readouterr().out
    body = _odds()
    body[0]["home_team"] = "Unknown Team"
    capture = _capture(tmp_path / "unknown", CaptureKind.ODDS, body)
    assert (
        main(
            [
                "load-odds",
                "--database",
                str(store),
                "--season",
                "2026",
                "--week",
                "1",
                "--capture",
                str(capture),
            ]
        )
        == 1
    )
    assert "Unknown Team" in capsys.readouterr().out


def test_lane_ingests_both_feeds_and_status_and_readiness_light_up(
    seeded_week: Any,  # noqa: F811 - pytest fixture injection
    tmp_path: Path,
) -> None:
    week = seeded_week
    # Synthetic lane inputs adapt the reviewed shapes to its four matchups and September 13
    # kickoff. This is NOT the real forecast; the real unmodified body is tested above.
    names = {code: name.title() for name, code in TEAM_CODES_BY_NAME.items()}
    events = []
    payloads = []
    requests = []
    for index, (away, home) in enumerate(GAMES):
        event = copy.deepcopy(_odds()[0])
        old_home, old_away = event["home_team"], event["away_team"]
        event.update(
            id=f"fixture-{index}",
            home_team=names[home],
            away_team=names[away],
            commence_time=utc_timestamp(KICKOFF),
        )
        for book in event["bookmakers"]:
            for outcome in book["markets"][0]["outcomes"]:
                outcome["name"] = names[home] if outcome["name"] == old_home else names[away]
                assert outcome["name"] != old_away
        events.append(event)
        body = _weather()
        body["hourly"]["time"] = [
            (datetime.fromisoformat(hour) + timedelta(days=12)).strftime("%Y-%m-%dT%H:%M")
            for hour in body["hourly"]["time"]
        ]
        stadium = find_stadium_for_team(home)
        assert stadium is not None
        filename = f"weather-{index}.json"
        payloads.append(
            CapturePayload(filename, json.dumps(body).encode(), CAPTURED_AT, WEATHER_SOURCE)
        )
        requests.append(
            SnapshotRequest(
                source=WEATHER_SOURCE,
                url="https://single-runs-api.open-meteo.com/v1/forecast",
                observed_at=CAPTURED_AT,
                attempts=1,
                status_code=200,
                file_path=f"weather/{filename}",
                stadium=stadium.name,
                stadium_table_version="2026-09-01.1",
                kickoff_at=KICKOFF,
                forecast_model_run_at=CAPTURED_AT - timedelta(hours=10),
                forecast_lead_time_seconds=104400,
            )
        )
    capture_payloads(
        week.snapshot_root,
        2026,
        1,
        CaptureKind.WEATHER,
        payloads,
        requests=requests,
        captured_at=CAPTURED_AT,
    )
    _capture(week.snapshot_root, CaptureKind.ODDS, events, observed=CAPTURED_AT)
    report = _run(week, tmp_path=tmp_path, accepted_readiness_failures=("projection_age",))
    assert report.ok, [(s.step, s.error_text) for s in report.steps]
    assert report.step("slate_odds").summary["rows_inserted"] == 8
    assert report.step("slate_weather").summary["rows_inserted"] == 4
    with connect_database(week.database) as connection:
        readiness = collect_slate_readiness(connection, slate_id=report.slate_id, as_of=DECISION_AT)
        checks = {check.name: check.passed for check in readiness.checks}
        assert checks["odds_coverage"] and checks["weather_coverage"]
        status = status_payload(
            collect_ops_status(connection, config=week, database=week.database, now=DECISION_AT)
        )
    captures = {item["kind"]: item for item in status["slate"]["captures"]}
    assert captures["odds"]["files_ingested"] == 1
    assert captures["weather"]["files_ingested"] == 4


def test_ambiguous_game_and_team_variants(store: Path, tmp_path: Path) -> None:
    capture = _capture(tmp_path, CaptureKind.ODDS, _odds())
    with connect_database(store) as connection:
        connection.execute("UPDATE teams SET abbreviation = 'LA' WHERE abbreviation = 'LAR'")
        original = dict(connection.execute("SELECT * FROM games WHERE game_id = 1").fetchone())
        original.pop("game_id")
        original["external_game_id"] = "ambiguous-second-identity"
        _insert(connection, "games", original)
        report = load_odds_capture(connection, capture, season=2026, week=1)
    assert report.games_matched == 1 and report.rows_inserted == 2
    assert "match 2 games" in report.errors[0]


@pytest.mark.parametrize("kind", [CaptureKind.ODDS, CaptureKind.WEATHER])
def test_same_key_different_content_never_overwrites(
    store: Path, tmp_path: Path, kind: CaptureKind
) -> None:
    body = _odds() if kind is CaptureKind.ODDS else _weather()
    first = _capture(tmp_path / "first", kind, body)
    loader = load_odds_capture if kind is CaptureKind.ODDS else load_weather_capture
    with connect_database(store) as connection:
        assert loader(connection, first, season=2026, week=1).ok
        before = connection.execute(f"SELECT * FROM {kind.value}_snapshots").fetchall()
        if kind is CaptureKind.ODDS:
            body[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = -120
        else:
            index = body["hourly"]["time"].index("2026-09-02T17:00")
            body["hourly"]["temperature_2m"][index] += 1
        changed = _capture(tmp_path / "changed", kind, body)
        report = loader(connection, changed, season=2026, week=1)
        assert not report.ok and report.rows_inserted == 0
        assert "key conflict" in report.errors[0]
        assert connection.execute(f"SELECT * FROM {kind.value}_snapshots").fetchall() == before


def test_weather_request_metadata_is_authoritative_and_missing_hour_is_named(
    store: Path,
    tmp_path: Path,
) -> None:
    capture = _capture(
        tmp_path, CaptureKind.WEATHER, _weather(), kickoff=datetime(2026, 9, 10, 0, 15, tzinfo=UTC)
    )
    with connect_database(store) as connection:
        report = load_weather_capture(connection, capture, season=2026, week=1)
    assert report.games_matched == 1 and report.rows_inserted == 0
    assert "Lumen Field" in report.errors[0] and "0 forecast values" in report.errors[0]


def test_new_migration_preserves_ops_history(tmp_path: Path) -> None:
    from narrative_alpha.ops.runs import StepRecorder

    legacy = tmp_path / "migrations"
    legacy.mkdir()
    for path in Path("src/narrative_alpha/store/migrations").glob("*.sql"):
        if int(path.name[:4]) < 23:
            (legacy / path.name).write_bytes(path.read_bytes())
    with connect_database(tmp_path / "migration.sqlite3") as connection:
        apply_migrations(connection, legacy)
        recorder = StepRecorder(connection, run_id="before-upgrade", step_errors=(ValueError,))
        recorder.skip("slate_salaries", "fixture has no salaries")
        before = tuple(connection.execute("SELECT * FROM ops_runs").fetchone())
        apply_migrations(connection)
        assert tuple(connection.execute("SELECT * FROM ops_runs").fetchone()) == before
        recorder.skip("slate_odds", "fixture has no odds")
        recorder.skip("slate_weather", "fixture has no weather")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM ops_runs")


def test_newest_empty_capture_never_falls_back(store: Path, tmp_path: Path) -> None:
    from narrative_alpha.snapshots.models import SnapshotError

    _capture(tmp_path, CaptureKind.ODDS, _odds())
    newer = OBSERVED + timedelta(hours=1)
    failed = capture_payloads(
        tmp_path,
        2026,
        1,
        CaptureKind.ODDS,
        [],
        captured_at=newer,
        errors=[
            SnapshotError(
                source=ODDS_SOURCE,
                occurred_at=newer,
                attempts=0,
                error_type="missing_api_key",
                message="no key",
            )
        ],
    )
    assert newest_game_input_capture(tmp_path, 2026, 1, CaptureKind.ODDS) == failed
    with connect_database(store) as connection, pytest.raises(GameInputIngestError, match="no key"):
        load_odds_capture(connection, failed, season=2026, week=1)


def test_all_files_verified_before_any_write(store: Path, tmp_path: Path) -> None:
    payloads = [
        CapturePayload(f"odds-{i}.json", json.dumps(_odds()).encode(), OBSERVED, ODDS_SOURCE)
        for i in range(2)
    ]
    capture = capture_payloads(tmp_path, 2026, 1, CaptureKind.ODDS, payloads, captured_at=OBSERVED)
    (capture / "odds/odds-1.json").write_text("[]")
    with connect_database(store) as connection:
        with pytest.raises(GameInputIngestError, match="hash"):
            load_odds_capture(connection, capture, season=2026, week=1)
        assert connection.execute("SELECT count(*) FROM odds_snapshots").fetchone()[0] == 0


def test_numeric_kickoff_is_not_interpreted_as_epoch(store: Path, tmp_path: Path) -> None:
    body = _odds()
    body[0]["commence_time"] = 1788999300
    capture = _capture(tmp_path, CaptureKind.ODDS, body)
    with connect_database(store) as connection:
        report = load_odds_capture(connection, capture, season=2026, week=1)
    assert report.rows_inserted == 2
    assert "not epoch numbers" in report.errors[0]
