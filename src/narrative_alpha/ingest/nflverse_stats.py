"""Pinned nflverse workload files as post-lock `results` stat lines.

Slice 34's usage rules read `<stat_key>` and `<stat_key>_baseline` from a result's stat
line and grade a workload claim against *that player's own* trailing reference. Nothing
wrote those keys, so every usage claim was ungradable by design. This module is the source:
the season's nflverse weekly player stats and snap counts, pinned by review date and
archived by hash through the one mechanism in :mod:`narrative_alpha.identity.pins`.

Three lines are worth stating plainly.

* **Canonical identity comes from the crosswalk, never from a name.** A weekly-stats row
  resolves through its nflverse GSIS id; a row that does not resolve enters the unresolved
  queue and is *held* — no row is written for a player we cannot name.
* **The two nflverse files are joined to each other by (season, week, team, name).** That
  is a join inside one vendor's own data for one team's one game, not an identity decision,
  and it refuses rather than picks when the pool is ambiguous.
* **A baseline may never see its own game.** The reference for a game in week *w* is the
  mean over the player's `baseline_games` games before week *w*. No prior game means no
  baseline key at all, which the usage rule already reports as ungradable.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import fmean
from types import MappingProxyType
from typing import ClassVar, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from narrative_alpha.identity.crosswalk import PlayerCrosswalk
from narrative_alpha.identity.models import PlayerIdentityInput
from narrative_alpha.identity.nflverse import NFLVERSE_SOURCE
from narrative_alpha.identity.normalization import (
    name_without_suffix,
    normalize_name,
    normalize_team_code,
)
from narrative_alpha.identity.pins import (
    HTTP_TIMEOUT,
    NflversePinError,
    PinnedRelease,
    archive_bytes,
    fetch_bytes,
    fetch_pinned,
)
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp

WORKLOAD_STATS_SOURCE = "nflverse-stats"
DEFAULT_WORKLOAD_STATS_CONFIG_PATH = Path("config/workload_stats.toml")

WEEKLY_STATS_LABEL = "nflverse weekly player stats"
SNAP_COUNTS_LABEL = "nflverse snap counts"

ROLLING_WEEKLY_STATS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/stats_player/"
    "stats_player_week_{season}.csv"
)
ROLLING_SNAP_COUNTS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/snap_counts/"
    "snap_counts_{season}.csv"
)

# The four workload dimensions `config/claim_grading.toml` grades, in stat-line order.
SHARE_KEYS: tuple[str, ...] = ("snap_share", "route_share", "target_share", "touch_share")

# nflverse publishes standard and full-PPR scoring only. DraftKings NFL is full PPR, so its
# points are carried directly; FanDuel's half-PPR is not among them, and §3.2 forbids
# deriving a site's label from a scoring rule we would have to reimplement here. A site with
# no column refuses rather than approximates.
SITE_SCORING_COLUMNS: Mapping[str, str] = MappingProxyType({"draftkings": "fantasy_points_ppr"})

_REQUIRED_WEEKLY_COLUMNS = frozenset(
    {
        "player_id",
        "player_display_name",
        "position",
        "team",
        "season",
        "week",
        "carries",
        "receptions",
        "target_share",
    }
)
_REQUIRED_SNAP_COLUMNS = frozenset(
    {
        "season",
        "week",
        "player",
        "pfr_player_id",
        "position",
        "team",
        "offense_snaps",
        "offense_pct",
        "defense_snaps",
        "st_snaps",
    }
)

_MISSING_NUMBERS = frozenset({"", "NA", "N/A", "NAN", "NULL", "NONE", "-"})
# Shares are stored to six decimals so a re-run of an unchanged pin produces byte-identical
# stat-line JSON, which is what makes the step idempotent.
_SHARE_PRECISION = 6


class NflverseStatsError(NflversePinError):
    """The pinned workload files cannot be read without inventing a fact."""


class StatsSchemaError(NflverseStatsError):
    """Raised when a pinned workload file's required columns have drifted."""


class UnpinnedStatsError(NflverseStatsError):
    """No workload release has been reviewed yet for the requested season and date.

    Distinct from every other failure here: nothing is wrong with the data, a person has
    simply not read a diff and pasted an entry, so the lane states the gap and moves on
    rather than recording a failure the operator cannot fix by rerunning.
    """


class WorkloadStatsConfigError(ValueError):
    """The workload-stats configuration is absent, malformed, or inconsistent."""


class WorkloadStatsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    config_version: str = Field(min_length=1)
    baseline_games: int = Field(ge=1, le=17)


