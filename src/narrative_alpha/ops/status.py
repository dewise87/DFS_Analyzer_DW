"""One screen that answers "did this week run, and what do I need to do by hand".

Everything here is a read. The screen never fixes anything, and it never hides a number
behind a healthy-looking summary: a missing roster is a warning in words, not a zero.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from narrative_alpha.identity.crosswalk import PlayerCrosswalk
from narrative_alpha.ingest.slates import SlateSummary, list_slates
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.narrative import (
    ExtractionError,
    list_inflight_extractions,
    list_pending_review_flags,
    load_batch_pricing,
    plan_extraction,
)
from narrative_alpha.narrative.anthropic_provider import DEFAULT_MODEL_ID
from narrative_alpha.narrative.extraction import DEFAULT_PRICING_PATH
from narrative_alpha.ops.config import NANOS_PER_USD, OpsConfig
from narrative_alpha.ops.runs import (
    BATCH_STEPS,
    SLATE_STEPS,
    OpsStep,
    last_run,
    last_run_any_status,
)
from narrative_alpha.ops.spend import month_start_utc, month_to_date_spend_nanos
from narrative_alpha.snapshots import MANIFEST_FILENAME, load_manifest
from narrative_alpha.snapshots.core import collect_status, snapshot_week_path
from narrative_alpha.snapshots.models import CaptureKind

RECEIPT_DIRECTORY_SUFFIX = ".stage1-receipts"
COLLECTION_WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class StepHistory:
    """Last success and last failure for one batch step."""

    step: OpsStep
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_failure_text: str | None

    def age(self, kind: str, *, as_of: datetime) -> timedelta | None:
        stamp = self.last_success_at if kind == "success" else self.last_failure_at
        return None if stamp is None else as_of - stamp


@dataclass(frozen=True)
class SnapshotWeekStatus:
    season: int
    week: int
    captured: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SlateCaptureStatus:
    """How much of one capture kind for the week has actually reached the store."""

    kind: str
    files_captured: int
    files_ingested: int


@dataclass(frozen=True)
class SlateStatus:
    """One ingested slate and the state of the decision built against it."""

    slate_id: int
    external_slate_id: str
    site: str
    slate_type: str
    name: str
    locks_at: datetime
    player_count: int
    unresolved_count: int
    latest_salary_at: datetime | None
    latest_projection_at: datetime | None
    latest_ownership_at: datetime | None
    feature_rows_at_decision: int
    decision_snapshot_id: str | None
    decision_at: datetime | None


@dataclass(frozen=True)
class SlateLaneStatus:
    """The slate lane's week: what is captured, what is ingested, what was decided."""

    season: int
    week: int
    captures: tuple[SlateCaptureStatus, ...]
    slates: tuple[SlateStatus, ...]
    decision_at: datetime | None
    episode_rows_at_decision: int


@dataclass(frozen=True)
class OpsStatus:
    """The whole screen as data, so `--json` and the text render never diverge."""

    as_of: datetime
    database: Path
    config_path: Path
    steps: tuple[StepHistory, ...]
    slate_steps: tuple[StepHistory, ...]
    dead_feed_count: int | None
    dead_feed_source_ids: tuple[str, ...]
    last_collection_at: datetime | None
    items_collected_last_7_days: int
    extraction_backlog: int
    extraction_backlog_cost_usd: str | None
    extraction_backlog_note: str | None
    pending_review_flags: int
    inflight_attempts: int
    pending_accepted_receipts: int
    unresolved_identities: int
    player_rows: int
    snapshot_week: SnapshotWeekStatus | None
    snapshot_problems: tuple[str, ...]
    slate: SlateLaneStatus | None
    month_to_date_spend_usd: str
    monthly_budget_usd: str
    budget_remaining_usd: str
    warnings: tuple[str, ...]

    @property
    def manual_actions(self) -> tuple[str, ...]:
        """What the operator must do by hand before the next lock."""

        actions: list[str] = []
        if self.player_rows == 0:
            actions.append(
                "seed the nflverse roster — no canonical player exists, so nothing can "
                "resolve identities"
            )
        if self.unresolved_identities:
            actions.append(
                f"clear {self.unresolved_identities} unresolved identity/identities: "
                "`na-crosswalk resolve`"
            )
        if self.pending_review_flags:
            actions.append(
                f"review {self.pending_review_flags} flagged item(s): `na-extract review`"
            )
        if self.inflight_attempts:
            actions.append(
                f"{self.inflight_attempts} extraction attempt(s) are in flight: rerun the "
                "batch to resume, or `na-extract abandon` a stuck one"
            )
        for step in (*self.steps, *self.slate_steps):
            if step.last_failure_at is not None and (
                step.last_success_at is None or step.last_failure_at > step.last_success_at
            ):
                actions.append(
                    f"the {step.step} step last failed: {_brief(step.last_failure_text)}"
                )
        if self.snapshot_week is None:
            actions.append(
                "capture this week's pre-lock snapshots — no snapshot week is initialized"
            )
        return tuple(actions)


