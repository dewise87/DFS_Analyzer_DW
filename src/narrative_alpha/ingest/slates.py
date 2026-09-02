"""Manifest-driven slate and salary ingestion from captured DK/FD salary exports.

A salary export is the only file that names a slate, so this loader is also the only
writer of ``slates`` rows. Salary exports carry no slate id, so one is derived
deterministically from site, season, week, slate type, and the slate's earliest kickoff:
re-downloading the same slate on Sunday resolves to the same ``external_slate_id`` and
therefore the same ``slate_id``, and a genuinely different slate resolves to a different
one. Everything else follows :mod:`narrative_alpha.ingest.projections`: hashes are verified
against the manifest, players go through the crosswalk and are never guessed, rows are
insert-only with the capture's observation time, and reloading a capture changes nothing.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from narrative_alpha.identity import PlayerCrosswalk, PlayerIdentityInput
from narrative_alpha.identity.normalization import normalize_team_code, team_code_variants
from narrative_alpha.ingest.salaries import (
    ParsedSalaryRow,
    SalaryCsvError,
    SalaryParseResult,
    SalarySite,
    SalarySlateType,
    parse_salary_csv,
)
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.snapshots import (
    MANIFEST_FILENAME,
    CaptureKind,
    load_manifest,
    sha256_file,
)
from narrative_alpha.snapshots.core import snapshot_week_path

SLATE_INGEST_VERSION = "slate-ingest-v1"
"""Written to ``source_version`` so a later loader change is visible in the rows."""

_SITE_ALIASES: dict[str, SalarySite] = {
    "dk": SalarySite.DRAFTKINGS,
    "draftkings": SalarySite.DRAFTKINGS,
    "fd": SalarySite.FANDUEL,
    "fanduel": SalarySite.FANDUEL,
}
_ROSTER_SLOT_ONLY = frozenset({"FLEX", "CPT", "MVP"})
_DEFENSE_POSITIONS = frozenset({"DST", "D", "DEF"})
DEFENSE_PLAYER_SOURCE = "slate-ingest-defense"


class SlateIngestError(RuntimeError):
    """Raised when capture integrity, site, or slate configuration is invalid."""


def normalize_site(value: str) -> SalarySite:
    """Map ``dk``/``fd``/``draftkings``/``fanduel`` onto the canonical site."""

    try:
        return _SITE_ALIASES[value.strip().casefold()]
    except KeyError as error:
        raise SlateIngestError("site must be dk, fd, draftkings, or fanduel") from error


class SalaryChange(BaseModel):
    """One player's salary move between two observations of the same slate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    player_id: int
    site_player_id: str
    name_raw: str
    previous_salary: int = Field(ge=0)
    previous_observed_at: datetime
    salary: int = Field(ge=0)


class UnresolvedSalaryPlayer(BaseModel):
    """A salary row the crosswalk refused to match, named so it can be resolved."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    unresolved_id: int | None
    site_player_id: str
    name_raw: str
    team: str
    position: str


class SlateLoadResult(BaseModel):
    """What one distinct slate in the capture contributed to the store."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slate_id: int
    external_slate_id: str
    site: str
    slate_type: str
    name: str
    starts_at: datetime
    locks_at: datetime
    observed_at: datetime
    slate_inserted: bool
    rows_parsed: int = Field(ge=0)
    salary_rows_inserted: int = Field(ge=0)
    duplicate_rows: int = Field(ge=0)
    salary_changes: tuple[SalaryChange, ...] = ()
    unresolved: tuple[UnresolvedSalaryPlayer, ...] = ()
    games_inserted: int = Field(default=0, ge=0)
    teams_inserted: int = Field(default=0, ge=0)
    matchups_without_kickoff: tuple[str, ...] = ()
    source_files: tuple[str, ...] = ()


