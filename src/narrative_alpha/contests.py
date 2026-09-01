"""Manual, append-only contest and payout-curve capture."""

from __future__ import annotations

import csv
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.portfolio import ContestArchetype, DfsSite
from narrative_alpha.store import ContestPayoutRow, ContestRow

PAYOUT_CSV_HEADERS = ("rank_from", "rank_to", "prize_cents")


class ContestEntryError(ValueError):
    """Raised when a manual contest or payout table cannot be stored safely."""


class ManualContest(BaseModel):
    """Operator-copied contest metadata before database-generated columns are added."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    external_contest_id: str
    site: DfsSite
    slate_id: int = Field(gt=0)
    archetype: ContestArchetype
    field_size: int = Field(gt=0)
    entry_limit: int = Field(gt=0)
    entry_fee_cents: int = Field(ge=0)
    total_prizes_cents: int | None = Field(default=None, ge=0)
    payout_curve_id: str | None = None
    source: str = "manual-site-lobby"
    published_at: datetime | None = None
    observed_at: datetime
    effective_at: datetime | None = None
    source_version: str = "manual-contest-v1"

    @field_validator("external_contest_id", "source", "source_version")
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

    @field_validator("published_at", "observed_at", "effective_at")
    @classmethod
    def utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)


class PayoutBand(BaseModel):
    """One inclusive payout band copied from a site lobby."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rank_from: int = Field(ge=1)
    rank_to: int = Field(ge=1)
    prize_cents: int = Field(ge=0)

    @model_validator(mode="after")
    def ordered_ranks(self) -> Self:
        if self.rank_from > self.rank_to:
            raise ValueError("rank_from must not exceed rank_to")
        return self

    @property
    def band_total_cents(self) -> int:
        return (self.rank_to - self.rank_from + 1) * self.prize_cents


@dataclass(frozen=True)
class ContestAddResult:
    contest: ContestRow
    payouts: tuple[ContestPayoutRow, ...]


def parse_payout_csv(csv_path: Path) -> tuple[PayoutBand, ...]:
    """Read the exact three-column manual payout CSV contract."""

    try:
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = tuple(reader.fieldnames or ())
            if headers != PAYOUT_CSV_HEADERS:
                raise ContestEntryError(
                    "payout CSV headers must be exactly: " + ",".join(PAYOUT_CSV_HEADERS)
                )
            bands: list[PayoutBand] = []
            for row_number, row in enumerate(reader, start=2):
                if None in row:
                    raise ContestEntryError(f"payout CSV row {row_number} has extra columns")
                try:
                    bands.append(
                        PayoutBand(
                            rank_from=int(row["rank_from"]),
                            rank_to=int(row["rank_to"]),
                            prize_cents=int(row["prize_cents"]),
                        )
                    )
                except (TypeError, ValueError) as error:
                    raise ContestEntryError(
                        f"invalid payout CSV row {row_number}: {error}"
                    ) from error
    except UnicodeDecodeError as error:
        raise ContestEntryError("payout CSV is not UTF-8") from error

    if not bands:
        raise ContestEntryError("payout CSV contains no payout bands")
    return tuple(bands)


def add_contest(
    connection: sqlite3.Connection,
    contest: ManualContest,
    payouts: Sequence[PayoutBand],
    *,
    ingested_at: datetime | None = None,
    run_id: str | None = None,
) -> ContestAddResult:
    """Atomically insert one contest observation and its non-overlapping payout bands."""

    bands = tuple(payouts)
    if not bands:
        raise ContestEntryError("at least one payout band is required")
    if contest.payout_curve_id is None:
        raise ContestEntryError("payout_curve_id is required when payout bands are supplied")
    _refuse_overlapping_bands(bands)
    calculated_total = sum(band.band_total_cents for band in bands)
    if (
        contest.total_prizes_cents is not None
        and calculated_total != contest.total_prizes_cents
    ):
        raise ContestEntryError(
            f"payout bands total {calculated_total} cents, but total_prizes_cents is "
            f"{contest.total_prizes_cents} cents"
        )

    ingestion_time = ensure_utc(ingested_at or datetime.now(UTC))
    observed_at = utc_timestamp(contest.observed_at)
    point_in_time = (
        contest.source,
        None if contest.published_at is None else utc_timestamp(contest.published_at),
        observed_at,
        utc_timestamp(ingestion_time),
        None if contest.effective_at is None else utc_timestamp(contest.effective_at),
        observed_at,
        None,
        contest.source_version,
        run_id,
    )

    _refuse_existing_overlaps(connection, contest.payout_curve_id, bands, observed_at)
    connection.execute("SAVEPOINT add_manual_contest")
    try:
        contest_cursor = connection.execute(
            """
            INSERT INTO contests(
                external_contest_id, site, slate_id, archetype, field_size,
                entry_limit, entry_fee_cents, total_prizes_cents, payout_curve_id,
                source, published_at, observed_at, ingested_at, effective_at,
                valid_from, valid_to, source_version, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contest.external_contest_id,
                contest.site.value,
                contest.slate_id,
                contest.archetype.value,
                contest.field_size,
                contest.entry_limit,
                contest.entry_fee_cents,
                contest.total_prizes_cents,
                contest.payout_curve_id,
                *point_in_time,
            ),
        )
        contest_id = _required_lastrowid(contest_cursor, "contest")
        payout_ids: list[int] = []
        for band in sorted(bands, key=lambda item: (item.rank_from, item.rank_to)):
            payout_cursor = connection.execute(
                """
                INSERT INTO contest_payouts(
                    payout_curve_id, rank_from, rank_to, prize_cents,
                    source, published_at, observed_at, ingested_at, effective_at,
                    valid_from, valid_to, source_version, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contest.payout_curve_id,
                    band.rank_from,
                    band.rank_to,
                    band.prize_cents,
                    *point_in_time,
                ),
            )
            payout_ids.append(_required_lastrowid(payout_cursor, "contest payout"))
    except sqlite3.IntegrityError as error:
        connection.execute("ROLLBACK TO SAVEPOINT add_manual_contest")
        connection.execute("RELEASE SAVEPOINT add_manual_contest")
        raise ContestEntryError(f"contest entry violates the store schema: {error}") from error
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT add_manual_contest")
        connection.execute("RELEASE SAVEPOINT add_manual_contest")
        raise
    else:
        connection.execute("RELEASE SAVEPOINT add_manual_contest")

    contest_row = load_contest(connection, contest_id=contest_id)
    payout_rows = tuple(
        ContestPayoutRow.from_db(
            connection.execute(
                "SELECT * FROM contest_payouts WHERE contest_payout_id = ?", (payout_id,)
            ).fetchone()
        )
        for payout_id in payout_ids
    )
    return ContestAddResult(contest=contest_row, payouts=payout_rows)


