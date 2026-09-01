"""HTTP collectors that persist raw responses through the snapshot capture machinery."""

from __future__ import annotations

import csv
import logging
import re
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from narrative_alpha.snapshots.core import CapturePayload, capture_payloads, snapshot_week_path
from narrative_alpha.snapshots.models import (
    CaptureKind,
    SnapshotError,
    SnapshotRequest,
)
from narrative_alpha.snapshots.stadiums import (
    STADIUM_TABLE_VERSION,
    RoofType,
    Stadium,
    find_stadium,
    find_stadium_for_team,
)

# The Odds API only accepts its key as a query parameter, so the full request URL is a
# secret. Stored URLs are redacted; this keeps httpx's INFO-level request lines (which
# include the full URL) out of any future logging configuration as well.
logging.getLogger("httpx").setLevel(logging.WARNING)

ODDS_API_URL = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds"
WEATHER_API_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
ODDS_SOURCE = "the-odds-api-v4"
WEATHER_SOURCE = "open-meteo-gfs-seamless"
ODDS_QUOTA_HEADERS = ("x-requests-remaining", "x-requests-used", "x-requests-last")
# precipitation_probability is deliberately absent: it is an ensemble-derived field that
# makes the single-runs API require a GEFS run alongside the pinned GFS run and rejects
# the request (live-verified HTTP 400, 2026-09-01). Deterministic amounts suffice.
WEATHER_HOURLY_FIELDS = (
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "rain",
    "showers",
    "snowfall",
    "weather_code",
    "cloud_cover",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
)
HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
MAX_ATTEMPTS = 3
INITIAL_BACKOFF_SECONDS = 0.25
# GFS initializes at 00/06/12/18 UTC. Pin a cycle old enough to have been ingested instead of
# using Open-Meteo's blended latest forecast, whose underlying initialization is not identified.
MODEL_AVAILABILITY_LAG = timedelta(hours=6)

Sleeper = Callable[[float], None]