@dataclass(frozen=True)
class LoadedWorkloadStatsConfig:
    """The reviewed configuration bytes plus their hash, stamped onto every written row."""

    path: Path
    sha256: str
    config: WorkloadStatsConfig


def load_workload_stats_config(
    path: Path = DEFAULT_WORKLOAD_STATS_CONFIG_PATH,
) -> LoadedWorkloadStatsConfig:
    """Load strict TOML and hash the exact bytes a run's baselines were computed under."""

    try:
        raw_bytes = path.read_bytes()
    except OSError as error:
        raise WorkloadStatsConfigError(f"cannot read workload stats config {path}: {error}") from (
            error
        )
    try:
        raw = tomllib.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise WorkloadStatsConfigError(f"invalid workload stats config {path}: {error}") from error
    try:
        config = WorkloadStatsConfig.model_validate(raw)
    except ValidationError as error:
        raise WorkloadStatsConfigError(f"invalid workload stats config {path}: {error}") from error
    return LoadedWorkloadStatsConfig(
        path=path,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        config=config,
    )


@dataclass(frozen=True)
class PinnedWeeklyStatsRelease(PinnedRelease):
    """One reviewed nflverse weekly-player-stats file."""

    label: ClassVar[str] = WEEKLY_STATS_LABEL


@dataclass(frozen=True)
class PinnedSnapCountsRelease(PinnedRelease):
    """One reviewed nflverse snap-counts file."""

    label: ClassVar[str] = SNAP_COUNTS_LABEL


@dataclass(frozen=True)
class PinnedStatsRelease:
    """Both workload files for one season, reviewed together on one date.

    They are one pin because a stat line is built from both at once: a snap share from one
    file beside a target share from another vintage of the other would be a mixture nobody
    reviewed.
    """

    season: int
    reviewed_at: date
    weekly_url: str
    weekly_sha256: str
    snaps_url: str
    snaps_sha256: str

    @property
    def weekly(self) -> PinnedWeeklyStatsRelease:
        return PinnedWeeklyStatsRelease(
            season=self.season,
            url=self.weekly_url,
            sha256=self.weekly_sha256,
            reviewed_at=self.reviewed_at,
        )

    @property
    def snaps(self) -> PinnedSnapCountsRelease:
        return PinnedSnapCountsRelease(
            season=self.season,
            url=self.snaps_url,
            sha256=self.snaps_sha256,
            reviewed_at=self.reviewed_at,
        )


# A refresh appends a reviewed entry; it never edits an older pin. The table is empty until
# a person has actually read a diff and pasted an entry — an unreviewed hash is not a pin,
# and the lane says so rather than fetching whatever upstream serves today.
PINNED_STATS_RELEASES: Mapping[int, tuple[PinnedStatsRelease, ...]] = MappingProxyType({})


def pinned_stats_release(
    season: int,
    as_of: date | datetime,
    *,
    releases: Mapping[int, tuple[PinnedStatsRelease, ...]] = PINNED_STATS_RELEASES,
) -> PinnedStatsRelease:
    """Return the newest reviewed workload pin available on ``as_of``; never look ahead."""

    cutoff = as_of.date() if isinstance(as_of, datetime) else as_of
    eligible = tuple(
        release
        for release in releases.get(season, ())
        if release.season == season and release.reviewed_at <= cutoff
    )
    if not eligible:
        raise UnpinnedStatsError(
            f"no nflverse workload stats release is pinned for season {season} at or before "
            f"{cutoff.isoformat()}; review and add its hashes"
        )
    return max(enumerate(eligible), key=lambda item: (item[1].reviewed_at, item[0]))[1]


def fetch_pinned_stats(
    release: PinnedStatsRelease,
    archive_dir: Path,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[Path, Path]:
    """Return both files' verified bytes, fetching only on an archive miss."""

    owns_client = client is None
    http_client = client or httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True)
    try:
        weekly = fetch_pinned(release.weekly, archive_dir, client=http_client, sleep=sleep)
        snaps = fetch_pinned(release.snaps, archive_dir, client=http_client, sleep=sleep)
    finally:
        if owns_client:
            http_client.close()
    return weekly, snaps