def load_contest(connection: sqlite3.Connection, *, contest_id: int) -> ContestRow:
    """Reload one contest by its database identity through the typed row contract."""

    row = connection.execute(
        "SELECT * FROM contests WHERE contest_id = ?", (contest_id,)
    ).fetchone()
    if row is None:
        raise ContestEntryError(f"contest {contest_id} does not exist")
    return ContestRow.from_db(row)


def load_contest_payouts(
    connection: sqlite3.Connection,
    *,
    payout_curve_id: str,
    as_of: datetime | None = None,
) -> tuple[ContestPayoutRow, ...]:
    """Reload one version of a payout curve in inclusive-rank order.

    Returns the newest observation at or before ``as_of`` (default: the newest
    observation of any age). Mixing observations would double-count bands, so a single
    version is always returned.
    """

    bound = utc_timestamp(ensure_utc(as_of)) if as_of is not None else None
    version = connection.execute(
        """
        SELECT max(observed_at) AS observed_at FROM contest_payouts
        WHERE payout_curve_id = ?
          AND (? IS NULL OR observed_at <= ?)
        """,
        (payout_curve_id, bound, bound),
    ).fetchone()
    if version is None or version["observed_at"] is None:
        return ()
    rows = connection.execute(
        """
        SELECT * FROM contest_payouts
        WHERE payout_curve_id = ? AND observed_at = ?
        ORDER BY rank_from, rank_to, contest_payout_id
        """,
        (payout_curve_id, str(version["observed_at"])),
    ).fetchall()
    return tuple(ContestPayoutRow.from_db(row) for row in rows)


def _refuse_overlapping_bands(bands: tuple[PayoutBand, ...]) -> None:
    ordered = sorted(bands, key=lambda item: (item.rank_from, item.rank_to))
    for prior, current in pairwise(ordered):
        if current.rank_from <= prior.rank_to:
            raise ContestEntryError(
                "payout bands overlap: "
                f"{prior.rank_from}-{prior.rank_to} and {current.rank_from}-{current.rank_to}"
            )


def _refuse_existing_overlaps(
    connection: sqlite3.Connection,
    payout_curve_id: str,
    bands: tuple[PayoutBand, ...],
    observed_at: str,
) -> None:
    # Scoped to one observation. These tables are append-only versions, so a later
    # re-observation of the same curve (payouts and field size move while a contest
    # fills) is a new version, not an overlap; readers pick a version by observed_at.
    for band in bands:
        overlap = connection.execute(
            """
            SELECT rank_from, rank_to FROM contest_payouts
            WHERE payout_curve_id = ?
              AND observed_at = ?
              AND rank_from <= ?
              AND rank_to >= ?
            LIMIT 1
            """,
            (payout_curve_id, observed_at, band.rank_to, band.rank_from),
        ).fetchone()
        if overlap is not None:
            raise ContestEntryError(
                f"payout band {band.rank_from}-{band.rank_to} overlaps existing band "
                f"{int(overlap['rank_from'])}-{int(overlap['rank_to'])} in curve "
                f"{payout_curve_id!r} at observation {observed_at}"
            )


def _required_lastrowid(cursor: sqlite3.Cursor, description: str) -> int:
    row_id = cursor.lastrowid
    if row_id is None:
        raise ContestEntryError(f"SQLite did not return a {description} row ID")
    return int(row_id)