class SlateLoadReport(BaseModel):
    """The whole ingest as data, so the CLI and the ops lane cannot diverge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capture_path: str
    season: int = Field(ge=1)
    week: int = Field(ge=1, le=99)
    site: str
    observed_at: datetime
    files_seen: int = Field(ge=0)
    files_skipped: tuple[str, ...] = ()
    rows_seen: int = Field(ge=0)
    rows_rejected: int = Field(ge=0)
    slates: tuple[SlateLoadResult, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def salary_rows_inserted(self) -> int:
        return sum(slate.salary_rows_inserted for slate in self.slates)

    @property
    def duplicate_rows(self) -> int:
        return sum(slate.duplicate_rows for slate in self.slates)

    @property
    def unresolved_rows(self) -> int:
        return sum(len(slate.unresolved) for slate in self.slates)

    @property
    def ok(self) -> bool:
        return not self.errors and self.unresolved_rows == 0 and self.rows_rejected == 0


@dataclass(frozen=True)
class _SlateGroup:
    """One distinct slate assembled from the capture's salary files."""

    external_slate_id: str
    site: SalarySite
    slate_type: SalarySlateType
    starts_at: datetime
    observed_at: datetime
    rows: tuple[tuple[ParsedSalaryRow, str, datetime], ...]
    source_files: tuple[str, ...]


@dataclass(frozen=True)
class _InsertOutcome:
    inserted: bool = False
    duplicate: bool = False
    error: str | None = None
    change: SalaryChange | None = None


def newest_salary_capture(snapshot_root: Path, season: int, week: int) -> Path:
    """Return the newest capture directory for the week that manifests a salary file."""

    week_path = snapshot_week_path(snapshot_root, season, week)
    if not week_path.is_dir():
        raise SlateIngestError(f"snapshot week does not exist: {week_path}")

    captures = sorted((path for path in week_path.iterdir() if path.is_dir()), reverse=True)
    for capture_path in captures:
        manifest_path = capture_path / MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        manifest = load_manifest(manifest_path)
        if any(record.kind is CaptureKind.SALARIES for record in manifest.files):
            return capture_path

    raise SlateIngestError(
        f"no capture under {week_path} manifests a '{CaptureKind.SALARIES.value}' file; "
        "capture the salary export first with `na-snapshot capture --kind salaries`"
    )


def load_salary_capture(
    connection: sqlite3.Connection,
    capture_path: Path,
    *,
    season: int,
    week: int,
    site: str | SalarySite,
    slate_name: str | None = None,
    starts_at: datetime | None = None,
    crosswalk: PlayerCrosswalk | None = None,
    ingested_at: datetime | None = None,
    run_id: str | None = None,
) -> SlateLoadReport:
    """Load a capture's salary exports into point-in-time slate and salary rows."""

    salary_site = site if isinstance(site, SalarySite) else normalize_site(site)
    manifest = load_manifest(capture_path / MANIFEST_FILENAME)
    if manifest.season != season or manifest.week != week:
        raise SlateIngestError(
            f"capture {capture_path} is season {manifest.season} week {manifest.week:02d}, "
            f"not the requested season {season} week {week:02d}"
        )
    if starts_at is not None:
        starts_at = _utc(starts_at)

    identity_crosswalk = crosswalk or PlayerCrosswalk(connection)
    ingestion_time = _utc(ingested_at or datetime.now(UTC))

    errors = [
        f"capture error [{error.error_type}] {error.source}: {error.message}"
        for error in manifest.errors
    ]
    files_seen = 0
    files_skipped: list[str] = []
    rows_seen = 0
    rows_rejected = 0
    groups: dict[str, _SlateGroup] = {}

    for file_record in manifest.files:
        if file_record.kind is not CaptureKind.SALARIES:
            continue
        files_seen += 1
        source_path = capture_path.joinpath(*PurePosixPath(file_record.path).parts)
        actual_hash = sha256_file(source_path)
        if actual_hash != file_record.sha256:
            raise SlateIngestError(
                f"captured file hash mismatch for {source_path}: "
                f"expected {file_record.sha256}, got {actual_hash}"
            )
        try:
            parsed = parse_salary_csv(source_path)
        except SalaryCsvError as error:
            errors.append(f"{file_record.path}: {error}")
            continue
        if _format_site(parsed) is not salary_site:
            files_skipped.append(file_record.path)
            continue

        rows_seen += parsed.parse_report.rows_seen
        rows_rejected += parsed.parse_report.rows_rejected
        errors.extend(
            f"{file_record.path} row {rejected.row_number}: {'; '.join(rejected.reasons)}"
            for rejected in parsed.parse_report.rejected
        )
        try:
            group = _group_for_file(
                parsed,
                file_record.path,
                file_record.sha256,
                file_record.observed_at,
                season=season,
                week=week,
                starts_at=starts_at,
                observed_at=manifest.captured_at,
            )
        except SlateIngestError as error:
            errors.append(f"{file_record.path}: {error}")
            continue
        groups[group.external_slate_id] = _merge_group(
            groups.get(group.external_slate_id), group
        )

    slates: list[SlateLoadResult] = []
    for external_slate_id in sorted(groups):
        slates.append(
            _load_slate_group(
                connection,
                groups[external_slate_id],
                season=season,
                week=week,
                slate_name=slate_name,
                crosswalk=identity_crosswalk,
                ingested_at=ingestion_time,
                run_id=run_id,
                errors=errors,
            )
        )

    return SlateLoadReport(
        capture_path=str(capture_path),
        season=season,
        week=week,
        site=salary_site.value,
        observed_at=manifest.captured_at,
        files_seen=files_seen,
        files_skipped=tuple(files_skipped),
        rows_seen=rows_seen,
        rows_rejected=rows_rejected,
        slates=tuple(slates),
        errors=tuple(errors),
    )


