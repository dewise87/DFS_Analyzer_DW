"""Pinned Open-Meteo runs; absent hours and fields never become invented forecasts."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field, StrictFloat, StrictInt

from narrative_alpha.ingest.game_inputs import (
    GameInputLoadReport,
    insert_observation,
    verified_capture,
)
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.snapshots import CaptureKind
from narrative_alpha.snapshots.fetch import WEATHER_SOURCE
from narrative_alpha.snapshots.stadiums import find_stadium, find_stadium_for_team

WEATHER_FORMAT_VERSION = "open-meteo-single-run-hourly-v1"


class WeatherBody(BaseModel):
    utc_offset_seconds: StrictInt
    hourly_units: dict[str, str]
    hourly: dict[str, list[StrictFloat | str | None]]


class WeatherValues(BaseModel):
    temperature_c: float = Field(allow_inf_nan=False, strict=True)
    precipitation_probability: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    wind_speed_kph: float = Field(ge=0, allow_inf_nan=False, strict=True)
    wind_gust_kph: float = Field(ge=0, allow_inf_nan=False, strict=True)
    weather_code: int = Field(ge=0)


def parse_weather(raw: object, kickoff: datetime) -> tuple[datetime, WeatherValues]:
    """Use the UTC hour containing kickoff (floor minutes/seconds, no interpolation).

    The request asks for hourly UTC data, not a rounded kickoff. This explicitly defines
    the loader's rounding. The real single-runs schema omits ensemble probability; NULL
    means unavailable, never a dry forecast inferred from deterministic millimeters.
    """
    body = WeatherBody.model_validate(raw)
    if body.utc_offset_seconds != 0:
        raise ValueError("weather body must use UTC (utc_offset_seconds = 0)")
    units = {
        "time": "iso8601",
        "temperature_2m": "°C",
        "wind_speed_10m": "km/h",
        "wind_gusts_10m": "km/h",
        "weather_code": "wmo code",
    }
    if (
        "precipitation_probability" in body.hourly
        or "precipitation_probability" in body.hourly_units
    ):
        units["precipitation_probability"] = "%"
    for field, unit in units.items():
        if body.hourly_units.get(field) != unit:
            raise ValueError(f"units for {field} must say {unit!r}")
        if field not in body.hourly:
            raise ValueError(f"missing hourly field {field}")
    times = body.hourly["time"]
    for name, series in body.hourly.items():
        if len(series) != len(times):
            raise ValueError(f"hourly {name} length differs from time")
    forecast_for = ensure_utc(kickoff).replace(minute=0, second=0, microsecond=0)
    hour = forecast_for.strftime("%Y-%m-%dT%H:%M")
    if times.count(hour) != 1:
        raise ValueError(
            f"kickoff hour {hour} has {times.count(hour)} forecast values; expected one"
        )
    index = times.index(hour)
    probability: float | None = None
    if "precipitation_probability" in units:
        percent = body.hourly["precipitation_probability"][index]
        if not isinstance(percent, (int, float)) or not 0 <= percent <= 100:
            raise ValueError("precipitation_probability must be a numeric percent in [0,100]")
        probability = percent / 100.0  # '%' units, always divide, even 0.5 means 0.005.
    code = body.hourly["weather_code"][index]
    if not isinstance(code, (int, float)) or not float(code).is_integer():
        raise ValueError("weather_code must be an integer")
    values = WeatherValues.model_validate(
        dict(
            temperature_c=body.hourly["temperature_2m"][index],
            wind_speed_kph=body.hourly["wind_speed_10m"][index],
            wind_gust_kph=body.hourly["wind_gusts_10m"][index],
            weather_code=code,
            precipitation_probability=probability,
        )
    )
    return forecast_for, values


def load_weather_capture(
    connection: sqlite3.Connection,
    capture_path: Path,
    *,
    season: int,
    week: int,
    ingested_at: datetime | None = None,
) -> GameInputLoadReport:
    manifest, records = verified_capture(
        capture_path, season, week, CaptureKind.WEATHER, WEATHER_SOURCE
    )
    ingestion = ensure_utc(ingested_at or datetime.now(UTC))
    errors = [f"capture error [{e.error_type}]: {e.message}" for e in manifest.errors]
    notes: list[str] = []
    matched: set[int] = set()
    inserted = duplicates = rejected = 0
    games = connection.execute(
        "SELECT g.game_id, g.kickoff_at, g.stadium_name, h.abbreviation "
        "FROM games g JOIN teams h ON h.team_id = g.home_team_id "
        "WHERE g.season = ? AND g.week = ?",
        (season, week),
    ).fetchall()
    for record in records:
        label = record.path
        try:
            requests = [
                request for request in manifest.requests if request.file_path == record.path
            ]
            if len(requests) != 1:
                raise ValueError("expected exactly one manifest request for weather response")
            request = requests[0]
            label += f" {request.stadium} kickoff={request.kickoff_at}"
            if (
                request.stadium is None
                or request.kickoff_at is None
                or request.forecast_model_run_at is None
                or request.forecast_lead_time_seconds is None
            ):
                raise ValueError("request lacks stadium, kickoff, model run or lead time")
            if request.source != WEATHER_SOURCE or request.observed_at != record.observed_at:
                raise ValueError("request source/observation disagrees with file record")
            stadium = find_stadium(request.stadium)
            if stadium is None:
                raise ValueError(f"unknown stadium {request.stadium!r}")
            matches: list[int] = []
            for game in games:
                # Salary exports supply no stadium. Use the maintained home-venue table
                # only when absent; an explicit unknown venue is never replaced.
                venue = (
                    find_stadium(str(game[2])) if game[2] else find_stadium_for_team(str(game[3]))
                )
                kickoff = ensure_utc(datetime.fromisoformat(str(game[1])))
                if venue == stadium and kickoff == request.kickoff_at:
                    matches.append(int(game[0]))
            if len(matches) != 1:
                raise ValueError(
                    f"stadium/kickoff match {len(matches)} games; expected exactly one"
                )
            game_id = matches[0]
            matched.add(game_id)
            forecast_for, values = parse_weather(
                json.loads((capture_path / record.path).read_bytes()),
                request.kickoff_at,
            )
            if values.precipitation_probability is None:
                notes.append(
                    f"{record.path}: precipitation_probability absent in source; stored NULL"
                )
            observed = utc_timestamp(record.observed_at)
            result = insert_observation(
                connection,
                "weather_snapshots",
                dict(
                    source=WEATHER_SOURCE,
                    game_id=game_id,
                    forecast_model=WEATHER_SOURCE,
                    forecast_run_at=utc_timestamp(request.forecast_model_run_at),
                    forecast_for_at=utc_timestamp(forecast_for),
                    observed_at=observed,
                ),
                values.model_dump()
                | dict(
                    stadium_name=request.stadium,
                    lead_time_seconds=request.forecast_lead_time_seconds,
                    response_file_sha256=record.sha256,
                    source_version=WEATHER_FORMAT_VERSION,
                    valid_from=observed,
                    published_at=None,
                ),
                ingested_at=ingestion,
            )
            inserted += int(result)
            duplicates += int(not result)
        except ValueError as error:
            rejected += 1
            errors.append(f"{label}: {error}")
    return GameInputLoadReport(
        files_seen=len(records),
        rows_seen=len(records),
        games_matched=len(matched),
        rows_inserted=inserted,
        duplicate_rows=duplicates,
        rejected_rows=rejected,
        errors=tuple(errors),
        notes=tuple(notes),
    )