def collect_ops_status(
    connection: sqlite3.Connection,
    *,
    config: OpsConfig,
    database: Path,
    now: datetime | None = None,
    pricing_path: Path = DEFAULT_PRICING_PATH,
) -> OpsStatus:
    """Gather the whole screen. Every section degrades to a stated gap, never a crash."""

    as_of = ensure_utc(now or datetime.now(UTC))
    warnings: list[str] = []

    steps = tuple(_step_history(connection, step=step) for step in BATCH_STEPS)
    slate_steps = tuple(_step_history(connection, step=step) for step in SLATE_STEPS)
    collect_run = last_run(connection, step="collect", status="succeeded")
    latest_collect = max(
        (
            run
            for run in (
                collect_run,
                last_run(connection, step="collect", status="failed"),
            )
            if run is not None
        ),
        key=lambda run: run.started_at,
        default=None,
    )
    dead_ids: tuple[str, ...] = ()
    dead_count: int | None = None
    if latest_collect is not None:
        raw_ids = latest_collect.summary.get("dead_source_ids")
        dead_ids = tuple(str(value) for value in raw_ids) if isinstance(raw_ids, list) else ()
        raw_count = latest_collect.summary.get("dead_sources")
        dead_count = int(raw_count) if isinstance(raw_count, int) else len(dead_ids)

    player_rows = _count(connection, "SELECT count(*) FROM players")
    if player_rows == 0:
        warnings.append(
            "ROSTER NOT SEEDED: the players table is empty. Identity resolution, claims, "
            "and lineup generation all fail closed until an nflverse roster is seeded."
        )

    backlog, backlog_cost, backlog_note = _extraction_backlog(
        connection, as_of=as_of, pricing_path=pricing_path
    )
    if backlog_note:
        warnings.append(backlog_note)

    receipts, receipt_note = _pending_receipts(database)
    if receipt_note:
        warnings.append(receipt_note)

    snapshot_week, snapshot_problems = _snapshot_status(config.snapshot_root)
    warnings.extend(snapshot_problems)

    slate, slate_problems = _slate_status(
        connection, snapshot_root=config.snapshot_root, week=snapshot_week
    )
    warnings.extend(slate_problems)

    month_start = month_start_utc(as_of, timezone=config.timezone)
    spent = month_to_date_spend_nanos(connection, since=month_start)
    budget = config.monthly_llm_budget_nanos
    if spent > budget:
        warnings.append(
            f"Stage 1 month-to-date spend ${_usd(spent)} is already over the "
            f"${_usd(budget)} budget; the batch lane will refuse to submit."
        )

    return OpsStatus(
        as_of=as_of,
        database=database,
        config_path=config.path,
        steps=steps,
        slate_steps=slate_steps,
        dead_feed_count=dead_count,
        dead_feed_source_ids=dead_ids,
        last_collection_at=None if latest_collect is None else latest_collect.started_at,
        items_collected_last_7_days=_count(
            connection,
            "SELECT count(*) FROM source_items WHERE rtrim(observed_at, 'Z') >= rtrim(?, 'Z')",
            (utc_timestamp(as_of - COLLECTION_WINDOW),),
        ),
        extraction_backlog=backlog,
        extraction_backlog_cost_usd=backlog_cost,
        extraction_backlog_note=backlog_note,
        pending_review_flags=len(list_pending_review_flags(connection)),
        inflight_attempts=len(list_inflight_extractions(connection)),
        pending_accepted_receipts=receipts,
        unresolved_identities=len(PlayerCrosswalk(connection).list_unresolved()),
        player_rows=player_rows,
        snapshot_week=snapshot_week,
        snapshot_problems=snapshot_problems,
        slate=slate,
        month_to_date_spend_usd=_usd(spent),
        monthly_budget_usd=_usd(budget),
        budget_remaining_usd=_usd(max(budget - spent, 0)),
        warnings=tuple(warnings),
    )