def _format_site(parsed: SalaryParseResult) -> SalarySite:
    site_text, _, _ = parsed.salary_format.value.partition("_")
    return SalarySite(site_text)


def _format_slate_type(parsed: SalaryParseResult) -> SalarySlateType:
    _, _, type_text = parsed.salary_format.value.partition("_")
    return SalarySlateType(type_text)


def _group_for_file(
    parsed: SalaryParseResult,
    file_path: str,
    file_sha256: str,
    file_observed_at: datetime,
    *,
    season: int,
    week: int,
    starts_at: datetime | None,
    observed_at: datetime,
) -> _SlateGroup:
    site = _format_site(parsed)
    slate_type = _format_slate_type(parsed)
    kickoffs = tuple(row.game_time for row in parsed.rows if row.game_time is not None)
    if kickoffs:
        earliest = min(kickoffs)
        if starts_at is not None and starts_at != earliest:
            raise SlateIngestError(
                f"--starts-at {utc_timestamp(starts_at)} contradicts the earliest kickoff "
                f"{utc_timestamp(earliest)} carried by the export; omit it"
            )
    elif starts_at is not None:
        earliest = starts_at
    else:
        raise SlateIngestError(
            "the export carries no kickoff time (FanDuel classic exports omit it); "
            "pass --starts-at with the slate's first kickoff, and use the same value "
            "for every re-download of this slate"
        )

    return _SlateGroup(
        external_slate_id=_external_slate_id(site, season, week, slate_type, earliest),
        site=site,
        slate_type=slate_type,
        starts_at=earliest,
        observed_at=observed_at,
        rows=tuple((row, file_sha256, file_observed_at) for row in parsed.rows),
        source_files=(file_path,),
    )


def _merge_group(existing: _SlateGroup | None, group: _SlateGroup) -> _SlateGroup:
    if existing is None:
        return group
    return _SlateGroup(
        external_slate_id=group.external_slate_id,
        site=group.site,
        slate_type=group.slate_type,
        starts_at=group.starts_at,
        observed_at=min(existing.observed_at, group.observed_at),
        rows=existing.rows + group.rows,
        source_files=existing.source_files + group.source_files,
    )


def _external_slate_id(
    site: SalarySite,
    season: int,
    week: int,
    slate_type: SalarySlateType,
    starts_at: datetime,
) -> str:
    """Derive the slate key salary exports do not carry, from what they do carry."""

    kickoff = _utc(starts_at).strftime("%Y%m%dT%H%M%SZ")
    return f"{site.value}:{season}:w{week:02d}:{slate_type.value}:{kickoff}"


