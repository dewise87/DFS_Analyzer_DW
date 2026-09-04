"""Read-only monthly operational review assembled from the SQLite store."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TypedDict
from zoneinfo import ZoneInfo

from narrative_alpha.grading.core import posterior_from_weights
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.ops.config import NANOS_PER_USD
from narrative_alpha.ops.runs import BATCH_STEPS, RESULTS_STEPS, SLATE_STEPS


class MonthlyReportError(ValueError):
    """Raised when a requested calendar month cannot be represented safely."""


@dataclass(frozen=True)
class MonthlyWindow:
    """The operator-local calendar month expressed as an exact UTC half-open interval."""

    month: str
    start: datetime
    end: datetime
    timezone: ZoneInfo

    @property
    def label(self) -> str:
        return f"[{utc_timestamp(self.start)}, {utc_timestamp(self.end)})"


class CredibilityCell(TypedDict):
    claim_type: str
    n: int
    mean: float
    low: float
    high: float
    mass: float


class SourceYieldRow(TypedDict):
    source_id: str
    collected: int
    retained: int
    extracted: int
    claims: int
    grades: int
    credibility: tuple[CredibilityCell, ...]


class Stage1Cost(TypedDict):
    attempts: int
    spent: int
    input_tokens: int
    output_tokens: int


class Stage1Evaluation(TypedDict):
    model_eval_id: str
    observed_at: str
    item_count: int
    label_row_count: int
    claim_f1: str


class PromptVersionRow(TypedDict):
    prompt_version_id: str
    model_id: str
    attempts: int
    evaluation: Stage1Evaluation | None


class OwnershipEvaluationRow(TypedDict):
    model_eval_id: str
    ownership_site: str
    ownership_archetype: str
    model_id: str
    item_count: int
    beat_baseline: bool
    metrics: tuple[tuple[str, str, str], ...]


def monthly_window(month: str, *, timezone: ZoneInfo) -> MonthlyWindow:
    """Parse ``YYYY-MM`` and return that local calendar month in UTC.

    The budget guard already defines a month in the operator's configured timezone.  The
    report deliberately uses the same boundary, including any daylight-saving transition,
    rather than treating a local budget as a UTC month.
    """

    if len(month) != 7 or month[4] != "-" or not month[:4].isdigit() or not month[5:].isdigit():
        raise MonthlyReportError("month must be YYYY-MM")
    year, number = int(month[:4]), int(month[5:])
    if not 1 <= year <= 9999 or not 1 <= number <= 12:
        raise MonthlyReportError("month must be YYYY-MM")
    try:
        start = datetime(year, number, 1, tzinfo=timezone)
        end = datetime(year + (number == 12), 1 if number == 12 else number + 1, 1, tzinfo=timezone)
    except ValueError as error:  # Defensive: datetime's year bound is part of the CLI contract.
        raise MonthlyReportError("month must be YYYY-MM") from error
    return MonthlyWindow(month, start.astimezone(UTC), end.astimezone(UTC), timezone)


def build_monthly_report(
    connection: sqlite3.Connection,
    *,
    window: MonthlyWindow,
    budget_nanos: int,
) -> str:
    """Render the required monthly review without changing any store row.

    Each event is selected by the timestamp at which that fact entered its own immutable
    table.  In particular, extraction cost uses ``source_item_extractions.ingested_at``
    just as the budget guard does, while grades use ``claim_grades.graded_at``.
    """

    start, end = utc_timestamp(window.start), utc_timestamp(window.end)
    source_rows = _source_yield(connection, start=start, end=end)
    cost = _stage1_cost(connection, start=start, end=end)
    prompts = _prompt_versions(connection, start=start, end=end)
    ownership = _ownership_evaluations(connection, start=start, end=end)
    signals = _signal_statuses(connection, start=start, end=end)
    failures = _lane_failures(connection, start=start, end=end)

    lines = [
        "NARRATIVE ALPHA — MONTHLY REVIEW",
        f"  month         {window.month} ({window.timezone.key})",
        f"  query window  {window.label}",
        "",
        "SOURCE YIELD",
        f"  query window  {window.label} (items/claims use ingested_at; grades use graded_at; "
        "credibility cells use as_of_at of the newest grading run in the window)",
    ]
    if not source_rows:
        lines.append("  none recorded")
    else:
        for source_row in source_rows:
            lines.append(f"  {source_row['source_id']}")
            lines.append(
                "    items collected="
                f"{source_row['collected']}  not yet purged={source_row['retained']}  "
                f"extracted={source_row['extracted']}  claims={source_row['claims']}  "
                f"grades={source_row['grades']}"
            )
            cells = source_row["credibility"]
            if not cells:
                lines.append("    pooled credibility cell: none recorded")
            else:
                for cell in cells:
                    lines.append(
                        "    pooled credibility cell "
                        f"{cell['claim_type']}: posterior={cell['mean']:.3f} "
                        f"n={cell['n']} {cell['mass']:.0%} interval "
                        f"[{cell['low']:.3f}, {cell['high']:.3f}]"
                    )

    retained = sum(int(row["retained"]) for row in source_rows)
    claims = sum(int(row["claims"]) for row in source_rows)
    spent = int(cost["spent"])
    lines.extend(
        (
            "",
            "STAGE 1 COST",
            f"  query window  {window.label} (source_item_extractions.ingested_at)",
            f"  spend         ${_usd(spent)} of ${_usd(budget_nanos)} budget",
            f"  tokens        input={cost['input_tokens']}  output={cost['output_tokens']}",
            f"  cost / retained item  {_cost_per(spent, retained)}",
            f"  cost / claim          {_cost_per(spent, claims)}",
        )
    )
    if cost["attempts"] == 0:
        lines.append("  none recorded")

    lines.extend(
        (
            "",
            "PROMPT VERSIONS AND STAGE 1 EVALUATIONS",
            f"  query window  {window.label} (prompt use); evaluations newest as of window end",
        )
    )
    if not prompts:
        lines.append("  none recorded")
    else:
        for prompt_row in prompts:
            lines.append(
                f"  {prompt_row['prompt_version_id']}  model={prompt_row['model_id']}  "
                f"attempts={prompt_row['attempts']}"
            )
            evaluation = prompt_row["evaluation"]
            if evaluation is None:
                lines.append("    newest Stage 1 evaluation: none recorded")
            else:
                lines.append(
                    f"    newest Stage 1 evaluation {evaluation['model_eval_id']} at "
                    f"{evaluation['observed_at']}  items={evaluation['item_count']} "
                    f"labels={evaluation['label_row_count']}  "
                    f"claim-presence f1={evaluation['claim_f1']}"
                )

    lines.extend(
        (
            "",
            "OWNERSHIP EVALUATIONS",
            f"  query window  {window.label} (model_evals.observed_at)",
        )
    )
    if not ownership:
        lines.append("  none recorded")
    else:
        for ownership_row in ownership:
            verdict = "YES" if ownership_row["beat_baseline"] else "NO"
            lines.append(
                f"  {ownership_row['model_eval_id']}  {ownership_row['ownership_site']} "
                f"{ownership_row['ownership_archetype']}  model={ownership_row['model_id']}"
            )
            lines.append(
                f"    verdict: model beat untouched vendor baseline = {verdict}  "
                f"rows={ownership_row['item_count']}"
            )
            for metric, model, baseline in ownership_row["metrics"]:
                lines.append(f"    {metric}: model={model}  vendor baseline={baseline}")

    lines.extend(
        (
            "",
            "SIGNAL STATUSES",
            f"  query window  {window.label} (ownership_scenarios.observed_at)",
        )
    )
    if not signals:
        lines.append("  none recorded")
    else:
        for signal in signals:
            lines.append(
                f"  run={signal['run_id']}  slate={signal['slate_id']}  site={signal['site']}  "
                f"archetype={signal['contest_archetype']}  "
                f"governance_status={signal['governance_status']}  "
                f"multiplier={signal['status_multiplier']:.2f}  signals={signal['signal_count']}"
            )

    lines.extend(
        (
            "",
            "LANE STEP FAILURES",
            f"  query window  {window.label} (ops_runs.started_at; failed steps only)",
        )
    )
    if not failures:
        lines.append("  none recorded")
    else:
        for failure in failures:
            reason = failure["error_text"] or "no reason recorded"
            lines.append(
                f"  {failure['lane']:<7} {failure['step']:<18} {failure['started_at']} "
                f"run={failure['batch_run_id']} — {reason}"
            )
    return "\n".join(lines) + "\n"


def _source_yield(
    connection: sqlite3.Connection, *, start: str, end: str
) -> list[SourceYieldRow]:
    """Return source-level counts and newest-in-window pooled ledger cells."""

    source_ids = [
        str(row[0])
        for row in connection.execute("SELECT source_id FROM source_keys ORDER BY source_id")
    ]
    collected = _counts(connection, "source_items", "source_id", "ingested_at", start, end)
    retained = _counts(
        connection,
        "source_items",
        "source_id",
        "ingested_at",
        start,
        end,
        extra="raw_content IS NOT NULL",
    )
    extracted = _counts(
        connection,
        "source_item_extractions AS extraction JOIN source_items AS item "
        "ON item.source_item_id = extraction.source_item_id",
        "item.source_id",
        "extraction.ingested_at",
        start,
        end,
        extra="extraction.status IN ('succeeded', 'flagged')",
    )
    claims = _counts(
        connection,
        "claims AS claim JOIN source_items AS item ON item.source_item_id = claim.source_item_id",
        "item.source_id",
        "claim.ingested_at",
        start,
        end,
    )
    grades = _counts(connection, "claim_grades", "source_id", "graded_at", start, end)
    credibility = _pooled_credibility(connection, start=start, end=end)
    rows: list[SourceYieldRow] = [
        {
            "source_id": source_id,
            "collected": collected.get(source_id, 0),
            "retained": retained.get(source_id, 0),
            "extracted": extracted.get(source_id, 0),
            "claims": claims.get(source_id, 0),
            "grades": grades.get(source_id, 0),
            "credibility": credibility.get(source_id, ()),
        }
        for source_id in source_ids
    ]
    # A source with nothing this month is a catalog entry, not a yield; listing every
    # retired source for ever is how a monthly report gets skipped.
    return [
        row
        for row in rows
        if row["collected"] or row["extracted"] or row["claims"] or row["grades"]
        or row["credibility"]
    ]


def _counts(
    connection: sqlite3.Connection,
    table: str,
    source_column: str,
    timestamp_column: str,
    start: str,
    end: str,
    *,
    extra: str | None = None,
) -> dict[str, int]:
    where = (
        f"rtrim({timestamp_column}, 'Z') >= rtrim(?, 'Z') "
        f"AND rtrim({timestamp_column}, 'Z') < rtrim(?, 'Z')"
    )
    if extra:
        where += f" AND {extra}"
    rows = connection.execute(
        f"SELECT {source_column} AS source_id, count(*) AS count FROM {table} "
        f"WHERE {where} GROUP BY {source_column}",
        (start, end),
    )
    return {str(row["source_id"]): int(row["count"]) for row in rows}


def _pooled_credibility(
    connection: sqlite3.Connection, *, start: str, end: str
) -> dict[str, tuple[CredibilityCell, ...]]:
    rows = connection.execute(
        """
        WITH newest_run AS (
            -- One grading run, so every pooled cell shares one reference instant and
            -- one prior; cells from different Tuesdays carry decay computed as of
            -- different instants and must not be summed.
            SELECT grading_run_id
            FROM source_credibility
            WHERE rtrim(as_of_at, 'Z') >= rtrim(?, 'Z') AND rtrim(as_of_at, 'Z') < rtrim(?, 'Z')
            ORDER BY rtrim(as_of_at, 'Z') DESC, grading_run_id DESC
            LIMIT 1
        )
        SELECT sc.* FROM source_credibility AS sc
        JOIN newest_run ON newest_run.grading_run_id = sc.grading_run_id
        ORDER BY sc.source_id, sc.claim_type, sc.team, sc.claim_dimension
        """,
        (start, end),
    ).fetchall()
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["source_id"]), str(row["claim_type"]))].append(row)
    output: dict[str, list[CredibilityCell]] = defaultdict(list)
    for (source_id, claim_type), cells in sorted(grouped.items()):
        first = cells[0]
        weighted_correct = sum(float(cell["weighted_correct"]) for cell in cells)
        weighted_incorrect = sum(float(cell["weighted_incorrect"]) for cell in cells)
        mean, low, high = posterior_from_weights(
            float(first["beta_prior_alpha"]),
            float(first["beta_prior_beta"]),
            weighted_correct,
            weighted_incorrect,
            interval_mass=float(first["posterior_interval_mass"]),
        )
        output[source_id].append(
            {
                "claim_type": claim_type,
                "n": sum(int(cell["n_graded"]) for cell in cells),
                "mean": mean,
                "low": low,
                "high": high,
                "mass": float(first["posterior_interval_mass"]),
            }
        )
    return {source_id: tuple(cells) for source_id, cells in output.items()}


def _stage1_cost(connection: sqlite3.Connection, *, start: str, end: str) -> Stage1Cost:
    row = connection.execute(
        """
        SELECT count(*) AS attempts, coalesce(sum(cost_nanos_usd), 0) AS spent,
               coalesce(sum(input_tokens), 0) AS input_tokens,
               coalesce(sum(output_tokens), 0) AS output_tokens
        FROM source_item_extractions
        WHERE rtrim(ingested_at, 'Z') >= rtrim(?, 'Z')
          AND rtrim(ingested_at, 'Z') < rtrim(?, 'Z') AND cost_nanos_usd IS NOT NULL
        """,
        (start, end),
    ).fetchone()
    return {
        "attempts": int(row["attempts"]),
        "spent": int(row["spent"]),
        "input_tokens": int(row["input_tokens"]),
        "output_tokens": int(row["output_tokens"]),
    }


def _prompt_versions(
    connection: sqlite3.Connection, *, start: str, end: str
) -> list[PromptVersionRow]:
    rows = connection.execute(
        """
        SELECT prompt_version_id, model_id, count(*) AS attempts
        FROM source_item_extractions
        WHERE rtrim(ingested_at, 'Z') >= rtrim(?, 'Z')
          AND rtrim(ingested_at, 'Z') < rtrim(?, 'Z')
        GROUP BY prompt_version_id, model_id
        ORDER BY prompt_version_id, model_id
        """,
        (start, end),
    ).fetchall()
    output: list[PromptVersionRow] = []
    for row in rows:
        evaluation = connection.execute(
            """
            SELECT model_eval_id, observed_at, item_count, label_row_count, metrics_json
            FROM model_evals
            WHERE evaluation_kind = 'stage1' AND prompt_version_id = ? AND model_id = ?
              AND rtrim(observed_at, 'Z') < rtrim(?, 'Z')
            ORDER BY rtrim(observed_at, 'Z') DESC, model_eval_id DESC
            LIMIT 1
            """,
            (row["prompt_version_id"], row["model_id"], end),
        ).fetchone()
        output.append(
            {
                "prompt_version_id": str(row["prompt_version_id"]),
                "model_id": str(row["model_id"]),
                "attempts": int(row["attempts"]),
                "evaluation": None if evaluation is None else _stage1_evaluation(evaluation),
            }
        )
    return output


def _stage1_evaluation(row: sqlite3.Row) -> Stage1Evaluation:
    metrics = json.loads(str(row["metrics_json"]))
    claim_presence = metrics.get("claim_presence")
    f1 = claim_presence.get("f1") if isinstance(claim_presence, dict) else None
    return {
        "model_eval_id": str(row["model_eval_id"]),
        "observed_at": str(row["observed_at"]),
        "item_count": int(row["item_count"]),
        "label_row_count": int(row["label_row_count"]),
        "claim_f1": "unavailable" if f1 is None else _number(f1),
    }


def _ownership_evaluations(
    connection: sqlite3.Connection, *, start: str, end: str
) -> list[OwnershipEvaluationRow]:
    rows = connection.execute(
        """
        SELECT * FROM model_evals
        WHERE evaluation_kind = 'ownership' AND rtrim(observed_at, 'Z') >= rtrim(?, 'Z')
          AND rtrim(observed_at, 'Z') < rtrim(?, 'Z')
        ORDER BY observed_at, model_eval_id
        """,
        (start, end),
    ).fetchall()
    metric_names = (
        "mae_percentage_points",
        "log_score",
        "brier_score",
        "calibration_slope",
        "calibration_intercept",
        "top20_rank_correlation",
        "material_delta_directional_accuracy",
    )
    output: list[OwnershipEvaluationRow] = []
    for row in rows:
        parsed = json.loads(str(row["metrics_json"]))
        metrics: list[tuple[str, str, str]] = []
        for name in metric_names:
            value = parsed.get(name)
            if isinstance(value, dict):
                metrics.append((name, _number(value.get("model")), _number(value.get("baseline"))))
        output.append(
            {
                "model_eval_id": str(row["model_eval_id"]),
                "ownership_site": str(row["ownership_site"]),
                "ownership_archetype": str(row["ownership_archetype"]),
                "model_id": str(row["model_id"]),
                "item_count": int(row["item_count"]),
                "beat_baseline": bool(row["beat_baseline"]),
                "metrics": tuple(metrics),
            }
        )
    return output


def _signal_statuses(
    connection: sqlite3.Connection, *, start: str, end: str
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT run_id, slate_id, site, contest_archetype, governance_status,
               status_multiplier, count(*) AS signal_count
        FROM ownership_scenarios
        WHERE rtrim(observed_at, 'Z') >= rtrim(?, 'Z')
          AND rtrim(observed_at, 'Z') < rtrim(?, 'Z')
        GROUP BY run_id, slate_id, site, contest_archetype, governance_status, status_multiplier
        ORDER BY run_id, slate_id, site, contest_archetype
        """,
        (start, end),
    ).fetchall()
    return [dict(row) for row in rows]


