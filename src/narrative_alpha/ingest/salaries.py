"""Strict DraftKings and FanDuel NFL salary CSV parsers."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

_DRAFTKINGS_HEADERS = frozenset(
    {
        "Position",
        "Name + ID",
        "Name",
        "ID",
        "Roster Position",
        "Salary",
        "Game Info",
        "TeamAbbrev",
        "AvgPointsPerGame",
    }
)
_FANDUEL_CLASSIC_HEADERS = frozenset(
    {
        "Id",
        "Position",
        "First Name",
        "Nickname",
        "Last Name",
        "FPPG",
        "Played",
        "Salary",
        "Game",
        "Team",
        "Opponent",
        "Injury Indicator",
        "Injury Details",
    }
)
_FANDUEL_SHOWDOWN_HEADERS = _FANDUEL_CLASSIC_HEADERS | {"Roster Position"}
_FANDUEL_OPTIONAL_HEADERS = frozenset({"Tier"})
_SHOWDOWN_SLOTS = {"CPT", "MVP"}
_MATCHUP_PATTERN = re.compile(r"^(?P<away>[A-Z]{2,4})@(?P<home>[A-Z]{2,4})$")
_NAME_AND_ID_PATTERN = re.compile(r"^(?P<name>.+) \((?P<identifier>[^()]+)\)$")
_TIMEZONE_NAMES = {
    "ET": "America/New_York",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "CT": "America/Chicago",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "MT": "America/Denver",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "PT": "America/Los_Angeles",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "UTC": "UTC",
}


class SalarySite(StrEnum):
    DRAFTKINGS = "draftkings"
    FANDUEL = "fanduel"


class SalarySlateType(StrEnum):
    CLASSIC = "classic"
    SHOWDOWN = "showdown"


class SalaryFormat(StrEnum):
    DRAFTKINGS_CLASSIC = "draftkings_classic"
    DRAFTKINGS_SHOWDOWN = "draftkings_showdown"
    FANDUEL_CLASSIC = "fanduel_classic"
    FANDUEL_SHOWDOWN = "fanduel_showdown"


class SalaryCsvError(ValueError):
    """Base error for an unreadable or structurally invalid salary export."""


class SalarySchemaError(SalaryCsvError):
    """Structured header-drift error naming every missing and unexpected column."""

    def __init__(
        self,
        *,
        detected_near: str,
        headers: tuple[str, ...],
        missing_columns: tuple[str, ...],
        unexpected_columns: tuple[str, ...],
    ) -> None:
        self.detected_near = detected_near
        self.headers = headers
        self.missing_columns = missing_columns
        self.unexpected_columns = unexpected_columns
        missing = ", ".join(missing_columns) or "none"
        unexpected = ", ".join(unexpected_columns) or "none"
        super().__init__(
            f"salary CSV header drift near {detected_near}; "
            f"missing columns: {missing}; unexpected columns: {unexpected}"
        )


class RejectedSalaryRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row_number: int = Field(ge=2)
    site_player_id: str | None
    reasons: tuple[str, ...] = Field(min_length=1)


class SalaryParseReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rows_seen: int = Field(ge=0)
    rows_parsed: int = Field(ge=0)
    rows_rejected: int = Field(ge=0)
    rejected: tuple[RejectedSalaryRow, ...] = ()

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.rows_seen != self.rows_parsed + self.rows_rejected:
            raise ValueError("rows_seen must equal parsed plus rejected rows")
        if self.rows_rejected != len(self.rejected):
            raise ValueError("rows_rejected must equal rejected detail count")
        return self


class ParsedSalaryRow(BaseModel):
    """Site-native salary row before canonical player crosswalk resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    site: SalarySite
    slate_type: SalarySlateType
    slate_id: str | None
    slate_name: str | None
    site_player_id: str
    name_raw: str
    team: str
    opponent: str
    listed_position: str
    eligible_roster_slots: tuple[str, ...] = Field(min_length=1)
    salary: int = Field(gt=0)
    game_time: datetime | None
    is_home: bool | None = None
    """Whether ``team`` is the home side; both sites write the game as ``AWAY@HOME``."""
    player_status: str | None = None

    @field_validator("site_player_id", "name_raw", "team", "opponent", "listed_position")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator("team", "opponent", "listed_position")
    @classmethod
    def uppercase_codes(cls, value: str) -> str:
        return value.upper()

    @field_validator("eligible_roster_slots")
    @classmethod
    def normalize_slots(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(slot.strip().upper() for slot in value if slot.strip()))
        if not normalized:
            raise ValueError("eligible roster slots must not be empty")
        return normalized

    @field_validator("game_time")
    @classmethod
    def normalize_game_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("game_time must include a timezone")
        return value.astimezone(UTC)

    @field_validator("player_status")
    @classmethod
    def normalize_player_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        return normalized or None

    @model_validator(mode="after")
    def validate_site_rules(self) -> Self:
        if self.team == self.opponent:
            raise ValueError("team and opponent must differ")

        if self.slate_type is SalarySlateType.CLASSIC:
            positions = {
                SalarySite.DRAFTKINGS: {"QB", "RB", "WR", "TE", "DST"},
                SalarySite.FANDUEL: {"QB", "RB", "WR", "TE", "D"},
            }[self.site]
            slots = positions | {"FLEX"}
            if self.listed_position not in positions:
                raise ValueError(
                    f"invalid {self.site.value} classic position: {self.listed_position}"
                )
            invalid_slots = set(self.eligible_roster_slots) - slots
            if invalid_slots:
                raise ValueError(f"invalid classic roster slots: {sorted(invalid_slots)}")
            primary_slot = "D" if self.listed_position == "D" else self.listed_position
            if primary_slot not in self.eligible_roster_slots:
                raise ValueError("eligible slots must contain the listed position")
            if "FLEX" in self.eligible_roster_slots and self.listed_position not in {
                "RB",
                "WR",
                "TE",
            }:
                raise ValueError("only RB, WR, and TE may be FLEX-eligible in classic slates")
        else:
            positions = {
                SalarySite.DRAFTKINGS: {"QB", "RB", "WR", "TE", "K", "DST"},
                SalarySite.FANDUEL: {"QB", "RB", "WR", "TE", "K", "D"},
            }[self.site]
            slots = {
                SalarySite.DRAFTKINGS: {"CPT", "FLEX"},
                SalarySite.FANDUEL: {"MVP", "FLEX"},
            }[self.site]
            if self.listed_position not in positions:
                raise ValueError(
                    f"invalid {self.site.value} showdown position: {self.listed_position}"
                )
            invalid_slots = set(self.eligible_roster_slots) - slots
            if invalid_slots:
                raise ValueError(f"invalid showdown roster slots: {sorted(invalid_slots)}")
        return self


class SalaryParseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    salary_format: SalaryFormat
    source_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rows: tuple[ParsedSalaryRow, ...]
    parse_report: SalaryParseReport


def parse_salary_csv(
    csv_path: Path,
    *,
    slate_id: str | None = None,
    slate_name: str | None = None,
) -> SalaryParseResult:
    """Detect and parse a DK/FD classic or showdown salary export."""

    try:
        raw_bytes = csv_path.read_bytes()
    except OSError as error:
        raise SalaryCsvError(f"cannot read salary CSV {csv_path}: {error}") from error
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise SalaryCsvError(f"salary CSV is not UTF-8: {csv_path}") from error

    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if reader.fieldnames is None:
            raise SalarySchemaError(
                detected_near="unknown",
                headers=(),
                missing_columns=tuple(sorted(_DRAFTKINGS_HEADERS)),
                unexpected_columns=(),
            )
        headers = tuple(reader.fieldnames)
        site, header_showdown = _detect_site(headers)
        raw_rows = list(reader)
    except csv.Error as error:
        raise SalaryCsvError(f"malformed salary CSV: {error}") from error

    slate_type = _detect_slate_type(site, header_showdown, raw_rows)
    salary_format = SalaryFormat(f"{site.value}_{slate_type.value}")
    parsed_rows: list[ParsedSalaryRow] = []
    rejected_rows: list[RejectedSalaryRow] = []
    for row_number, raw_row in enumerate(raw_rows, start=2):
        extra_values = raw_row.get(None)
        if extra_values:
            rejected_rows.append(
                RejectedSalaryRow(
                    row_number=row_number,
                    site_player_id=_raw_player_id(site, raw_row),
                    reasons=("row has more values than the header",),
                )
            )
            continue
        if not any(value and value.strip() for key, value in raw_row.items() if key is not None):
            continue
        try:
            if site is SalarySite.DRAFTKINGS:
                parsed = _parse_draftkings_row(
                    raw_row, slate_type, slate_id=slate_id, slate_name=slate_name
                )
            else:
                parsed = _parse_fanduel_row(
                    raw_row, slate_type, slate_id=slate_id, slate_name=slate_name
                )
        except (KeyError, ValueError, ValidationError) as error:
            rejected_rows.append(
                RejectedSalaryRow(
                    row_number=row_number,
                    site_player_id=_raw_player_id(site, raw_row),
                    reasons=_error_reasons(error),
                )
            )
        else:
            parsed_rows.append(parsed)

    if not parsed_rows and not rejected_rows:
        raise SalaryCsvError(f"salary CSV has headers but no data rows: {csv_path}")

    report = SalaryParseReport(
        rows_seen=len(parsed_rows) + len(rejected_rows),
        rows_parsed=len(parsed_rows),
        rows_rejected=len(rejected_rows),
        rejected=tuple(rejected_rows),
    )
    return SalaryParseResult(
        salary_format=salary_format,
        source_file_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        rows=tuple(parsed_rows),
        parse_report=report,
    )