def _load_slate_group(
    connection: sqlite3.Connection,
    group: _SlateGroup,
    *,
    season: int,
    week: int,
    slate_name: str | None,
    crosswalk: PlayerCrosswalk,
    ingested_at: datetime,
    run_id: str | None,
    errors: list[str],
) -> SlateLoadResult:
    name = (slate_name or group.external_slate_id).strip() or group.external_slate_id
    slate_id, slate_inserted, slate_errors = _resolve_slate(
        connection,
        group,
        season=season,
        week=week,
        name=name,
        ingested_at=ingested_at,
        run_id=run_id,
    )
    errors.extend(slate_errors)

    teams: dict[str, int] = {}
    teams_inserted = 0
    games: dict[tuple[str, str], int | None] = {}
    games_inserted = 0
    matchups_without_kickoff: set[str] = set()
    salary_rows_inserted = 0
    duplicate_rows = 0
    changes: list[SalaryChange] = []
    unresolved: list[UnresolvedSalaryPlayer] = []

    for parsed_row, file_sha256, observed_at in group.rows:
        for code in (parsed_row.team, parsed_row.opponent):
            canonical = normalize_team_code(code)
            if canonical in teams:
                continue
            resolved, created = _resolve_team(
                connection,
                code,
                observed_at=observed_at,
                ingested_at=ingested_at,
                source=group.site.value,
                run_id=run_id,
            )
            teams[canonical] = resolved
            teams_inserted += int(created)
        team_id = teams[normalize_team_code(parsed_row.team)]
        opponent_id = teams[normalize_team_code(parsed_row.opponent)]

        matchup = _matchup_key(parsed_row)
        if matchup not in games:
            game_id, created, conflict = _resolve_game(
                connection,
                parsed_row,
                season=season,
                week=week,
                team_ids=teams,
                observed_at=observed_at,
                ingested_at=ingested_at,
                source=group.site.value,
                run_id=run_id,
            )
            games[matchup] = game_id
            games_inserted += int(created)
            if conflict is not None:
                errors.append(conflict)
        game_id = games[matchup]
        if game_id is None:
            matchups_without_kickoff.add(_matchup_label(parsed_row))

        if parsed_row.listed_position.upper() in _DEFENSE_POSITIONS:
            # A team defense is not a person and never appears on the nflverse roster:
            # every site names it differently ("Packers", "Green Bay Packers"), so it
            # resolves deterministically to one canonical defense row per franchise.
            player_id: int | None = _resolve_team_defense(
                connection,
                normalize_team_code(parsed_row.team),
                observed_at=observed_at,
                ingested_at=ingested_at,
                run_id=run_id,
            )
            unresolved_id: int | None = None
        else:
            match = crosswalk.match(
                PlayerIdentityInput(
                    source=group.site.value,
                    site=group.site.value,
                    external_player_id=parsed_row.site_player_id,
                    name_raw=parsed_row.name_raw,
                    team=parsed_row.team,
                    opponent=parsed_row.opponent,
                    position=parsed_row.listed_position,
                    roster_status=parsed_row.player_status,
                    eligible_positions=_eligible_positions(parsed_row),
                    observed_at=observed_at,
                    ingested_at=ingested_at,
                    source_file_sha256=file_sha256,
                    run_id=run_id,
                )
            )
            player_id = match.player_id
            unresolved_id = match.unresolved_id
        if player_id is None:
            unresolved.append(
                UnresolvedSalaryPlayer(
                    unresolved_id=unresolved_id,
                    site_player_id=parsed_row.site_player_id,
                    name_raw=parsed_row.name_raw,
                    team=normalize_team_code(parsed_row.team),
                    position=parsed_row.listed_position,
                )
            )
            continue

        outcome = _insert_salary(
            connection,
            parsed_row,
            slate_id=slate_id,
            player_id=player_id,
            team_id=team_id,
            opponent_team_id=opponent_id,
            game_id=game_id,
            site=group.site,
            file_sha256=file_sha256,
            observed_at=observed_at,
            ingested_at=ingested_at,
            run_id=run_id,
        )
        salary_rows_inserted += int(outcome.inserted)
        duplicate_rows += int(outcome.duplicate)
        if outcome.error is not None:
            errors.append(outcome.error)
        if outcome.change is not None:
            changes.append(outcome.change)

    return SlateLoadResult(
        slate_id=slate_id,
        external_slate_id=group.external_slate_id,
        site=group.site.value,
        slate_type=group.slate_type.value,
        name=name,
        starts_at=group.starts_at,
        locks_at=group.starts_at,
        observed_at=group.observed_at,
        slate_inserted=slate_inserted,
        rows_parsed=len(group.rows),
        salary_rows_inserted=salary_rows_inserted,
        duplicate_rows=duplicate_rows,
        salary_changes=tuple(changes),
        unresolved=tuple(unresolved),
        games_inserted=games_inserted,
        teams_inserted=teams_inserted,
        matchups_without_kickoff=tuple(sorted(matchups_without_kickoff)),
        source_files=tuple(sorted(set(group.source_files))),
    )


