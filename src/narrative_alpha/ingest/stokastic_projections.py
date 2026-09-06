"""Strict ingestion for the Stokastic NFL Data Hub Projections export.

One export per site carries both the projection and the ownership view of a slate, so
the same bytes serve ``parse_projections`` and ``parse_ownership``. Nothing about the
file says which site it belongs to — there is no id column, no slate column and no
vendor timestamp — so the site is read from the one token that does differ: the
position the vendor gives a team defense, ``DST`` on DraftKings and ``D`` on FanDuel.
A file with no defense row, or with more than one defense token, is refused by name
rather than attributed by guesswork.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from narrative_alpha.identity.defense import is_defense_position
from narrative_alpha.ingest.projections import (
    OwnershipParseResult,
    ParsedOwnership,
    ParsedProjection,
    ProjectionParseResult,
    RejectedSourceRow,
    SourceFormatError,
)

STOKASTIC_PROJECTIONS_FORMAT = "stokastic-projections-v1"
"""Prefix of ``source_version``; the file's own hash follows it."""

PROJECTIONS_HEADERS: tuple[str, ...] = (
    "Player",
    "Salary",
    "Position",
    "Team",
    "Opponent",
    "Projection",
    "Value",
    "Ownership %",
    "Optimal %",
    "Leverage",
    "Std Dev",
    "Boom",
    "Bust",
    "Ceiling",
    "Floor",
)
"""The exact column set of the real export; anything else is drift, not a variant."""

OWNERSHIP_COLUMN = "Ownership %"
"""The ``%`` in this header name — never a row's magnitude — is what divides by 100."""

OWNERSHIP_ROLE: Literal["classic"] = "classic"
"""The Data Hub export prices a classic slate; it carries no captain or MVP column."""

_DEFENSE_SITES = {"DST": "draftkings", "D": "fanduel"}
"""The vendor's defense position token, which is the only per-site difference."""


@dataclass(frozen=True)
class _Row:
    """One accepted export row, shared by the projection and the ownership view."""

    row_number: int
    name_raw: str
    team: str
    opponent: str
    position: str
    vendor_salary: int | None
    projection_mean: float
    projection_floor: float | None
    projection_ceiling: float | None
    ownership: float


@dataclass(frozen=True)
class _File:
    """A parsed export before it is projected onto one of the two parse results."""

    site: str
    source_version: str
    rows_seen: int
    rows: tuple[_Row, ...]
    rejected: tuple[RejectedSourceRow, ...]
    zero_projection_rows: int
    range_dropped: tuple[str, ...]


def parse_stokastic_projections(path: Path) -> ProjectionParseResult:
    """Parse the export's projection view: mean, bounds, and embedded ownership."""

    parsed = _read(path)
    rows: list[ParsedProjection] = []
    rejected = list(parsed.rejected)
    for row in parsed.rows:
        try:
            rows.append(
                ParsedProjection(
                    name_raw=row.name_raw,
                    team=row.team,
                    opponent=row.opponent,
                    position=row.position,
                    vendor_salary=row.vendor_salary,
                    projection_mean=row.projection_mean,
                    projection_floor=row.projection_floor,
                    projection_ceiling=row.projection_ceiling,
                    ownership_projection=row.ownership,
                    source_version=parsed.source_version,
                )
            )
        except ValidationError as error:
            rejected.append(_rejected(row.row_number, error))
    return ProjectionParseResult(
        site=parsed.site,
        rows_seen=parsed.rows_seen,
        rows=tuple(rows),
        rejected=tuple(sorted(rejected, key=lambda item: item.row_number)),
        zero_projection_rows=parsed.zero_projection_rows,
        range_dropped=parsed.range_dropped,
    )


def parse_stokastic_ownership(path: Path) -> OwnershipParseResult:
    """Parse the export's ownership view from the same bytes and the same rows."""

    parsed = _read(path)
    rows: list[ParsedOwnership] = []
    rejected = list(parsed.rejected)
    for row in parsed.rows:
        try:
            rows.append(
                ParsedOwnership(
                    name_raw=row.name_raw,
                    team=row.team,
                    opponent=row.opponent,
                    position=row.position,
                    vendor_salary=row.vendor_salary,
                    role=OWNERSHIP_ROLE,
                    ownership=row.ownership,
                    source_version=parsed.source_version,
                )
            )
        except ValidationError as error:
            rejected.append(_rejected(row.row_number, error))
    return OwnershipParseResult(
        site=parsed.site,
        rows_seen=parsed.rows_seen,
        rows=tuple(rows),
        rejected=tuple(sorted(rejected, key=lambda item: item.row_number)),
    )