def _step_history(connection: sqlite3.Connection, *, step: OpsStep) -> StepHistory:
    success = last_run(connection, step=step, status="succeeded")
    failure = last_run(connection, step=step, status="failed")
    skipped = last_run(connection, step=step, status="skipped")
    # A skip is not a success and the operator must see why, so it reports as the latest
    # non-success alongside real failures.
    if failure is None or (skipped is not None and skipped.started_at > failure.started_at):
        failure = skipped
    return StepHistory(
        step=step,
        last_success_at=None if success is None else success.started_at,
        last_failure_at=None if failure is None else failure.started_at,
        last_failure_text=None if failure is None else failure.error_text,
    )


def _extraction_backlog(
    connection: sqlite3.Connection,
    *,
    as_of: datetime,
    pricing_path: Path,
) -> tuple[int, str | None, str | None]:
    """Items the extraction lane would submit today, priced at the worst case.

    This is the real plan, not a row count: it applies the same policy, retention, and
    injection gates the batch lane applies, so the number cannot flatter the operator.
    """

    earliest = connection.execute(
        "SELECT min(observed_at) FROM source_items WHERE raw_content IS NOT NULL"
    ).fetchone()[0]
    if not isinstance(earliest, str) or not earliest:
        return 0, None, None
    window_start = ensure_utc(datetime.fromisoformat(earliest.replace("Z", "+00:00")))
    if window_start >= as_of:
        return 0, None, None
    try:
        pricing = load_batch_pricing(pricing_path, model_id=DEFAULT_MODEL_ID)
        plan = plan_extraction(
            connection,
            window_start=window_start,
            window_end=as_of,
            pricing=pricing,
            planned_at=as_of,
        )
    except (ExtractionError, OSError, sqlite3.Error, ValueError) as error:
        return 0, None, f"extraction backlog is unknown: {type(error).__name__}: {error}"
    return len(plan.ready), _usd(plan.estimated_cost_nanos_usd), None


def _pending_receipts(database: Path) -> tuple[int, str | None]:
    directory = database.resolve().with_name(database.name + RECEIPT_DIRECTORY_SUFFIX)
    try:
        if not directory.is_dir():
            return 0, None
        return len(list(directory.glob("accepted-*.json"))), None
    except OSError as error:
        return 0, f"cannot read accepted-batch receipts in {directory}: {error}"


def _snapshot_status(
    snapshot_root: Path,
) -> tuple[SnapshotWeekStatus | None, tuple[str, ...]]:
    try:
        report = collect_status(snapshot_root)
    except (OSError, ValueError) as error:
        return None, (f"cannot read snapshots under {snapshot_root}: {error}",)
    if not report.weeks:
        return None, report.problems
    # "Current" is the newest initialized week; nothing else in the store knows the week.
    week = max(report.weeks, key=lambda status: (status.season, status.week))
    captured = tuple(
        (
            kind.value,
            "MISSING"
            if week.last_captured.get(kind) is None
            else utc_timestamp(week.last_captured[kind]),
        )
        for kind in CaptureKind
    )
    return (
        SnapshotWeekStatus(season=week.season, week=week.week, captured=captured),
        report.problems,
    )


