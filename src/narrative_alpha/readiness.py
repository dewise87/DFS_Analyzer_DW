"""Slate input readiness: is this slate's pool actually there at the decision instant?

`na-ops status` proves that a captured file's hash reached the store. That is not the same
question as "can I build this slate": candidate selection joins salaries to projections, so
every salaried player without a projection row simply vanishes from the pool, and a slate
with 40% projection coverage optimizes as happily as one with 100%. This module measures
the pool the build would actually get, at one explicit instant, using the same
observed/ingested/valid cutoffs candidate selection uses, and names every threshold it
misses.

A leaf module on purpose (standard library plus the stadium table): `build` imports it, and
`build` sits below `narrative_alpha.ops` in the import graph — `ops.__init__` reaches
`build` through the dashboard and slate lanes. The operator surfaces (`na-ops readiness`,
the status screen, the dashboard page) import *this*, not the other way around.

Nothing here writes. The build's refusal is the only side effect anywhere downstream, and
it happens before a single artifact is written.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from narrative_alpha.ingest.availability import inactive_salary_status
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.snapshots.stadiums import RoofType, find_stadium, find_stadium_for_team

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _shipped(relative: str) -> Path:
    """The shipped config file: under the source tree when run from it, else the cwd."""

    candidate = _REPOSITORY_ROOT / relative
    return candidate if candidate.is_file() else Path(relative)


DEFAULT_READINESS_CONFIG_PATH = _shipped("config/readiness.toml")

# The decision manifest entry and the file it names inside a decision's artifact directory.
READINESS_ARTIFACT_KIND: Literal["readiness"] = "readiness"
READINESS_ARTIFACT_FILENAME = "readiness.json"

# How many players one list names before it collapses to a total.
MAX_LISTED_PLAYERS = 25

PROJECTION_COVERAGE = "projection_coverage"
PROJECTION_AGE = "projection_age"
OWNERSHIP_COVERAGE = "ownership_coverage"
OWNERSHIP_COVERAGE_CAPTAIN = "ownership_coverage_captain"
OWNERSHIP_COVERAGE_FLEX = "ownership_coverage_flex"
ODDS_COVERAGE = "odds_coverage"
WEATHER_COVERAGE = "weather_coverage"

# Every name `--accept-readiness` will admit. A typo must be refused loudly rather than
# quietly failing to except the failure the operator meant to except.
READINESS_CHECK_NAMES = frozenset(
    {
        PROJECTION_COVERAGE,
        PROJECTION_AGE,
        OWNERSHIP_COVERAGE,
        OWNERSHIP_COVERAGE_CAPTAIN,
        OWNERSHIP_COVERAGE_FLEX,
        ODDS_COVERAGE,
        WEATHER_COVERAGE,
    }
)

_OWNERSHIP_CHECK_BY_ROLE = {
    "classic": OWNERSHIP_COVERAGE,
    "captain": OWNERSHIP_COVERAGE_CAPTAIN,
    "flex": OWNERSHIP_COVERAGE_FLEX,
}


class ReadinessError(RuntimeError):
    """Raised when readiness cannot be read or its configuration is unusable."""


class ReadinessConfigError(ReadinessError):
    """Raised when the readiness threshold file is missing or internally inconsistent."""


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadinessConfig:
    """The thresholds in force, and the exact bytes they were read from."""

    config_version: str
    config_sha256: str
    minimum_projection_coverage: float
    minimum_ownership_coverage: float
    maximum_projection_age_minutes: int
    maximum_projection_age_minutes_showdown: int
    odds_required: bool
    weather_required: bool
    weather_outdoor_only: bool
    raw_bytes: bytes = b""

    def maximum_projection_age(self, slate_type: str) -> timedelta:
        minutes = (
            self.maximum_projection_age_minutes_showdown
            if slate_type == "showdown"
            else self.maximum_projection_age_minutes
        )
        return timedelta(minutes=minutes)


def load_readiness_config(
    path: Path = DEFAULT_READINESS_CONFIG_PATH,
) -> ReadinessConfig:
    """Load and validate the exact versioned TOML whose bytes a decision records."""

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ReadinessConfigError(f"cannot read readiness config {path}: {error}") from error
    return load_readiness_config_bytes(raw, source=str(path))


def load_readiness_config_bytes(raw: bytes, *, source: str) -> ReadinessConfig:
    """Validate configuration bytes — the shipped file, or a frozen decision's copy."""

    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ReadinessConfigError(f"readiness config is not valid UTF-8 TOML: {source}") from error
    config_version = _text(parsed, "config_version")
    coverage = _table(parsed, "coverage")
    freshness = _table(parsed, "freshness")
    market = _table(parsed, "market")
    return ReadinessConfig(
        config_version=config_version,
        config_sha256=hashlib.sha256(raw).hexdigest(),
        minimum_projection_coverage=_fraction(coverage, "minimum_projection_coverage"),
        minimum_ownership_coverage=_fraction(coverage, "minimum_ownership_coverage"),
        maximum_projection_age_minutes=_positive_int(freshness, "maximum_projection_age_minutes"),
        maximum_projection_age_minutes_showdown=_positive_int(
            freshness, "maximum_projection_age_minutes_showdown"
        ),
        odds_required=_flag(market, "odds_required"),
        weather_required=_flag(market, "weather_required"),
        weather_outdoor_only=_flag(market, "weather_outdoor_only"),
        raw_bytes=raw,
    )


def readiness_config_payload(config: ReadinessConfig) -> dict[str, object]:
    """The thresholds as JSON, so a frozen decision carries the ruleset it was judged by."""

    return {
        "config_version": config.config_version,
        "config_sha256": config.config_sha256,
        "minimum_projection_coverage": _rounded(config.minimum_projection_coverage),
        "minimum_ownership_coverage": _rounded(config.minimum_ownership_coverage),
        "maximum_projection_age_minutes": config.maximum_projection_age_minutes,
        "maximum_projection_age_minutes_showdown": (
            config.maximum_projection_age_minutes_showdown
        ),
        "odds_required": config.odds_required,
        "weather_required": config.weather_required,
        "weather_outdoor_only": config.weather_outdoor_only,
    }