@dataclass(frozen=True)
class StatsRefreshReport:
    """Non-mutating review of the rolling workload assets against the newest pin."""

    season: int
    reviewed_at: date
    weekly_url: str
    weekly_sha256: str
    snaps_url: str
    snaps_sha256: str
    weekly_rows: int
    snap_rows: int
    matches_pin: bool
    compared_with: PinnedStatsRelease | None

    def render(self) -> str:
        """Render the review summary and a syntactically valid pin entry."""

        compared = (
            "none (first pin for this season)"
            if self.compared_with is None
            else self.compared_with.reviewed_at.isoformat()
        )
        lines = [
            f"season={self.season}",
            f"compared_pin_reviewed_at={compared}",
            f"matches_pin={str(self.matches_pin).lower()}",
            f"weekly_url={self.weekly_url}",
            f"weekly_sha256={self.weekly_sha256}",
            f"weekly_rows={self.weekly_rows}",
            f"snaps_url={self.snaps_url}",
            f"snaps_sha256={self.snaps_sha256}",
            f"snap_rows={self.snap_rows}",
            "paste_entry:",
            "PinnedStatsRelease(",
            f"    season={self.season},",
            "    reviewed_at="
            f"date({self.reviewed_at.year}, {self.reviewed_at.month}, {self.reviewed_at.day}),",
            f"    weekly_url={self.weekly_url!r},",
            f"    weekly_sha256={self.weekly_sha256!r},",
            f"    snaps_url={self.snaps_url!r},",
            f"    snaps_sha256={self.snaps_sha256!r},",
            ")",
        ]
        return "\n".join(lines) + "\n"


def refresh_stats_release(
    season: int,
    archive_dir: Path,
    *,
    reviewed_at: date,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    releases: Mapping[int, tuple[PinnedStatsRelease, ...]] = PINNED_STATS_RELEASES,
    today: date | None = None,
) -> StatsRefreshReport:
    """Fetch and hash the rolling workload assets without changing the reviewed pin table.

    Both files are archived under their own hashes, so the entry this report prints is
    fetchable offline once pasted — even after upstream overwrites the rolling assets.
    Archiving under a self-computed hash trusts nothing: the pin table stays the only
    authority on which bytes a run may read.
    """

    current_day = today or datetime.now(UTC).date()
    if reviewed_at > current_day:
        raise NflverseStatsError(
            f"reviewed_at {reviewed_at.isoformat()} is in the future (today is "
            f"{current_day.isoformat()}); as-of selection could never choose that pin"
        )
    existing = tuple(release for release in releases.get(season, ()) if release.season == season)
    newest = (
        None
        if not existing
        else max(enumerate(existing), key=lambda item: (item[1].reviewed_at, item[0]))[1]
    )
    if newest is not None and reviewed_at < newest.reviewed_at:
        raise NflverseStatsError(
            f"reviewed_at {reviewed_at.isoformat()} precedes newest pin "
            f"{newest.reviewed_at.isoformat()}"
        )

    weekly_url = ROLLING_WEEKLY_STATS_URL.format(season=season)
    snaps_url = ROLLING_SNAP_COUNTS_URL.format(season=season)
    owns_client = client is None
    http_client = client or httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True)
    try:
        weekly_bytes = fetch_bytes(http_client, weekly_url, label=WEEKLY_STATS_LABEL, sleep=sleep)
        snaps_bytes = fetch_bytes(http_client, snaps_url, label=SNAP_COUNTS_LABEL, sleep=sleep)
    finally:
        if owns_client:
            http_client.close()

    weekly_sha256 = hashlib.sha256(weekly_bytes).hexdigest()
    snaps_sha256 = hashlib.sha256(snaps_bytes).hexdigest()
    archive_bytes(archive_dir, weekly_bytes, weekly_sha256, label=WEEKLY_STATS_LABEL)
    archive_bytes(archive_dir, snaps_bytes, snaps_sha256, label=SNAP_COUNTS_LABEL)

    # Parsed only so a drifted header is reported here, at review time, rather than on the
    # Tuesday a person pastes the entry and the lane first reads it. Every site's scoring
    # column is required at review, not just the one some later run happens to ask for.
    weekly_rows = _parse_weekly(
        weekly_bytes,
        season=season,
        scoring_columns=tuple(sorted(SITE_SCORING_COLUMNS.values())),
    )
    snap_rows = _parse_snaps(snaps_bytes, season=season)
    return StatsRefreshReport(
        season=season,
        reviewed_at=reviewed_at,
        weekly_url=weekly_url,
        weekly_sha256=weekly_sha256,
        snaps_url=snaps_url,
        snaps_sha256=snaps_sha256,
        weekly_rows=sum(len(rows) for rows in weekly_rows.values()),
        snap_rows=sum(len(rows) for rows in snap_rows.values()),
        matches_pin=bool(
            newest is not None
            and newest.weekly_sha256 == weekly_sha256
            and newest.snaps_sha256 == snaps_sha256
        ),
        compared_with=newest,
    )


