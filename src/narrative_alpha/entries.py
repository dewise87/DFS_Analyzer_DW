"""Append-only contest-entry assignments and realized receipt reporting."""

from __future__ import annotations

import io
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import cast

from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.portfolio import DfsSite, Lineup, OptimizationRequest, UploadEntry


class ContestEntryError(RuntimeError):
    """Raised when frozen upload entries cannot be tied to stored contests."""


def validate_upload_contests(
    connection: sqlite3.Connection,
    *,
    entries: tuple[UploadEntry, ...],
    site: DfsSite,
    slate_id: int,
    decision_at: datetime,
) -> None:
    """Fail before optimization if an upload entry cannot enter the ledger."""

    for entry in entries:
        _contest_for_entry(
            connection,
            entry=entry,
            site=site,
            slate_id=slate_id,
            decision_at=decision_at,
        )


def record_contest_entries(
    connection: sqlite3.Connection,
    *,
    decision_snapshot_id: str,
    decision_at: datetime,
    request: OptimizationRequest,
    lineups: tuple[Lineup, ...],
    source: str,
    indexes: tuple[int, ...] | None = None,
) -> int:
    """Append the selected frozen entry-to-lineup assignments, idempotently."""

    entries = request.upload_entries
    if not entries:
        return 0
    selected = tuple(range(len(entries))) if indexes is None else indexes
    if any(index < 0 or index >= len(entries) for index in selected):
        raise ContestEntryError("contest-entry assignment index is outside the frozen portfolio")
    inserted = 0
    for index in selected:
        entry = entries[index]
        contest = _contest_for_entry(
            connection,
            entry=entry,
            site=request.site,
            slate_id=request.slate_id,
            decision_at=decision_at,
        )
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO contest_entries(
                decision_snapshot_id, contest_id, entry_id, entry_fee_cents,
                lineup_id, recorded_at, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_snapshot_id,
                int(contest["contest_id"]),
                entry.entry_id,
                int(contest["entry_fee_cents"]),
                lineups[index].lineup_id,
                utc_timestamp(decision_at),
                source,
            ),
        )
        inserted += int(cursor.rowcount == 1)
    return inserted


def _contest_for_entry(
    connection: sqlite3.Connection,
    *,
    entry: UploadEntry,
    site: DfsSite,
    slate_id: int,
    decision_at: datetime,
) -> sqlite3.Row:
    stamp = utc_timestamp(decision_at)
    contest = connection.execute(
        """
        SELECT c.contest_id, c.entry_fee_cents
        FROM contests AS c
        WHERE c.site = ? AND c.external_contest_id = ? AND c.slate_id = ?
          AND rtrim(c.observed_at, 'Z') <= rtrim(?, 'Z')
          AND rtrim(c.valid_from, 'Z') <= rtrim(?, 'Z')
          AND (c.valid_to IS NULL OR rtrim(c.valid_to, 'Z') > rtrim(?, 'Z'))
        ORDER BY rtrim(c.observed_at, 'Z') DESC, c.contest_id DESC
        LIMIT 1
        """,
        (site.value, entry.contest_id, slate_id, stamp, stamp, stamp),
    ).fetchone()
    if contest is None:
        raise ContestEntryError(
            f"upload entry {entry.entry_id!r} names unknown contest {entry.contest_id!r}; "
            "add its metadata with `na-contest add`, then rerun `na-ops slate`"
        )
    return cast(sqlite3.Row, contest)


@dataclass(frozen=True)
class EntryReceiptRow:
    archetype: str
    entries: int
    settled: int
    unsettled: int
    fees_cents: int
    winnings_cents: int
    net_cents: int
    realized_roi: float | None
    best_rank: int | None
    worst_rank: int | None
    label_rows: int
    labeled_weeks: int
    cost_per_labeled_week_cents: int | None


@dataclass(frozen=True)
class EntryReceiptReport:
    season: int
    through_week: int
    rows: tuple[EntryReceiptRow, ...]
    unledgered_entries: int = 0

    @property
    def fees_cents(self) -> int:
        return sum(row.fees_cents for row in self.rows)

    @property
    def net_cents(self) -> int:
        return sum(row.net_cents for row in self.rows)


