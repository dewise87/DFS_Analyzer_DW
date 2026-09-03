"""Append-only history of operator-lane steps, and the reads `na-ops status` needs.

All three lanes record here. ``na-ops batch`` runs the Wed-Fri data steps, ``na-ops slate``
runs the Saturday/Sunday decision steps, and ``na-ops results`` closes the week on Tuesday.
The step recorder below is what makes a lane a lane: every step is isolated, its outcome is
committed on its own, and a failure is a recorded fact rather than an exception that costs
the run its history.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, get_args

from narrative_alpha import __version__
from narrative_alpha.ingest.timestamps import ensure_utc
from narrative_alpha.store import OpsRunRow

OpsBatchStep = Literal["collect", "purge", "extract", "nflverse_refresh", "episodes"]
OpsSlateStep = Literal[
    "slate_salaries",
    "slate_projections",
    "slate_episodes",
    "slate_features",
    "slate_build",
    "slate_memo",
]
OpsResultsStep = Literal[
    "results_capture",
    "results_ingest",
    "results_replay",
    "results_report",
    "results_labels",
]
OpsStep = OpsBatchStep | OpsSlateStep | OpsResultsStep
OpsStepStatus = Literal["succeeded", "failed", "skipped"]

BATCH_STEPS: tuple[OpsBatchStep, ...] = get_args(OpsBatchStep)
SLATE_STEPS: tuple[OpsSlateStep, ...] = get_args(OpsSlateStep)
RESULTS_STEPS: tuple[OpsResultsStep, ...] = get_args(OpsResultsStep)
OPS_STEPS: tuple[OpsStep, ...] = BATCH_STEPS + SLATE_STEPS + RESULTS_STEPS


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


@dataclass(frozen=True)
class StepOutcome:
    """One recorded step of a lane."""

    step: OpsStep
    status: OpsStepStatus
    started_at: datetime
    finished_at: datetime
    summary: dict[str, object]
    error_text: str | None = None

    @property
    def ok(self) -> bool:
        return self.status != "failed"


class StepFailure(Exception):
    """A step decided it failed for a reason it can state; not an unexpected error.

    The summary it carries is recorded alongside the message, so a refusal keeps the
    numbers that explain it instead of degrading to a bare sentence.
    """

    def __init__(self, message: str, summary: dict[str, object]) -> None:
        super().__init__(message)
        self.summary = summary


StepAction = Callable[[], tuple[OpsStepStatus, dict[str, object], str | None]]


class StepRecorder:
    """Runs each step of one lane invocation, isolates its failure, appends its row.

    ``step_errors`` is the lane's own list of exceptions that describe a bad week rather
    than a broken program. Anything outside it propagates: a lane must not swallow a bug.

    ``base_summary`` is merged under every recorded summary, so a fact that identifies the
    whole invocation — the slate lane's decision instant, say — survives even a step that
    failed before it had a summary of its own. A step may not overwrite it.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        step_errors: tuple[type[BaseException], ...],
        base_summary: dict[str, object] | None = None,
    ) -> None:
        self._connection = connection
        self._run_id = run_id
        self._step_errors = step_errors
        self._base_summary = dict(base_summary or {})
        self.outcomes: list[StepOutcome] = []

    def run(self, step: OpsStep, action: StepAction) -> StepOutcome:
        started_at = ensure_utc(datetime.now(UTC))
        try:
            status, summary, error_text = action()
        except StepFailure as failure:
            status, summary, error_text = "failed", failure.summary, str(failure)
        except self._step_errors as error:
            status, summary, error_text = "failed", {}, f"{type(error).__name__}: {error}"
        return self._record(step, status, started_at, summary, error_text)

    def skip(
        self,
        step: OpsStep,
        reason: str,
        summary: dict[str, object] | None = None,
    ) -> StepOutcome:
        started_at = ensure_utc(datetime.now(UTC))
        return self._record(step, "skipped", started_at, summary or {}, reason)

    def _record(
        self,
        step: OpsStep,
        status: OpsStepStatus,
        started_at: datetime,
        summary: dict[str, object],
        error_text: str | None,
    ) -> StepOutcome:
        finished_at = ensure_utc(datetime.now(UTC))
        outcome = StepOutcome(
            step=step,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            summary=self._base_summary | summary,
            error_text=error_text,
        )
        # History is the whole point of the lane; commit it on its own so a later step's
        # rollback can never take an earlier step's record with it.
        try:
            record_ops_run(
                self._connection,
                batch_run_id=self._run_id,
                step=step,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                summary=outcome.summary,
                error_text=error_text,
            )
            self._connection.commit()
        except (sqlite3.Error, ValueError):
            self._connection.rollback()
            raise
        self.outcomes.append(outcome)
        return outcome


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

    ``batch_run_id`` identifies one lane invocation — ``ops-…`` for the batch lane,
    ``slate-…`` for the slate lane. The column keeps its original name because the
    history it holds predates the second lane and must stay readable unchanged.

    Validation happens in :class:`OpsRunRow` before SQLite sees anything, so a malformed
    outcome is a programming error caught at the boundary rather than a CHECK failure
    that loses the whole run's history.
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
    cursor = connection.execute(f"INSERT INTO ops_runs ({columns}) VALUES ({placeholders})", values)
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


def recent_runs(
    connection: sqlite3.Connection,
    *,
    limit: int = 20,
) -> tuple[RecordedRun, ...]:
    """Return the most recent recorded steps across all lanes, newest first.

    The history a reader wants is "what happened last", which is neither lane's own
    ordering: a slate step and a batch step interleave in real time and must interleave
    here too.
    """

    if limit <= 0:
        raise ValueError(f"limit must be positive, not {limit}")
    rows = connection.execute(
        """
        SELECT * FROM ops_runs
        ORDER BY started_at DESC, ops_run_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return tuple(_recorded(row) for row in rows)


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