def _resolve_slate(
    connection: sqlite3.Connection,
    group: _SlateGroup,
    *,
    season: int,
    week: int,
    name: str,
    ingested_at: datetime,
    run_id: str | None,
) -> tuple[int, bool, tuple[str, ...]]:
    """Return the stable ``slate_id`` for this key, inserting it once.

    A slate is an identity that salaries, projections, episodes, and features all point
    at, so its id must not change when Sunday's export is loaded on top of Saturday's.
    The kickoff is part of the key, so a slate whose first game moved is a different
    slate; anything else that differs is a conflict the operator has to see, never a
    silent update.
    """

    existing = connection.execute(
        """
        SELECT slate_id, slate_type, season, week, name, starts_at, locks_at
        FROM slates
        WHERE site = ? AND external_slate_id = ?
        ORDER BY observed_at, slate_id
        LIMIT 1
        """,
        (group.site.value, group.external_slate_id),
    ).fetchone()
    starts_text = utc_timestamp(group.starts_at)
    if existing is not None:
        stored = (
            str(existing["slate_type"]),
            int(existing["season"]),
            int(existing["week"]),
            str(existing["name"]),
            str(existing["starts_at"]),
            str(existing["locks_at"]),
        )
        requested = (
            group.slate_type.value,
            season,
            week,
            name,
            starts_text,
            starts_text,
        )
        if stored != requested:
            differing = ", ".join(
                field
                for field, before, after in zip(
                    ("slate_type", "season", "week", "name", "starts_at", "locks_at"),
                    stored,
                    requested,
                    strict=True,
                )
                if before != after
            )
            return (
                int(existing["slate_id"]),
                False,
                (
                    f"slate {group.external_slate_id} already exists with a different "
                    f"{differing}; slate rows are never updated, so the existing slate "
                    f"{existing['slate_id']} was used unchanged",
                ),
            )
        return int(existing["slate_id"]), False, ()

    cursor = connection.execute(
        """
        INSERT INTO slates(
            external_slate_id, site, slate_type, season, week, name, starts_at,
            locks_at, source, published_at, observed_at, ingested_at, effective_at,
            valid_from, valid_to, source_version, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?, NULL, ?, ?)
        """,
        (
            group.external_slate_id,
            group.site.value,
            group.slate_type.value,
            season,
            week,
            name,
            starts_text,
            starts_text,
            group.site.value,
            utc_timestamp(group.observed_at),
            utc_timestamp(ingested_at),
            utc_timestamp(group.observed_at),
            SLATE_INGEST_VERSION,
            run_id,
        ),
    )
    if cursor.lastrowid is None:  # pragma: no cover - SQLite always supplies a rowid
        raise SlateIngestError(f"could not insert slate {group.external_slate_id}")
    return int(cursor.lastrowid), True, ()


def _resolve_team(
    connection: sqlite3.Connection,
    code: str,
    *,
    observed_at: datetime,
    ingested_at: datetime,
    source: str,
    run_id: str | None,
) -> tuple[int, bool]:
    """Return the ``team_id`` for a site team code, recording an unseen one as observed.

    ``salaries.team_id`` is NOT NULL and nothing else in the pipeline writes ``teams``,
    so the salary export is where a franchise first appears. Only what the file states
    is stored: the canonical code, as its own key and name. A later source carrying real
    franchise names versions the row rather than being blocked by a placeholder.

    A franchise is an identity, not an observation, so the earliest row for the code is
    reused whatever order captures are loaded in; a second ``team_id`` for the same
    franchise would silently split a slate's salaries.
    """

    canonical = normalize_team_code(code)
    variants = team_code_variants(code)
    placeholders = ", ".join("?" for _ in variants)
    observed_text = utc_timestamp(observed_at)
    row = connection.execute(
        f"""
        SELECT team_id FROM teams
        WHERE upper(abbreviation) IN ({placeholders})
        ORDER BY valid_from, team_id
        LIMIT 1
        """,
        variants,
    ).fetchone()
    if row is not None:
        return int(row["team_id"]), False

    cursor = connection.execute(
        """
        INSERT INTO teams(
            team_key, abbreviation, canonical_name, league, source, published_at,
            observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES (?, ?, ?, 'NFL', ?, NULL, ?, ?, NULL, ?, NULL, ?, ?)
        """,
        (
            canonical,
            canonical,
            canonical,
            source,
            observed_text,
            utc_timestamp(ingested_at),
            observed_text,
            SLATE_INGEST_VERSION,
            run_id,
        ),
    )
    if cursor.lastrowid is None:  # pragma: no cover - SQLite always supplies a rowid
        raise SlateIngestError(f"could not insert team {canonical}")
    return int(cursor.lastrowid), True