@dataclass(frozen=True)
class _WeeklyRow:
    row_number: int
    nflverse_player_id: str
    name_raw: str
    team: str
    position: str | None
    week: int
    carries: float
    receptions: float
    target_share: float | None
    routes: float | None
    fantasy_points: dict[str, float | None]


@dataclass(frozen=True)
class _SnapRow:
    row_number: int
    pfr_player_id: str
    name_raw: str
    team: str
    week: int
    offense_snaps: float
    offense_pct: float | None
    total_snaps: float


@dataclass(frozen=True)
class _PlayerGame:
    """One player's game as the two pinned files jointly describe it."""

    week: int
    team: str
    name_raw: str
    position: str | None
    shares: Mapping[str, float]
    played: bool | None
    fantasy_points: Mapping[str, float | None]
    hold_reason: str | None = None


class HeldStatsRow(BaseModel):
    """A player-game the step refused to write, and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    nflverse_player_id: str
    name_raw: str
    team: str
    reason: str
    unresolved_id: int | None = None


class WorkloadStatsReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    season: int
    week: int
    site: str
    scoring_column: str
    reviewed_at: date
    weekly_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snap_counts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_version: str
    rows_seen: int = Field(ge=0)
    players_written: int = Field(ge=0)
    players_unchanged: int = Field(ge=0)
    players_held: int = Field(ge=0)
    players_without_baseline: int = Field(ge=0)
    players_not_salaried: int = Field(ge=0)
    salaried_without_stats: int = Field(ge=0)
    unresolved_ids: tuple[int, ...] = ()
    held: tuple[HeldStatsRow, ...] = ()


def load_workload_stats(
    connection: sqlite3.Connection,
    *,
    season: int,
    week: int,
    site: str,
    archive_dir: Path,
    observed_at: datetime,
    as_of: date | datetime | None = None,
    config_path: Path = DEFAULT_WORKLOAD_STATS_CONFIG_PATH,
    releases: Mapping[int, tuple[PinnedStatsRelease, ...]] = PINNED_STATS_RELEASES,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    run_id: str | None = None,
) -> WorkloadStatsReport:
    """Write one `results` row per salaried player-game from the pinned workload files."""

    scoring_column = SITE_SCORING_COLUMNS.get(site)
    if scoring_column is None:
        raise NflverseStatsError(
            f"nflverse publishes no fantasy-point column for {site}; it carries standard and "
            f"full-PPR scoring only ({', '.join(sorted(SITE_SCORING_COLUMNS))})"
        )
    observed_at = ensure_utc(observed_at)
    loaded_config = load_workload_stats_config(config_path)
    release = pinned_stats_release(season, as_of or observed_at, releases=releases)
    weekly_path, snaps_path = fetch_pinned_stats(release, archive_dir, client=client, sleep=sleep)

    # `fetch_pinned` has already refused anything whose bytes are not the reviewed ones,
    # on an archive hit as well as on a fetch.
    weekly_bytes = weekly_path.read_bytes()
    snaps_bytes = snaps_path.read_bytes()

    weekly_rows = _parse_weekly(weekly_bytes, season=season, scoring_columns=(scoring_column,))
    snap_rows = _parse_snaps(snaps_bytes, season=season)
    games = _player_games(weekly_rows, snap_rows)

    source_version = (
        f"nflverse-stats-v1:season:{season}:reviewed:{release.reviewed_at.isoformat()}"
        f":weekly:sha256:{release.weekly_sha256}:snaps:sha256:{release.snaps_sha256}"
        f":baseline:{loaded_config.config.config_version}"
        f":n:{loaded_config.config.baseline_games}:config:sha256:{loaded_config.sha256}"
    )
    source_file_sha256 = hashlib.sha256(
        f"{release.weekly_sha256}\n{release.snaps_sha256}".encode()
    ).hexdigest()

    salaried = _salaried_games(connection, season=season, week=week, site=site)
    crosswalk = PlayerCrosswalk(connection)
    timestamp = utc_timestamp(observed_at)

    held: list[HeldStatsRow] = []
    unresolved_ids: list[int] = []
    written = 0
    unchanged = 0
    without_baseline = 0
    not_salaried = 0
    rows_seen = 0
    resolved_players: set[int] = set()

    for nflverse_player_id, history in sorted(games.items()):
        current = next((game for game in history if game.week == week), None)
        if current is None:
            continue
        rows_seen += 1
        if current.hold_reason is not None:
            held.append(
                HeldStatsRow(
                    nflverse_player_id=nflverse_player_id,
                    name_raw=current.name_raw,
                    team=current.team,
                    reason=current.hold_reason,
                )
            )
            continue
        points = current.fantasy_points.get(scoring_column)
        if points is None:
            held.append(
                HeldStatsRow(
                    nflverse_player_id=nflverse_player_id,
                    name_raw=current.name_raw,
                    team=current.team,
                    reason=f"nflverse carries no {scoring_column} value for this game",
                )
            )
            continue

        match = crosswalk.match(
            PlayerIdentityInput(
                source=NFLVERSE_SOURCE,
                site=None,
                external_player_id=nflverse_player_id,
                name_raw=current.name_raw,
                team=current.team,
                position=current.position,
                observed_at=observed_at,
                ingested_at=observed_at,
                source_file_sha256=release.weekly_sha256,
                run_id=run_id,
            )
        )
        if match.player_id is None:
            if match.unresolved_id is not None:
                unresolved_ids.append(match.unresolved_id)
            held.append(
                HeldStatsRow(
                    nflverse_player_id=nflverse_player_id,
                    name_raw=current.name_raw,
                    team=current.team,
                    reason="no canonical player resolves this nflverse id",
                    unresolved_id=match.unresolved_id,
                )
            )
            continue
        resolved_players.add(match.player_id)

        game_ids = salaried.get(match.player_id, ())
        if not game_ids:
            not_salaried += 1
            continue
        if len(game_ids) > 1:
            held.append(
                HeldStatsRow(
                    nflverse_player_id=nflverse_player_id,
                    name_raw=current.name_raw,
                    team=current.team,
                    reason=(
                        f"salaried in {len(game_ids)} different games this week on {site}; "
                        "one player-week is one game"
                    ),
                )
            )
            continue

        baselines = _baselines(history, week=week, games=loaded_config.config.baseline_games)
        if not baselines:
            without_baseline += 1
        stat_line = _stat_line(current, baselines, scoring=scoring_column)
        outcome = _write_result(
            connection,
            game_id=game_ids[0],
            player_id=match.player_id,
            site=site,
            fantasy_points=points,
            stat_line_json=stat_line,
            source_file_sha256=source_file_sha256,
            timestamp=timestamp,
            source_version=source_version,
            run_id=run_id,
        )
        if outcome == "conflict":
            held.append(
                HeldStatsRow(
                    nflverse_player_id=nflverse_player_id,
                    name_raw=current.name_raw,
                    team=current.team,
                    reason=(
                        f"a different {WORKLOAD_STATS_SOURCE} row already exists for this "
                        f"player-game at {timestamp}; one observation instant cannot hold "
                        "two different facts"
                    ),
                )
            )
            continue
        written += int(outcome == "written")
        unchanged += int(outcome == "unchanged")

    return WorkloadStatsReport(
        season=season,
        week=week,
        site=site,
        scoring_column=scoring_column,
        reviewed_at=release.reviewed_at,
        weekly_sha256=release.weekly_sha256,
        snap_counts_sha256=release.snaps_sha256,
        source_version=source_version,
        rows_seen=rows_seen,
        players_written=written,
        players_unchanged=unchanged,
        players_held=len(held),
        players_without_baseline=without_baseline,
        players_not_salaried=not_salaried,
        salaried_without_stats=len(set(salaried) - resolved_players),
        unresolved_ids=tuple(unresolved_ids),
        held=tuple(held),
    )


def _stat_line(game: _PlayerGame, baselines: Mapping[str, float], *, scoring: str) -> str:
    """Serialize the graded keys canonically so an unchanged pin re-serializes identically.

    ``scoring`` names the nflverse column behind this row's ``fantasy_points``. It is
    nflverse's scoring, not the site's — DraftKings adds yardage bonuses that full PPR
    does not — so the row says so, and the evaluation report never reads it as a label.
    """

    payload: dict[str, object] = {key: game.shares[key] for key in SHARE_KEYS if key in game.shares}
    payload["played"] = bool(game.played)
    payload["scoring"] = f"nflverse:{scoring}"
    payload.update(sorted(baselines.items()))
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _baselines(history: Sequence[_PlayerGame], *, week: int, games: int) -> dict[str, float]:
    """The player's own reference: a mean over the `games` season games before this one.

    The window is the previous games themselves, not the previous games that happen to
    carry a value, so a receiver who ran no routes for three weeks does not silently have
    an older week stand in for them.
    """

    prior = sorted((game for game in history if game.week < week), key=lambda game: game.week)
    window = prior[-games:]
    references: dict[str, float] = {}
    for key in SHARE_KEYS:
        values = [game.shares[key] for game in window if key in game.shares]
        if values:
            references[f"{key}_baseline"] = round(fmean(values), _SHARE_PRECISION)
    return references


def _write_result(
    connection: sqlite3.Connection,
    *,
    game_id: int,
    player_id: int,
    site: str,
    fantasy_points: float,
    stat_line_json: str,
    source_file_sha256: str,
    timestamp: str,
    source_version: str,
    run_id: str | None,
) -> Literal["written", "unchanged", "conflict"]:
    """Append one result row, or nothing when the newest row already says exactly this.

    Idempotency is by content, not by clock: a rerun on the same pin re-derives identical
    points and stat line and inserts nothing, while a re-pinned file that actually changes
    a number appends a new observation instead of overwriting the old one. A changed fact
    that collides with an existing row at the *same* observation instant is a conflict the
    caller reports, never a silently dropped insert.
    """

    existing = connection.execute(
        """
        SELECT fantasy_points, stat_line_json FROM results
        WHERE source = ? AND site = ? AND game_id = ? AND player_id = ?
        ORDER BY rtrim(observed_at, 'Z') DESC, result_id DESC
        LIMIT 1
        """,
        (WORKLOAD_STATS_SOURCE, site, game_id, player_id),
    ).fetchone()
    if (
        existing is not None
        and float(existing["fantasy_points"]) == fantasy_points
        and str(existing["stat_line_json"]) == stat_line_json
    ):
        return "unchanged"
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO results(
            game_id, player_id, site, fantasy_points, stat_line_json,
            source_file_sha256, source, published_at, observed_at, ingested_at,
            effective_at, valid_from, valid_to, source_version, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?, NULL, ?, ?)
        """,
        (
            game_id,
            player_id,
            site,
            fantasy_points,
            stat_line_json,
            source_file_sha256,
            WORKLOAD_STATS_SOURCE,
            timestamp,
            timestamp,
            timestamp,
            source_version,
            run_id,
        ),
    )
    return "written" if cursor.rowcount == 1 else "conflict"


