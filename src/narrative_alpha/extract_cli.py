"""Operator CLI for policy-gated Stage 1 structured extraction."""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import anthropic

from narrative_alpha.collect_cli import DEFAULT_DATABASE_PATH
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.narrative.anthropic_provider import (
    DEFAULT_BATCH_TIMEOUT_SECONDS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MODEL_ID,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_PROVIDER_IO_TIMEOUT_SECONDS,
    DEFAULT_SUBMISSION_TIMEOUT_SECONDS,
    AnthropicBatchPreflightError,
    AnthropicBatchProvider,
)
from narrative_alpha.narrative.extraction import (
    DEFAULT_PRICING_PATH,
    BatchPricing,
    ExtractionError,
    abandon_extraction,
    list_execution_leases,
    list_inflight_extractions,
    list_pending_review_flags,
    load_batch_pricing,
    plan_extraction,
    release_dead_run,
    run_extraction_batch,
)
from narrative_alpha.narrative.extraction_diagnostics import list_refused_attempts
from narrative_alpha.narrative.extraction_models import (
    ExtractionItemError,
    ExtractionPlan,
    ExtractionProvider,
    ExtractionReport,
    PreparedExtraction,
    ProviderBatchSubmission,
    ProviderResult,
)
from narrative_alpha.narrative.stage1_eval import (
    Stage1EvaluationError,
    create_review_sample,
    evaluate_labels,
    format_eval_report,
)
from narrative_alpha.store import (
    MigrationError,
    StoreConfigurationError,
    apply_migrations,
    connect_database,
)

ProviderFactory = Callable[[], ExtractionProvider]
COMMANDS = ("run", "abandon", "release", "review", "sample", "eval")
EXIT_OK = 0
EXIT_FAILED = 2
EXIT_PENDING = 3
PENDING_HINT = (
    "provider batch still processing: rerun the identical command to resume it; "
    "nothing is re-billed"
)