def readiness_config_from_payload(payload: Mapping[str, object]) -> ReadinessConfig:
    """Rebuild the thresholds a decision was judged under from its frozen artifact.

    The frozen bytes carry the resolved numbers, so a replay is judged by the ruleset in
    force when the decision was made rather than by whatever `config/readiness.toml` says
    today. ``raw_bytes`` stays empty: the artifact holds the values, not the file.
    """

    try:
        return ReadinessConfig(
            config_version=str(payload["config_version"]),
            config_sha256=str(payload["config_sha256"]),
            minimum_projection_coverage=float(payload["minimum_projection_coverage"]),  # type: ignore[arg-type]
            minimum_ownership_coverage=float(payload["minimum_ownership_coverage"]),  # type: ignore[arg-type]
            maximum_projection_age_minutes=int(payload["maximum_projection_age_minutes"]),  # type: ignore[call-overload]
            maximum_projection_age_minutes_showdown=int(
                payload["maximum_projection_age_minutes_showdown"]  # type: ignore[call-overload]
            ),
            odds_required=bool(payload["odds_required"]),
            weather_required=bool(payload["weather_required"]),
            weather_outdoor_only=bool(payload["weather_outdoor_only"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ReadinessConfigError(
            f"frozen readiness thresholds are incomplete or malformed: {error}"
        ) from error


# --------------------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlayerLine:
    """One salaried player, named well enough for an operator to act on the line."""

    player_id: int
    name: str
    team: str
    position: str
    salary: int


@dataclass(frozen=True)
class SourceCoverage:
    """What one named source covers of the active salaried pool at the instant."""

    source: str
    covered: int
    missing: int
    latest_observed_at: datetime | None
    latest_ingested_at: datetime | None


@dataclass(frozen=True)
class InputCoverage:
    """One per-player input's coverage of the active salaried pool."""

    input: str
    eligible: int
    covered: int
    missing: int
    latest_observed_at: datetime | None
    latest_ingested_at: datetime | None
    by_source: tuple[SourceCoverage, ...]

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(item.source for item in self.by_source)

    @property
    def fraction(self) -> float:
        return 0.0 if self.eligible == 0 else self.covered / self.eligible


@dataclass(frozen=True)
class OwnershipRoleCoverage:
    """One ownership role, split by which of the two ownership sources a player would get.

    The build prefers a dedicated `ownership_baselines` row and falls back, per player, to
    ownership embedded in a projection file. Those are different numbers from different
    vendors, so a pool that mixes them is a fact the operator has to be told, not an
    average to be quietly taken.
    """

    role: str
    eligible: int
    dedicated: int
    embedded: int
    missing: int
    embedded_is_fallback: bool
    dedicated_sources: tuple[str, ...]
    embedded_sources: tuple[str, ...]
    latest_observed_at: datetime | None
    latest_ingested_at: datetime | None
    embedded_players: tuple[PlayerLine, ...]
    embedded_players_total: int
    missing_players: tuple[PlayerLine, ...]
    missing_players_total: int

    @property
    def covered(self) -> int:
        return self.dedicated + self.embedded

    @property
    def fraction(self) -> float:
        return 0.0 if self.eligible == 0 else self.covered / self.eligible


@dataclass(frozen=True)
class GameLine:
    """One game of the slate, with the roof classification weather depends on."""

    external_game_id: str
    matchup: str
    kickoff_at: datetime
    stadium_name: str | None
    roof: str


@dataclass(frozen=True)
class GameCoverage:
    """A per-game input: odds for every game, weather for the ones played outdoors."""

    input: str
    required: int
    covered: int
    missing: int
    missing_games: tuple[GameLine, ...]
    sources: tuple[str, ...]
    latest_observed_at: datetime | None
    latest_ingested_at: datetime | None


@dataclass(frozen=True)
class ReadinessCheck:
    """One named threshold, and the number that met or missed it."""

    name: str
    passed: bool
    observed: str
    threshold: str
    detail: str


@dataclass(frozen=True)
class SlateReadiness:
    """Everything one slate's inputs look like at one explicit instant."""

    slate_id: int
    external_slate_id: str
    site: str
    slate_type: str
    season: int
    week: int
    name: str
    locks_at: datetime
    as_of: datetime
    config_version: str
    config_sha256: str
    salaried_players: int
    inactive_salary_players: int
    ruled_out_players: int
    active_players: int
    active_players_without_game: int
    latest_salary_observed_at: datetime | None
    latest_salary_ingested_at: datetime | None
    projections: InputCoverage
    ownership: tuple[OwnershipRoleCoverage, ...]
    odds: GameCoverage
    weather: GameCoverage
    projected_stats: InputCoverage
    games: tuple[GameLine, ...]
    unprojected_players: tuple[PlayerLine, ...]
    unprojected_players_total: int
    checks: tuple[ReadinessCheck, ...]

    @property
    def failures(self) -> tuple[ReadinessCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)

    @property
    def ready(self) -> bool:
        return not self.failures

    @property
    def summary_line(self) -> str:
        """The one line the status screen and the dashboard both show."""

        failures = self.failures
        if not failures:
            return "READY"
        first = failures[0]
        suffix = "" if len(failures) == 1 else f" (+{len(failures) - 1} more)"
        return f"NOT READY — {first.name}: {first.detail}{suffix}"


@dataclass(frozen=True)
class FrozenReadiness:
    """A decision's frozen readiness report, beside a re-measurement of the same instant.

    ``payload`` is what the decision was actually judged by; ``measured`` is what the store
    says now about that same instant, under the same frozen thresholds. They agree unless
    rows were backfilled afterwards, and ``store_matches`` says which.
    """

    payload: Mapping[str, object]
    accepted_failures: tuple[str, ...]
    config: ReadinessConfig
    measured: SlateReadiness
    store_matches: bool

    @property
    def summary(self) -> str:
        return str(self.payload.get("summary", "unrecorded"))

    @property
    def ready(self) -> bool:
        return bool(self.payload.get("ready", False))

    @property
    def failed_checks(self) -> tuple[str, ...]:
        raw = self.payload.get("failed_checks")
        return () if not isinstance(raw, list) else tuple(str(item) for item in raw)

    @property
    def active_players(self) -> int:
        raw = self.payload.get("active_players")
        return int(raw) if isinstance(raw, int) else 0

    @property
    def projection_coverage(self) -> str:
        raw = self.payload.get("projections")
        if not isinstance(raw, Mapping):
            return "unrecorded"
        return f"{raw.get('covered')} of {raw.get('eligible')}"


# --------------------------------------------------------------------------------------
# The read
# --------------------------------------------------------------------------------------


def collect_slate_readiness(
    connection: sqlite3.Connection,
    *,
    slate_id: int,
    as_of: datetime,
    config: ReadinessConfig | None = None,
    config_path: Path = DEFAULT_READINESS_CONFIG_PATH,
) -> SlateReadiness:
    """Measure one slate's inputs at ``as_of`` and judge them against the thresholds.

    Every count uses the same observed/ingested/valid bounds candidate selection uses, so
    a row imported after the instant cannot make a slate look ready at it.
    """

    thresholds = config or load_readiness_config(config_path)
    cutoff = ensure_utc(as_of)
    stamp = utc_timestamp(cutoff)
    slate = _slate_row(connection, slate_id=slate_id, stamp=stamp)
    site = str(slate["site"])
    slate_type = str(slate["slate_type"])

    salary_rows = _salary_rows(connection, slate_id=slate_id, site=site, stamp=stamp)
    if not salary_rows:
        raise ReadinessError(
            f"slate {slate_id} has no {site} salary row eligible at {stamp}; ingest the "
            "salary capture before asking whether the slate is ready"
        )
    active = tuple(row for row in salary_rows if not _ruled_out(row))
    active_ids = frozenset(int(row["player_id"]) for row in active)
    lines = {int(row["player_id"]): _player_line(row) for row in active}

    projections, projected_ids, embedded_ownership, embedded_sources = _projection_coverage(
        connection,
        slate_id=slate_id,
        site=site,
        stamp=stamp,
        active_ids=active_ids,
    )
    ownership = _ownership_coverage(
        connection,
        slate_id=slate_id,
        site=site,
        stamp=stamp,
        slate_type=slate_type,
        active_ids=active_ids,
        lines=lines,
        embedded_ownership=embedded_ownership,
        embedded_sources=embedded_sources,
    )
    games_by_id = _game_lines(
        connection,
        stamp=stamp,
        game_ids=frozenset(
            int(row["game_id"]) for row in salary_rows if row["game_id"] is not None
        ),
    )
    odds = _game_input_coverage(
        connection,
        stamp=stamp,
        games=games_by_id,
        input_name="odds",
        table="odds_snapshots",
        required_ids=frozenset(games_by_id),
    )
    weather = _game_input_coverage(
        connection,
        stamp=stamp,
        games=games_by_id,
        input_name="weather",
        table="weather_snapshots",
        required_ids=frozenset(
            game_id
            for game_id, line in games_by_id.items()
            if not thresholds.weather_outdoor_only or line.roof != "indoor"
        ),
    )
    projected_stats = _projected_stats_coverage(
        connection,
        stamp=stamp,
        season=int(slate["season"]),
        week=int(slate["week"]),
        active_ids=active_ids,
    )
    unprojected = _ordered_lines(
        lines[player_id] for player_id in active_ids - projected_ids
    )

    readiness = SlateReadiness(
        slate_id=slate_id,
        external_slate_id=str(slate["external_slate_id"]),
        site=site,
        slate_type=slate_type,
        season=int(slate["season"]),
        week=int(slate["week"]),
        name=str(slate["name"]),
        locks_at=_parse_stamp(str(slate["locks_at"])),
        as_of=cutoff,
        config_version=thresholds.config_version,
        config_sha256=thresholds.config_sha256,
        salaried_players=len(salary_rows),
        inactive_salary_players=sum(
            1 for row in salary_rows if inactive_salary_status(row["player_status"])
        ),
        ruled_out_players=len(salary_rows) - len(active),
        active_players=len(active),
        active_players_without_game=sum(1 for row in active if row["game_id"] is None),
        latest_salary_observed_at=_newest(row["observed_at"] for row in salary_rows),
        latest_salary_ingested_at=_newest(row["ingested_at"] for row in salary_rows),
        projections=projections,
        ownership=ownership,
        odds=odds,
        weather=weather,
        projected_stats=projected_stats,
        games=tuple(games_by_id.values()),
        unprojected_players=unprojected[:MAX_LISTED_PLAYERS],
        unprojected_players_total=len(unprojected),
        checks=(),
    )
    checks = _checks(readiness, thresholds)
    return replace(readiness, checks=checks)


def _checks(readiness: SlateReadiness, config: ReadinessConfig) -> tuple[ReadinessCheck, ...]:
    """Name every threshold, whether it passed or missed, with the number either way."""

    checks: list[ReadinessCheck] = [
        _coverage_check(
            PROJECTION_COVERAGE,
            covered=readiness.projections.covered,
            eligible=readiness.projections.eligible,
            minimum=config.minimum_projection_coverage,
            noun="active salaried player(s) with a projection from any source",
        )
    ]
    maximum_age = config.maximum_projection_age(readiness.slate_type)
    newest = readiness.projections.latest_observed_at
    if newest is None:
        checks.append(
            ReadinessCheck(
                name=PROJECTION_AGE,
                passed=False,
                observed="no projection",
                threshold=_humanize(maximum_age),
                detail=(
                    "no projection row is eligible at the decision instant, so the pool "
                    "has no age at all"
                ),
            )
        )
    else:
        age = readiness.as_of - newest
        checks.append(
            ReadinessCheck(
                name=PROJECTION_AGE,
                passed=age <= maximum_age,
                observed=_humanize(age),
                threshold=_humanize(maximum_age),
                detail=(
                    f"the newest projection was observed {_humanize(age)} before the "
                    f"decision instant; the {readiness.slate_type} bound is "
                    f"{_humanize(maximum_age)}"
                ),
            )
        )
    for role in readiness.ownership:
        checks.append(
            _coverage_check(
                _OWNERSHIP_CHECK_BY_ROLE[role.role],
                covered=role.covered,
                eligible=role.eligible,
                minimum=config.minimum_ownership_coverage,
                noun=(
                    f"active salaried player(s) with a {role.role} ownership number "
                    f"({role.dedicated} dedicated, {role.embedded} embedded)"
                ),
            )
        )
    checks.append(
        _game_check(ODDS_COVERAGE, readiness.odds, required=config.odds_required, kind="odds")
    )
    checks.append(
        _game_check(
            WEATHER_COVERAGE,
            readiness.weather,
            required=config.weather_required,
            kind=(
                "weather for the outdoor games"
                if config.weather_outdoor_only
                else "weather for every game"
            ),
        )
    )
    return tuple(checks)


def _coverage_check(
    name: str,
    *,
    covered: int,
    eligible: int,
    minimum: float,
    noun: str,
) -> ReadinessCheck:
    if eligible == 0:
        return ReadinessCheck(
            name=name,
            passed=False,
            observed="0 of 0",
            threshold=f"{minimum:.2%}",
            detail="no active salaried player remains at the instant, so nothing is covered",
        )
    fraction = covered / eligible
    return ReadinessCheck(
        name=name,
        passed=fraction >= minimum,
        observed=f"{fraction:.2%} ({covered} of {eligible})",
        threshold=f"{minimum:.2%}",
        detail=f"{covered} of {eligible} {noun} — {fraction:.2%} against a {minimum:.2%} floor",
    )


def _game_check(
    name: str,
    coverage: GameCoverage,
    *,
    required: bool,
    kind: str,
) -> ReadinessCheck:
    if not required:
        return ReadinessCheck(
            name=name,
            passed=True,
            observed=f"{coverage.covered} of {coverage.required} game(s)",
            threshold="not required",
            detail=f"{kind} is not required by this readiness configuration",
        )
    if coverage.required == 0:
        return ReadinessCheck(
            name=name,
            passed=True,
            observed="0 of 0 game(s)",
            threshold="every required game",
            detail=f"no game on this slate needs {kind}",
        )
    named = ", ".join(game.matchup for game in coverage.missing_games[:5])
    remaining = len(coverage.missing_games) - 5
    suffix = "" if remaining <= 0 else f", +{remaining} more"
    return ReadinessCheck(
        name=name,
        passed=coverage.missing == 0,
        observed=f"{coverage.covered} of {coverage.required} game(s)",
        threshold="every required game",
        detail=(
            f"{kind}: every one of {coverage.required} game(s) is covered"
            if coverage.missing == 0
            else f"{kind}: {coverage.missing} of {coverage.required} game(s) missing — "
            f"{named}{suffix}"
        ),
    )


# --------------------------------------------------------------------------------------
# Store reads — every one bounded by the same cutoffs candidate selection uses
# --------------------------------------------------------------------------------------


def _bounds(alias: str) -> str:
    return (
        f"rtrim({alias}.observed_at, 'Z') <= rtrim(:as_of, 'Z')\n"
        f"AND rtrim({alias}.ingested_at, 'Z') <= rtrim(:as_of, 'Z')\n"
        f"AND rtrim({alias}.valid_from, 'Z') <= rtrim(:as_of, 'Z')\n"
        f"AND ({alias}.valid_to IS NULL OR rtrim({alias}.valid_to, 'Z') > rtrim(:as_of, 'Z'))"
    )


def _slate_row(
    connection: sqlite3.Connection, *, slate_id: int, stamp: str
) -> sqlite3.Row:
    row = connection.execute(
        f"""
        SELECT * FROM slates AS s
        WHERE s.slate_id = :slate_id AND {_bounds("s")}
        ORDER BY rtrim(s.observed_at, 'Z') DESC
        LIMIT 1
        """,
        {"slate_id": slate_id, "as_of": stamp},
    ).fetchone()
    if row is None:
        raise ReadinessError(f"slate {slate_id} does not exist as of {stamp}")
    if not isinstance(row, sqlite3.Row):
        raise ReadinessError("readiness reads require a sqlite3.Row connection row_factory")
    return row


def _salary_rows(
    connection: sqlite3.Connection, *, slate_id: int, site: str, stamp: str
) -> tuple[sqlite3.Row, ...]:
    """The latest salary row per player, with the availability decision that governs it.

    ``is_ruled_out`` below mirrors `candidate_selection._candidate_from_rows` exactly: an
    official availability decision wins over a salary-feed label in both directions, so the
    pool measured here is the pool the build would treat as usable.
    """

    return tuple(
        connection.execute(
            f"""
            WITH ranked_salaries AS (
                SELECT s.*,
                       row_number() OVER (
                           PARTITION BY s.player_id
                           ORDER BY rtrim(s.observed_at, 'Z') DESC, s.salary_id DESC
                       ) AS version_rank
                FROM salaries AS s
                WHERE s.slate_id = :slate_id AND {_bounds("s")}
            ),
            ranked_availability AS (
                SELECT pa.*,
                       row_number() OVER (
                           PARTITION BY pa.slate_id, pa.site, pa.player_id
                           ORDER BY rtrim(pa.observed_at, 'Z') DESC, pa.availability_id DESC
                       ) AS version_rank
                FROM player_availability AS pa
                WHERE pa.slate_id = :slate_id AND pa.site = :site AND {_bounds("pa")}
            )
            SELECT s.player_id, s.salary, s.player_status, s.game_id,
                   s.observed_at, s.ingested_at,
                   p.canonical_name, p.position,
                   team.abbreviation AS team,
                   pa.availability_status
            FROM ranked_salaries AS s
            JOIN players AS p ON p.player_id = s.player_id AND {_bounds("p")}
            JOIN teams AS team ON team.team_id = s.team_id AND {_bounds("team")}
            LEFT JOIN ranked_availability AS pa
              ON pa.player_id = s.player_id AND pa.version_rank = 1
            WHERE s.version_rank = 1
            ORDER BY s.player_id
            """,
            {"slate_id": slate_id, "site": site, "as_of": stamp},
        ).fetchall()
    )


def _ruled_out(row: sqlite3.Row) -> bool:
    status = row["availability_status"]
    if status is not None:
        return str(status) == "unavailable"
    return inactive_salary_status(row["player_status"])


def _projection_coverage(
    connection: sqlite3.Connection,
    *,
    slate_id: int,
    site: str,
    stamp: str,
    active_ids: frozenset[int],
) -> tuple[InputCoverage, frozenset[int], frozenset[int], tuple[str, ...]]:
    """Per-source projection coverage, plus who carries ownership embedded in that file."""

    rows = connection.execute(
        f"""
        WITH ranked AS (
            SELECT ps.*,
                   row_number() OVER (
                       PARTITION BY ps.source, ps.player_id
                       ORDER BY rtrim(ps.observed_at, 'Z') DESC,
                                ps.projection_snapshot_id DESC
                   ) AS version_rank
            FROM projection_snapshots AS ps
            WHERE ps.slate_id = :slate_id AND ps.site = :site AND {_bounds("ps")}
        )
        SELECT source, player_id, ownership_projection, observed_at, ingested_at
        FROM ranked WHERE version_rank = 1
        ORDER BY source, player_id
        """,
        {"slate_id": slate_id, "site": site, "as_of": stamp},
    ).fetchall()
    by_source: dict[str, list[sqlite3.Row]] = {}
    covered_any: set[int] = set()
    embedded: set[int] = set()
    embedded_sources: set[str] = set()
    for row in rows:
        player_id = int(row["player_id"])
        if player_id not in active_ids:
            continue
        by_source.setdefault(str(row["source"]), []).append(row)
        covered_any.add(player_id)
        if row["ownership_projection"] is not None:
            embedded.add(player_id)
            embedded_sources.add(str(row["source"]))
    eligible = len(active_ids)
    sources = tuple(
        SourceCoverage(
            source=source,
            covered=len(source_rows),
            missing=eligible - len(source_rows),
            latest_observed_at=_newest(row["observed_at"] for row in source_rows),
            latest_ingested_at=_newest(row["ingested_at"] for row in source_rows),
        )
        for source, source_rows in sorted(by_source.items())
    )
    coverage = InputCoverage(
        input="projections",
        eligible=eligible,
        covered=len(covered_any),
        missing=eligible - len(covered_any),
        latest_observed_at=_newest_of(item.latest_observed_at for item in sources),
        latest_ingested_at=_newest_of(item.latest_ingested_at for item in sources),
        by_source=sources,
    )
    return coverage, frozenset(covered_any), frozenset(embedded), tuple(sorted(embedded_sources))


def _ownership_coverage(
    connection: sqlite3.Connection,
    *,
    slate_id: int,
    site: str,
    stamp: str,
    slate_type: str,
    active_ids: frozenset[int],
    lines: Mapping[int, PlayerLine],
    embedded_ownership: frozenset[int],
    embedded_sources: tuple[str, ...],
) -> tuple[OwnershipRoleCoverage, ...]:
    """Dedicated baselines per role, and who would fall back to an embedded number.

    Only classic has an embedded fallback: `_candidate_from_rows` reads
    ``baseline.get("classic", baseline.get("flex"))``, and showdown candidate selection
    refuses outright unless every player has both a captain and a flex baseline. So on a
    showdown slate an embedded ownership column is not a fallback, and this says so.
    """

    roles = ("captain", "flex") if slate_type == "showdown" else ("classic",)
    role_binds = ", ".join(f":role_{index}" for index, _ in enumerate(roles))
    rows = connection.execute(
        f"""
        WITH ranked AS (
            SELECT ob.*,
                   row_number() OVER (
                       PARTITION BY ob.player_id, ob.role
                       ORDER BY rtrim(ob.observed_at, 'Z') DESC,
                                rtrim(ob.ingested_at, 'Z') DESC,
                                ob.ownership_baseline_id DESC
                   ) AS baseline_rank
            FROM ownership_baselines AS ob
            WHERE ob.slate_id = :slate_id AND ob.site = :site
              AND ob.role IN ({role_binds})
              AND {_bounds("ob")}
        )
        SELECT player_id, role, source, observed_at, ingested_at
        FROM ranked WHERE baseline_rank = 1
        ORDER BY role, player_id
        """,
        {
            "slate_id": slate_id,
            "site": site,
            "as_of": stamp,
            **{f"role_{index}": role for index, role in enumerate(roles)},
        },
    ).fetchall()
    by_role: dict[str, list[sqlite3.Row]] = {role: [] for role in roles}
    for row in rows:
        if int(row["player_id"]) in active_ids:
            by_role[str(row["role"])].append(row)

    coverage: list[OwnershipRoleCoverage] = []
    for role in roles:
        role_rows = by_role[role]
        dedicated_ids = frozenset(int(row["player_id"]) for row in role_rows)
        fallback_allowed = role == "classic"
        embedded_ids = (
            frozenset(embedded_ownership - dedicated_ids) if fallback_allowed else frozenset()
        )
        missing_ids = active_ids - dedicated_ids - embedded_ids
        embedded_lines = _ordered_lines(lines[player_id] for player_id in embedded_ids)
        missing_lines = _ordered_lines(lines[player_id] for player_id in missing_ids)
        coverage.append(
            OwnershipRoleCoverage(
                role=role,
                eligible=len(active_ids),
                dedicated=len(dedicated_ids),
                embedded=len(embedded_ids),
                missing=len(missing_ids),
                embedded_is_fallback=fallback_allowed,
                dedicated_sources=tuple(sorted({str(row["source"]) for row in role_rows})),
                embedded_sources=embedded_sources if fallback_allowed else (),
                latest_observed_at=_newest(row["observed_at"] for row in role_rows),
                latest_ingested_at=_newest(row["ingested_at"] for row in role_rows),
                embedded_players=embedded_lines[:MAX_LISTED_PLAYERS],
                embedded_players_total=len(embedded_lines),
                missing_players=missing_lines[:MAX_LISTED_PLAYERS],
                missing_players_total=len(missing_lines),
            )
        )
    return tuple(coverage)


def _game_lines(
    connection: sqlite3.Connection, *, stamp: str, game_ids: frozenset[int]
) -> dict[int, GameLine]:
    """The slate's games, in kickoff order, each classified by its venue's roof.

    ``games.game_id`` is the primary key, so there is one row per game and no version
    ranking to do — the same assumption `select_candidate_scenario` makes when it joins
    salaries to games.
    """

    if not game_ids:
        return {}
    binds = {f"game_{index}": game_id for index, game_id in enumerate(sorted(game_ids))}
    placeholders = ", ".join(f":{key}" for key in binds)
    rows = connection.execute(
        f"""
        SELECT g.game_id, g.external_game_id, g.kickoff_at, g.stadium_name,
               home.abbreviation AS home_team, away.abbreviation AS away_team
        FROM games AS g
        JOIN teams AS home ON home.team_id = g.home_team_id AND {_bounds("home")}
        JOIN teams AS away ON away.team_id = g.away_team_id AND {_bounds("away")}
        WHERE g.game_id IN ({placeholders}) AND {_bounds("g")}
        ORDER BY rtrim(g.kickoff_at, 'Z'), g.external_game_id
        """,
        {"as_of": stamp, **binds},
    ).fetchall()
    return {
        int(row["game_id"]): GameLine(
            external_game_id=str(row["external_game_id"]),
            matchup=f"{row['away_team']}@{row['home_team']}",
            kickoff_at=_parse_stamp(str(row["kickoff_at"])),
            stadium_name=None if row["stadium_name"] is None else str(row["stadium_name"]),
            roof=_roof(row["stadium_name"], str(row["home_team"])),
        )
        for row in rows
    }


def _roof(stadium_name: object, home_team: str) -> str:
    """Classify a venue's exposure; an unrecognized venue is never assumed to be a dome."""

    stadium = None
    if stadium_name is not None and str(stadium_name).strip():
        stadium = find_stadium(str(stadium_name))
    if stadium is None:
        stadium = find_stadium_for_team(home_team)
    if stadium is None:
        return "unknown"
    return "indoor" if stadium.roof is RoofType.INDOOR else str(stadium.roof.value)


def _game_input_coverage(
    connection: sqlite3.Connection,
    *,
    stamp: str,
    games: Mapping[int, GameLine],
    input_name: str,
    table: str,
    required_ids: frozenset[int],
) -> GameCoverage:
    """Coverage of a per-game input. A covered *game*, never a covered player."""

    if not games:
        return GameCoverage(
            input=input_name,
            required=0,
            covered=0,
            missing=0,
            missing_games=(),
            sources=(),
            latest_observed_at=None,
            latest_ingested_at=None,
        )
    binds = {f"game_{index}": game_id for index, game_id in enumerate(sorted(games))}
    placeholders = ", ".join(f":{key}" for key in binds)
    rows = connection.execute(
        f"""
        SELECT x.game_id AS game_id, x.source AS source,
               x.observed_at AS observed_at, x.ingested_at AS ingested_at
        FROM {table} AS x
        WHERE x.game_id IN ({placeholders}) AND {_bounds("x")}
        """,
        {"as_of": stamp, **binds},
    ).fetchall()
    covered_ids = {int(row["game_id"]) for row in rows}
    missing = tuple(
        line
        for game_id, line in games.items()
        if game_id in required_ids and game_id not in covered_ids
    )
    return GameCoverage(
        input=input_name,
        required=len(required_ids),
        covered=len(required_ids & covered_ids),
        missing=len(missing),
        missing_games=missing,
        sources=tuple(sorted({str(row["source"]) for row in rows})),
        latest_observed_at=_newest(row["observed_at"] for row in rows),
        latest_ingested_at=_newest(row["ingested_at"] for row in rows),
    )


def _projected_stats_coverage(
    connection: sqlite3.Connection,
    *,
    stamp: str,
    season: int,
    week: int,
    active_ids: frozenset[int],
) -> InputCoverage:
    """Slice 46's component facts. Informational: nothing in the build reads them."""

    rows = connection.execute(
        f"""
        SELECT source, player_id,
               max(rtrim(observed_at, 'Z')) AS observed_at,
               max(rtrim(ingested_at, 'Z')) AS ingested_at
        FROM projected_stats AS x
        WHERE x.season = :season AND x.week = :week AND {_bounds("x")}
        GROUP BY source, player_id
        ORDER BY source, player_id
        """,
        {"season": season, "week": week, "as_of": stamp},
    ).fetchall()
    by_source: dict[str, list[sqlite3.Row]] = {}
    covered_any: set[int] = set()
    for row in rows:
        player_id = int(row["player_id"])
        if player_id not in active_ids:
            continue
        by_source.setdefault(str(row["source"]), []).append(row)
        covered_any.add(player_id)
    eligible = len(active_ids)
    sources = tuple(
        SourceCoverage(
            source=source,
            covered=len(source_rows),
            missing=eligible - len(source_rows),
            latest_observed_at=_newest(row["observed_at"] for row in source_rows),
            latest_ingested_at=_newest(row["ingested_at"] for row in source_rows),
        )
        for source, source_rows in sorted(by_source.items())
    )
    return InputCoverage(
        input="projected_stats",
        eligible=eligible,
        covered=len(covered_any),
        missing=eligible - len(covered_any),
        latest_observed_at=_newest_of(item.latest_observed_at for item in sources),
        latest_ingested_at=_newest_of(item.latest_ingested_at for item in sources),
        by_source=sources,
    )


# --------------------------------------------------------------------------------------
# Rendering and payloads
# --------------------------------------------------------------------------------------


def readiness_payload(readiness: SlateReadiness) -> dict[str, object]:
    """The whole report as JSON, so every surface renders the same read."""

    return {
        "slate_id": readiness.slate_id,
        "external_slate_id": readiness.external_slate_id,
        "site": readiness.site,
        "slate_type": readiness.slate_type,
        "season": readiness.season,
        "week": readiness.week,
        "name": readiness.name,
        "locks_at": utc_timestamp(readiness.locks_at),
        "as_of": utc_timestamp(readiness.as_of),
        "config_version": readiness.config_version,
        "config_sha256": readiness.config_sha256,
        "ready": readiness.ready,
        "summary": readiness.summary_line,
        "salaried_players": readiness.salaried_players,
        "inactive_salary_players": readiness.inactive_salary_players,
        "ruled_out_players": readiness.ruled_out_players,
        "active_players": readiness.active_players,
        "active_players_without_game": readiness.active_players_without_game,
        "latest_salary_observed_at": _optional_stamp(readiness.latest_salary_observed_at),
        "latest_salary_ingested_at": _optional_stamp(readiness.latest_salary_ingested_at),
        "projections": _input_payload(readiness.projections),
        "ownership": [_ownership_payload(role) for role in readiness.ownership],
        "odds": _game_payload(readiness.odds),
        "weather": _game_payload(readiness.weather),
        "projected_stats": _input_payload(readiness.projected_stats),
        "games": [_game_line_payload(game) for game in readiness.games],
        "unprojected_players": [_player_payload(line) for line in readiness.unprojected_players],
        "unprojected_players_total": readiness.unprojected_players_total,
        "checks": [
            {
                "name": check.name,
                "passed": check.passed,
                "observed": check.observed,
                "threshold": check.threshold,
                "detail": check.detail,
            }
            for check in readiness.checks
        ],
        "failed_checks": [check.name for check in readiness.failures],
    }


def readiness_artifact_bytes(
    readiness: SlateReadiness,
    *,
    accepted_failures: Sequence[str] = (),
    config: ReadinessConfig,
) -> bytes:
    """Canonical JSON for the decision artifact: the report, the ruleset, the acceptances.

    Byte-stable by construction — sorted keys, compact separators, no wall clock — so a
    replay at the same instant reproduces exactly these bytes or says why it cannot.
    """

    payload = {
        "accepted_failures": sorted(set(accepted_failures)),
        "config": readiness_config_payload(config),
        "readiness": readiness_payload(readiness),
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def frozen_readiness(
    connection: sqlite3.Connection,
    *,
    slate_id: int,
    as_of: datetime,
    artifact_bytes: bytes,
) -> FrozenReadiness:
    """Read a decision's frozen readiness, and re-measure the store at the same instant.

    The frozen report is the authority on what this decision was judged by, and its bytes
    are already pinned by the manifest hash the caller verified. This additionally
    recomputes readiness from the store at the same instant, under the *frozen* thresholds
    rather than whatever `config/readiness.toml` says today, and records whether the two
    still agree.

    A difference is reported, not refused. Readiness is a measurement of the pool, not a
    decision input: nothing the optimizer consumed comes from here, and candidate selection
    is pinned to its own manifest artifacts. A row backfilled into the store afterwards
    with an earlier ingestion stamp changes what the instant now looks like without
    changing the decision, and refusing the replay for that would break the replayability
    of every earlier decision over an unconsumed number. So the drift is named
    (``store_matches``) and carried into the memo instead.

    What *is* refused is an artifact that contradicts itself: a frozen report naming a
    failed check the decision never accepted could only come from a bypassed build guard.
    """

    try:
        parsed = json.loads(artifact_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReadinessError("frozen readiness artifact is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise ReadinessError("frozen readiness artifact must be a JSON object")
    config_payload = parsed.get("config")
    if not isinstance(config_payload, Mapping):
        raise ReadinessError("frozen readiness artifact carries no threshold configuration")
    report = parsed.get("readiness")
    if not isinstance(report, Mapping):
        raise ReadinessError("frozen readiness artifact carries no readiness report")
    accepted = _string_list(parsed.get("accepted_failures"), "accepted_failures")
    failed = _string_list(report.get("failed_checks"), "failed_checks")
    unaccepted = tuple(name for name in failed if name not in accepted)
    if unaccepted:
        raise ReadinessError(
            "the frozen decision records readiness failure(s) it never accepted: "
            + ", ".join(unaccepted)
            + " — this decision could not have passed the build guard"
        )
    config = readiness_config_from_payload(config_payload)
    measured = collect_slate_readiness(connection, slate_id=slate_id, as_of=as_of, config=config)
    rebuilt = readiness_artifact_bytes(measured, accepted_failures=accepted, config=config)
    return FrozenReadiness(
        payload=dict(report),
        accepted_failures=accepted,
        config=config,
        measured=measured,
        store_matches=rebuilt == artifact_bytes,
    )


def _string_list(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ReadinessError(f"frozen readiness {name} must be a list of check names")
    return tuple(sorted({str(item) for item in value}))


def render_readiness(
    readiness: SlateReadiness, *, accepted_failures: Sequence[str] = ()
) -> str:
    """The operator's screen: can I build this slate, and if not, what is missing."""

    accepted = frozenset(accepted_failures)
    lines = [
        "NARRATIVE ALPHA — SLATE INPUT READINESS",
        f"  slate            {readiness.slate_id} ({readiness.external_slate_id}) "
        f"{readiness.site} {readiness.slate_type} — {readiness.name}",
        f"  season/week      {readiness.season} week {readiness.week:02d}",
        f"  as of            {utc_timestamp(readiness.as_of)}",
        f"  locks at         {utc_timestamp(readiness.locks_at)}",
        f"  thresholds       {readiness.config_version} ({readiness.config_sha256[:12]})",
        "",
        f"  {readiness.summary_line}",
        "",
        "CHECKS",
    ]
    for check in readiness.checks:
        mark = "PASS" if check.passed else ("ACCEPTED" if check.name in accepted else "FAIL")
        lines.append(f"  {mark:<9} {check.name:<28} {check.detail}")
    lines.extend(
        [
            "",
            "POOL",
            f"  salaried players           {readiness.salaried_players}",
            f"  ruled out                  {readiness.ruled_out_players} "
            f"({readiness.inactive_salary_players} by salary-feed status)",
            f"  active salaried players    {readiness.active_players}",
            f"  active with no game row    {readiness.active_players_without_game}",
            f"  salaries observed/ingested {_slot(readiness.latest_salary_observed_at)} / "
            f"{_slot(readiness.latest_salary_ingested_at)}",
            "",
            "PROJECTIONS",
        ]
    )
    lines.extend(_render_input(readiness.projections))
    lines.append("")
    lines.append("OWNERSHIP")
    for role in readiness.ownership:
        lines.append(
            f"  {role.role:<12} {role.covered} of {role.eligible} covered — "
            f"{role.dedicated} dedicated, {role.embedded} embedded, {role.missing} missing"
        )
        lines.append(
            f"    dedicated sources        "
            f"{', '.join(role.dedicated_sources) or 'none'}"
        )
        if role.embedded_is_fallback:
            lines.append(
                f"    embedded fallback from   "
                f"{', '.join(role.embedded_sources) or 'none'}"
            )
        else:
            lines.append(
                "    embedded fallback        not available: showdown candidate selection "
                "requires a dedicated baseline for both roles"
            )
        lines.append(
            f"    observed/ingested        {_slot(role.latest_observed_at)} / "
            f"{_slot(role.latest_ingested_at)}"
        )
        lines.extend(
            _render_players(
                "    would use embedded ownership",
                role.embedded_players,
                role.embedded_players_total,
            )
        )
        lines.extend(
            _render_players(
                "    no ownership at all",
                role.missing_players,
                role.missing_players_total,
            )
        )
    lines.extend(["", "GAMES"])
    for coverage in (readiness.odds, readiness.weather):
        lines.append(
            f"  {coverage.input:<12} {coverage.covered} of {coverage.required} required "
            f"game(s) covered  sources {', '.join(coverage.sources) or 'none'}"
        )
        lines.append(
            f"    observed/ingested        {_slot(coverage.latest_observed_at)} / "
            f"{_slot(coverage.latest_ingested_at)}"
        )
        for game in coverage.missing_games:
            lines.append(
                f"    MISSING {game.matchup:<10} {game.external_game_id}  "
                f"kickoff {utc_timestamp(game.kickoff_at)}  roof {game.roof}"
            )
    lines.extend(["", "PROJECTED STATS (informational — not a build input)"])
    lines.extend(_render_input(readiness.projected_stats))
    lines.append("")
    lines.append("PLAYERS THE BUILD WOULD DROP (no projection from any source)")
    if not readiness.unprojected_players:
        lines.append("  none — every active salaried player has a projection")
    else:
        for line in readiness.unprojected_players:
            lines.append(
                f"  ${line.salary:>6}  {line.name} ({line.position}, {line.team}) "
                f"player_id {line.player_id}"
            )
        if readiness.unprojected_players_total > len(readiness.unprojected_players):
            remaining = readiness.unprojected_players_total - len(readiness.unprojected_players)
            lines.append(f"  +{remaining} more, {readiness.unprojected_players_total} in total")
    return "\n".join(lines) + "\n"


def _render_input(coverage: InputCoverage) -> list[str]:
    lines = [
        f"  any source    {coverage.covered} of {coverage.eligible} covered, "
        f"{coverage.missing} missing",
        f"    observed/ingested        {_slot(coverage.latest_observed_at)} / "
        f"{_slot(coverage.latest_ingested_at)}",
    ]
    if not coverage.by_source:
        lines.append("    sources                  none")
    for item in coverage.by_source:
        lines.append(
            f"    {item.source:<24} {item.covered} covered, {item.missing} missing  "
            f"observed {_slot(item.latest_observed_at)}"
        )
    return lines


def _render_players(
    label: str, players: tuple[PlayerLine, ...], total: int
) -> list[str]:
    if total == 0:
        return []
    lines = [f"{label}: {total}"]
    for line in players:
        lines.append(f"      ${line.salary:>6}  {line.name} ({line.position}, {line.team})")
    if total > len(players):
        lines.append(f"      +{total - len(players)} more")
    return lines


def _input_payload(coverage: InputCoverage) -> dict[str, object]:
    return {
        "input": coverage.input,
        "eligible": coverage.eligible,
        "covered": coverage.covered,
        "missing": coverage.missing,
        "fraction": _rounded(coverage.fraction),
        "latest_observed_at": _optional_stamp(coverage.latest_observed_at),
        "latest_ingested_at": _optional_stamp(coverage.latest_ingested_at),
        "sources": list(coverage.sources),
        "by_source": [
            {
                "source": item.source,
                "covered": item.covered,
                "missing": item.missing,
                "latest_observed_at": _optional_stamp(item.latest_observed_at),
                "latest_ingested_at": _optional_stamp(item.latest_ingested_at),
            }
            for item in coverage.by_source
        ],
    }


def _ownership_payload(role: OwnershipRoleCoverage) -> dict[str, object]:
    return {
        "role": role.role,
        "eligible": role.eligible,
        "covered": role.covered,
        "dedicated": role.dedicated,
        "embedded": role.embedded,
        "missing": role.missing,
        "fraction": _rounded(role.fraction),
        "embedded_is_fallback": role.embedded_is_fallback,
        "dedicated_sources": list(role.dedicated_sources),
        "embedded_sources": list(role.embedded_sources),
        "latest_observed_at": _optional_stamp(role.latest_observed_at),
        "latest_ingested_at": _optional_stamp(role.latest_ingested_at),
        "embedded_players": [_player_payload(line) for line in role.embedded_players],
        "embedded_players_total": role.embedded_players_total,
        "missing_players": [_player_payload(line) for line in role.missing_players],
        "missing_players_total": role.missing_players_total,
    }


def _game_payload(coverage: GameCoverage) -> dict[str, object]:
    return {
        "input": coverage.input,
        "required": coverage.required,
        "covered": coverage.covered,
        "missing": coverage.missing,
        "missing_games": [_game_line_payload(game) for game in coverage.missing_games],
        "sources": list(coverage.sources),
        "latest_observed_at": _optional_stamp(coverage.latest_observed_at),
        "latest_ingested_at": _optional_stamp(coverage.latest_ingested_at),
    }


def _game_line_payload(game: GameLine) -> dict[str, object]:
    return {
        "external_game_id": game.external_game_id,
        "matchup": game.matchup,
        "kickoff_at": utc_timestamp(game.kickoff_at),
        "stadium_name": game.stadium_name,
        "roof": game.roof,
    }


def _player_payload(line: PlayerLine) -> dict[str, object]:
    return {
        "player_id": line.player_id,
        "name": line.name,
        "team": line.team,
        "position": line.position,
        "salary": line.salary,
    }


# --------------------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------------------


def _player_line(row: sqlite3.Row) -> PlayerLine:
    return PlayerLine(
        player_id=int(row["player_id"]),
        name=str(row["canonical_name"]),
        team=str(row["team"]),
        position=str(row["position"] or "UNKNOWN"),
        salary=int(row["salary"]),
    )


def _ordered_lines(lines: Iterable[PlayerLine]) -> tuple[PlayerLine, ...]:
    """Most expensive first; player id breaks a tie so the order never depends on a scan."""

    return tuple(sorted(lines, key=lambda line: (-line.salary, line.player_id)))


def _newest(values: Iterable[object]) -> datetime | None:
    stamps = [str(value) for value in values if value is not None]
    return None if not stamps else _parse_stamp(max(stamps))


def _newest_of(values: Iterable[datetime | None]) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present, default=None)


def _parse_stamp(value: str) -> datetime:
    text = value if value.endswith("Z") else f"{value}Z"
    return ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))


def _optional_stamp(value: datetime | None) -> str | None:
    return None if value is None else utc_timestamp(value)


def _slot(value: datetime | None) -> str:
    return "MISSING" if value is None else utc_timestamp(value)


def _rounded(value: float) -> float:
    """Fixed precision so the frozen artifact never depends on float repr drift."""

    return round(value, 6)


def _humanize(age: timedelta) -> str:
    seconds = int(age.total_seconds())
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{sign}{hours}h{minutes:02d}m"
    return f"{sign}{minutes}m"


def _table(values: dict[str, Any], key: str) -> dict[str, Any]:
    value = values.get(key)
    if not isinstance(value, dict):
        raise ReadinessConfigError(f"{key} must be a TOML table")
    return value


def _text(values: dict[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ReadinessConfigError(f"{key} must be non-empty text")
    return value.strip()


def _fraction(values: dict[str, Any], key: str) -> float:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReadinessConfigError(f"{key} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ReadinessConfigError(f"{key} must be a finite fraction between 0 and 1")
    return result


def _positive_int(values: dict[str, Any], key: str) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReadinessConfigError(f"{key} must be a positive integer")
    return value


def _flag(values: dict[str, Any], key: str) -> bool:
    value = values.get(key)
    if not isinstance(value, bool):
        raise ReadinessConfigError(f"{key} must be true or false")
    return value