def _salaried_games(
    connection: sqlite3.Connection, *, season: int, week: int, site: str
) -> dict[int, tuple[int, ...]]:
    """Canonical player to the game(s) they were priced in on one site in one week."""

    rows = connection.execute(
        """
        SELECT DISTINCT salary.player_id, salary.game_id
        FROM salaries AS salary
        JOIN slates AS slate ON slate.slate_id = salary.slate_id
        WHERE slate.season = ? AND slate.week = ? AND slate.site = ?
          AND salary.game_id IS NOT NULL AND salary.valid_to IS NULL
        ORDER BY salary.player_id, salary.game_id
        """,
        (season, week, site),
    ).fetchall()
    games: dict[int, list[int]] = {}
    for row in rows:
        games.setdefault(int(row["player_id"]), []).append(int(row["game_id"]))
    return {player_id: tuple(values) for player_id, values in games.items()}


def _player_games(
    weekly: Mapping[str, tuple[_WeeklyRow, ...]],
    snaps: Mapping[str, tuple[_SnapRow, ...]],
) -> dict[str, tuple[_PlayerGame, ...]]:
    """Join the two pinned files by (week, team, name) and derive every share."""

    team_totals = _team_week_totals(weekly)
    snap_index = _snap_index(snaps)
    games: dict[str, list[_PlayerGame]] = {}
    for nflverse_player_id, rows in weekly.items():
        for row in rows:
            snap, snap_problem = _match_snap_row(snap_index, row)
            shares: dict[str, float] = {}
            totals = team_totals[(row.week, row.team)]
            if snap is not None and snap.offense_pct is not None:
                shares["snap_share"] = round(snap.offense_pct, _SHARE_PRECISION)
            if row.routes is not None and totals.routes > 0:
                shares["route_share"] = round(row.routes / totals.routes, _SHARE_PRECISION)
            if row.target_share is not None:
                shares["target_share"] = round(row.target_share, _SHARE_PRECISION)
            if totals.touches > 0:
                shares["touch_share"] = round(
                    (row.carries + row.receptions) / totals.touches, _SHARE_PRECISION
                )
            games.setdefault(nflverse_player_id, []).append(
                _PlayerGame(
                    week=row.week,
                    team=row.team,
                    name_raw=row.name_raw,
                    position=row.position,
                    shares=shares,
                    # Any snap counts, not offensive snaps alone: a returner who scores has
                    # played, and calling that a DNP would put the result row's own points
                    # in conflict with its played fact.
                    played=None if snap is None else snap.total_snaps > 0,
                    fantasy_points=row.fantasy_points,
                    hold_reason=snap_problem,
                )
            )
    return {
        nflverse_player_id: tuple(sorted(rows, key=lambda game: game.week))
        for nflverse_player_id, rows in games.items()
    }