class _LazyProvider:
    """Construct the API-backed provider only if the final live plan needs it."""

    model_id = DEFAULT_MODEL_ID
    max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS

    def __init__(
        self,
        factory: ProviderFactory,
        *,
        timeout_seconds: float = DEFAULT_BATCH_TIMEOUT_SECONDS,
        io_timeout_seconds: float = DEFAULT_PROVIDER_IO_TIMEOUT_SECONDS,
        submission_timeout_seconds: float = DEFAULT_SUBMISSION_TIMEOUT_SECONDS,
    ) -> None:
        self._factory = factory
        self._provider: ExtractionProvider | None = None
        self.timeout_seconds = timeout_seconds
        self.io_timeout_seconds = io_timeout_seconds
        self.submission_timeout_seconds = submission_timeout_seconds

    def _get(self, *, submission_preflight: bool = False) -> ExtractionProvider:
        if self._provider is None:
            try:
                provider = self._factory()
                if (
                    getattr(provider, "model_id", self.model_id) != self.model_id
                    or getattr(provider, "max_output_tokens", self.max_output_tokens)
                    != self.max_output_tokens
                ):
                    raise ValueError(
                        "provider factory returned a model/max-output configuration "
                        "that differs from the extraction plan"
                    )
                for timeout_name, advertised in (
                    ("timeout_seconds", self.timeout_seconds),
                    ("io_timeout_seconds", self.io_timeout_seconds),
                    (
                        "submission_timeout_seconds",
                        self.submission_timeout_seconds,
                    ),
                ):
                    actual = getattr(provider, timeout_name, advertised)
                    if (
                        isinstance(actual, bool)
                        or not isinstance(actual, int | float)
                        or not math.isfinite(actual)
                        or actual <= 0
                        or actual > advertised
                    ):
                        raise ValueError(
                            f"provider factory {timeout_name} exceeds its advertised "
                            "lease-sizing contract"
                        )
                self._provider = provider
            except Exception as error:
                if submission_preflight:
                    raise AnthropicBatchPreflightError(
                        "provider initialization failed before batch submission"
                    ) from error
                raise
        return self._provider

    def submit_batch(
        self,
        requests: tuple[PreparedExtraction, ...],
    ) -> ProviderBatchSubmission:
        return self._get(submission_preflight=True).submit_batch(requests)

    def retrieve_batch(
        self,
        requests: tuple[PreparedExtraction, ...],
        submission: ProviderBatchSubmission,
    ) -> tuple[ProviderResult, ...]:
        return self._get().retrieve_batch(requests, submission)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="na-extract",
        description=(
            "Extract provenance-bearing claims from retained source items. "
            "`run` is assumed when no subcommand is given."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser(
        "run",
        help="plan and submit a Stage 1 batch for a window, or resume an accepted one",
    )
    run.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    run.add_argument("--window-start", required=True, type=_timestamp)
    run.add_argument("--window-end", required=True, type=_timestamp)
    run.add_argument("--run-at", type=_timestamp, help="dry-run only: evaluate as of this instant")
    run.add_argument("--pricing-config", type=Path, default=DEFAULT_PRICING_PATH)
    run.add_argument(
        "--poll-seconds",
        type=_positive_float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
    )
    run.add_argument(
        "--timeout-seconds",
        type=_positive_float,
        default=DEFAULT_BATCH_TIMEOUT_SECONDS,
        help="how long to wait for the batch before exiting with the pending status",
    )
    run.add_argument(
        "--max-items",
        type=_positive_int,
        help="submit at most this many fresh items (a bounded smoke test); the rest wait",
    )
    run.add_argument("--dry-run", action="store_true", help="plan and price; no API, no writes")
    run.add_argument(
        "--show-prompts",
        action="store_true",
        help="dry-run only: include every rendered user prompt in the output",
    )

    abandon = commands.add_parser(
        "abandon",
        help="terminate a stuck in-flight attempt so its item can be retried",
    )
    abandon.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    abandon.add_argument("--extraction-id", required=True)
    abandon.add_argument("--reason", required=True)

    release = commands.add_parser(
        "release",
        help="mark a run whose process died as failed and drop its leases so the next "
        "run can resume its accepted batch",
    )
    release.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    release.add_argument("--run-id", required=True)
    release.add_argument("--reason", required=True)

    review = commands.add_parser(
        "review",
        help="list refusals by bucket, pending review flags, in-flight attempts, and held leases",
    )
    review.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    review.add_argument(
        "--prompt-version-id", help="filter refused attempts to this prompt version"
    )

    sample = commands.add_parser(
        "sample",
        help="write a local stratified review CSV from stored Stage 1 results",
    )
    sample.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    sample.add_argument("--size", type=_positive_int, default=50)
    sample.add_argument("--output", type=Path, required=True, help="local output directory")

    evaluate = commands.add_parser(
        "eval",
        help="score a completed review CSV and store its prompt/model evaluation",
    )
    evaluate.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    evaluate.add_argument("--labels", type=Path, required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    provider_factory: ProviderFactory | None = None,
) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] not in COMMANDS and raw[0] not in {"-h", "--help"}:
        raw.insert(0, "run")
    arguments = build_parser().parse_args(raw)
    try:
        if arguments.command == "abandon":
            return _abandon(arguments)
        if arguments.command == "release":
            return _release(arguments)
        if arguments.command == "review":
            return _review(arguments)
        if arguments.command == "sample":
            return _sample(arguments)
        if arguments.command == "eval":
            return _eval(arguments)
        return _run(arguments, provider_factory=provider_factory)
    except (
        anthropic.AnthropicError,
        ExtractionError,
        MigrationError,
        OSError,
        sqlite3.Error,
        Stage1EvaluationError,
        StoreConfigurationError,
        ValueError,
    ) as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return EXIT_FAILED