def _read(path: Path) -> _File:
    """Read the export once: exact header, attributed site, and per-row values."""

    try:
        raw_bytes = path.read_bytes()
        text = raw_bytes.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise SourceFormatError(f"cannot read Stokastic projections CSV {path}: {error}") from error

    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = tuple(reader.fieldnames or ())
    if headers != PROJECTIONS_HEADERS:
        _refuse_header(path, headers)

    rows: list[_Row] = []
    rejected: list[RejectedSourceRow] = []
    defense_tokens: set[str] = set()
    zero_projection_rows = 0
    range_dropped: list[str] = []
    rows_seen = 0

    for row_number, raw in enumerate(reader, start=2):
        if None in raw:
            raise SourceFormatError(f"{path} row {row_number}: more cells than header columns")
        rows_seen += 1
        position = _text(raw, "Position").upper()
        if is_defense_position(position):
            defense_tokens.add(position)
        reasons: list[str] = []
        mean = _number(raw, "Projection", reasons)
        ownership_percent = _number(raw, OWNERSHIP_COLUMN, reasons)
        name = _text(raw, "Player")
        team = _text(raw, "Team")
        opponent = _text(raw, "Opponent")
        for column, value in (("Player", name), ("Position", position), ("Team", team)):
            if not value:
                reasons.append(f"{column} is empty")
        salary = _optional_integer(raw, "Salary", reasons)
        if reasons or mean is None or ownership_percent is None:
            rejected.append(RejectedSourceRow(row_number=row_number, reasons=tuple(reasons)))
            continue

        floor = _optional_number(raw, "Floor", reasons)
        ceiling = _optional_number(raw, "Ceiling", reasons)
        if reasons:
            rejected.append(RejectedSourceRow(row_number=row_number, reasons=tuple(reasons)))
            continue
        if (floor is not None and floor > mean) or (ceiling is not None and ceiling < mean):
            # The vendor's own bounds contradict its mean (one FanDuel defense row does
            # this). The mean is still the vendor's number, so keep it and drop the pair.
            range_dropped.append(name)
            floor = None
            ceiling = None
        if mean == 0:
            zero_projection_rows += 1

        rows.append(
            _Row(
                row_number=row_number,
                name_raw=name,
                team=team,
                opponent=opponent,
                position="DST" if is_defense_position(position) else position,
                vendor_salary=salary,
                projection_mean=mean,
                projection_floor=floor,
                projection_ceiling=ceiling,
                # The header says "Ownership %", so the column is a percentage.
                ownership=ownership_percent / 100.0,
            )
        )

    site = _attributed_site(path, defense_tokens)
    digest = hashlib.sha256(raw_bytes).hexdigest()
    return _File(
        site=site,
        source_version=f"{STOKASTIC_PROJECTIONS_FORMAT}:{digest[:12]}",
        rows_seen=rows_seen,
        rows=tuple(rows),
        rejected=tuple(rejected),
        zero_projection_rows=zero_projection_rows,
        range_dropped=tuple(range_dropped),
    )


def _refuse_header(path: Path, headers: tuple[str, ...]) -> None:
    expected = set(PROJECTIONS_HEADERS)
    actual = set(headers)
    missing = ", ".join(sorted(expected - actual)) or "none"
    unexpected = ", ".join(sorted(actual - expected)) or "none"
    order_note = ""
    if expected == actual:
        order_note = f"; expected column order: {', '.join(PROJECTIONS_HEADERS)}"
    raise SourceFormatError(
        f"{path} is not a Stokastic Data Hub Projections export; "
        f"missing columns: {missing}; unexpected columns: {unexpected}{order_note}"
    )


def _attributed_site(path: Path, defense_tokens: set[str]) -> str:
    """Read the site off the defense position token, refusing anything ambiguous."""

    if not defense_tokens:
        raise SourceFormatError(
            f"{path} carries no team-defense row, so the site it was exported for "
            "cannot be attributed; a Stokastic export names a defense 'DST' on "
            "DraftKings and 'D' on FanDuel"
        )
    if len(defense_tokens) > 1:
        raise SourceFormatError(
            f"{path} mixes team-defense position tokens "
            f"({', '.join(sorted(defense_tokens))}), so it belongs to no single site"
        )
    token = defense_tokens.pop()
    site = _DEFENSE_SITES.get(token)
    if site is None:
        raise SourceFormatError(
            f"{path} names its team defenses {token!r}, which attributes to no site; "
            f"expected {' or '.join(repr(known) for known in sorted(_DEFENSE_SITES))}"
        )
    return site


def _text(raw: dict[str, str | None], column: str) -> str:
    value = raw.get(column)
    return "" if value is None else value.strip()


def _number(raw: dict[str, str | None], column: str, reasons: list[str]) -> float | None:
    """A column that must carry a finite number; blank or non-numeric rejects the row."""

    text = _text(raw, column)
    if not text:
        reasons.append(f"{column} is blank")
        return None
    try:
        value = float(text)
    except ValueError:
        reasons.append(f"{column} is not numeric: {text!r}")
        return None
    if value != value or value in {float("inf"), float("-inf")}:
        reasons.append(f"{column} is not finite: {text!r}")
        return None
    return value


def _optional_number(raw: dict[str, str | None], column: str, reasons: list[str]) -> float | None:
    """A bound the vendor leaves blank for its deep bench; present must still be numeric."""

    if not _text(raw, column):
        return None
    return _number(raw, column, reasons)


def _optional_integer(raw: dict[str, str | None], column: str, reasons: list[str]) -> int | None:
    text = _text(raw, column)
    if not text:
        return None
    try:
        return int(text.replace("$", "").replace(",", ""))
    except ValueError:
        reasons.append(f"{column} is not a whole number: {text!r}")
        return None


def _rejected(row_number: int, error: ValidationError) -> RejectedSourceRow:
    return RejectedSourceRow(
        row_number=row_number,
        reasons=tuple(
            f"{'.'.join(str(part) for part in detail['loc'])}: {detail['msg']}"
            for detail in error.errors()
        )
        or ("row failed validation",),
    )