def _resolve_team_defense(
    connection: sqlite3.Connection,
    team_code: str,
    *,
    observed_at: datetime,
    ingested_at: datetime,
    run_id: str | None,
) -> int:
    """Return the canonical defense ``player_id`` for a franchise, inserting it once.

    The key is ``dst:<code>``; the name is the code plus ``DST`` so it reads the same on
    every site. Like a team, a defense is an identity: the earliest row is reused.
    """

    key = f"dst:{team_code}"
    row = connection.execute(
        "SELECT player_id FROM players WHERE player_key = ? ORDER BY valid_from, player_id LIMIT 1",
        (key,),
    ).fetchone()
    if row is not None:
        return int(row["player_id"])
    observed_text = utc_timestamp(observed_at)
    cursor = connection.execute(
        """
        INSERT INTO players(
            player_key, canonical_name, position, birth_date, source, published_at,
            observed_at, ingested_at, effective_at, valid_from, valid_to, source_version,
            run_id
        ) VALUES (?, ?, 'DST', NULL, ?, NULL, ?, ?, NULL, ?, NULL, ?, ?)
        """,
        (
            key,
            f"{team_code} DST",
            DEFENSE_PLAYER_SOURCE,
            observed_text,
            utc_timestamp(ingested_at),
            observed_text,
            SLATE_INGEST_VERSION,
            run_id,
        ),
    )
    if cursor.lastrowid is None:  # pragma: no cover - SQLite always supplies a rowid
        raise SlateIngestError(f"could not insert defense player {key}")
    return int(cursor.lastrowid)


def _matchup_key(parsed_row: ParsedSalaryRow) -> tuple[str, str]:
    """Return ``(away, home)`` for the row's game; the export names both sides."""

    team = normalize_team_code(parsed_row.team)
    opponent = normalize_team_code(parsed_row.opponent)
    return (team, opponent) if team <= opponent else (opponent, team)


def _matchup_label(parsed_row: ParsedSalaryRow) -> str:
    """Render the game as ``AWAY@HOME`` when the export says which side is home."""

    team = normalize_team_code(parsed_row.team)
    opponent = normalize_team_code(parsed_row.opponent)
    if parsed_row.is_home is None:
        return "/".join(_matchup_key(parsed_row))
    return f"{opponent}@{team}" if parsed_row.is_home else f"{team}@{opponent}"


def _resolve_game(
    connection: sqlite3.Connection,
    parsed_row: ParsedSalaryRow,
    *,
    season: int,
    week: int,
    team_ids: dict[str, int],
    observed_at: datetime,
    ingested_at: datetime,
    source: str,
    run_id: str | None,
) -> tuple[int | None, bool, str | None]:
    """Return the ``game_id`` for the row's matchup, or ``None`` with no kickoff.

    The key is the alphabetical pair so both sites and both sides of a game land on one
    row; home and away come from the export's own ``AWAY@HOME`` field, never from the
    key's order. A file without kickoff times (FanDuel) leaves ``game_id`` NULL and is
    reported rather than invented. Like a team, a game is an identity the whole week
    points at, so the first row for the matchup is reused; a kickoff that later disagrees
    is returned as a message rather than overwriting what was already observed.
    """

    if parsed_row.game_time is None or parsed_row.is_home is None:
        return None, False, None

    first, second = _matchup_key(parsed_row)
    external_game_id = f"{season}:w{week:02d}:{first}-{second}"
    observed_text = utc_timestamp(observed_at)
    kickoff_text = utc_timestamp(parsed_row.game_time)
    home = normalize_team_code(
        parsed_row.team if parsed_row.is_home else parsed_row.opponent
    )
    away = normalize_team_code(
        parsed_row.opponent if parsed_row.is_home else parsed_row.team
    )
    row = connection.execute(
        """
        SELECT game_id, kickoff_at FROM games
        WHERE external_game_id = ? AND season = ? AND week = ?
        ORDER BY valid_from, game_id
        LIMIT 1
        """,
        (external_game_id, season, week),
    ).fetchone()
    if row is not None:
        conflict = (
            None
            if str(row["kickoff_at"]) == kickoff_text
            else (
                f"game {external_game_id} was already observed kicking off at "
                f"{row['kickoff_at']}; this export says {kickoff_text}. Game rows are "
                f"never updated, so game {row['game_id']} was used unchanged"
            )
        )
        return int(row["game_id"]), False, conflict

    cursor = connection.execute(
        """
        INSERT INTO games(
            external_game_id, season, week, kickoff_at, home_team_id, away_team_id,
            stadium_name, game_status, source, published_at, observed_at, ingested_at,
            effective_at, valid_from, valid_to, source_version, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, 'scheduled', ?, NULL, ?, ?, NULL, ?, NULL, ?, ?)
        """,
        (
            external_game_id,
            season,
            week,
            kickoff_text,
            team_ids[home],
            team_ids[away],
            source,
            observed_text,
            utc_timestamp(ingested_at),
            observed_text,
            SLATE_INGEST_VERSION,
            run_id,
        ),
    )
    if cursor.lastrowid is None:  # pragma: no cover - SQLite always supplies a rowid
        raise SlateIngestError(f"could not insert game {external_game_id}")
    return int(cursor.lastrowid), True, None