def _detect_site(headers: tuple[str, ...]) -> tuple[SalarySite, bool]:
    if len(headers) != len(set(headers)):
        duplicates = sorted(header for header in set(headers) if headers.count(header) > 1)
        raise SalarySchemaError(
            detected_near="unknown",
            headers=headers,
            missing_columns=(),
            unexpected_columns=tuple(f"duplicate:{header}" for header in duplicates),
        )

    actual = frozenset(headers)
    if actual == _DRAFTKINGS_HEADERS:
        return SalarySite.DRAFTKINGS, False
    if actual - _FANDUEL_OPTIONAL_HEADERS == _FANDUEL_CLASSIC_HEADERS:
        return SalarySite.FANDUEL, False
    if actual - _FANDUEL_OPTIONAL_HEADERS == _FANDUEL_SHOWDOWN_HEADERS:
        return SalarySite.FANDUEL, True

    signatures = (
        ("draftkings", _DRAFTKINGS_HEADERS),
        ("fanduel_classic", _FANDUEL_CLASSIC_HEADERS),
        ("fanduel_classic", _FANDUEL_CLASSIC_HEADERS | _FANDUEL_OPTIONAL_HEADERS),
        ("fanduel_showdown", _FANDUEL_SHOWDOWN_HEADERS),
        ("fanduel_showdown", _FANDUEL_SHOWDOWN_HEADERS | _FANDUEL_OPTIONAL_HEADERS),
    )
    detected_near, expected = min(
        signatures,
        key=lambda item: len(actual.symmetric_difference(item[1])),
    )
    raise SalarySchemaError(
        detected_near=detected_near,
        headers=headers,
        missing_columns=tuple(sorted(expected - actual)),
        unexpected_columns=tuple(sorted(actual - expected)),
    )


def _detect_slate_type(
    site: SalarySite,
    header_showdown: bool,
    raw_rows: list[dict[str, str | None]],
) -> SalarySlateType:
    if header_showdown:
        return SalarySlateType.SHOWDOWN
    if site is SalarySite.DRAFTKINGS:
        slots = {
            slot for row in raw_rows for slot in _split_slots(row.get("Roster Position") or "")
        }
        if slots & _SHOWDOWN_SLOTS:
            return SalarySlateType.SHOWDOWN
    return SalarySlateType.CLASSIC


def _parse_draftkings_row(
    row: dict[str, str | None],
    slate_type: SalarySlateType,
    *,
    slate_id: str | None,
    slate_name: str | None,
) -> ParsedSalaryRow:
    site_player_id = _required(row, "ID")
    name = _required(row, "Name")
    name_and_id = _required(row, "Name + ID")
    match = _NAME_AND_ID_PATTERN.fullmatch(name_and_id)
    if match is None:
        raise ValueError("Name + ID must end with the site ID in parentheses")
    if match.group("name") != name or match.group("identifier") != site_player_id:
        raise ValueError("Name + ID does not match the Name and ID columns")

    team = _required(row, "TeamAbbrev").upper()
    opponent, is_home, game_time = _parse_game_info(
        _required(row, "Game Info"), team, kickoff_required=True
    )
    return ParsedSalaryRow(
        site=SalarySite.DRAFTKINGS,
        slate_type=slate_type,
        slate_id=slate_id,
        slate_name=slate_name,
        site_player_id=site_player_id,
        name_raw=name,
        team=team,
        opponent=opponent,
        listed_position=_normalize_position(SalarySite.DRAFTKINGS, _required(row, "Position")),
        eligible_roster_slots=_split_slots(_required(row, "Roster Position")),
        salary=_parse_salary(_required(row, "Salary")),
        game_time=game_time,
        is_home=is_home,
    )