def build_entry_receipt_report(
    connection: sqlite3.Connection, *, season: int, week: int
) -> EntryReceiptReport:
    """Aggregate the latest assignment and settlement for every season-to-date entry."""

    rows = connection.execute(
        """
        WITH ranked_assignments AS (
            SELECT ce.*, c.external_contest_id, c.site, c.archetype, s.season, s.week,
                   row_number() OVER (
                       PARTITION BY c.site, c.external_contest_id, ce.entry_id
                       ORDER BY rtrim(d.decision_at, 'Z') DESC, ce.contest_entry_id DESC
                   ) AS assignment_rank
            FROM contest_entries AS ce
            JOIN contests AS c ON c.contest_id = ce.contest_id
            JOIN slates AS s ON s.slate_id = c.slate_id
            JOIN decision_snapshots AS d
              ON d.decision_snapshot_id = ce.decision_snapshot_id
            WHERE s.season = ? AND s.week <= ?
        ), current_assignments AS (
            SELECT * FROM ranked_assignments WHERE assignment_rank = 1
        ), ranked_results AS (
            SELECT cer.*,
                   row_number() OVER (
                       PARTITION BY cer.contest_entry_id
                       ORDER BY rtrim(cer.observed_at, 'Z') DESC,
                                cer.contest_entry_result_id DESC
                   ) AS result_rank
            FROM contest_entry_results AS cer
        ), labels AS (
            SELECT ao.site, ao.external_contest_id, count(*) AS label_rows
            FROM actual_ownership AS ao
            JOIN slates AS ls ON ls.slate_id = ao.slate_id
            WHERE ls.season = ? AND ls.week <= ?
            GROUP BY ao.site, ao.external_contest_id
        ), receipt_totals AS (
            SELECT ca.archetype,
                   count(*) AS entries,
                   sum(CASE WHEN rr.settlement_status = 'settled' THEN 1 ELSE 0 END) AS settled,
                   sum(CASE WHEN rr.settlement_status = 'unsettled' THEN 1 ELSE 0 END) AS unsettled,
                   sum(ca.entry_fee_cents) AS fees_cents,
                   sum(CASE WHEN rr.settlement_status = 'settled' THEN rr.payout_cents ELSE 0 END)
                       AS winnings_cents,
                   min(CASE WHEN rr.settlement_status = 'settled' THEN rr.rank END) AS best_rank,
                   max(CASE WHEN rr.settlement_status = 'settled' THEN rr.rank END) AS worst_rank
            FROM current_assignments AS ca
            LEFT JOIN ranked_results AS rr
              ON rr.contest_entry_id = ca.contest_entry_id AND rr.result_rank = 1
            GROUP BY ca.archetype
        ), contest_set AS (
            SELECT DISTINCT archetype, site, external_contest_id, week
            FROM current_assignments
        ), label_totals AS (
            SELECT cs.archetype, sum(COALESCE(labels.label_rows, 0)) AS label_rows,
                   count(DISTINCT CASE WHEN labels.label_rows IS NOT NULL THEN cs.week END)
                       AS labeled_weeks
            FROM contest_set AS cs
            LEFT JOIN labels
              ON labels.site = cs.site AND labels.external_contest_id = cs.external_contest_id
            GROUP BY cs.archetype
        )
        SELECT rt.*, COALESCE(lt.label_rows, 0) AS label_rows,
               COALESCE(lt.labeled_weeks, 0) AS labeled_weeks
        FROM receipt_totals AS rt
        LEFT JOIN label_totals AS lt ON lt.archetype = rt.archetype
        ORDER BY rt.archetype
        """,
        (season, week, season, week),
    ).fetchall()
    report_rows: list[EntryReceiptRow] = []
    for row in rows:
        fees = int(row["fees_cents"])
        winnings = int(row["winnings_cents"] or 0)
        net = winnings - fees
        labeled_weeks = int(row["labeled_weeks"])
        report_rows.append(
            EntryReceiptRow(
                archetype=str(row["archetype"]),
                entries=int(row["entries"]),
                settled=int(row["settled"] or 0),
                unsettled=int(row["unsettled"] or 0),
                fees_cents=fees,
                winnings_cents=winnings,
                net_cents=net,
                realized_roi=None if fees == 0 else net / fees,
                best_rank=None if row["best_rank"] is None else int(row["best_rank"]),
                worst_rank=None if row["worst_rank"] is None else int(row["worst_rank"]),
                label_rows=int(row["label_rows"] or 0),
                labeled_weeks=labeled_weeks,
                cost_per_labeled_week_cents=(
                    None if labeled_weeks == 0 else round(fees / labeled_weeks)
                ),
            )
        )
    unledgered_row = connection.execute(
        """
        WITH ranked AS (
            SELECT CAST(json_extract(summary_json, '$.unledgered_entries') AS INTEGER) AS count,
                   row_number() OVER (
                       PARTITION BY json_extract(summary_json, '$.season'),
                                    json_extract(summary_json, '$.week'),
                                    json_extract(summary_json, '$.site')
                       ORDER BY started_at DESC, ops_run_id DESC
                   ) AS row_rank
            FROM ops_runs
            WHERE step = 'results_ingest' AND status = 'succeeded'
              AND CAST(json_extract(summary_json, '$.season') AS INTEGER) = ?
              AND CAST(json_extract(summary_json, '$.week') AS INTEGER) <= ?
        )
        SELECT COALESCE(sum(count), 0) AS count FROM ranked WHERE row_rank = 1
        """,
        (season, week),
    ).fetchone()
    return EntryReceiptReport(
        season=season,
        through_week=week,
        rows=tuple(report_rows),
        unledgered_entries=int(unledgered_row["count"]),
    )


def render_entry_receipt_report(report: EntryReceiptReport, *, section: bool = False) -> str:
    """Render a cumulative, realized-only receipt; it contains no forecast fields."""

    output = io.StringIO(newline="")
    output.write("ENTRY RECEIPTS — REALIZED, NOT PROJECTED\n")
    output.write(f"season={report.season}\nthrough_week={report.through_week}\n")
    output.write(
        "standings_entries_for_our_name_not_in_ledger="
        f"{report.unledgered_entries}\n"
    )
    output.write(
        "archetype,entries,settled,unsettled,fees,winnings,net,realized_roi,"
        "best_rank,worst_rank,label_rows,labeled_weeks,cost_per_labeled_week\n"
    )
    for row in report.rows:
        output.write(
            ",".join(
                (
                    row.archetype,
                    str(row.entries),
                    str(row.settled),
                    str(row.unsettled),
                    _money(row.fees_cents),
                    _money(row.winnings_cents),
                    _money(row.net_cents),
                    "NA" if row.realized_roi is None else f"{row.realized_roi:.4f}",
                    "NA" if row.best_rank is None else str(row.best_rank),
                    "NA" if row.worst_rank is None else str(row.worst_rank),
                    str(row.label_rows),
                    str(row.labeled_weeks),
                    (
                        "NA"
                        if row.cost_per_labeled_week_cents is None
                        else _money(row.cost_per_labeled_week_cents)
                    ),
                )
            )
            + "\n"
        )
    rendered = output.getvalue()
    return "\n" + rendered if section else rendered


def _money(cents: int) -> str:
    return f"{Decimal(cents) / Decimal(100):.2f}"