def _lane_failures(
    connection: sqlite3.Connection, *, start: str, end: str
) -> list[dict[str, object]]:
    lane_by_step: dict[str, str] = {
        **{step: "BATCH" for step in BATCH_STEPS},
        **{step: "SLATE" for step in SLATE_STEPS},
        **{step: "RESULTS" for step in RESULTS_STEPS},
    }
    rows = connection.execute(
        """
        SELECT batch_run_id, step, started_at, error_text
        FROM ops_runs
        WHERE status = 'failed'
          AND rtrim(started_at, 'Z') >= rtrim(?, 'Z')
          AND rtrim(started_at, 'Z') < rtrim(?, 'Z')
        ORDER BY rtrim(started_at, 'Z'), ops_run_id
        """,
        (start, end),
    ).fetchall()
    return [
        {
            "lane": lane_by_step.get(str(row["step"]), "unknown"),
            "batch_run_id": str(row["batch_run_id"]),
            "step": str(row["step"]),
            "started_at": str(row["started_at"]),
            "error_text": None if row["error_text"] is None else str(row["error_text"]),
        }
        for row in rows
    ]


def _usd(nanos: int) -> str:
    return f"{Decimal(nanos) / Decimal(NANOS_PER_USD):.2f}"


def _cost_per(nanos: int, denominator: int) -> str:
    if denominator == 0:
        return "none recorded"
    return f"${Decimal(nanos) / Decimal(NANOS_PER_USD) / Decimal(denominator):.2f}"


def _number(value: object) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, (int, float)):
        return f"{value:.6g}"
    return str(value)
