"""Append-only history of `na-ops batch` steps, and the reads `na-ops status` needs."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, get_args

from narrative_alpha import __version__
from narrative_alpha.ingest.timestamps import ensure_utc
from narrative_alpha.store import OpsRunRow

OpsStep = Literal["collect", "purge", "extract", "nflverse_refresh"]
OpsStepStatus = Literal["succeeded", "failed", "skipped"]
OPS_STEPS: tuple[OpsStep, ...] = get_args(OpsStep)


@dataclass(frozen=True)
class RecordedRun:
    """One `ops_runs` row as the status screen reads it."""

    ops_run_id: int
    batch_run_id: str
    step: OpsStep
    status: OpsStepStatus
    started_at: datetime
    finished_at: datetime
    summary: dict[str, object]
    code_version: str
    error_text: str | None


def record_ops_run(
    connection: sqlite3.Connection,
    *,
    batch_run_id: str,
    step: OpsStep,
    status: OpsStepStatus,
    started_at: datetime,
    finished_at: datetime,
    summary: dict[str, object],
    error_text: str | None = None,
    code_version: str = __version__,
) -> int:
    """Append one step outcome and return its row id.

    Validation happens in :class:`OpsRunRow` before SQLite sees anything, so a malformed
    outcome is a programming error caught at the boundary rather than a CHECK failure
    that loses the whole batch's history.
    """

    row = OpsRunRow(
        ops_run_id=1,  # placeholder; SQLite assigns the real identity
        batch_run_id=batch_run_id,
        step=step,
        status=status,
        started_at=ensure_utc(started_at),
        finished_at=ensure_utc(finished_at),
        summary_json=summary,
        code_version=code_version,
        error_text=error_text,
    )
    values = row.db_values()
    values.pop("ops_run_id")
    columns = ", ".join(values)
    placeholders = ", ".join(f":{column}" for column in values)
    cursor = connection.execute(
        f"INSERT INTO ops_runs ({columns}) VALUES ({placeholders})", values
    )
    if cursor.lastrowid is None:  # pragma: no cover - SQLite always assigns a rowid here
        raise sqlite3.DatabaseError("ops_runs insert did not return a row id")
    return int(cursor.lastrowid)


def last_run(
    connection: sqlite3.Connection,
    *,
    step: OpsStep,
    status: OpsStepStatus,
) -> RecordedRun | None:
    """Return the most recent recorded run for one step and outcome."""

    row = connection.execute(
        """
        SELECT * FROM ops_runs
        WHERE step = ? AND status = ?
        ORDER BY started_at DESC, ops_run_id DESC
        LIMIT 1
        """,
        (step, status),
    ).fetchone()
    return None if row is None else _recorded(row)


def last_run_any_status(connection: sqlite3.Connection, *, step: OpsStep) -> RecordedRun | None:
    """Return the most recent recorded run for one step regardless of its outcome."""

    row = connection.execute(
        """
        SELECT * FROM ops_runs
        WHERE step = ?
        ORDER BY started_at DESC, ops_run_id DESC
        LIMIT 1
        """,
        (step,),
    ).fetchone()
    return None if row is None else _recorded(row)


def _recorded(row: sqlite3.Row) -> RecordedRun:
    stored = OpsRunRow.from_db(row)
    return RecordedRun(
        ops_run_id=stored.ops_run_id,
        batch_run_id=stored.batch_run_id,
        step=stored.step,
        status=stored.status,
        started_at=stored.started_at,
        finished_at=stored.finished_at,
        summary=dict(stored.summary_json),
        code_version=stored.code_version,
        error_text=stored.error_text,
    )

