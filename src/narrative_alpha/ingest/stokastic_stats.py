"""Strict ingestion and bonus-free site scoring for Stokastic Stats exports."""

from __future__ import annotations

import csv
import hashlib
import io
import math
import sqlite3
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from narrative_alpha.identity import PlayerCrosswalk, PlayerIdentityInput
from narrative_alpha.identity.normalization import normalize_name, normalize_team_code
from narrative_alpha.ingest.projections import (
    OwnershipParseResult,
    ProjectionParseResult,
    SourceFormatError,
    SourceFormatRegistry,
    SourcePlayerFields,
)
from narrative_alpha.ingest.slates import normalize_site
from narrative_alpha.ingest.timestamps import ensure_utc, optional_utc_timestamp, utc_timestamp
from narrative_alpha.snapshots import MANIFEST_FILENAME, CaptureKind, load_manifest, sha256_file
from narrative_alpha.snapshots.core import snapshot_week_path

DEFAULT_DERIVED_SCORING_PATH = Path("config/derived_scoring.toml")
STOKASTIC_SOURCE = "stokastic"
STOKASTIC_STATS_FORMAT_VERSION = "stokastic-stats-v1"
DERIVED_SOURCE = "stokastic-stats-derived"
RECEPTION_PLACEHOLDER_NOTE = (
    "receptions are a vendor placeholder: Catch % is fixed at 75% and Rec is 0.75 x Tgt"
)

# The exports round targets/receptions to tenths and receiving yards/YPC to whole/tenths.
# These bounds accept every real row while still refusing a doctored material mismatch.
RECEPTION_PLACEHOLDER_TOLERANCE = 0.080001
RECEIVING_YARDS_TOLERANCE = 1.150001


class MissingStatsCapture(SourceFormatError):
    """The week has no captured Stokastic Stats export yet."""


class ProjectedStat(StrEnum):
    """Closed vocabulary represented by the three real export schemas."""

    PASS_ATT = "pass_att"
    PASS_CMP = "pass_cmp"
    PASS_YDS = "pass_yds"
    PASS_TD = "pass_td"
    PASS_INT = "pass_int"
    RUSH_ATT = "rush_att"
    RUSH_YDS = "rush_yds"
    RUSH_TD = "rush_td"
    TARGETS = "targets"
    TARGET_SHARE = "target_share"
    RECEPTIONS = "receptions"
    REC_YDS = "rec_yds"
    REC_TD = "rec_td"
    PASS_FUMBLES = "pass_fumbles"
    RUSH_FUMBLES = "rush_fumbles"
    REC_FUMBLES = "rec_fumbles"


class StatsFileKind(StrEnum):
    PASSING = "passing"
    RUSHING = "rushing"
    RECEIVING = "receiving"


_HEADERS: dict[StatsFileKind, tuple[str, ...]] = {
    StatsFileKind.PASSING: (
        "Player",
        "Team",
        "Opp",
        "Att",
        "Comp",
        "Pass Yds",
        "TD",
        "INT",
        "Fum",
    ),
    StatsFileKind.RUSHING: (
        "Player",
        "Team",
        "Opp",
        "Rush",
        "Rush Yds",
        "TD",
        "Fum",
    ),
    StatsFileKind.RECEIVING: (
        "Player",
        "Team",
        "Opp",
        "Tgt",
        "Tgt %",
        "Rec",
        "Catch %",
        "Rec Yds",
        "YPC",
        "TD",
        "Fum",
    ),
}


class ParsedStokasticStatLine(SourcePlayerFields):
    row_number: int = Field(ge=2)
    file_kind: StatsFileKind
    stats: dict[ProjectedStat, float]


class StokasticStatsParseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    file_kind: StatsFileKind
    rows_seen: int = Field(ge=0)
    rows: tuple[ParsedStokasticStatLine, ...]
    receptions_are_vendor_placeholder: bool = False
    placeholder_note: str | None = None