@dataclass
class _TeamWeekTotals:
    touches: float = 0.0
    routes: float = 0.0


def _team_week_totals(
    weekly: Mapping[str, tuple[_WeeklyRow, ...]],
) -> dict[tuple[int, str], _TeamWeekTotals]:
    totals: dict[tuple[int, str], _TeamWeekTotals] = {}
    for rows in weekly.values():
        for row in rows:
            total = totals.setdefault((row.week, row.team), _TeamWeekTotals())
            total.touches += row.carries + row.receptions
            total.routes += row.routes or 0.0
    return totals


@dataclass(frozen=True)
class _SnapIndex:
    exact: Mapping[tuple[int, str, str], tuple[_SnapRow, ...]]
    unsuffixed: Mapping[tuple[int, str, str], tuple[_SnapRow, ...]]


def _snap_index(snaps: Mapping[str, tuple[_SnapRow, ...]]) -> _SnapIndex:
    exact: dict[tuple[int, str, str], list[_SnapRow]] = {}
    unsuffixed: dict[tuple[int, str, str], list[_SnapRow]] = {}
    for rows in snaps.values():
        for row in rows:
            exact.setdefault((row.week, row.team, normalize_name(row.name_raw)), []).append(row)
            unsuffixed.setdefault(
                (row.week, row.team, name_without_suffix(row.name_raw)), []
            ).append(row)
    return _SnapIndex(
        exact={key: tuple(value) for key, value in exact.items()},
        unsuffixed={key: tuple(value) for key, value in unsuffixed.items()},
    )


