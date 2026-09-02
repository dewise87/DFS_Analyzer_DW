"""The Wed-Fri batch lane: collect, purge, extract, and check the roster refresh.

Every step is isolated. A step that fails is recorded and the next safe step still runs,
so one dead feed or one expired credential never costs the week its purge or its history.
Nothing here retries silently: a failure is a recorded outcome an operator reads back from
`na-ops status`.

This module owns orchestration and the budget guard only. Collection, purge, extraction,
and the roster refresh are the existing library functions, called directly.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import anthropic
import httpx

from narrative_alpha.identity.nflverse import NflverseRosterError, refresh_roster_release
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.narrative import (
    BatchPricing,
    CollectionError,
    CollectionRunReport,
    ExtractionError,
    ExtractionPlan,
    ExtractionReport,
    PurgeReport,
    collect_enabled_sources,
    load_batch_pricing,
    plan_extraction,
    purge_expired_content,
    run_extraction_batch,
)
from narrative_alpha.narrative.anthropic_provider import (
    DEFAULT_BATCH_TIMEOUT_SECONDS,
    DEFAULT_MODEL_ID,
    AnthropicBatchProvider,
)
from narrative_alpha.ops.config import NANOS_PER_USD, OpsConfig
from narrative_alpha.ops.runs import OpsStep, OpsStepStatus, last_run, record_ops_run
from narrative_alpha.ops.spend import month_start_utc, month_to_date_spend_nanos
from narrative_alpha.store import MigrationError, StoreConfigurationError

# The window a first run covers when no successful extraction has ever been recorded and
# the store holds no items: one collection cadence, so a fresh install submits nothing.
DEFAULT_FIRST_WINDOW = timedelta(days=7)

CollectStep = Callable[..., CollectionRunReport]
PurgeStep = Callable[..., PurgeReport]
PlanStep = Callable[..., ExtractionPlan]
ExtractStep = Callable[..., ExtractionReport]
RefreshStep = Callable[..., object]


@dataclass(frozen=True)
class BatchDependencies:
    """The library calls the lane makes, injectable so tests need no network.

    Defaults are the production functions; nothing here re-implements them.
    """

    collect: CollectStep = collect_enabled_sources
    purge: PurgeStep = purge_expired_content
    plan_extraction: PlanStep = plan_extraction
    run_extraction: ExtractStep = run_extraction_batch
    refresh_roster: RefreshStep = refresh_roster_release
    load_pricing: Callable[..., BatchPricing] = load_batch_pricing
    provider_factory: Callable[[], object] | None = None


DEFAULT_DEPENDENCIES = BatchDependencies()

# Errors a step may raise that describe a bad week, not a broken program.
_STEP_ERRORS = (
    anthropic.AnthropicError,
    CollectionError,
    ExtractionError,
    MigrationError,
    NflverseRosterError,
    OSError,
    StoreConfigurationError,
    ValueError,
    httpx.HTTPError,
    sqlite3.Error,
)


@dataclass(frozen=True)
class StepOutcome:
    """One recorded step of the lane."""

    step: OpsStep
    status: OpsStepStatus
    started_at: datetime
    finished_at: datetime
    summary: dict[str, object]
    error_text: str | None = None

    @property
    def ok(self) -> bool:
        return self.status != "failed"


@dataclass(frozen=True)
class BatchReport:
    """Everything one `na-ops batch` invocation did."""

    batch_run_id: str
    started_at: datetime
    finished_at: datetime
    steps: tuple[StepOutcome, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)

    def step(self, name: OpsStep) -> StepOutcome | None:
        return next((outcome for outcome in self.steps if outcome.step == name), None)


def run_batch(
    connection: sqlite3.Connection,
    *,
    config: OpsConfig,
    window_start: datetime | None = None,
    now: datetime | None = None,
    max_items: int | None = None,
    dependencies: BatchDependencies = DEFAULT_DEPENDENCIES,
    pricing_path: Path | None = None,
    clock: Callable[[], datetime] | None = None,
) -> BatchReport:
    """Run the batch lane in order, isolating each step, and record every outcome.

    ``now`` pins the whole lane to one instant (tests, replays). Otherwise the extraction
    window closes at a fresh reading of ``clock`` taken *after* collection, so the items
    this very run just collected fall inside the window instead of waiting a cadence.
    """

    started_at = ensure_utc(now or datetime.now(UTC))
    if clock is None:
        clock = (lambda: started_at) if now is not None else (lambda: datetime.now(UTC))
    batch_run_id = f"ops-{uuid4().hex}"
    recorder = _Recorder(connection, batch_run_id=batch_run_id)

    collection = recorder.run(
        "collect",
        lambda: _collect(dependencies, connection, observed_at=started_at),
    )
    # Purge always runs: retention is a legal obligation, not a consequence of a good fetch.
    recorder.run("purge", lambda: _purge(dependencies, connection, as_of=started_at))

    # A step that raised outright collected nothing too, and records an empty summary.
    collected_sources = collection.summary.get("collected_sources", 0)
    collected_nothing = collection.status == "failed" and not collected_sources
    if collected_nothing:
        recorder.skip(
            "extract",
            "collection failed entirely (no source was collected), so the window has no "
            "new input to extract; fix collection and rerun `na-ops batch`",
        )
    else:
        recorder.run(
            "extract",
            lambda: _extract(
                dependencies,
                connection,
                config=config,
                window_start=window_start,
                started_at=started_at,
                window_end=ensure_utc(max(started_at, clock())),
                max_items=max_items,
                pricing_path=pricing_path,
            ),
        )

    recorder.run(
        "nflverse_refresh",
        lambda: _nflverse_refresh(dependencies, config=config, now=started_at),
    )

    return BatchReport(
        batch_run_id=batch_run_id,
        started_at=started_at,
        finished_at=ensure_utc(datetime.now(UTC)),
        steps=tuple(recorder.outcomes),
    )


class _StepFailure(Exception):
    """A step decided it failed for a reason it can state; not an unexpected error."""

    def __init__(self, message: str, summary: dict[str, object]) -> None:
        super().__init__(message)
        self.summary = summary


class _Recorder:
    """Runs each step, isolates its failure, and appends its `ops_runs` row."""

    def __init__(self, connection: sqlite3.Connection, *, batch_run_id: str) -> None:
        self._connection = connection
        self._batch_run_id = batch_run_id
        self.outcomes: list[StepOutcome] = []

    def run(
        self,
        step: OpsStep,
        action: Callable[[], tuple[OpsStepStatus, dict[str, object], str | None]],
    ) -> StepOutcome:
        started_at = ensure_utc(datetime.now(UTC))
        try:
            status, summary, error_text = action()
        except _StepFailure as failure:
            status, summary, error_text = "failed", failure.summary, str(failure)
        except _STEP_ERRORS as error:
            status, summary, error_text = "failed", {}, f"{type(error).__name__}: {error}"
        return self._record(step, status, started_at, summary, error_text)

    def skip(self, step: OpsStep, reason: str) -> StepOutcome:
        started_at = ensure_utc(datetime.now(UTC))
        return self._record(step, "skipped", started_at, {}, reason)

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
            summary=summary,
            error_text=error_text,
        )
        # History is the whole point of the lane; commit it on its own so a later step's
        # rollback can never take an earlier step's record with it.
        try:
            record_ops_run(
                self._connection,
                batch_run_id=self._batch_run_id,
                step=step,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                summary=summary,
                error_text=error_text,
            )
            self._connection.commit()
        except (sqlite3.Error, ValueError):
            self._connection.rollback()
            raise
        self.outcomes.append(outcome)
        return outcome


def _collect(
    dependencies: BatchDependencies,
    connection: sqlite3.Connection,
    *,
    observed_at: datetime,
) -> tuple[OpsStepStatus, dict[str, object], str | None]:
    run = dependencies.collect(connection, observed_at=observed_at)
    summary: dict[str, object] = {
        "attempted_sources": len(run.attempted_source_ids),
        "collected_sources": len(run.reports),
        "dead_sources": len(run.errors),
        "dead_source_ids": [error.source_id for error in run.errors],
        "fetched_items": run.fetched_items,
        "inserted_items": run.inserted_items,
        "observed_at": utc_timestamp(run.observed_at),
    }
    if not run.attempted_source_ids:
        raise _StepFailure(
            "no source is enabled: `na-collect seed` has never run against this database, "
            "or every source version is disabled",
            summary,
        )
    if run.errors:
        detail = "; ".join(f"{error.source_id}: {error.message}" for error in run.errors[:5])
        more = "" if len(run.errors) <= 5 else f" (+{len(run.errors) - 5} more)"
        raise _StepFailure(
            f"{len(run.errors)} of {len(run.attempted_source_ids)} sources failed — {detail}{more}",
            summary,
        )
    return "succeeded", summary, None


def _purge(
    dependencies: BatchDependencies,
    connection: sqlite3.Connection,
    *,
    as_of: datetime,
) -> tuple[OpsStepStatus, dict[str, object], str | None]:
    report = dependencies.purge(connection, as_of=as_of)
    connection.commit()
    return (
        "succeeded",
        {
            "as_of": utc_timestamp(report.as_of),
            "tombstones_written": report.tombstones_written,
            "source_items_purged": len(report.source_items_purged),
        },
        None,
    )


def _extract(
    dependencies: BatchDependencies,
    connection: sqlite3.Connection,
    *,
    config: OpsConfig,
    window_start: datetime | None,
    started_at: datetime,
    window_end: datetime,
    max_items: int | None,
    pricing_path: Path | None,
) -> tuple[OpsStepStatus, dict[str, object], str | None]:
    now = window_end
    start = window_start or extraction_window_start(connection, now=now)
    window: dict[str, object] = {
        "window_start": utc_timestamp(start),
        "window_end": utc_timestamp(now),
    }
    if start >= now:
        return (
            "skipped",
            window,
            "the last successful extraction already covers everything up to now",
        )
    players = int(connection.execute("SELECT count(*) FROM players").fetchone()[0])
    if players == 0:
        # Extracting before a roster exists would send every name to the unresolved queue,
        # each one a by-hand `na-crosswalk resolve`; seeding afterwards does not resolve them
        # retroactively. The watermark stays put, so nothing is stepped over.
        return (
            "skipped",
            window,
            "roster not seeded: the players table is empty, so every extracted name would "
            "queue for manual resolution; seed the nflverse roster first, then rerun "
            "`na-ops batch`",
        )
    pricing = dependencies.load_pricing(
        pricing_path or Path("config/model_pricing.toml"), model_id=DEFAULT_MODEL_ID
    )
    plan = dependencies.plan_extraction(
        connection,
        window_start=start,
        window_end=now,
        pricing=pricing,
        planned_at=now,
        max_items=max_items,
    )
    summary: dict[str, object] = {
        **window,
        "ready_items": len(plan.ready),
        "resumable_items": len(plan.resumable),
        "ineligible_items": len(plan.ineligible),
        "estimated_cost_usd": _usd(plan.estimated_cost_nanos_usd),
    }

    guard = _budget_guard(connection, config=config, plan=plan, now=now)
    summary |= guard.summary
    if guard.refused:
        raise _StepFailure(guard.message or "budget guard refused the batch", summary)

    if not (plan.ready or plan.resumable or plan.submission_unknown or plan.injection_blocked):
        return "succeeded", summary | {"submitted_items": 0, "claims_stored": 0}, None

    if dependencies.provider_factory is None and not (
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    ):
        # Same refusal as `na-extract`, before any item is reserved: a missing credential
        # must never leave reservations behind.
        raise _StepFailure(
            "ANTHROPIC_API_KEY is not set for this process; a scheduled run reads it from "
            "the macOS Keychain through the wrapper `na-ops schedule install` writes",
            summary,
        )

    factory = dependencies.provider_factory or (
        lambda: AnthropicBatchProvider(timeout_seconds=DEFAULT_BATCH_TIMEOUT_SECONDS)
    )
    report = dependencies.run_extraction(
        connection,
        window_start=start,
        window_end=now,
        provider=factory(),
        pricing=pricing,
        run_at=started_at,
        max_items=max_items,
    )
    summary |= {
        "run_id": report.run_id,
        "selected_items": report.selected_items,
        "submitted_items": report.submitted_items,
        "succeeded_items": report.succeeded_items,
        "claims_stored": report.claims_stored,
        "flagged_items": len(report.flagged_item_ids),
        "item_errors": len(report.errors),
        "pending": report.pending,
    }
    if report.pending:
        raise _StepFailure(
            "the provider batch is still processing; rerun `na-ops batch` and it resumes "
            "the accepted batch without re-billing",
            summary,
        )
    if not report.ok:
        detail = "; ".join(f"item {error.source_item_id}: {error.code}" for error in report.errors)
        raise _StepFailure(f"extraction reported item failures — {detail}", summary)
    return "succeeded", summary, None


@dataclass(frozen=True)
class _BudgetDecision:
    refused: bool
    summary: dict[str, object]
    message: str | None


def _budget_guard(
    connection: sqlite3.Connection,
    *,
    config: OpsConfig,
    plan: ExtractionPlan,
    now: datetime,
) -> _BudgetDecision:
    """Refuse the whole batch when it could take the month past budget.

    The plan's estimate is the worst case (input plus the full output ceiling for every
    item), and the guard is all-or-nothing on purpose: submitting "what fits" would make
    the covered window a function of the budget, which no later replay could reconstruct.
    """

    budget_nanos = config.monthly_llm_budget_nanos
    month_start = month_start_utc(now, timezone=config.timezone)
    spent = month_to_date_spend_nanos(connection, since=month_start)
    estimate = plan.estimated_cost_nanos_usd if plan.ready else 0
    summary: dict[str, object] = {
        "budget_month_start": utc_timestamp(month_start),
        "budget_usd": _usd(budget_nanos),
        "month_to_date_spend_usd": _usd(spent),
        "estimated_batch_cost_usd": _usd(estimate),
    }
    if estimate and spent + estimate > budget_nanos:
        return _BudgetDecision(
            refused=True,
            summary=summary,
            message=(
                f"monthly LLM budget guard refused the batch: month-to-date "
                f"${_cents(spent)} plus a worst-case ${_cents(estimate)} for "
                f"{len(plan.ready)} item(s) exceeds the ${_cents(budget_nanos)} budget in "
                f"{config.path}. Raise monthly_llm_budget_usd or narrow the window with "
                f"`na-ops batch --max-items N`; nothing was submitted"
            ),
        )
    return _BudgetDecision(refused=False, summary=summary, message=None)


def extraction_window_start(connection: sqlite3.Connection, *, now: datetime) -> datetime:
    """Where the next extraction window begins.

    The watermark is the end of the last *successful* extraction step. A failed or skipped
    run never advances it, so a bad week is retried rather than silently stepped over.
    """

    recorded = last_run(connection, step="extract", status="succeeded")
    if recorded is not None:
        stored = recorded.summary.get("window_end")
        if isinstance(stored, str):
            return ensure_utc(datetime.fromisoformat(stored.replace("Z", "+00:00")))
    earliest = connection.execute(
        "SELECT min(observed_at) FROM source_items WHERE raw_content IS NOT NULL"
    ).fetchone()[0]
    if isinstance(earliest, str) and earliest:
        return ensure_utc(datetime.fromisoformat(earliest.replace("Z", "+00:00")))
    return ensure_utc(now) - DEFAULT_FIRST_WINDOW


def _nflverse_refresh(
    dependencies: BatchDependencies,
    *,
    config: OpsConfig,
    now: datetime,
) -> tuple[OpsStepStatus, dict[str, object], str | None]:
    """Report-only: compare the rolling roster with the pin. It never changes the pin."""

    # UTC date: the helper rejects a review date later than today's UTC date, and a local
    # zone east of UTC can already be on tomorrow.
    reviewed_at: date = ensure_utc(now).date()
    try:
        report = dependencies.refresh_roster(
            config.season,
            config.nflverse_archive,
            reviewed_at=reviewed_at,
        )
    except NflverseRosterError as error:
        raise _StepFailure(
            f"nflverse refresh check failed — {error}. To re-pin: run "
            f"`na-crosswalk nflverse-refresh --season {config.season} --reviewed-at "
            f"{reviewed_at.isoformat()}` (add `--allow-missing-prior` if the old bytes are "
            "gone), review the output, and paste the entry into PINNED_ROSTER_RELEASES",
            {"report_only": True, "season": config.season, "reviewed_at": reviewed_at.isoformat()},
        ) from error
    added = getattr(report, "added", ())
    removed = getattr(report, "removed", ())
    changed = getattr(report, "changed", ())
    sha256 = getattr(report, "sha256", None)
    pin = getattr(report, "compared_with", None)
    matches_pin = bool(pin is not None and sha256 == pin.sha256)
    summary: dict[str, object] = {
        "report_only": True,
        "season": config.season,
        "reviewed_at": reviewed_at.isoformat(),
        "rolling_sha256": sha256,
        "matches_pin": matches_pin,
        "players_added": len(added),
        "players_removed": len(removed),
        "players_changed": len(changed),
        "prior_available": bool(getattr(report, "prior_available", True)),
    }
    if not matches_pin:
        # A moved roster is not an error, but it is a by-hand task the screen must show.
        raise _StepFailure(
            f"the rolling nflverse roster ({str(sha256)[:12]}…) no longer matches the newest "
            f"pin: +{len(added)} -{len(removed)} ~{len(changed)} players. Review with "
            f"`na-crosswalk nflverse-refresh --season {config.season} --reviewed-at "
            f"{reviewed_at.isoformat()}` and paste the new pin entry",
            summary,
        )
    return "succeeded", summary, None


def _usd(nanos: int) -> str:
    """Exact USD for a stored summary; the audit trail keeps every digit."""

    return str(Decimal(nanos) / Decimal(NANOS_PER_USD))


def _cents(nanos: int) -> str:
    """Rounded USD for a sentence a person reads."""

    return f"{Decimal(nanos) / Decimal(NANOS_PER_USD):.2f}"