def _eligible_positions(parsed_row: ParsedSalaryRow) -> tuple[str, ...]:
    """Roster slots minus the slot-only labels, so FLEX/CPT never look like positions."""

    slots = tuple(
        slot for slot in parsed_row.eligible_roster_slots if slot not in _ROSTER_SLOT_ONLY
    )
    return slots or (parsed_row.listed_position,)


def _insert_salary(
    connection: sqlite3.Connection,
    parsed_row: ParsedSalaryRow,
    *,
    slate_id: int,
    player_id: int,
    team_id: int,
    opponent_team_id: int,
    game_id: int | None,
    site: SalarySite,
    file_sha256: str,
    observed_at: datetime,
    ingested_at: datetime,
    run_id: str | None,
) -> _InsertOutcome:
    observed_text = utc_timestamp(observed_at)
    roster_positions = json.dumps(
        list(parsed_row.eligible_roster_slots), separators=(",", ":")
    )
    content = (
        game_id,
        team_id,
        opponent_team_id,
        parsed_row.site_player_id,
        roster_positions,
        parsed_row.salary,
        parsed_row.player_status,
        file_sha256,
    )
    existing = connection.execute(
        """
        SELECT game_id, team_id, opponent_team_id, site_player_id,
               roster_positions_json, salary, player_status, source_file_sha256
        FROM salaries
        WHERE slate_id = ? AND player_id = ? AND observed_at = ?
        """,
        (slate_id, player_id, observed_text),
    ).fetchone()
    if existing is not None:
        if tuple(existing) == content:
            return _InsertOutcome(duplicate=True)
        return _InsertOutcome(
            error=(
                f"salaries key conflict for slate_id={slate_id} player_id={player_id} "
                f"observed_at={observed_text}: an existing row for this key has "
                "different content"
            )
        )

    previous = connection.execute(
        """
        SELECT salary, observed_at FROM salaries
        WHERE slate_id = ? AND player_id = ? AND observed_at < ?
        ORDER BY observed_at DESC, salary_id DESC
        LIMIT 1
        """,
        (slate_id, player_id, observed_text),
    ).fetchone()
    connection.execute(
        """
        INSERT INTO salaries(
            slate_id, player_id, game_id, team_id, opponent_team_id, site_player_id,
            roster_positions_json, salary, player_status, source_file_sha256, source,
            published_at, observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?, NULL, ?, ?)
        """,
        (
            slate_id,
            player_id,
            game_id,
            team_id,
            opponent_team_id,
            parsed_row.site_player_id,
            roster_positions,
            parsed_row.salary,
            parsed_row.player_status,
            file_sha256,
            site.value,
            observed_text,
            utc_timestamp(ingested_at),
            observed_text,
            SLATE_INGEST_VERSION,
            run_id,
        ),
    )
    change: SalaryChange | None = None
    if previous is not None and int(previous["salary"]) != parsed_row.salary:
        change = SalaryChange(
            player_id=player_id,
            site_player_id=parsed_row.site_player_id,
            name_raw=parsed_row.name_raw,
            previous_salary=int(previous["salary"]),
            previous_observed_at=_parse_timestamp(str(previous["observed_at"])),
            salary=parsed_row.salary,
        )
    return _InsertOutcome(inserted=True, change=change)


def _utc(value: datetime) -> datetime:
    try:
        return ensure_utc(value)
    except ValueError as error:
        raise SlateIngestError("timestamps must include a timezone") from error