class StokasticStatsSourceFormat:
    """A SourceFormatRegistry adapter whose exact header selects one stats schema."""

    name = STOKASTIC_SOURCE

    def parse_stats(self, path: Path) -> StokasticStatsParseResult:
        return parse_stokastic_stats(path)

    def parse_projections(self, path: Path) -> ProjectionParseResult:
        raise SourceFormatError(
            f"{path} is a Stokastic stats component export, not a projections export"
        )

    def parse_ownership(self, path: Path) -> OwnershipParseResult:
        raise SourceFormatError(
            f"{path} is a Stokastic stats component export, not an ownership export"
        )


@runtime_checkable
class _StatsParser(Protocol):
    def parse_stats(self, path: Path) -> StokasticStatsParseResult: ...


class IngestSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_unresolved_fraction: float = Field(ge=0, le=1, allow_inf_nan=False)


class ScoringWeights(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pass_yds: float = Field(allow_inf_nan=False)
    pass_td: float = Field(allow_inf_nan=False)
    pass_int: float = Field(allow_inf_nan=False)
    rush_yds: float = Field(allow_inf_nan=False)
    rush_td: float = Field(allow_inf_nan=False)
    receptions: float = Field(allow_inf_nan=False)
    rec_yds: float = Field(allow_inf_nan=False)
    rec_td: float = Field(allow_inf_nan=False)


class ScoringSites(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    draftkings: ScoringWeights
    fanduel: ScoringWeights


class ScoringExclusions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    yardage_bonuses: str
    fumbles: str

    @field_validator("yardage_bonuses", "fumbles")
    @classmethod
    def explicitly_excluded(cls, value: str) -> str:
        if not value.strip().casefold().startswith("excluded:"):
            raise ValueError("must explicitly begin with 'excluded:'")
        return value.strip()


class DerivedScoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    config_version: str = Field(min_length=1)
    ingest: IngestSettings
    sites: ScoringSites
    excluded: ScoringExclusions


@dataclass(frozen=True)
class LoadedDerivedScoringConfig:
    path: Path
    sha256: str
    config: DerivedScoringConfig


class UnresolvedStatsPlayer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name_raw: str
    team: str
    unresolved_id: int | None


class StokasticStatsLoadReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capture_path: Path
    season: int = Field(ge=1)
    week: int = Field(ge=1, le=99)
    source: str
    files_seen: int = Field(ge=0)
    rows_seen: int = Field(ge=0)
    players_seen: int = Field(ge=0)
    players_written: int = Field(ge=0)
    stat_rows_inserted: int = Field(ge=0)
    duplicate_stat_rows: int = Field(ge=0)
    unresolved_fraction: float = Field(ge=0, le=1)
    max_unresolved_fraction: float = Field(ge=0, le=1)
    unresolved: tuple[UnresolvedStatsPlayer, ...] = ()
    held: bool
    out_of_slate_rows: int = Field(ge=0)
    slate_team_count: int = Field(ge=0)
    receptions_are_vendor_placeholder: bool
    placeholder_note: str

    @property
    def ok(self) -> bool:
        return not self.held


class DerivedProjectionMean(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    player_id: int = Field(gt=0)
    canonical_name: str
    team: str | None
    site: str
    projection_mean: float = Field(allow_inf_nan=False)
    source: str
    source_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime


@dataclass(frozen=True)
class _CapturedLine:
    parsed: ParsedStokasticStatLine
    file_sha256: str
    observed_at: datetime
    source: str


@dataclass(frozen=True)
class _Fact:
    player_key: tuple[str, str]
    stat: ProjectedStat
    value: float
    file_sha256: str
    observed_at: datetime
    source: str


def default_stokastic_stats_registry() -> SourceFormatRegistry:
    """Build the explicit production registry for the component-stats source."""

    registry = SourceFormatRegistry()
    registry.register(StokasticStatsSourceFormat())
    return registry


def load_derived_scoring_config(
    path: Path = DEFAULT_DERIVED_SCORING_PATH,
) -> LoadedDerivedScoringConfig:
    """Load strict scoring settings and hash the exact bytes used by a read."""

    try:
        raw_bytes = path.read_bytes()
    except OSError as error:
        raise SourceFormatError(f"cannot read derived scoring config {path}: {error}") from error
    try:
        payload = tomllib.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SourceFormatError(f"invalid derived scoring config {path}: {error}") from error
    try:
        config = DerivedScoringConfig.model_validate(payload)
    except ValidationError as error:
        raise SourceFormatError(f"invalid derived scoring config {path}: {error}") from error
    return LoadedDerivedScoringConfig(
        path=path,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        config=config,
    )


def parse_stokastic_stats(path: Path) -> StokasticStatsParseResult:
    """Parse exactly one of the three real header signatures, refusing drift."""

    try:
        text = path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise SourceFormatError(f"cannot read Stokastic stats CSV {path}: {error}") from error
    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = tuple(reader.fieldnames or ())
    file_kind = _detect_header(headers)
    rows: list[ParsedStokasticStatLine] = []
    for row_number, row in enumerate(reader, start=2):
        if None in row:
            raise SourceFormatError(f"row {row_number}: more cells than header columns")
        rows.append(_parse_row(file_kind, row, row_number))
    receiving = file_kind is StatsFileKind.RECEIVING
    return StokasticStatsParseResult(
        file_kind=file_kind,
        rows_seen=len(rows),
        rows=tuple(rows),
        receptions_are_vendor_placeholder=receiving,
        placeholder_note=RECEPTION_PLACEHOLDER_NOTE if receiving else None,
    )


def newest_stats_capture(snapshot_root: Path, season: int, week: int) -> Path:
    """Return the newest capture directory for the week that manifests a stats file."""

    week_path = snapshot_week_path(snapshot_root, season, week)
    if not week_path.is_dir():
        raise MissingStatsCapture(f"snapshot week does not exist: {week_path}")
    captures = sorted((path for path in week_path.iterdir() if path.is_dir()), reverse=True)
    for capture_path in captures:
        manifest_path = capture_path / MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        manifest = load_manifest(manifest_path)
        if any(record.kind is CaptureKind.STATS for record in manifest.files):
            return capture_path
    raise MissingStatsCapture(
        f"no capture under {week_path} manifests a '{CaptureKind.STATS.value}' file; capture "
        "the three Stokastic Stats exports first with `na-snapshot capture --kind stats`"
    )


def render_stats_load(report: StokasticStatsLoadReport) -> str:
    """Render the load as fixed lines; a hold or a queued identity is never summarized away."""

    lines = [
        f"STOKASTIC STATS LOAD — {report.season} week {report.week:02d}",
        f"  capture      {report.capture_path}",
        f"  files        {report.files_seen} stats file(s), {report.rows_seen} row(s), "
        f"{report.players_seen} identities",
        f"  written      {report.players_written} player(s), "
        f"{report.stat_rows_inserted} stat row(s) inserted, "
        f"{report.duplicate_stat_rows} already loaded",
        f"  slate teams  {report.slate_team_count} on the site's ingested slates; "
        f"{report.out_of_slate_rows} row(s) outside them (stored anyway)",
        f"  note         {report.placeholder_note}",
    ]
    if report.held:
        lines.append(
            f"  HELD         {report.unresolved_fraction:.1%} of identities unresolved exceeds "
            f"the {report.max_unresolved_fraction:.0%} limit; no facts were written"
        )
    for unresolved in report.unresolved:
        reference = (
            "(not queued)"
            if unresolved.unresolved_id is None
            else f"na-crosswalk resolve --unresolved-id {unresolved.unresolved_id} "
            "--player-id <player_id>"
        )
        lines.append(f"  ? unresolved {unresolved.name_raw} {unresolved.team} — {reference}")
    lines.append("")
    return "\n".join(lines)


def load_stokastic_stats_capture(
    connection: sqlite3.Connection,
    capture_path: Path,
    *,
    season: int,
    week: int,
    site: str,
    slate_id: int | None = None,
    registry: SourceFormatRegistry | None = None,
    crosswalk: PlayerCrosswalk | None = None,
    config_path: Path = DEFAULT_DERIVED_SCORING_PATH,
    ingested_at: datetime | None = None,
    run_id: str | None = None,
) -> StokasticStatsLoadReport:
    """Resolve and insert one complete three-file stats capture, or hold all facts."""

    manifest = load_manifest(capture_path / MANIFEST_FILENAME)
    if manifest.season != season or manifest.week != week:
        raise SourceFormatError(
            f"capture is {manifest.season} week {manifest.week}, not {season} week {week}"
        )
    if manifest.errors:
        raise SourceFormatError("stats capture manifest contains errors; refusing a partial load")
    loaded_config = load_derived_scoring_config(config_path)
    identity_crosswalk = crosswalk or PlayerCrosswalk(connection)
    source_registry = registry or default_stokastic_stats_registry()
    ingestion_time = _utc(ingested_at or datetime.now(UTC))

    captured: list[_CapturedLine] = []
    seen_kinds: set[StatsFileKind] = set()
    stats_files = [record for record in manifest.files if record.kind is CaptureKind.STATS]
    for record in stats_files:
        source_path = capture_path.joinpath(*PurePosixPath(record.path).parts)
        actual_hash = sha256_file(source_path)
        if actual_hash != record.sha256:
            raise SourceFormatError(
                f"captured file hash mismatch for {source_path}: "
                f"expected {record.sha256}, got {actual_hash}"
            )
        source_format = source_registry.get(record.source)
        if not isinstance(source_format, _StatsParser):
            raise SourceFormatError(
                f"registered SourceFormat {source_format.name!r} does not parse stats captures"
            )
        parsed_result = source_format.parse_stats(source_path)
        if parsed_result.file_kind in seen_kinds:
            raise SourceFormatError(
                f"capture contains more than one {parsed_result.file_kind.value} stats export"
            )
        seen_kinds.add(parsed_result.file_kind)
        captured.extend(
            _CapturedLine(
                parsed=row,
                file_sha256=record.sha256,
                observed_at=record.observed_at,
                source=record.source.strip().casefold(),
            )
            for row in parsed_result.rows
        )
    expected_kinds = set(StatsFileKind)
    if seen_kinds != expected_kinds:
        missing = ", ".join(sorted(kind.value for kind in expected_kinds - seen_kinds)) or "none"
        raise SourceFormatError(
            f"stats capture must contain one passing, rushing, and receiving export; "
            f"missing: {missing}"
        )
    observed_times = {line.observed_at for line in captured}
    sources = {line.source for line in captured}
    if len(observed_times) != 1:
        raise SourceFormatError("all three stats files must share one capture observation time")
    if len(sources) != 1:
        raise SourceFormatError("all three stats files must share one manifest source")

    identities: dict[tuple[str, str], ParsedStokasticStatLine] = {}
    identity_hashes: dict[tuple[str, str], set[str]] = {}
    facts: dict[tuple[tuple[str, str], ProjectedStat], _Fact] = {}
    for line in captured:
        player_key = (normalize_name(line.parsed.name_raw), normalize_team_code(line.parsed.team))
        identities.setdefault(player_key, line.parsed)
        identity_hashes.setdefault(player_key, set()).add(line.file_sha256)
        for stat, value in line.parsed.stats.items():
            fact_key = (player_key, stat)
            if fact_key in facts:
                raise SourceFormatError(
                    f"duplicate {stat.value} for {line.parsed.name_raw} on {line.parsed.team}"
                )
            facts[fact_key] = _Fact(
                player_key=player_key,
                stat=stat,
                value=value,
                file_sha256=line.file_sha256,
                observed_at=line.observed_at,
                source=line.source,
            )

    resolved: dict[tuple[str, str], int] = {}
    unresolved: list[UnresolvedStatsPlayer] = []
    source = next(iter(sources))
    observed_at = next(iter(observed_times))
    for player_key, identity_line in sorted(identities.items()):
        provenance_hash = hashlib.sha256(
            "\n".join(sorted(identity_hashes[player_key])).encode("ascii")
        ).hexdigest()
        match = identity_crosswalk.match(
            PlayerIdentityInput(
                source=source,
                site=None,
                external_player_id=None,
                name_raw=identity_line.name_raw,
                team=identity_line.team,
                opponent=identity_line.opponent,
                position=None,
                observed_at=observed_at,
                ingested_at=ingestion_time,
                source_file_sha256=provenance_hash,
                run_id=run_id,
            )
        )
        if match.player_id is None:
            unresolved.append(
                UnresolvedStatsPlayer(
                    name_raw=identity_line.name_raw,
                    team=normalize_team_code(identity_line.team),
                    unresolved_id=match.unresolved_id,
                )
            )
        else:
            resolved[player_key] = match.player_id

    unresolved_fraction = len(unresolved) / len(identities) if identities else 0.0
    maximum = loaded_config.config.ingest.max_unresolved_fraction
    slate_teams = _slate_teams(connection, season=season, week=week, site=site, slate_id=slate_id)
    out_of_slate_rows = sum(
        normalize_team_code(line.parsed.team) not in slate_teams for line in captured
    )
    if unresolved_fraction > maximum:
        return StokasticStatsLoadReport(
            capture_path=capture_path,
            season=season,
            week=week,
            source=source,
            files_seen=len(stats_files),
            rows_seen=len(captured),
            players_seen=len(identities),
            players_written=0,
            stat_rows_inserted=0,
            duplicate_stat_rows=0,
            unresolved_fraction=unresolved_fraction,
            max_unresolved_fraction=maximum,
            unresolved=tuple(unresolved),
            held=True,
            out_of_slate_rows=out_of_slate_rows,
            slate_team_count=len(slate_teams),
            receptions_are_vendor_placeholder=True,
            placeholder_note=RECEPTION_PLACEHOLDER_NOTE,
        )

    inserted = 0
    duplicates = 0
    written_players: set[int] = set()
    connection.execute("SAVEPOINT stokastic_stats_facts")
    try:
        for fact in facts.values():
            player_id = resolved.get(fact.player_key)
            if player_id is None:
                continue
            outcome = _insert_fact(
                connection,
                fact,
                season=season,
                week=week,
                player_id=player_id,
                ingested_at=ingestion_time,
                run_id=run_id,
            )
            inserted += int(outcome == "inserted")
            duplicates += int(outcome == "duplicate")
            if outcome == "inserted":
                written_players.add(player_id)
    except Exception:
        connection.execute("ROLLBACK TO stokastic_stats_facts")
        connection.execute("RELEASE stokastic_stats_facts")
        raise
    connection.execute("RELEASE stokastic_stats_facts")
    return StokasticStatsLoadReport(
        capture_path=capture_path,
        season=season,
        week=week,
        source=source,
        files_seen=len(stats_files),
        rows_seen=len(captured),
        players_seen=len(identities),
        players_written=len(written_players),
        stat_rows_inserted=inserted,
        duplicate_stat_rows=duplicates,
        unresolved_fraction=unresolved_fraction,
        max_unresolved_fraction=maximum,
        unresolved=tuple(unresolved),
        held=False,
        out_of_slate_rows=out_of_slate_rows,
        slate_team_count=len(slate_teams),
        receptions_are_vendor_placeholder=True,
        placeholder_note=RECEPTION_PLACEHOLDER_NOTE,
    )


def read_derived_projection_means(
    connection: sqlite3.Connection,
    *,
    season: int,
    week: int,
    site: str,
    source: str = STOKASTIC_SOURCE,
    config_path: Path = DEFAULT_DERIVED_SCORING_PATH,
    as_of: datetime | None = None,
    slate_id: int | None = None,
) -> tuple[DerivedProjectionMean, ...]:
    """Score latest component means without bonuses or ambiguous fumble deductions."""

    loaded = load_derived_scoring_config(config_path)
    canonical_site = normalize_site(site).value
    weights = getattr(loaded.config.sites, canonical_site)
    coefficients = {
        ProjectedStat.PASS_YDS.value: weights.pass_yds,
        ProjectedStat.PASS_TD.value: weights.pass_td,
        ProjectedStat.PASS_INT.value: weights.pass_int,
        ProjectedStat.RUSH_YDS.value: weights.rush_yds,
        ProjectedStat.RUSH_TD.value: weights.rush_td,
        ProjectedStat.RECEPTIONS.value: weights.receptions,
        ProjectedStat.REC_YDS.value: weights.rec_yds,
        ProjectedStat.REC_TD.value: weights.rec_td,
    }
    cutoff_sql = "AND valid_to IS NULL"
    parameters: list[object] = [source.strip().casefold(), season, week]
    cutoff_text: str | None = None
    if as_of is not None:
        cutoff_text = utc_timestamp(_utc(as_of))
        cutoff_sql = (
            "AND observed_at <= ? AND ingested_at <= ? AND valid_from <= ? "
            "AND (valid_to IS NULL OR valid_to > ?)"
        )
        parameters.extend([cutoff_text, cutoff_text, cutoff_text, cutoff_text])
    slate_teams = (
        None
        if slate_id is None
        else _slate_teams(
            connection, season=season, week=week, site=canonical_site, slate_id=slate_id
        )
    )
    rows = connection.execute(
        f"""
        WITH eligible AS (
            SELECT player_id, stat, value, observed_at
            FROM projected_stats
            WHERE source = ? AND season = ? AND week = ? {cutoff_sql}
        ), latest_capture AS (
            SELECT max(observed_at) AS observed_at FROM eligible
        )
        SELECT eligible.player_id, eligible.stat, eligible.value, eligible.observed_at,
               players.canonical_name
        FROM eligible
        JOIN latest_capture ON latest_capture.observed_at = eligible.observed_at
        JOIN players ON players.player_id = eligible.player_id
        ORDER BY eligible.player_id, eligible.stat
        """,
        tuple(parameters),
    ).fetchall()
    totals: dict[int, float] = {}
    names: dict[int, str] = {}
    observations: dict[int, datetime] = {}
    for row in rows:
        stat = str(row["stat"])
        coefficient = coefficients.get(stat)
        if coefficient is None:
            continue
        player_id = int(row["player_id"])
        totals[player_id] = totals.get(player_id, 0.0) + float(row["value"]) * coefficient
        names[player_id] = str(row["canonical_name"])
        observation = _parse_timestamp(str(row["observed_at"]))
        observations[player_id] = max(observations.get(player_id, observation), observation)

    derived = [
        DerivedProjectionMean(
            player_id=player_id,
            canonical_name=names[player_id],
            team=_player_team(
                connection,
                player_id=player_id,
                season=season,
                week=week,
                as_of=cutoff_text,
            ),
            site=canonical_site,
            projection_mean=mean,
            source=DERIVED_SOURCE,
            source_version=loaded.sha256,
            observed_at=observations[player_id],
        )
        for player_id, mean in totals.items()
        if slate_teams is None
        or _player_team(
            connection,
            player_id=player_id,
            season=season,
            week=week,
            as_of=cutoff_text,
        )
        in slate_teams
    ]
    return tuple(sorted(derived, key=lambda item: (-item.projection_mean, item.canonical_name)))


def render_derived_projection_means(
    rows: tuple[DerivedProjectionMean, ...],
    *,
    season: int,
    week: int,
    site: str,
    slate_id: int | None = None,
    out_of_slate_rows: int | None = None,
) -> str:
    """Render the read-only operator comparison with its derived-source labels."""

    canonical_site = normalize_site(site).value
    header = f"STOKASTIC DERIVED STATS — {season} week {week:02d} {canonical_site}"
    if slate_id is not None:
        header += (
            f" — slate {slate_id}; {out_of_slate_rows or 0} export row(s) outside the slate"
        )
    if not rows:
        return f"{header}\n  no projected stats loaded\n"
    lines = [
        header,
        f"  source         {rows[0].source}",
        f"  source version {rows[0].source_version}",
        "  bonuses        excluded (expected threshold bonuses cannot be inferred)",
        "  fumbles        excluded (fumbles-lost units are unknown)",
        "",
        f"  {'MEAN':>8}  {'TEAM':<4} {'PLAYER ID':>9}  PLAYER",
    ]
    lines.extend(
        f"  {row.projection_mean:>8.3f}  {(row.team or '—'):<4} "
        f"{row.player_id:>9}  {row.canonical_name}"
        for row in rows
    )
    lines.append("")
    return "\n".join(lines)


def _detect_header(headers: tuple[str, ...]) -> StatsFileKind:
    for file_kind, expected in _HEADERS.items():
        if headers == expected:
            return file_kind
    header_set = set(headers)
    closest_kind, closest = min(
        _HEADERS.items(),
        key=lambda item: (len(set(item[1]) ^ header_set), item[0].value),
    )
    missing = sorted(set(closest) - header_set)
    unexpected = sorted(header_set - set(closest))
    missing_text = ", ".join(missing) or "none"
    unexpected_text = ", ".join(unexpected) or "none"
    order_note = ""
    if not missing and not unexpected:
        order_note = f"; expected column order: {', '.join(closest)}"
    raise SourceFormatError(
        f"unknown or drifted Stokastic stats header (closest: {closest_kind.value}); "
        f"missing columns: {missing_text}; unexpected columns: {unexpected_text}{order_note}"
    )


def _parse_row(
    file_kind: StatsFileKind, row: Mapping[str, str | None], row_number: int
) -> ParsedStokasticStatLine:
    name = _text(row, "Player", row_number)
    team = _text(row, "Team", row_number)
    opponent = _text(row, "Opp", row_number)
    if file_kind is StatsFileKind.PASSING:
        stats = {
            ProjectedStat.PASS_ATT: _number(row, "Att", row_number),
            ProjectedStat.PASS_CMP: _number(row, "Comp", row_number),
            ProjectedStat.PASS_YDS: _number(row, "Pass Yds", row_number),
            ProjectedStat.PASS_TD: _number(row, "TD", row_number),
            ProjectedStat.PASS_INT: _number(row, "INT", row_number),
            ProjectedStat.PASS_FUMBLES: _number(row, "Fum", row_number),
        }
    elif file_kind is StatsFileKind.RUSHING:
        stats = {
            ProjectedStat.RUSH_ATT: _number(row, "Rush", row_number),
            ProjectedStat.RUSH_YDS: _number(row, "Rush Yds", row_number),
            ProjectedStat.RUSH_TD: _number(row, "TD", row_number),
            ProjectedStat.RUSH_FUMBLES: _number(row, "Fum", row_number),
        }
    else:
        targets = _number(row, "Tgt", row_number)
        target_share = _percentage(row, "Tgt %", row_number)
        receptions = _number(row, "Rec", row_number)
        catch_rate = _percentage(row, "Catch %", row_number)
        receiving_yards = _number(row, "Rec Yds", row_number)
        yards_per_catch = _number(row, "YPC", row_number)
        if not math.isclose(catch_rate, 0.75, rel_tol=0.0, abs_tol=1e-12):
            raise SourceFormatError(
                f"row {row_number} Catch % is {catch_rate:.6f}, expected the 0.75 placeholder"
            )
        if abs(receptions - 0.75 * targets) > RECEPTION_PLACEHOLDER_TOLERANCE:
            raise SourceFormatError(
                f"row {row_number} Rec {receptions} is not approximately 0.75 x Tgt {targets}"
            )
        if abs(receiving_yards - receptions * yards_per_catch) > RECEIVING_YARDS_TOLERANCE:
            raise SourceFormatError(
                f"row {row_number} Rec Yds {receiving_yards} is not approximately "
                f"Rec {receptions} x YPC {yards_per_catch}"
            )
        stats = {
            ProjectedStat.TARGETS: targets,
            ProjectedStat.TARGET_SHARE: target_share,
            ProjectedStat.RECEPTIONS: receptions,
            ProjectedStat.REC_YDS: receiving_yards,
            ProjectedStat.REC_TD: _number(row, "TD", row_number),
            ProjectedStat.REC_FUMBLES: _number(row, "Fum", row_number),
        }
    return ParsedStokasticStatLine(
        name_raw=name,
        team=team,
        opponent=opponent,
        position=None,
        external_player_id=None,
        source_version=STOKASTIC_STATS_FORMAT_VERSION,
        row_number=row_number,
        file_kind=file_kind,
        stats=stats,
    )


def _text(row: Mapping[str, str | None], column: str, row_number: int) -> str:
    value = row.get(column)
    if value is None or not value.strip():
        raise SourceFormatError(f"row {row_number} column {column!r} is empty")
    return value.strip()


def _number(row: Mapping[str, str | None], column: str, row_number: int) -> float:
    raw = _text(row, column, row_number)
    try:
        value = float(raw)
    except ValueError as error:
        raise SourceFormatError(
            f"row {row_number} column {column!r} is not numeric: {raw!r}"
        ) from error
    if not math.isfinite(value) or value < 0:
        raise SourceFormatError(
            f"row {row_number} column {column!r} must be a finite non-negative number"
        )
    return value


def _percentage(row: Mapping[str, str | None], column: str, row_number: int) -> float:
    raw = _text(row, column, row_number)
    if not raw.endswith("%"):
        raise SourceFormatError(
            f"row {row_number} percentage column {column!r} must carry an explicit % sign"
        )
    value = _number({column: raw[:-1]}, column, row_number) / 100.0
    if value > 1:
        raise SourceFormatError(f"row {row_number} column {column!r} exceeds 100%")
    return value


def _insert_fact(
    connection: sqlite3.Connection,
    fact: _Fact,
    *,
    season: int,
    week: int,
    player_id: int,
    ingested_at: datetime,
    run_id: str | None,
) -> str:
    observed_text = utc_timestamp(fact.observed_at)
    existing = connection.execute(
        """
        SELECT value, file_sha256, published_at, source_version
        FROM projected_stats
        WHERE source = ? AND season = ? AND week = ? AND player_id = ?
          AND stat = ? AND observed_at = ?
        """,
        (
            fact.source,
            season,
            week,
            player_id,
            fact.stat.value,
            observed_text,
        ),
    ).fetchone()
    content = (fact.value, fact.file_sha256, None, STOKASTIC_STATS_FORMAT_VERSION)
    if existing is not None:
        if tuple(existing) == content:
            return "duplicate"
        raise SourceFormatError(
            f"projected_stats key conflict for source={fact.source} season={season} "
            f"week={week} player_id={player_id} stat={fact.stat.value} "
            f"observed_at={observed_text}: existing content differs"
        )
    connection.execute(
        """
        INSERT INTO projected_stats(
            source, season, week, player_id, stat, value, file_sha256,
            published_at, observed_at, ingested_at, effective_at, valid_from,
            valid_to, source_version, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)
        """,
        (
            fact.source,
            season,
            week,
            player_id,
            fact.stat.value,
            fact.value,
            fact.file_sha256,
            optional_utc_timestamp(None),
            observed_text,
            utc_timestamp(ingested_at),
            observed_text,
            STOKASTIC_STATS_FORMAT_VERSION,
            run_id,
        ),
    )
    return "inserted"


def _slate_teams(
    connection: sqlite3.Connection,
    *,
    season: int,
    week: int,
    site: str,
    slate_id: int | None,
) -> set[str]:
    canonical_site = normalize_site(site).value
    parameters: list[object] = [season, week, canonical_site]
    slate_clause = ""
    if slate_id is not None:
        slate_clause = "AND slates.slate_id = ?"
        parameters.append(slate_id)
    rows = connection.execute(
        f"""
        SELECT DISTINCT teams.abbreviation
        FROM slates
        JOIN salaries ON salaries.slate_id = slates.slate_id
        JOIN teams ON teams.team_id = salaries.team_id
        WHERE slates.season = ? AND slates.week = ? AND slates.site = ? {slate_clause}
        """,
        tuple(parameters),
    ).fetchall()
    return {normalize_team_code(str(row["abbreviation"])) for row in rows}


def _player_team(
    connection: sqlite3.Connection,
    *,
    player_id: int,
    season: int,
    week: int,
    as_of: str | None,
) -> str | None:
    cutoff = ""
    parameters: list[object] = [player_id, season, week]
    if as_of is not None:
        cutoff = "AND observed_at <= ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"
        parameters.extend([as_of, as_of, as_of])
    else:
        cutoff = "AND valid_to IS NULL"
    row = connection.execute(
        f"""
        SELECT team FROM player_team_history
        WHERE player_id = ? AND (season IS NULL OR season = ?)
          AND (week IS NULL OR week = ?) {cutoff}
        ORDER BY observed_at DESC, player_team_history_id DESC
        LIMIT 1
        """,
        tuple(parameters),
    ).fetchone()
    return None if row is None else normalize_team_code(str(row["team"]))


def _utc(value: datetime) -> datetime:
    try:
        return ensure_utc(value)
    except ValueError as error:
        raise SourceFormatError("timestamps must include a timezone") from error


def _parse_timestamp(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