def _match_snap_row(index: _SnapIndex, row: _WeeklyRow) -> tuple[_SnapRow | None, str | None]:
    """Exactly one snap row for this team-week, or a stated reason there is none."""

    for candidates in (
        index.exact.get((row.week, row.team, normalize_name(row.name_raw)), ()),
        index.unsuffixed.get((row.week, row.team, name_without_suffix(row.name_raw)), ()),
    ):
        if len(candidates) == 1:
            return candidates[0], None
        if len(candidates) > 1:
            return None, (
                f"{len(candidates)} nflverse snap-count rows name "
                f"{row.name_raw!r} for {row.team} in week {row.week:02d}; refusing to pick one"
            )
    return None, (
        f"no nflverse snap-count row for {row.name_raw!r} on {row.team} in "
        f"week {row.week:02d}, so snaps and the played fact are unknown"
    )


def _parse_weekly(
    content: bytes,
    *,
    season: int,
    scoring_columns: Sequence[str],
) -> dict[str, tuple[_WeeklyRow, ...]]:
    """Parse the pinned weekly file, refusing on header drift and unreadable numbers."""

    reader, headers = _reader(content, label=WEEKLY_STATS_LABEL)
    required = _REQUIRED_WEEKLY_COLUMNS | frozenset(scoring_columns)
    _require_columns(WEEKLY_STATS_LABEL, headers, required)
    # nflverse does not publish routes run in the weekly file today. The column is read when
    # the pinned bytes carry it and `route_share` is simply absent when they do not, exactly
    # as a missing baseline is absent: route-share claims stay ungradable rather than being
    # graded against an invented denominator.
    has_routes = "routes" in headers

    rows: dict[str, list[_WeeklyRow]] = {}
    for row_number, row in enumerate(reader, start=2):
        if _season(row.get("season"), WEEKLY_STATS_LABEL, row_number) != season:
            continue
        nflverse_player_id = (row.get("player_id") or "").strip()
        name_raw = (row.get("player_display_name") or "").strip()
        team = normalize_team_code((row.get("team") or "").strip())
        if not nflverse_player_id or not name_raw or not team:
            raise StatsSchemaError(
                f"{WEEKLY_STATS_LABEL} row {row_number} is missing player_id, "
                "player_display_name, or team"
            )
        week = _week(row.get("week"), WEEKLY_STATS_LABEL, row_number)
        rows.setdefault(nflverse_player_id, []).append(
            _WeeklyRow(
                row_number=row_number,
                nflverse_player_id=nflverse_player_id,
                name_raw=name_raw,
                team=team,
                position=_optional_upper(row.get("position")),
                week=week,
                carries=_number(row.get("carries"), "carries", row_number) or 0.0,
                receptions=_number(row.get("receptions"), "receptions", row_number) or 0.0,
                target_share=_fraction(
                    _number(row.get("target_share"), "target_share", row_number),
                    "target_share",
                    row_number,
                ),
                routes=(_number(row.get("routes"), "routes", row_number) if has_routes else None),
                fantasy_points={
                    column: _number(row.get(column), column, row_number)
                    for column in scoring_columns
                },
            )
        )
    return {key: tuple(value) for key, value in rows.items()}