def _parse_timestamp(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


class SlateSummary(BaseModel):
    """One slate as `na-slate list` shows it: the ids the rest of the lane needs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slate_id: int
    external_slate_id: str
    site: str
    slate_type: str
    name: str
    starts_at: datetime
    locks_at: datetime
    player_count: int = Field(ge=0)
    unresolved_count: int = Field(ge=0)
    latest_salary_at: datetime | None
    latest_projection_at: datetime | None
    latest_ownership_at: datetime | None


def list_slates(
    connection: sqlite3.Connection,
    *,
    season: int,
    week: int,
    site: str | SalarySite | None = None,
) -> tuple[SlateSummary, ...]:
    """List the week's slates with the observation times each later stage depends on."""

    salary_site = (
        None if site is None else (site if isinstance(site, SalarySite) else normalize_site(site))
    )
    parameters: list[object] = [season, week]
    site_filter = ""
    if salary_site is not None:
        site_filter = "AND site = ?"
        parameters.append(salary_site.value)
    rows = connection.execute(
        f"""
        SELECT slate_id, external_slate_id, site, slate_type, name, starts_at, locks_at
        FROM slates
        WHERE season = ? AND week = ? {site_filter}
        ORDER BY starts_at, site, slate_type, slate_id
        """,
        tuple(parameters),
    ).fetchall()

    summaries: list[SlateSummary] = []
    for row in rows:
        slate_id = int(row["slate_id"])
        site_value = str(row["site"])
        summaries.append(
            SlateSummary(
                slate_id=slate_id,
                external_slate_id=str(row["external_slate_id"]),
                site=site_value,
                slate_type=str(row["slate_type"]),
                name=str(row["name"]),
                starts_at=_parse_timestamp(str(row["starts_at"])),
                locks_at=_parse_timestamp(str(row["locks_at"])),
                player_count=int(
                    connection.execute(
                        "SELECT count(DISTINCT player_id) FROM salaries WHERE slate_id = ?",
                        (slate_id,),
                    ).fetchone()[0]
                ),
                unresolved_count=int(
                    connection.execute(
                        """
                        SELECT count(*) FROM unresolved_player_matches
                        WHERE status = 'pending' AND site = ?
                        """,
                        (site_value,),
                    ).fetchone()[0]
                ),
                latest_salary_at=_latest_observation(connection, "salaries", slate_id),
                latest_projection_at=_latest_observation(
                    connection, "projection_snapshots", slate_id
                ),
                latest_ownership_at=_latest_observation(
                    connection, "ownership_baselines", slate_id
                ),
            )
        )
    return tuple(summaries)


def render_slates(summaries: tuple[SlateSummary, ...], *, season: int, week: int) -> str:
    """Render `na-slate list` as fixed columns: no colour, no truncation, no summary."""

    header = f"SLATES — {season} week {week:02d}"
    if not summaries:
        return (
            f"{header}\n  none ingested — run `na-slate ingest` on this week's "
            "salaries capture\n"
        )

    lines = [
        header,
        f"  {'ID':>4}  {'SITE':<11} {'TYPE':<9} {'LOCKS AT':<28} "
        f"{'PLAYERS':>7} {'UNRESOLVED':>10}  NAME",
    ]
    for slate in summaries:
        lines.append(
            f"  {slate.slate_id:>4}  {slate.site:<11} {slate.slate_type:<9} "
            f"{utc_timestamp(slate.locks_at):<28} {slate.player_count:>7} "
            f"{slate.unresolved_count:>10}  {slate.name}"
        )
        lines.append(
            f"        salaries    {_stamp(slate.latest_salary_at)}    "
            f"projections {_stamp(slate.latest_projection_at)}    "
            f"ownership   {_stamp(slate.latest_ownership_at)}"
        )
    lines.append("")
    return "\n".join(lines)


def _latest_observation(
    connection: sqlite3.Connection, table: str, slate_id: int
) -> datetime | None:
    if table not in {"salaries", "projection_snapshots", "ownership_baselines"}:
        raise SlateIngestError(f"unsupported observation table: {table}")
    value = connection.execute(
        f"SELECT max(observed_at) FROM {table} WHERE slate_id = ?",
        (slate_id,),
    ).fetchone()[0]
    return None if value is None else _parse_timestamp(str(value))


def _stamp(value: datetime | None) -> str:
    return "MISSING" if value is None else utc_timestamp(value)