# Where each capture kind lands, and the column that proves one file reached the store.
_INGEST_TARGETS: dict[CaptureKind, str] = {
    CaptureKind.SALARIES: "salaries",
    CaptureKind.PROJECTIONS: "projection_snapshots",
    CaptureKind.OWNERSHIP: "ownership_baselines",
}


def _slate_status(
    connection: sqlite3.Connection,
    *,
    snapshot_root: Path,
    week: SnapshotWeekStatus | None,
) -> tuple[SlateLaneStatus | None, tuple[str, ...]]:
    """The slate lane's week. Reads only; a gap is stated, never inferred away."""

    if week is None:
        return None, ()
    try:
        captures = _capture_ingest_status(connection, snapshot_root, week.season, week.week)
    except (OSError, ValueError) as error:
        return None, (
            f"cannot read this week's captures under {snapshot_root}: {error}",
        )

    decision_at = _latest_decision_instant(connection, season=week.season, week=week.week)
    episode_rows = (
        0
        if decision_at is None
        else _count(
            connection,
            "SELECT count(*) FROM narrative_episodes WHERE as_of = ?",
            (utc_timestamp(decision_at),),
        )
    )
    slates = tuple(
        _slate_row(connection, summary, decision_at=decision_at)
        for summary in list_slates(connection, season=week.season, week=week.week)
    )
    return (
        SlateLaneStatus(
            season=week.season,
            week=week.week,
            captures=captures,
            slates=slates,
            decision_at=decision_at,
            episode_rows_at_decision=episode_rows,
        ),
        (),
    )


def _slate_row(
    connection: sqlite3.Connection,
    summary: SlateSummary,
    *,
    decision_at: datetime | None,
) -> SlateStatus:
    decision = connection.execute(
        """
        SELECT decision_snapshot_id, decision_at FROM decision_snapshots
        WHERE slate_id = ?
        ORDER BY rtrim(decision_at, 'Z') DESC, decision_snapshot_id DESC
        LIMIT 1
        """,
        (summary.slate_id,),
    ).fetchone()
    features = (
        0
        if decision_at is None
        else _count(
            connection,
            "SELECT count(*) FROM narrative_features "
            "WHERE slate_id = ? AND site = ? AND as_of = ?",
            (summary.slate_id, summary.site, utc_timestamp(decision_at)),
        )
    )
    decided_at = (
        None
        if decision is None
        else _parse_stamp(str(decision["decision_at"]))
    )
    return SlateStatus(
        slate_id=summary.slate_id,
        external_slate_id=summary.external_slate_id,
        site=summary.site,
        slate_type=summary.slate_type,
        name=summary.name,
        locks_at=summary.locks_at,
        player_count=summary.player_count,
        unresolved_count=summary.unresolved_count,
        latest_salary_at=summary.latest_salary_at,
        latest_projection_at=summary.latest_projection_at,
        latest_ownership_at=summary.latest_ownership_at,
        feature_rows_at_decision=features,
        decision_snapshot_id=None if decision is None else str(decision["decision_snapshot_id"]),
        decision_at=decided_at,
    )


def _capture_ingest_status(
    connection: sqlite3.Connection,
    snapshot_root: Path,
    season: int,
    week: int,
) -> tuple[SlateCaptureStatus, ...]:
    """Count, per kind, the week's captured files and how many reached the store.

    "Ingested" is proved by the file's own sha256 appearing on a row, not by a lane
    having reported success: a step can succeed and still have loaded nothing.
    """

    captured: dict[CaptureKind, list[str]] = {kind: [] for kind in _INGEST_TARGETS}
    week_path = snapshot_week_path(snapshot_root, season, week)
    if week_path.is_dir():
        for capture_path in sorted(path for path in week_path.iterdir() if path.is_dir()):
            manifest_path = capture_path / MANIFEST_FILENAME
            if not manifest_path.is_file():
                continue
            for record in load_manifest(manifest_path).files:
                if record.kind in captured:
                    captured[record.kind].append(record.sha256)
    return tuple(
        SlateCaptureStatus(
            kind=kind.value,
            files_captured=len(hashes),
            files_ingested=sum(
                1
                for file_sha256 in hashes
                if connection.execute(
                    f"SELECT 1 FROM {_INGEST_TARGETS[kind]} WHERE source_file_sha256 = ? LIMIT 1",
                    (file_sha256,),
                ).fetchone()
                is not None
            ),
        )
        for kind, hashes in captured.items()
    )