@dataclass(frozen=True)
class FetchReport:
    """Outcome of a fetch command, including captures written in degraded mode."""

    capture_path: Path
    files_captured: int
    errors: tuple[SnapshotError, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class HttpRequestFailure(Exception):
    """Structured terminal failure from the shared retrying HTTP path."""

    attempts: int
    error_type: str
    status_code: int | None
    response_headers: Mapping[str, str]


@dataclass(frozen=True)
class _Game:
    row_number: int
    stadium: Stadium
    kickoff_at: datetime


def fetch_odds(
    snapshot_root: Path,
    season: int,
    week: int,
    *,
    api_key: str | None,
    client: httpx.Client | None = None,
    observed_at: datetime | None = None,
    sleep: Sleeper = time.sleep,
) -> FetchReport:
    """Fetch current NFL spreads/totals and capture the response bytes verbatim."""

    snapshot_week_path(snapshot_root, season, week)
    capture_time = _utc(observed_at or datetime.now(UTC))
    params = {
        "apiKey": api_key or "",
        "regions": "us",
        "markets": "spreads,totals",
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    request_url = _redacted_url(ODDS_API_URL, params)
    payloads: list[CapturePayload] = []
    requests: list[SnapshotRequest] = []
    errors: list[SnapshotError] = []

    if not api_key:
        errors.append(
            SnapshotError(
                source=ODDS_SOURCE,
                occurred_at=capture_time,
                attempts=0,
                error_type="missing_api_key",
                message="ODDS_API_KEY is not set",
                request_url=request_url,
            )
        )
    else:
        with _http_client(client) as http_client:
            try:
                response, attempts = get_with_retry(http_client, ODDS_API_URL, params, sleep=sleep)
            except HttpRequestFailure as failure:
                errors.append(
                    _snapshot_error(
                        failure,
                        source=ODDS_SOURCE,
                        occurred_at=capture_time,
                        request_url=request_url,
                        header_names=ODDS_QUOTA_HEADERS,
                    )
                )
            else:
                filename = "odds.json"
                payloads.append(
                    CapturePayload(filename, response.content, capture_time, ODDS_SOURCE)
                )
                requests.append(
                    SnapshotRequest(
                        source=ODDS_SOURCE,
                        url=request_url,
                        observed_at=capture_time,
                        attempts=attempts,
                        status_code=response.status_code,
                        response_headers=_selected_headers(response.headers, ODDS_QUOTA_HEADERS),
                        file_path=f"odds/{filename}",
                    )
                )

    capture_path = capture_payloads(
        snapshot_root,
        season,
        week,
        CaptureKind.ODDS,
        payloads,
        requests=requests,
        errors=errors,
        captured_at=capture_time,
    )
    return FetchReport(capture_path, len(payloads), tuple(errors))


def fetch_weather(
    snapshot_root: Path,
    season: int,
    week: int,
    games_csv: Path,
    *,
    client: httpx.Client | None = None,
    observed_at: datetime | None = None,
    sleep: Sleeper = time.sleep,
) -> FetchReport:
    """Fetch point-in-time forecasts for weather-exposed stadiums in a games CSV."""

    snapshot_week_path(snapshot_root, season, week)
    capture_time = _utc(observed_at or datetime.now(UTC))
    games, input_errors = _read_games(games_csv, capture_time)
    payloads: list[CapturePayload] = []
    requests: list[SnapshotRequest] = []
    errors = list(input_errors)
    if not games:
        # A headered CSV with zero usable rows must not look like a successful capture.
        errors.append(
            SnapshotError(
                source="games-csv",
                occurred_at=capture_time,
                attempts=0,
                error_type="no_games",
                message=f"games CSV {games_csv} contains no usable game rows",
            )
        )

    with _http_client(client) as http_client:
        for game in games:
            if game.stadium.roof is RoofType.INDOOR:
                continue
            model_run_at = _forecast_model_run(capture_time)
            lead_time = int((game.kickoff_at - model_run_at).total_seconds())
            if lead_time < 0:
                errors.append(
                    SnapshotError(
                        source=WEATHER_SOURCE,
                        occurred_at=capture_time,
                        attempts=0,
                        error_type="past_kickoff",
                        message=(
                            f"games CSV row {game.row_number} kickoff precedes the "
                            "forecast model run"
                        ),
                    )
                )
                continue

            params = _weather_params(game, model_run_at)
            request_url = _redacted_url(WEATHER_API_URL, params)
            filename = f"{game.row_number:02d}_{_slug(game.stadium.name)}.json"
            try:
                response, attempts = get_with_retry(
                    http_client, WEATHER_API_URL, params, sleep=sleep
                )
            except HttpRequestFailure as failure:
                errors.append(
                    _snapshot_error(
                        failure,
                        source=WEATHER_SOURCE,
                        occurred_at=capture_time,
                        request_url=request_url,
                    )
                )
                continue

            payloads.append(
                CapturePayload(filename, response.content, capture_time, WEATHER_SOURCE)
            )
            requests.append(
                SnapshotRequest(
                    source=WEATHER_SOURCE,
                    url=request_url,
                    observed_at=capture_time,
                    attempts=attempts,
                    status_code=response.status_code,
                    file_path=f"weather/{filename}",
                    stadium=game.stadium.name,
                    stadium_table_version=STADIUM_TABLE_VERSION,
                    kickoff_at=game.kickoff_at,
                    forecast_model_run_at=model_run_at,
                    forecast_lead_time_seconds=lead_time,
                )
            )

    capture_path = capture_payloads(
        snapshot_root,
        season,
        week,
        CaptureKind.WEATHER,
        payloads,
        requests=requests,
        errors=errors,
        captured_at=capture_time,
    )
    return FetchReport(capture_path, len(payloads), tuple(errors))


def get_with_retry(
    client: httpx.Client,
    url: str,
    params: Mapping[str, str],
    *,
    sleep: Sleeper,
) -> tuple[httpx.Response, int]:
    last_error: httpx.HTTPError | None = None
    last_response: httpx.Response | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.get(url, params=params)
            last_response = response
            response.raise_for_status()
        except httpx.HTTPError as error:
            last_error = error
            # A non-429 4xx is deterministic; retrying burns quota against a response
            # that can never succeed.
            deterministic = (
                isinstance(error, httpx.HTTPStatusError)
                and 400 <= error.response.status_code < 500
                and error.response.status_code != 429
            )
            if not deterministic and attempt < MAX_ATTEMPTS:
                sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            break
        else:
            return response, attempt

    assert last_error is not None
    raise HttpRequestFailure(
        attempts=attempt,
        error_type=type(last_error).__name__,
        status_code=None if last_response is None else last_response.status_code,
        response_headers={} if last_response is None else dict(last_response.headers),
    )


@contextmanager
def _http_client(client: httpx.Client | None) -> Iterator[httpx.Client]:
    if client is not None:
        yield client
        return
    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=False) as owned_client:
        yield owned_client


def _read_games(
    games_csv: Path, occurred_at: datetime
) -> tuple[tuple[_Game, ...], tuple[SnapshotError, ...]]:
    games: list[_Game] = []
    errors: list[SnapshotError] = []
    try:
        source_file = games_csv.open(newline="", encoding="utf-8-sig")
    except OSError as error:
        raise ValueError(f"cannot read games CSV {games_csv}: {error}") from error

    with source_file:
        reader = csv.DictReader(source_file)
        if reader.fieldnames is None:
            raise ValueError("games CSV must have a header row")
        for row_number, row in enumerate(reader, start=2):
            stadium_name = _first_value(row, "stadium", "stadium_name")
            home_team = _first_value(row, "home_team", "host_team")
            kickoff_text = _first_value(row, "kickoff", "kickoff_at", "commence_time")
            try:
                if stadium_name is None and home_team is None:
                    raise ValueError("missing stadium or home_team")
                stadium = (
                    find_stadium(stadium_name)
                    if stadium_name is not None
                    else find_stadium_for_team(home_team or "")
                )
                if stadium is None:
                    supplied_venue = stadium_name if stadium_name is not None else home_team
                    raise ValueError(f"unknown stadium or home_team: {supplied_venue}")
                if kickoff_text is None:
                    raise ValueError("missing kickoff")
                kickoff_at = _parse_timestamp(kickoff_text)
            except ValueError as error:
                errors.append(
                    SnapshotError(
                        source="games-csv",
                        occurred_at=occurred_at,
                        attempts=0,
                        error_type="invalid_game",
                        message=f"games CSV row {row_number}: {error}",
                    )
                )
                continue
            games.append(_Game(row_number, stadium, kickoff_at))

    return tuple(games), tuple(errors)


def _first_value(row: Mapping[str, str | None], *names: str) -> str | None:
    # csv.DictReader parks trailing extra cells under a None key (and as a list value);
    # a hand-edited games CSV with a stray comma must not crash the whole fetch.
    normalized = {
        key.strip().casefold(): value
        for key, value in row.items()
        if isinstance(key, str) and (value is None or isinstance(value, str))
    }
    for name in names:
        value = normalized.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _parse_timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"invalid ISO 8601 kickoff: {value}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("kickoff must include a timezone offset")
    return parsed.astimezone(UTC)


def _forecast_model_run(observed_at: datetime) -> datetime:
    available_at = observed_at - MODEL_AVAILABILITY_LAG
    cycle_hour = available_at.hour - (available_at.hour % 6)
    return available_at.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)


