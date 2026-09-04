"""Strict DK/FD contest standings parsing and point-in-time label ingestion."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from narrative_alpha.contests import load_contest_payouts
from narrative_alpha.identity import PlayerCrosswalk, PlayerIdentityInput
from narrative_alpha.identity.normalization import name_without_suffix, normalize_name
from narrative_alpha.ingest.salaries import SalarySite, SalarySlateType
from narrative_alpha.ingest.timestamps import ensure_utc, optional_utc_timestamp, utc_timestamp

_DRAFTKINGS_ENTRY_HEADERS = frozenset(
    {"rank", "entryid", "entryname", "timeremaining", "points", "lineup"}
)
_FANDUEL_ENTRY_HEADERS = frozenset({"rank", "entryid", "entryname", "score", "lineup"})
_ATHLETE_HEADERS = frozenset({"player", "rosterposition", "drafted", "fpts"})

# Reported ownership percentages are rounded by the sites to at most whole
# percentage points, so a valid whole-lineup count reconstructs the reported
# fraction to within half a percentage point.
_OWNERSHIP_ROUNDING_TOLERANCE = 0.005 + 1e-9

# Showdown captain (CPT/MVP) fantasy points are the base score multiplied by
# 1.5; the tolerance absorbs the sites' two-decimal rounding on both rows.
_CAPTAIN_MULTIPLIER = 1.5
_CAPTAIN_POINTS_TOLERANCE = 0.02


class ContestArchetype(StrEnum):
    CASH = "cash"
    SINGLE_ENTRY = "single_entry"
    THREE_MAX = "3max"
    TWENTY_MAX = "20max"
    MASS_MULTI_ENTRY = "mass_multi_entry"
    SHOWDOWN = "showdown"


class ContestStandingsError(ValueError):
    """Base failure for an unreadable standings export."""


class ContestSchemaError(ContestStandingsError):
    """Structured section/header drift with no guessed fallback."""

    def __init__(
        self,
        *,
        section: str,
        headers: tuple[str, ...],
        missing_columns: tuple[str, ...],
        unexpected_columns: tuple[str, ...],
    ) -> None:
        self.section = section
        self.headers = headers
        self.missing_columns = missing_columns
        self.unexpected_columns = unexpected_columns
        missing = ", ".join(missing_columns) or "none"
        unexpected = ", ".join(unexpected_columns) or "none"
        observed = ",".join(headers) or "<missing>"
        super().__init__(
            f"contest standings {section} header drift; header row: {observed}; "
            f"missing columns: {missing}; "
            f"unexpected columns: {unexpected}"
        )


class ContestMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contest_id: str
    site: SalarySite
    slate_id: int
    slate_type: SalarySlateType
    contest_archetype: ContestArchetype
    entry_limit: int = Field(gt=0)
    entry_fee_cents: int = Field(ge=0)
    observed_at: datetime
    expected_field_size: int | None = Field(default=None, gt=0)
    payout_curve_id: str | None = None
    published_at: datetime | None = None
    effective_at: datetime | None = None
    source_version: str = "contest-standings-v1"

    @field_validator("contest_id", "source_version")
    @classmethod
    def required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator("payout_curve_id")
    @classmethod
    def optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("observed_at", "published_at", "effective_at")
    @classmethod
    def utc_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("contest timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_archetype(self) -> Self:
        if (
            self.slate_type is SalarySlateType.SHOWDOWN
            and self.contest_archetype is not ContestArchetype.SHOWDOWN
        ):
            raise ValueError("showdown slates require the showdown contest archetype")
        if (
            self.slate_type is SalarySlateType.CLASSIC
            and self.contest_archetype is ContestArchetype.SHOWDOWN
        ):
            raise ValueError("classic slates cannot use the showdown contest archetype")
        return self


class ParsedActualOwnership(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row_number: int = Field(ge=2)
    name_raw: str
    roster_position: str
    role: Literal["classic", "flex", "captain"]
    lineup_count: int = Field(gt=0)
    roster_count: int = Field(ge=0)
    actual_ownership: float = Field(ge=0, le=1)
    reported_ownership: float = Field(ge=0, le=1)
    fantasy_points: float = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def counts_agree(self) -> Self:
        if self.roster_count > self.lineup_count:
            raise ValueError("roster_count cannot exceed lineup_count")
        expected = self.roster_count / self.lineup_count
        if abs(expected - self.actual_ownership) > 1e-12:
            raise ValueError("actual_ownership must equal roster_count / lineup_count")
        return self


class ParsedContestEntry(BaseModel):
    """One entrant row retained for matching against the frozen-entry ledger."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    row_number: int = Field(ge=2)
    rank: int = Field(ge=1)
    entry_id: str = Field(min_length=1)
    entry_name: str = Field(min_length=1)
    points: float = Field(allow_inf_nan=False)
    # Retained by the parser for simulator calibration. The operational store still
    # persists only ledger-matched settlements; calibration reads the immutable captured
    # standings bytes by their already-stored SHA-256.
    lineup: str = Field(min_length=1)


class RejectedContestRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row_number: int = Field(ge=2)
    section: Literal["entries", "athletes"]
    reasons: tuple[str, ...] = Field(min_length=1)


class ContestStandingsParseReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entry_rows_seen: int = Field(ge=0)
    athlete_rows_seen: int = Field(ge=0)
    athlete_rows_parsed: int = Field(ge=0)
    rows_rejected: int = Field(ge=0)
    rejected: tuple[RejectedContestRow, ...] = ()

    @model_validator(mode="after")
    def counts_agree(self) -> Self:
        if self.athlete_rows_seen != self.athlete_rows_parsed + sum(
            issue.section == "athletes" for issue in self.rejected
        ):
            raise ValueError("athlete report counts do not agree")
        if self.rows_rejected != len(self.rejected):
            raise ValueError("rows_rejected must equal rejected detail count")
        return self


class ContestStandingsParseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    site: SalarySite
    source_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    field_size: int = Field(gt=0)
    entries: tuple[ParsedContestEntry, ...]
    rows: tuple[ParsedActualOwnership, ...]
    parse_report: ContestStandingsParseReport


class ContestLoadReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ownership_rows_inserted: int = Field(ge=0)
    result_rows_inserted: int = Field(ge=0)
    entry_result_rows_inserted: int = Field(default=0, ge=0)
    entry_result_duplicate_rows: int = Field(default=0, ge=0)
    settled_entries: int = Field(default=0, ge=0)
    unsettled_entries: int = Field(default=0, ge=0)
    unledgered_entries: int = Field(default=0, ge=0)
    duplicate_rows: int = Field(ge=0)
    unresolved_rows: int = Field(ge=0)
    rejected_rows: int = Field(ge=0)
    unresolved_ids: tuple[int, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors and self.unresolved_rows == 0 and self.rejected_rows == 0


@dataclass(frozen=True)
class _SlatePlayer:
    player_id: int
    canonical_name: str
    position: str | None
    team: str
    game_id: int | None


def parse_contest_standings(
    csv_path: Path, metadata: ContestMetadata
) -> ContestStandingsParseResult:
    """Parse the entrant and athlete sections of a site standings export."""

    raw_bytes = csv_path.read_bytes()
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ContestStandingsError("contest standings CSV is not UTF-8") from error
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as error:
        raise ContestStandingsError(f"malformed contest standings CSV: {error}") from error
    if not rows:
        raise ContestStandingsError("contest standings CSV is empty")

    entry_header_index = _first_nonempty_row(rows)
    entry_headers = tuple(rows[entry_header_index])
    expected_entry_headers = (
        _DRAFTKINGS_ENTRY_HEADERS
        if metadata.site is SalarySite.DRAFTKINGS
        else _FANDUEL_ENTRY_HEADERS
    )
    _validate_headers("entries", entry_headers, expected_entry_headers)

    athlete_header_index = _find_athlete_header(rows, entry_header_index + 1)
    athlete_headers = tuple(rows[athlete_header_index])
    _validate_headers("athletes", athlete_headers, _ATHLETE_HEADERS)

    rejected: list[RejectedContestRow] = []
    entry_rows_seen = 0
    parsed_entries: list[ParsedContestEntry] = []
    entry_ids: set[str] = set()
    entry_map = {_header(cell): index for index, cell in enumerate(entry_headers)}
    for row_index in range(entry_header_index + 1, athlete_header_index):
        row = rows[row_index]
        if not any(cell.strip() for cell in row):
            continue
        entry_rows_seen += 1
        try:
            if len(row) != len(entry_headers):
                raise ValueError("entry row width does not match header")
            entry_id = row[entry_map["entryid"]].strip()
            entry_name = row[entry_map["entryname"]].strip()
            if not entry_id or not entry_name:
                raise ValueError("entry ID and entry name are required")
            if entry_id in entry_ids:
                raise ValueError("duplicate entry ID")
            parsed_entries.append(
                ParsedContestEntry(
                    row_number=row_index + 1,
                    rank=int(row[entry_map["rank"]].strip()),
                    entry_id=entry_id,
                    entry_name=entry_name,
                    points=float(row[entry_map[_entry_points_header(metadata.site)]].strip()),
                    lineup=" ".join(row[entry_map["lineup"]].split()),
                )
            )
            entry_ids.add(entry_id)
        except ValueError as error:
            rejected.append(
                RejectedContestRow(
                    row_number=row_index + 1,
                    section="entries",
                    reasons=(str(error),),
                )
            )

    field_size = len(parsed_entries)
    if field_size <= 0:
        raise ContestStandingsError("contest standings contains no parseable entrant rows")
    if metadata.expected_field_size is not None and field_size != metadata.expected_field_size:
        raise ContestStandingsError(
            f"field size mismatch: metadata says {metadata.expected_field_size}, "
            f"CSV has {field_size}"
        )

    parsed: list[ParsedActualOwnership] = []
    athlete_rows_seen = 0
    identities: set[tuple[str, str]] = set()
    header_map = {_header(cell): index for index, cell in enumerate(athlete_headers)}
    drafted_names_percentage = _names_percentage(athlete_headers[header_map["drafted"]])
    for row_index in range(athlete_header_index + 1, len(rows)):
        row = rows[row_index]
        if not any(cell.strip() for cell in row):
            continue
        athlete_rows_seen += 1
        try:
            if len(row) != len(athlete_headers):
                raise ValueError("athlete row width does not match header")
            name = row[header_map["player"]].strip()
            roster_position = row[header_map["rosterposition"]].strip().upper()
            if not name or not roster_position:
                raise ValueError("player and roster position are required")
            role = _role(metadata.slate_type, roster_position)
            identity = (normalize_name(name), role)
            if identity in identities:
                raise ValueError("duplicate player/role athlete row")
            reported = _ownership(
                row[header_map["drafted"]],
                percentage_named_column=drafted_names_percentage,
            )
            roster_count = round(reported * field_size)
            actual = roster_count / field_size
            if abs(actual - reported) > _OWNERSHIP_ROUNDING_TOLERANCE:
                raise ValueError(
                    f"reported ownership {reported:.6f} does not match any whole "
                    f"lineup count for field size {field_size}"
                )
            fantasy_points = float(row[header_map["fpts"]].strip())
            parsed.append(
                ParsedActualOwnership(
                    row_number=row_index + 1,
                    name_raw=name,
                    roster_position=roster_position,
                    role=role,
                    lineup_count=field_size,
                    roster_count=roster_count,
                    actual_ownership=actual,
                    reported_ownership=reported,
                    fantasy_points=fantasy_points,
                )
            )
            identities.add(identity)
        except ValueError as error:
            rejected.append(
                RejectedContestRow(
                    row_number=row_index + 1,
                    section="athletes",
                    reasons=(str(error),),
                )
            )

    report = ContestStandingsParseReport(
        entry_rows_seen=entry_rows_seen,
        athlete_rows_seen=athlete_rows_seen,
        athlete_rows_parsed=len(parsed),
        rows_rejected=len(rejected),
        rejected=tuple(rejected),
    )
    return ContestStandingsParseResult(
        site=metadata.site,
        source_file_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        field_size=field_size,
        entries=tuple(parsed_entries),
        rows=tuple(parsed),
        parse_report=report,
    )


def load_contest_standings(
    connection: sqlite3.Connection,
    csv_path: Path,
    metadata: ContestMetadata,
    *,
    ingested_at: datetime | None = None,
    run_id: str | None = None,
) -> ContestLoadReport:
    """Insert contest-cohort ownership and weekly result labels without updates."""

    parsed = parse_contest_standings(csv_path, metadata)
    ingestion_time = ensure_utc(ingested_at or datetime.now(UTC))
    source = f"{metadata.site.value}-contest-standings"
    _validate_existing_contest(connection, metadata, parsed.field_size)
    slate_players = _slate_players(connection, metadata)
    crosswalk = PlayerCrosswalk(connection)
    ownership_inserted = 0
    result_inserted = 0
    duplicate_rows = 0
    unresolved_ids: list[int] = []
    errors: list[str] = []
    role_points: dict[int, dict[str, tuple[_SlatePlayer, float, str]]] = {}

    settlement = _settle_ledger_entries(
        connection,
        metadata=metadata,
        parsed=parsed,
        ingested_at=ingestion_time,
        run_id=run_id,
    )

    for ownership in parsed.rows:
        candidate = _resolve_slate_player(connection, source, ownership, slate_players)
        if candidate is None:
            unresolved = crosswalk.match(
                PlayerIdentityInput(
                    source=source,
                    site=metadata.site.value,
                    name_raw=ownership.name_raw,
                    team="UNKNOWN",
                    position=_listed_position(ownership.roster_position),
                    observed_at=metadata.observed_at,
                    ingested_at=ingestion_time,
                    source_file_sha256=parsed.source_file_sha256,
                    run_id=run_id,
                )
            )
            if unresolved.unresolved_id is not None:
                unresolved_ids.append(unresolved.unresolved_id)
            continue

        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO actual_ownership(
                external_contest_id, site, slate_id, contest_archetype,
                field_size, entry_limit, entry_fee_cents, payout_curve_id,
                player_id, role, lineup_count, roster_count, actual_ownership,
                source_file_sha256, source, published_at, observed_at,
                ingested_at, effective_at, valid_from, valid_to,
                source_version, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      NULL, ?, ?)
            """,
            (
                metadata.contest_id,
                metadata.site.value,
                metadata.slate_id,
                metadata.contest_archetype.value,
                parsed.field_size,
                metadata.entry_limit,
                metadata.entry_fee_cents,
                metadata.payout_curve_id,
                candidate.player_id,
                ownership.role,
                ownership.lineup_count,
                ownership.roster_count,
                ownership.actual_ownership,
                parsed.source_file_sha256,
                source,
                optional_utc_timestamp(metadata.published_at),
                utc_timestamp(metadata.observed_at),
                utc_timestamp(ingestion_time),
                optional_utc_timestamp(metadata.effective_at),
                utc_timestamp(metadata.observed_at),
                metadata.source_version,
                run_id,
            ),
        )
        inserted = int(cursor.rowcount == 1)
        ownership_inserted += inserted
        duplicate_rows += 1 - inserted

        player_roles = role_points.setdefault(candidate.player_id, {})
        prior_result = player_roles.get(ownership.role)
        if prior_result is not None:
            if prior_result[1] != ownership.fantasy_points:
                errors.append(f"conflicting FPTS for {ownership.name_raw} in one contest export")
        else:
            player_roles[ownership.role] = (
                candidate,
                ownership.fantasy_points,
                ownership.roster_position,
            )

    for candidate, fantasy_points, roster_position in _derive_game_results(
        metadata.slate_type, role_points, errors
    ):
        if candidate.game_id is None:
            errors.append(f"cannot store result for {candidate.canonical_name}: salary has no game")
            continue
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO results(
                game_id, player_id, site, fantasy_points, stat_line_json,
                source_file_sha256, source, published_at, observed_at,
                ingested_at, effective_at, valid_from, valid_to,
                source_version, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                candidate.game_id,
                candidate.player_id,
                metadata.site.value,
                fantasy_points,
                json.dumps(
                    {
                        "contest_id": metadata.contest_id,
                        "roster_position": roster_position,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                parsed.source_file_sha256,
                source,
                optional_utc_timestamp(metadata.published_at),
                utc_timestamp(metadata.observed_at),
                utc_timestamp(ingestion_time),
                optional_utc_timestamp(metadata.effective_at),
                utc_timestamp(metadata.observed_at),
                metadata.source_version,
                run_id,
            ),
        )
        inserted = int(cursor.rowcount == 1)
        result_inserted += inserted
        duplicate_rows += 1 - inserted

    return ContestLoadReport(
        ownership_rows_inserted=ownership_inserted,
        result_rows_inserted=result_inserted,
        entry_result_rows_inserted=settlement[0],
        entry_result_duplicate_rows=settlement[1],
        settled_entries=settlement[2],
        unsettled_entries=settlement[3],
        unledgered_entries=settlement[4],
        duplicate_rows=duplicate_rows,
        unresolved_rows=len(unresolved_ids),
        rejected_rows=parsed.parse_report.rows_rejected,
        unresolved_ids=tuple(unresolved_ids),
        errors=tuple(errors),
    )


def _settle_ledger_entries(
    connection: sqlite3.Connection,
    *,
    metadata: ContestMetadata,
    parsed: ContestStandingsParseResult,
    ingested_at: datetime,
    run_id: str | None,
) -> tuple[int, int, int, int, int]:
    """Append one receipt for each current ledger entry in this contest export."""

    ledger = connection.execute(
        """
        WITH ranked AS (
            SELECT ce.*, c.external_contest_id,
                   row_number() OVER (
                       PARTITION BY c.site, c.external_contest_id, ce.entry_id
                       ORDER BY rtrim(d.decision_at, 'Z') DESC, ce.contest_entry_id DESC
                   ) AS assignment_rank
            FROM contest_entries AS ce
            JOIN contests AS c ON c.contest_id = ce.contest_id
            JOIN decision_snapshots AS d
              ON d.decision_snapshot_id = ce.decision_snapshot_id
            WHERE c.site = ? AND c.external_contest_id = ? AND c.slate_id = ?
        )
        SELECT * FROM ranked WHERE assignment_rank = 1 ORDER BY entry_id
        """,
        (metadata.site.value, metadata.contest_id, metadata.slate_id),
    ).fetchall()
    if not ledger:
        return 0, 0, 0, 0, 0
    if metadata.payout_curve_id is None:
        raise ContestStandingsError(
            f"contest {metadata.contest_id} has ledger entries but no payout table; "
            "add the contest payout curve with `na-contest add`, then rerun `na-ops results`"
        )
    payouts = load_contest_payouts(
        connection, payout_curve_id=metadata.payout_curve_id, as_of=metadata.observed_at
    )
    if not payouts:
        raise ContestStandingsError(
            f"contest {metadata.contest_id} payout curve {metadata.payout_curve_id!r} has no "
            "payout table; add it with `na-contest add`, then rerun `na-ops results`"
        )
    by_id = {entry.entry_id: entry for entry in parsed.entries}
    ledger_ids = {str(row["entry_id"]) for row in ledger}
    our_names = {by_id[entry_id].entry_name for entry_id in ledger_ids if entry_id in by_id}
    unledgered = sum(
        entry.entry_name in our_names and entry.entry_id not in ledger_ids
        for entry in parsed.entries
    )
    inserted = duplicates = settled = unsettled = 0
    stamp = utc_timestamp(metadata.observed_at)
    source = f"{metadata.site.value}-contest-standings"
    for ledger_row in ledger:
        entry = by_id.get(str(ledger_row["entry_id"]))
        status = "settled" if entry is not None else "unsettled"
        prize = None
        reason = None
        if entry is None:
            unsettled += 1
            rank = points = None
            reason = "ledger entry is absent from the standings export"
        else:
            settled += 1
            rank = entry.rank
            points = entry.points
            prize = next(
                (
                    payout.prize_cents
                    for payout in payouts
                    if payout.rank_from <= entry.rank <= payout.rank_to
                ),
                0,
            )
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO contest_entry_results(
                contest_entry_id, settlement_status, rank, points, payout_cents,
                unsettled_reason, source_file_sha256, source, published_at,
                observed_at, ingested_at, effective_at, valid_from, valid_to,
                source_version, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                int(ledger_row["contest_entry_id"]),
                status,
                rank,
                points,
                prize,
                reason,
                parsed.source_file_sha256,
                source,
                optional_utc_timestamp(metadata.published_at),
                stamp,
                utc_timestamp(ingested_at),
                optional_utc_timestamp(metadata.effective_at),
                stamp,
                metadata.source_version,
                run_id,
            ),
        )
        was_inserted = int(cursor.rowcount == 1)
        inserted += was_inserted
        duplicates += 1 - was_inserted
    return inserted, duplicates, settled, unsettled, unledgered


def _derive_game_results(
    slate_type: SalarySlateType,
    role_points: dict[int, dict[str, tuple[_SlatePlayer, float, str]]],
    errors: list[str],
) -> tuple[tuple[_SlatePlayer, float, str], ...]:
    """Reduce per-role standings rows to one base-score game result per player.

    Showdown standings list dual-slot players twice: the captain row carries the
    1.5x-multiplied score and the flex row carries the base score.  The stored
    game result always comes from the flex/base row; a captain row is only
    cross-checked against it.  A captain-only player is a structured error
    because the base score cannot be derived without guessing a divisor.
    """

    derived: list[tuple[_SlatePlayer, float, str]] = []
    for player_roles in role_points.values():
        if slate_type is SalarySlateType.CLASSIC:
            derived.extend(player_roles.values())
            continue
        flex_result = player_roles.get("flex")
        captain_result = player_roles.get("captain")
        if flex_result is None:
            if captain_result is not None:
                errors.append(
                    f"showdown standings list {captain_result[0].canonical_name} only at "
                    "captain; cannot derive base fantasy points without a flex row"
                )
            continue
        if captain_result is not None:
            expected_captain = _CAPTAIN_MULTIPLIER * flex_result[1]
            if abs(captain_result[1] - expected_captain) > _CAPTAIN_POINTS_TOLERANCE:
                errors.append(
                    f"captain FPTS {captain_result[1]} for "
                    f"{flex_result[0].canonical_name} is not {_CAPTAIN_MULTIPLIER}x "
                    f"the flex FPTS {flex_result[1]}"
                )
                continue
        derived.append(flex_result)
    return tuple(derived)


def _slate_players(
    connection: sqlite3.Connection, metadata: ContestMetadata
) -> tuple[_SlatePlayer, ...]:
    as_of = utc_timestamp(metadata.observed_at)
    rows = connection.execute(
        """
        WITH ranked_salaries AS (
            SELECT s.*,
                   row_number() OVER (
                       PARTITION BY s.slate_id, s.player_id
                       ORDER BY s.observed_at DESC, s.salary_id DESC
                   ) AS version_rank
            FROM salaries AS s
            WHERE s.slate_id = ? AND s.observed_at <= ? AND s.valid_from <= ?
              AND (s.valid_to IS NULL OR s.valid_to > ?)
        )
        SELECT p.player_id, p.canonical_name, p.position, t.abbreviation AS team,
               s.game_id
        FROM ranked_salaries AS s
        JOIN players AS p ON p.player_id = s.player_id
        JOIN teams AS t ON t.team_id = s.team_id
        WHERE s.version_rank = 1
        ORDER BY p.player_id
        """,
        (metadata.slate_id, as_of, as_of, as_of),
    ).fetchall()
    return tuple(
        _SlatePlayer(
            player_id=int(row["player_id"]),
            canonical_name=str(row["canonical_name"]),
            position=None if row["position"] is None else str(row["position"]).upper(),
            team=str(row["team"]).upper(),
            game_id=None if row["game_id"] is None else int(row["game_id"]),
        )
        for row in rows
    )


def _validate_existing_contest(
    connection: sqlite3.Connection, metadata: ContestMetadata, field_size: int
) -> None:
    row = connection.execute(
        """
        SELECT slate_id, contest_archetype, field_size, entry_limit, entry_fee_cents
        FROM actual_ownership
        WHERE external_contest_id = ? AND site = ?
        LIMIT 1
        """,
        (metadata.contest_id, metadata.site.value),
    ).fetchone()
    if row is None:
        return
    existing = (
        int(row["slate_id"]),
        str(row["contest_archetype"]),
        int(row["field_size"]),
        int(row["entry_limit"]),
        int(row["entry_fee_cents"]),
    )
    requested = (
        metadata.slate_id,
        metadata.contest_archetype.value,
        field_size,
        metadata.entry_limit,
        metadata.entry_fee_cents,
    )
    if existing != requested:
        raise ContestStandingsError(
            f"contest {metadata.contest_id} on {metadata.site.value} already exists "
            "with different cohort metadata"
        )


def _resolve_slate_player(
    connection: sqlite3.Connection,
    source: str,
    ownership: ParsedActualOwnership,
    candidates: tuple[_SlatePlayer, ...],
) -> _SlatePlayer | None:
    normalized = normalize_name(ownership.name_raw)
    eligible = tuple(
        candidate
        for candidate in candidates
        if _position_agrees(candidate.position, ownership.roster_position)
    )
    manual_rows = connection.execute(
        """
        SELECT player_id FROM player_aliases
        WHERE source = ? AND normalized_alias = ? AND manual_override = 1
          AND valid_to IS NULL
        """,
        (source, normalized),
    ).fetchall()
    manual_ids = {int(row["player_id"]) for row in manual_rows}
    manual = tuple(candidate for candidate in eligible if candidate.player_id in manual_ids)
    if len(manual) == 1:
        return manual[0]

    exact = tuple(
        candidate
        for candidate in eligible
        if normalize_name(candidate.canonical_name) == normalized
    )
    if len(exact) == 1:
        return exact[0]
    suffix = tuple(
        candidate
        for candidate in eligible
        if name_without_suffix(candidate.canonical_name) == name_without_suffix(ownership.name_raw)
    )
    return suffix[0] if len(suffix) == 1 else None


def _validate_headers(section: str, headers: tuple[str, ...], expected: frozenset[str]) -> None:
    actual = frozenset(_header(value) for value in headers)
    if actual != expected or len(headers) != len(actual):
        raise ContestSchemaError(
            section=section,
            headers=headers,
            missing_columns=tuple(sorted(expected - actual)),
            unexpected_columns=tuple(sorted(actual - expected)),
        )


def _first_nonempty_row(rows: list[list[str]]) -> int:
    for index, row in enumerate(rows):
        if any(cell.strip() for cell in row):
            return index
    raise ContestStandingsError("contest standings CSV contains no rows")


def _find_athlete_header(rows: list[list[str]], start: int) -> int:
    for index in range(start, len(rows)):
        if frozenset(_header(value) for value in rows[index]) == _ATHLETE_HEADERS:
            return index
    raise ContestSchemaError(
        section="athletes",
        headers=(),
        missing_columns=tuple(sorted(_ATHLETE_HEADERS)),
        unexpected_columns=(),
    )


def _column(headers: tuple[str, ...], canonical: str) -> int:
    return tuple(_header(value) for value in headers).index(canonical)


def _entry_points_header(site: SalarySite) -> str:
    return "points" if site is SalarySite.DRAFTKINGS else "score"


def _header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold()).replace("percent", "")


def _names_percentage(raw_header: str) -> bool:
    return "%" in raw_header or "percent" in raw_header.casefold()


def _ownership(value: str, *, percentage_named_column: bool) -> float:
    text = value.strip()
    if "%" not in text and not percentage_named_column:
        raise ValueError(
            f"ownership value {value!r} has no percent sign and its column header "
            "does not name a percentage; refusing to guess units"
        )
    try:
        number = float(text.replace("%", "").strip())
    except ValueError as error:
        raise ValueError(f"invalid ownership value: {value!r}") from error
    ownership = number / 100
    if not 0 <= ownership <= 1:
        raise ValueError("ownership must be between 0% and 100%")
    return ownership


def _role(
    slate_type: SalarySlateType, roster_position: str
) -> Literal["classic", "flex", "captain"]:
    if slate_type is SalarySlateType.CLASSIC:
        return "classic"
    if roster_position in {"CPT", "CAPTAIN", "MVP"}:
        return "captain"
    if roster_position in {"FLEX", "UTIL", "ANYFLEX"}:
        return "flex"
    raise ValueError(f"unsupported showdown roster role: {roster_position}")


def _position_agrees(candidate_position: str | None, roster_position: str) -> bool:
    if roster_position in {"FLEX", "UTIL", "ANYFLEX", "CPT", "CAPTAIN", "MVP"}:
        return True
    if candidate_position is None:
        return False
    candidate = "DST" if candidate_position in {"D", "DEF"} else candidate_position
    roster = "DST" if roster_position in {"D", "DEF"} else roster_position
    return candidate == roster


def _listed_position(roster_position: str) -> str | None:
    if roster_position in {"FLEX", "UTIL", "ANYFLEX", "CPT", "CAPTAIN", "MVP"}:
        return None
    return "DST" if roster_position in {"D", "DEF"} else roster_position