def _parse_fanduel_row(
    row: dict[str, str | None],
    slate_type: SalarySlateType,
    *,
    slate_id: str | None,
    slate_name: str | None,
) -> ParsedSalaryRow:
    position = _normalize_position(SalarySite.FANDUEL, _required(row, "Position"))
    team = _required(row, "Team").upper()
    opponent_column = _required(row, "Opponent").upper()
    opponent, is_home, game_time = _parse_game_info(
        _required(row, "Game"), team, kickoff_required=False
    )
    if opponent != opponent_column:
        raise ValueError("Opponent does not match the Game column")
    if slate_type is SalarySlateType.SHOWDOWN:
        eligible_slots = _split_slots(_required(row, "Roster Position"))
    else:
        eligible_slots = _classic_slots(position)

    nickname = _required(row, "Nickname")
    return ParsedSalaryRow(
        site=SalarySite.FANDUEL,
        slate_type=slate_type,
        slate_id=slate_id,
        slate_name=slate_name,
        site_player_id=_required(row, "Id"),
        name_raw=nickname,
        team=team,
        opponent=opponent,
        listed_position=position,
        eligible_roster_slots=eligible_slots,
        salary=_parse_salary(_required(row, "Salary")),
        game_time=game_time,
        is_home=is_home,
        player_status=_optional(row, "Injury Indicator"),
    )


def _parse_game_info(
    value: str, team: str, *, kickoff_required: bool
) -> tuple[str, bool, datetime | None]:
    """Return ``(opponent, is_home, kickoff)`` from an ``AWAY@HOME [kickoff]`` field."""

    parts = value.split()
    if len(parts) not in {1, 4} or (kickoff_required and len(parts) != 4):
        expected = (
            "'<AWAY>@<HOME> MM/DD/YYYY HH:MMPM TZ'"
            if kickoff_required
            else "'<AWAY>@<HOME>' optionally followed by 'MM/DD/YYYY HH:MMPM TZ'"
        )
        raise ValueError(f"game field must be {expected}")
    matchup_match = _MATCHUP_PATTERN.fullmatch(parts[0].upper())
    if matchup_match is None:
        raise ValueError("game matchup must have AWAY@HOME team codes")
    away = matchup_match.group("away")
    home = matchup_match.group("home")
    if team not in {away, home}:
        raise ValueError("Team is not present in the game matchup")
    opponent = home if team == away else away
    is_home = team == home
    if len(parts) == 1:
        return opponent, is_home, None

    _, date_text, time_text, timezone_text = parts
    timezone_name = _TIMEZONE_NAMES.get(timezone_text.upper())
    if timezone_name is None:
        raise ValueError(f"unsupported game timezone: {timezone_text}")
    try:
        local_time = datetime.strptime(
            f"{date_text} {time_text.upper()}", "%m/%d/%Y %I:%M%p"
        ).replace(tzinfo=ZoneInfo(timezone_name))
    except ValueError as error:
        raise ValueError("invalid game date/time") from error
    return opponent, is_home, local_time.astimezone(UTC)


def _classic_slots(position: str) -> tuple[str, ...]:
    if position in {"RB", "WR", "TE"}:
        return position, "FLEX"
    return (position,)


def _normalize_position(site: SalarySite, value: str) -> str:
    position = value.strip().upper()
    if site is SalarySite.FANDUEL and position in {"DST", "DEF"}:
        return "D"
    if site is SalarySite.DRAFTKINGS and position in {"D", "DEF"}:
        return "DST"
    return position


def _split_slots(value: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(slot for slot in re.split(r"[/,\s]+", value.strip().upper()) if slot)
    )


def _parse_salary(value: str) -> int:
    normalized = value.strip().replace("$", "").replace(",", "")
    try:
        salary = int(normalized)
    except ValueError as error:
        raise ValueError(f"invalid salary: {value}") from error
    if salary <= 0:
        raise ValueError("salary must be positive")
    return salary


def _required(row: dict[str, str | None], column: str) -> str:
    value = row.get(column)
    if value is None or not value.strip():
        raise ValueError(f"{column} must not be empty")
    return value.strip()


def _optional(row: dict[str, str | None], column: str) -> str | None:
    value = row.get(column)
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _raw_player_id(site: SalarySite, row: dict[str, str | None]) -> str | None:
    value = row.get("ID" if site is SalarySite.DRAFTKINGS else "Id")
    return None if value is None or not value.strip() else value.strip()


def _error_reasons(error: Exception) -> tuple[str, ...]:
    if isinstance(error, ValidationError):
        return tuple(
            f"{'.'.join(str(part) for part in detail['loc'])}: {detail['msg']}"
            for detail in error.errors()
        )
    return (str(error),)