def _latest_decision_instant(
    connection: sqlite3.Connection,
    *,
    season: int,
    week: int,
) -> datetime | None:
    """The newest cutoff the slate lane worked at, whether or not it froze a decision."""

    candidates: list[datetime] = []
    for step in SLATE_STEPS:
        recorded = last_run_any_status(connection, step=step)
        if recorded is None:
            continue
        if recorded.summary.get("season") != season or recorded.summary.get("week") != week:
            continue
        stored = recorded.summary.get("decision_at")
        if isinstance(stored, str):
            candidates.append(_parse_stamp(stored))
    frozen = connection.execute(
        """
        SELECT max(rtrim(d.decision_at, 'Z')) FROM decision_snapshots AS d
        JOIN slates AS s ON s.slate_id = d.slate_id
        WHERE s.season = ? AND s.week = ?
        """,
        (season, week),
    ).fetchone()[0]
    if isinstance(frozen, str) and frozen:
        candidates.append(_parse_stamp(f"{frozen}Z"))
    return max(candidates, default=None)


def _parse_stamp(value: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _count(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> int:
    return int(connection.execute(sql, parameters).fetchone()[0])


def _usd(nanos: int) -> str:
    return f"{Decimal(nanos) / Decimal(NANOS_PER_USD):.2f}"


def render_status(status: OpsStatus) -> str:
    """Render the one screen. Plain text, fixed columns, no colour, no truncation."""

    lines = [
        "NARRATIVE ALPHA — OPERATOR STATUS",
        f"  as of      {utc_timestamp(status.as_of)}",
        f"  database   {status.database}",
        f"  config     {status.config_path}",
        "",
        "BATCH LANE (`na-ops batch`)",
    ]
    for step in status.steps:
        lines.append(
            f"  {step.step:<16} last success {_stamp(step.last_success_at, status.as_of)}"
        )
        if step.last_failure_at is not None:
            lines.append(
                f"  {'':<16} last failure {_stamp(step.last_failure_at, status.as_of)}"
            )
            lines.append(f"  {'':<16}   {step.last_failure_text}")

    dead = "unknown (no collection recorded)" if status.dead_feed_count is None else (
        f"{status.dead_feed_count}"
        + (
            ""
            if not status.dead_feed_source_ids
            else " — " + ", ".join(status.dead_feed_source_ids[:8])
            + ("" if len(status.dead_feed_source_ids) <= 8 else ", …")
        )
    )
    lines.extend(
        (
            "",
            "COLLECTION",
            f"  dead feeds in last run   {dead}",
            f"  items collected (7 days) {status.items_collected_last_7_days}",
            "",
            "STAGE 1 EXTRACTION",
            f"  backlog (eligible now)   {status.extraction_backlog}"
            + (
                ""
                if status.extraction_backlog_cost_usd is None
                else f"  (worst-case ${status.extraction_backlog_cost_usd} to clear)"
            ),
            f"  items awaiting review    {status.pending_review_flags}",
            f"  attempts in flight       {status.inflight_attempts}",
            f"  accepted-batch receipts  {status.pending_accepted_receipts}",
            f"  spend month-to-date      ${status.month_to_date_spend_usd} of "
            f"${status.monthly_budget_usd} (${status.budget_remaining_usd} left)",
            "",
            "IDENTITY",
            "  canonical players        "
            + (
                "NOT SEEDED — no roster has been loaded"
                if status.player_rows == 0
                else str(status.player_rows)
            ),
            f"  unresolved queue         {status.unresolved_identities}",
            "",
            "SNAPSHOTS",
        )
    )
    if status.snapshot_week is None:
        lines.append("  no snapshot week is initialized")
    else:
        week = status.snapshot_week
        lines.append(f"  {week.season} week {week.week:02d}")
        lines.extend(f"    {kind:<14} {stamp}" for kind, stamp in week.captured)

    lines.extend(("", "SLATE LANE (`na-ops slate`)"))
    for step in status.slate_steps:
        lines.append(
            f"  {step.step:<18} last success {_stamp(step.last_success_at, status.as_of)}"
        )
        if step.last_failure_at is not None:
            lines.append(
                f"  {'':<18} last failure {_stamp(step.last_failure_at, status.as_of)}"
            )
            lines.append(f"  {'':<18}   {step.last_failure_text}")
    lines.extend(_render_slate_week(status))

    if status.warnings:
        lines.extend(("", "WARNINGS"))
        lines.extend(f"  ! {warning}" for warning in status.warnings)

    lines.extend(("", "DO BY HAND"))
    actions = status.manual_actions
    if not actions:
        lines.append("  nothing — the lane is current")
    else:
        lines.extend(f"  - {action}" for action in actions)
    lines.append("")
    return "\n".join(lines)


def _render_slate_week(status: OpsStatus) -> list[str]:
    """The slate half of the screen: captures in, decision out, nothing summarized away."""

    slate = status.slate
    if slate is None:
        return ["", "  no snapshot week is initialized, so no slate week can be shown"]

    lines = ["", f"  {slate.season} week {slate.week:02d}"]
    for capture in slate.captures:
        state = (
            "none captured"
            if capture.files_captured == 0
            else f"{capture.files_ingested} of {capture.files_captured} file(s) ingested"
        )
        lines.append(f"    {capture.kind:<14} {state}")
    if slate.decision_at is None:
        lines.append("    decision       never — `na-ops slate` has not run for this week")
    else:
        lines.append(f"    decision at    {utc_timestamp(slate.decision_at)}")
        lines.append(f"    episodes       {slate.episode_rows_at_decision} at that instant")
    if not slate.slates:
        lines.append("    slates         none ingested — run `na-ops slate`")
        return lines
    for row in slate.slates:
        lines.append(
            f"    slate {row.slate_id}  {row.site} {row.slate_type}  "
            f"locks {utc_timestamp(row.locks_at)}  players {row.player_count}  "
            f"unresolved {row.unresolved_count}"
        )
        lines.append(
            f"      observed  salaries {_slot(row.latest_salary_at)}  "
            f"projections {_slot(row.latest_projection_at)}  "
            f"ownership {_slot(row.latest_ownership_at)}"
        )
        lines.append(
            f"      features  {row.feature_rows_at_decision} at the decision instant"
        )
        # The id and its cutoff come from one row, so either both are present or neither.
        decision = (
            "none frozen"
            if row.decision_snapshot_id is None or row.decision_at is None
            else f"{row.decision_snapshot_id} at {utc_timestamp(row.decision_at)}"
        )
        lines.append(f"      decision  {decision}")
    return lines


def _slot(value: datetime | None) -> str:
    return "MISSING" if value is None else utc_timestamp(value)


def status_payload(status: OpsStatus) -> dict[str, object]:
    """The same screen as JSON, for a dashboard or a scripted check."""

    return {
        "as_of": utc_timestamp(status.as_of),
        "config_path": str(status.config_path),
        "database": str(status.database),
        "steps": [
            {
                "step": step.step,
                "last_success_at": _optional_stamp(step.last_success_at),
                "last_success_age_seconds": _age_seconds(step.last_success_at, status.as_of),
                "last_failure_at": _optional_stamp(step.last_failure_at),
                "last_failure_age_seconds": _age_seconds(step.last_failure_at, status.as_of),
                "last_failure_text": step.last_failure_text,
            }
            for step in status.steps
        ],
        "slate_steps": [
            {
                "step": step.step,
                "last_success_at": _optional_stamp(step.last_success_at),
                "last_success_age_seconds": _age_seconds(step.last_success_at, status.as_of),
                "last_failure_at": _optional_stamp(step.last_failure_at),
                "last_failure_age_seconds": _age_seconds(step.last_failure_at, status.as_of),
                "last_failure_text": step.last_failure_text,
            }
            for step in status.slate_steps
        ],
        "slate": None
        if status.slate is None
        else {
            "season": status.slate.season,
            "week": status.slate.week,
            "decision_at": _optional_stamp(status.slate.decision_at),
            "episode_rows_at_decision": status.slate.episode_rows_at_decision,
            "captures": [
                {
                    "kind": capture.kind,
                    "files_captured": capture.files_captured,
                    "files_ingested": capture.files_ingested,
                }
                for capture in status.slate.captures
            ],
            "slates": [
                {
                    "slate_id": row.slate_id,
                    "external_slate_id": row.external_slate_id,
                    "site": row.site,
                    "slate_type": row.slate_type,
                    "name": row.name,
                    "locks_at": utc_timestamp(row.locks_at),
                    "player_count": row.player_count,
                    "unresolved_count": row.unresolved_count,
                    "latest_salary_at": _optional_stamp(row.latest_salary_at),
                    "latest_projection_at": _optional_stamp(row.latest_projection_at),
                    "latest_ownership_at": _optional_stamp(row.latest_ownership_at),
                    "feature_rows_at_decision": row.feature_rows_at_decision,
                    "decision_snapshot_id": row.decision_snapshot_id,
                    "decision_at": _optional_stamp(row.decision_at),
                }
                for row in status.slate.slates
            ],
        },
        "collection": {
            "dead_feed_count": status.dead_feed_count,
            "dead_feed_source_ids": list(status.dead_feed_source_ids),
            "last_collection_at": _optional_stamp(status.last_collection_at),
            "items_collected_last_7_days": status.items_collected_last_7_days,
        },
        "extraction": {
            "backlog": status.extraction_backlog,
            "backlog_worst_case_cost_usd": status.extraction_backlog_cost_usd,
            "backlog_note": status.extraction_backlog_note,
            "pending_review_flags": status.pending_review_flags,
            "inflight_attempts": status.inflight_attempts,
            "pending_accepted_receipts": status.pending_accepted_receipts,
            "month_to_date_spend_usd": status.month_to_date_spend_usd,
            "monthly_budget_usd": status.monthly_budget_usd,
            "budget_remaining_usd": status.budget_remaining_usd,
        },
        "identity": {
            "player_rows": status.player_rows,
            "roster_seeded": status.player_rows > 0,
            "unresolved_identities": status.unresolved_identities,
        },
        "snapshots": {
            "week": None
            if status.snapshot_week is None
            else {
                "season": status.snapshot_week.season,
                "week": status.snapshot_week.week,
                "captured": dict(status.snapshot_week.captured),
            },
            "problems": list(status.snapshot_problems),
        },
        "warnings": list(status.warnings),
        "manual_actions": list(status.manual_actions),
    }


def _brief(text: str | None, limit: int = 140) -> str:
    """One line for the action list; the step block above still carries the full text."""

    if not text:
        return "no reason recorded"
    first = text.strip().splitlines()[0]
    return first if len(first) <= limit else f"{first[: limit - 1].rstrip()}…"


def _stamp(value: datetime | None, as_of: datetime) -> str:
    if value is None:
        return "never"
    return f"{utc_timestamp(value)} ({_humanize(as_of - value)} ago)"


def _optional_stamp(value: datetime | None) -> str | None:
    return None if value is None else utc_timestamp(value)


def _age_seconds(value: datetime | None, as_of: datetime) -> int | None:
    return None if value is None else int((as_of - value).total_seconds())


def _humanize(age: timedelta) -> str:
    seconds = int(age.total_seconds())
    if seconds < 0:
        return "0m"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"