def _weather_params(game: _Game, model_run_at: datetime) -> dict[str, str]:
    # The single-runs API rejects start_date/end_date when a pinned `run` is given; the
    # response spans the run's default horizon, which always covers the coming kickoff.
    return {
        "latitude": str(game.stadium.latitude),
        "longitude": str(game.stadium.longitude),
        "run": model_run_at.strftime("%Y-%m-%dT%H:%M"),
        "models": "gfs_seamless",
        "hourly": ",".join(WEATHER_HOURLY_FIELDS),
        "timezone": "UTC",
    }


def _redacted_url(base_url: str, params: Mapping[str, str]) -> str:
    combined = httpx.URL(base_url, params=params)
    parsed = urlsplit(str(combined))
    redacted_query = urlencode(
        [
            (key, "REDACTED" if key.casefold() in {"apikey", "api_key", "key", "token"} else value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, redacted_query, parsed.fragment))


def _selected_headers(headers: Mapping[str, str], names: tuple[str, ...]) -> dict[str, str]:
    normalized = {key.casefold(): value for key, value in headers.items()}
    return {name: normalized[name] for name in names if name in normalized}


def _snapshot_error(
    failure: HttpRequestFailure,
    *,
    source: str,
    occurred_at: datetime,
    request_url: str,
    header_names: tuple[str, ...] = (),
) -> SnapshotError:
    status = "" if failure.status_code is None else f" (HTTP {failure.status_code})"
    return SnapshotError(
        source=source,
        occurred_at=occurred_at,
        attempts=failure.attempts,
        error_type=failure.error_type,
        message=f"request failed after {failure.attempts} attempts{status}",
        request_url=request_url,
        response_headers=_selected_headers(failure.response_headers, header_names),
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must include a timezone")
    return value.astimezone(UTC)