def _run(arguments: argparse.Namespace, *, provider_factory: ProviderFactory | None) -> int:
    pricing = load_batch_pricing(arguments.pricing_config, model_id=DEFAULT_MODEL_ID)
    if arguments.dry_run:
        planned_at = arguments.run_at or datetime.now(UTC)
        plan, excluded_titles = _dry_run_plan(
            arguments, pricing=pricing, planned_at=planned_at
        )
        payload = _plan_payload(
            plan,
            pricing=pricing,
            show_prompts=arguments.show_prompts,
            excluded_titles=excluded_titles,
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return EXIT_OK
    if arguments.run_at is not None:
        raise ExtractionError("--run-at is allowed only with --dry-run")
    if arguments.show_prompts:
        raise ExtractionError("--show-prompts is allowed only with --dry-run")
    if provider_factory is None and not (
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    ):
        # Refuse before touching the database: a credential failure must never reserve
        # items or create an ambiguous submission.
        raise ExtractionError(
            "ANTHROPIC_API_KEY is not set; export it in this shell (or use --dry-run)"
        )
    run_at = datetime.now(UTC)

    with connect_database(arguments.database) as connection:
        apply_migrations(connection)
        factory = provider_factory or (
            lambda: AnthropicBatchProvider(
                poll_interval_seconds=arguments.poll_seconds,
                timeout_seconds=arguments.timeout_seconds,
            )
        )
        provider = _LazyProvider(
            factory,
            timeout_seconds=arguments.timeout_seconds,
        )
        report = run_extraction_batch(
            connection,
            window_start=arguments.window_start,
            window_end=arguments.window_end,
            provider=provider,
            pricing=pricing,
            run_at=run_at,
            max_items=arguments.max_items,
        )
    print(json.dumps(_report_payload(report), indent=2, sort_keys=True))
    if report.ok:
        return EXIT_OK
    if report.pending:
        print(json.dumps({"hint": PENDING_HINT}, sort_keys=True), file=sys.stderr)
        return EXIT_PENDING
    return EXIT_FAILED


def _abandon(arguments: argparse.Namespace) -> int:
    with connect_database(arguments.database) as connection:
        apply_migrations(connection)
        row = abandon_extraction(
            connection,
            extraction_id=arguments.extraction_id,
            reason=arguments.reason,
        )
    print(
        json.dumps(
            {
                "extraction_id": row.extraction_id,
                "source_item_id": row.source_item_id,
                "status": row.status,
                "error_code": row.error_code,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return EXIT_OK


def _release(arguments: argparse.Namespace) -> int:
    with connect_database(arguments.database) as connection:
        apply_migrations(connection)
        dropped = release_dead_run(
            connection, run_id=arguments.run_id, reason=arguments.reason
        )
    print(
        json.dumps(
            {"run_id": arguments.run_id, "status": "failed", "leases_dropped": dropped},
            indent=2,
            sort_keys=True,
        )
    )
    return EXIT_OK


def _review(arguments: argparse.Namespace) -> int:
    with connect_database(arguments.database) as connection:
        apply_migrations(connection)
        flags = list_pending_review_flags(connection)
        inflight = list_inflight_extractions(connection)
        leases = list_execution_leases(connection)
        refusals = list_refused_attempts(connection, prompt_version_id=arguments.prompt_version_id)
    print(
        json.dumps(
            {
                "refused_attempts_by_bucket": list(refusals),
                "refused_attempt_count": sum(int(str(group["count"])) for group in refusals),
                "pending_review_flags": list(flags),
                "pending_review_flag_count": len(flags),
                "inflight_attempts": list(inflight),
                "inflight_attempt_count": len(inflight),
                "held_leases": list(leases),
                "how_to_clear_a_stuck_attempt": (
                    "na-extract abandon --extraction-id <id> --reason '<why>'"
                ),
                "how_to_release_a_dead_run": (
                    "na-extract release --run-id <owner_run_id> --reason '<why>' when a "
                    "held lease's owner_status is 'running' but no na-extract/na-ops "
                    "process is alive"
                ),
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    return EXIT_OK


def _sample(arguments: argparse.Namespace) -> int:
    with connect_database(arguments.database) as connection:
        apply_migrations(connection)
        report = create_review_sample(
            connection,
            size=arguments.size,
            output_dir=arguments.output,
        )
    print(
        json.dumps(
            {
                "model_id": report.model_id,
                "output_path": str(report.output_path),
                "prompt_version_id": report.prompt_version_id,
                "rows_written": report.rows_written,
                "sampled_items": report.sampled_items,
                "strata_counts": dict(report.strata_counts),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return EXIT_OK


def _eval(arguments: argparse.Namespace) -> int:
    with connect_database(arguments.database) as connection:
        apply_migrations(connection)
        report = evaluate_labels(connection, arguments.labels)
    print(format_eval_report(report), end="")
    return EXIT_OK


def _dry_run_plan(
    arguments: argparse.Namespace,
    *,
    pricing: BatchPricing,
    planned_at: datetime,
) -> tuple[ExtractionPlan, dict[int, str | None]]:
    with tempfile.TemporaryDirectory(prefix="na-extract-plan-") as directory:
        disposable_path = Path(directory) / "planning.sqlite3"
        with connect_database(disposable_path) as connection:
            if arguments.database.exists():
                source_uri = f"{arguments.database.resolve().as_uri()}?mode=ro"
                with sqlite3.connect(source_uri, uri=True) as source_connection:
                    source_connection.backup(connection)
            apply_migrations(connection)
            plan = plan_extraction(
                connection,
                window_start=arguments.window_start,
                window_end=arguments.window_end,
                pricing=pricing,
                planned_at=planned_at,
                max_items=arguments.max_items,
            )
            excluded_ids = {
                *(error.source_item_id for error in plan.ineligible),
                *(item.source_item_id for item in plan.injection_blocked),
            }
            titles = {
                int(row["source_item_id"]): (
                    None if row["title"] is None else str(row["title"])
                )
                for row in connection.execute("SELECT source_item_id, title FROM source_items")
                if int(row["source_item_id"]) in excluded_ids
            }
            return plan, titles


def _usd(nanos: int) -> str:
    return str(Decimal(nanos) / Decimal(1_000_000_000))


def _plan_payload(
    plan: ExtractionPlan,
    *,
    pricing: BatchPricing,
    show_prompts: bool,
    excluded_titles: dict[int, str | None] | None = None,
) -> dict[str, object]:
    titles = excluded_titles or {}
    items = [
        _plan_item_payload(item, status="ready_for_batch", show_prompts=show_prompts)
        for item in plan.ready
    ] + [
        _plan_item_payload(item, status="resume_submitted_batch", show_prompts=show_prompts)
        for item in plan.resumable
    ] + [
        _plan_item_payload(item, status="submission_outcome_unknown", show_prompts=show_prompts)
        for item in plan.submission_unknown
    ] + [
        _plan_item_payload(
            item,
            status="blocked_prompt_injection",
            show_prompts=show_prompts,
            title=titles.get(item.source_item_id),
        )
        for item in plan.injection_blocked
    ]
    system_prompt = next(
        (
            item.system_prompt
            for group in (plan.ready, plan.resumable, plan.submission_unknown)
            for item in group
        ),
        None,
    )
    input_cost = pricing.cost_nanos(input_tokens=plan.estimated_input_tokens, output_tokens=0)
    return {
        "dry_run": True,
        "counts": {
            "ready_for_batch": len(plan.ready),
            "resume_submitted_batch": len(plan.resumable),
            "submission_outcome_unknown": len(plan.submission_unknown),
            "blocked_prompt_injection": len(plan.injection_blocked),
            "ineligible": len(plan.ineligible),
            "deferred_by_max_items": plan.deferred_items,
            "skipped_terminal": plan.skipped_terminal_items,
        },
        "estimated_cost_nanos_usd": plan.estimated_cost_nanos_usd,
        "estimated_cost_usd": _usd(plan.estimated_cost_nanos_usd),
        "estimated_input_cost_usd": _usd(input_cost),
        "estimated_max_output_cost_usd": _usd(plan.estimated_cost_nanos_usd - input_cost),
        "estimated_input_tokens": plan.estimated_input_tokens,
        "estimated_max_output_tokens": plan.estimated_max_output_tokens,
        "ineligible": [
            {
                **_item_error_payload(error),
                "title": titles.get(error.source_item_id),
            }
            for error in plan.ineligible
        ],
        "items": items,
        "model_id": plan.model_id,
        "prompt_sha256": plan.prompt_sha256,
        "prompt_version_id": plan.prompt_version_id,
        "schema_version": plan.schema_version,
        "skipped_terminal_items": plan.skipped_terminal_items,
        "system_prompt": system_prompt,
        "token_estimate_method": plan.token_estimate_method,
        "window_end": utc_timestamp(plan.window_end),
        "window_start": utc_timestamp(plan.window_start),
    }


def _plan_item_payload(
    item: PreparedExtraction,
    *,
    status: str,
    show_prompts: bool,
    title: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "estimated_input_tokens": item.estimated_input_tokens,
        "source_id": item.source_id,
        "source_item_id": item.source_item_id,
        "status": status,
    }
    if status == "blocked_prompt_injection":
        payload["title"] = title
    if show_prompts:
        payload["user_prompt"] = item.user_prompt
    return payload


def _item_error_payload(error: ExtractionItemError) -> dict[str, object]:
    return {
        "code": error.code,
        "message": error.message,
        "source_item_id": error.source_item_id,
    }


def _report_payload(report: ExtractionReport) -> dict[str, object]:
    return {
        "claims_stored": report.claims_stored,
        "deferred_items": report.deferred_items,
        "errors": [_item_error_payload(error) for error in report.errors],
        "flagged_item_ids": report.flagged_item_ids,
        "ineligible": [_item_error_payload(error) for error in report.ineligible],
        "ok": report.ok,
        "pending": report.pending,
        "run_id": report.run_id,
        "selected_items": report.selected_items,
        "skipped_terminal_items": report.skipped_terminal_items,
        "submitted_items": report.submitted_items,
        "succeeded_items": report.succeeded_items,
        "warnings": list(report.warnings),
        "window_end": utc_timestamp(report.window_end),
        "window_start": utc_timestamp(report.window_start),
    }


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
