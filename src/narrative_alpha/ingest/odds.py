"""The Odds API v4 spreads/totals, reviewed against the 2026-09-01 capture."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field, StrictInt, field_validator

from narrative_alpha.identity.normalization import team_code_from_name, team_code_variants
from narrative_alpha.ingest.game_inputs import (
    GameInputIngestError,
    GameInputLoadReport,
    insert_observation,
    verified_capture,
)
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.snapshots import CaptureKind
from narrative_alpha.snapshots.fetch import ODDS_SOURCE

ODDS_FORMAT_VERSION = "the-odds-api-v4-spreads-totals-v1"


class Outcome(BaseModel):
    name: str
    point: float = Field(allow_inf_nan=False, strict=True)
    price: StrictInt


class ApiTimestampModel(BaseModel):
    @field_validator("last_update", "commence_time", mode="before", check_fields=False)
    @classmethod
    def iso_timestamp(cls, value: object) -> datetime:
        if not isinstance(value, str):
            raise ValueError("API timestamps must be ISO 8601 strings, not epoch numbers")
        return ensure_utc(datetime.fromisoformat(value))


class Market(ApiTimestampModel):
    key: str
    last_update: datetime
    outcomes: list[Outcome]


class Bookmaker(BaseModel):
    key: str = Field(min_length=1)
    markets: list[Market]


class Event(ApiTimestampModel):
    id: str
    home_team: str
    away_team: str
    commence_time: datetime
    bookmakers: list[dict[str, object]]


def parse_bookmaker(raw: dict[str, object], event: Event) -> dict[str, object]:
    book = Bookmaker.model_validate(raw)
    markets = {market.key: market for market in book.markets}
    if len(markets) != len(book.markets) or set(markets) != {"spreads", "totals"}:
        raise ValueError("requires exactly one spreads and one totals market")
    spread, total = markets["spreads"], markets["totals"]
    # The table has one publication timestamp. Refuse asynchronous markets rather than
    # silently assigning one market's time to the other.
    published = ensure_utc(spread.last_update)
    if published != ensure_utc(total.last_update):
        raise ValueError("spreads and totals last_update disagree")
    sides = {item.name: item for item in spread.outcomes}
    totals = {item.name: item for item in total.outcomes}
    if len(spread.outcomes) != 2 or set(sides) != {event.home_team, event.away_team}:
        raise ValueError("spread outcomes do not name both event teams exactly once")
    if len(total.outcomes) != 2 or set(totals) != {"Over", "Under"}:
        raise ValueError("totals require one Over and one Under")
    home, away = sides[event.home_team], sides[event.away_team]
    over, under = totals["Over"], totals["Under"]
    if home.point != -away.point:
        raise ValueError("home and away spreads disagree (must be exact opposites)")
    if over.point != under.point:
        raise ValueError("Over and Under total points disagree")
    if any(abs(item.price) < 100 for item in (home, away, over, under)):
        raise ValueError("prices must be American integers with absolute value >= 100")
    return dict(
        sportsbook=book.key,
        home_spread=home.point,
        away_spread=away.point,
        total=over.point,
        home_spread_price=home.price,
        away_spread_price=away.price,
        over_price=over.price,
        under_price=under.price,
        published_at=utc_timestamp(published),
    )


def load_odds_capture(
    connection: sqlite3.Connection,
    capture_path: Path,
    *,
    season: int,
    week: int,
    ingested_at: datetime | None = None,
) -> GameInputLoadReport:
    manifest, records = verified_capture(capture_path, season, week, CaptureKind.ODDS, ODDS_SOURCE)
    ingestion = ensure_utc(ingested_at or datetime.now(UTC))
    errors = [f"capture error [{e.error_type}]: {e.message}" for e in manifest.errors]
    matched: set[int] = set()
    seen = inserted = duplicates = rejected = unmatched = 0
    for record in records:
        raw = json.loads((capture_path / record.path).read_bytes())
        if not isinstance(raw, list):
            raise GameInputIngestError("odds body must be an event array")
        for index, item in enumerate(raw):
            seen += 1
            label = f"{record.path} event {index + 1}"
            if isinstance(item, dict):
                label += (
                    f" {item.get('id', '?')} {item.get('away_team', '?')}@"
                    f"{item.get('home_team', '?')}"
                )
            try:
                event = Event.model_validate(item)
                kickoff = utc_timestamp(event.commence_time)
                home = team_code_from_name(event.home_team)
                away = team_code_from_name(event.away_team)
                if home is None or away is None or home == away:
                    raise ValueError("unknown or identical event teams")
                home_codes, away_codes = team_code_variants(home), team_code_variants(away)
                games = connection.execute(
                    "SELECT g.game_id, g.kickoff_at FROM games g "
                    "JOIN teams h ON h.team_id = g.home_team_id "
                    "JOIN teams a ON a.team_id = g.away_team_id "
                    "WHERE g.season = ? AND g.week = ? "
                    f"AND upper(h.abbreviation) IN ({','.join('?' for _ in home_codes)}) "
                    f"AND upper(a.abbreviation) IN ({','.join('?' for _ in away_codes)})",
                    (season, week, *home_codes, *away_codes),
                ).fetchall()
                games = [
                    game
                    for game in games
                    if utc_timestamp(datetime.fromisoformat(str(game[1]))) == kickoff
                ]
                if not games:
                    # The feed lists every upcoming NFL game; the store only has the
                    # games a salary export created. Count it, do not call it an error.
                    unmatched += 1
                    continue
                if len(games) != 1:
                    raise ValueError(
                        f"teams/kickoff match {len(games)} games; expected exactly one"
                    )
                if not event.bookmakers:
                    raise ValueError("event has no bookmakers")
            except ValueError as error:
                rejected += 1
                errors.append(f"{label}: {error}")
                continue
            game_id = int(games[0][0])
            matched.add(game_id)
            for book in event.bookmakers:
                try:
                    content = parse_bookmaker(book, event)
                    sportsbook = content.pop("sportsbook")
                    observed = utc_timestamp(record.observed_at)
                    result = insert_observation(
                        connection,
                        "odds_snapshots",
                        dict(
                            source="the-odds-api",
                            game_id=game_id,
                            sportsbook=sportsbook,
                            observed_at=observed,
                        ),
                        content
                        | dict(
                            response_file_sha256=record.sha256,
                            source_version=ODDS_FORMAT_VERSION,
                            valid_from=observed,
                        ),
                        ingested_at=ingestion,
                    )
                    inserted += int(result)
                    duplicates += int(not result)
                except ValueError as error:
                    rejected += 1
                    errors.append(f"{label} bookmaker {book.get('key', '?')}: {error}")
    return GameInputLoadReport(
        files_seen=len(records),
        rows_seen=seen,
        games_matched=len(matched),
        rows_inserted=inserted,
        duplicate_rows=duplicates,
        rejected_rows=rejected,
        unmatched_rows=unmatched,
        errors=tuple(errors),
        notes=(
            (f"{unmatched} event(s) matched no ingested game for {season} week {week}",)
            if unmatched
            else ()
        ),
    )