def _parse_snaps(content: bytes, *, season: int) -> dict[str, tuple[_SnapRow, ...]]:
    """Parse the pinned snap-counts file, refusing on header drift and unreadable numbers."""

    reader, headers = _reader(content, label=SNAP_COUNTS_LABEL)
    _require_columns(SNAP_COUNTS_LABEL, headers, _REQUIRED_SNAP_COLUMNS)

    rows: dict[str, list[_SnapRow]] = {}
    for row_number, row in enumerate(reader, start=2):
        if _season(row.get("season"), SNAP_COUNTS_LABEL, row_number) != season:
            continue
        pfr_player_id = (row.get("pfr_player_id") or "").strip()
        name_raw = (row.get("player") or "").strip()
        team = normalize_team_code((row.get("team") or "").strip())
        if not pfr_player_id or not name_raw or not team:
            raise StatsSchemaError(
                f"{SNAP_COUNTS_LABEL} row {row_number} is missing pfr_player_id, player, or team"
            )
        offense = _number(row.get("offense_snaps"), "offense_snaps", row_number) or 0.0
        defense = _number(row.get("defense_snaps"), "defense_snaps", row_number) or 0.0
        special = _number(row.get("st_snaps"), "st_snaps", row_number) or 0.0
        rows.setdefault(pfr_player_id, []).append(
            _SnapRow(
                row_number=row_number,
                pfr_player_id=pfr_player_id,
                name_raw=name_raw,
                team=team,
                week=_week(row.get("week"), SNAP_COUNTS_LABEL, row_number),
                offense_snaps=offense,
                offense_pct=_fraction(
                    _number(row.get("offense_pct"), "offense_pct", row_number),
                    "offense_pct",
                    row_number,
                ),
                total_snaps=offense + defense + special,
            )
        )
    return {key: tuple(value) for key, value in rows.items()}


def _reader(content: bytes, *, label: str) -> tuple[csv.DictReader[str], frozenset[str]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise NflverseStatsError(f"{label} is not UTF-8") from error
    reader = csv.DictReader(io.StringIO(text, newline=""))
    return reader, frozenset(reader.fieldnames or ())


def _require_columns(label: str, headers: frozenset[str], required: frozenset[str]) -> None:
    missing = tuple(sorted(required - headers))
    if missing:
        raise StatsSchemaError(f"{label} is missing required columns: {', '.join(missing)}")


def _season(value: str | None, label: str, row_number: int) -> int:
    parsed = _number(value, "season", row_number)
    if parsed is None:
        raise StatsSchemaError(f"{label} row {row_number} has no season")
    return int(parsed)


def _week(value: str | None, label: str, row_number: int) -> int:
    parsed = _number(value, "week", row_number)
    if parsed is None or parsed < 1 or parsed != int(parsed):
        raise StatsSchemaError(f"{label} row {row_number} has an unusable week {value!r}")
    return int(parsed)


def _number(value: str | None, column: str, row_number: int) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if text.upper() in _MISSING_NUMBERS:
        return None
    try:
        return float(text)
    except ValueError as error:
        raise StatsSchemaError(
            f"row {row_number} column {column!r} is not a number: {value!r}"
        ) from error


def _fraction(value: float | None, column: str, row_number: int) -> float | None:
    if value is None:
        return None
    if not 0 <= value <= 1:
        raise StatsSchemaError(
            f"row {row_number} column {column!r} is {value}, which is not a fraction in "
            "[0, 1]; refusing to guess its units"
        )
    return value


def _optional_upper(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    return normalized or None
