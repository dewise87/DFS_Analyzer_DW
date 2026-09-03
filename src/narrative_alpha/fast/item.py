"""Single-item synchronous Stage 1 extraction for the Sunday fast lane."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from narrative_alpha.fast.rules import DEFAULT_FAST_LANE_RULES_PATH, load_fast_lane_rules
from narrative_alpha.ingest.timestamps import ensure_utc
from narrative_alpha.narrative import (
    DEFAULT_PRICING_PATH,
    ExtractionProvider,
    load_synchronous_pricing,
    plan_extraction,
    run_extraction_batch,
)
from narrative_alpha.narrative.anthropic_provider import AnthropicSynchronousProvider
from narrative_alpha.narrative.source_catalog import (
    CatalogError,
    catalog_source_grade,
    load_source_catalog,
)
from narrative_alpha.ops.spend import month_start_utc, month_to_date_spend_nanos
from narrative_alpha.store import apply_migrations, connect_database

DEFAULT_SOURCE_CATALOG_PATH = Path("config/narrative_sources.toml")
UTC_ZONE = ZoneInfo("UTC")


class FastItemError(RuntimeError):
    """Raised when an item is not eligible for synchronous fast extraction."""


@dataclass(frozen=True)
class FastClaim:
    claim_id: str
    claim_type: str
    claim_dimension: str
    players: tuple[str, ...]


@dataclass(frozen=True)
class FastItemReport:
    source_item_id: int
    source_id: str
    source_grade: str
    run_id: str | None
    claims: tuple[FastClaim, ...]
    players: tuple[str, ...]
    elapsed_seconds: float
    reused_existing: bool
    review_flagged: bool


def extract_fast_item(
    database: Path,
    *,
    url: str | None = None,
    source_item_id: int | None = None,
    api_key: str | None = None,
    provider: ExtractionProvider | None = None,
    catalog_path: Path = DEFAULT_SOURCE_CATALOG_PATH,
    pricing_path: Path = DEFAULT_PRICING_PATH,
    rules_path: Path = DEFAULT_FAST_LANE_RULES_PATH,
    monthly_budget_nanos: int | None = None,
    budget_timezone: ZoneInfo = UTC_ZONE,
    now: datetime | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> FastItemReport:
    """Extract exactly one already-collected A-graded item without the batch API.

    The same three gates as the batch lane stand in front of the call: the signed rule
    set must be active (an expired file stops a Sunday call as surely as a Wednesday
    one), the item may not already be mid-flight in the batch lane, and the monthly
    budget is checked with the worst-case estimate when a budget is given.
    """

    if (url is None) == (source_item_id is None):
        raise FastItemError("provide exactly one of --url or --source-item-id")
    executed_at = ensure_utc(now or datetime.now(UTC))
    started = monotonic()
    with connect_database(database) as connection:
        apply_migrations(connection)
        item = _resolve_item(connection, url=url, source_item_id=source_item_id)
        source_id = str(item["source_id"])
        grade = _source_grade(source_id, catalog_path=catalog_path)
        if grade != "A":
            raise FastItemError(
                f"source {source_id!r} is graded {grade}, not A; synchronous fast-lane "
                "extraction is refused"
            )
        observed_at = ensure_utc(
            datetime.fromisoformat(str(item["observed_at"]).replace("Z", "+00:00"))
        )
        if observed_at > executed_at:
            raise FastItemError(
                f"source item {item['source_item_id']} was observed after the fast-lane instant"
            )
        load_fast_lane_rules(rules_path, at=executed_at)
        item_id = int(item["source_item_id"])
        inflight = connection.execute(
            "SELECT extraction_id, status FROM source_item_extractions "
            "WHERE source_item_id = ? AND status IN ('creating', 'submitted', 'settling') "
            "ORDER BY extraction_id",
            (item_id,),
        ).fetchone()
        if inflight is not None:
            raise FastItemError(
                f"source item {item_id} already has attempt {inflight['extraction_id']} in "
                f"state {inflight['status']!r} from the batch lane; rerun `na-ops batch` to "
                f"settle it, or `na-extract abandon --extraction-id {inflight['extraction_id']} "
                "--reason '<why>'`, before extracting it synchronously"
            )
        pricing = load_synchronous_pricing(pricing_path)
        window_end = max(
            executed_at + timedelta(microseconds=1),
            observed_at + timedelta(microseconds=1),
        )
        if monthly_budget_nanos is not None:
            plan = plan_extraction(
                connection,
                window_start=observed_at,
                window_end=window_end,
                pricing=pricing,
                planned_at=executed_at,
                max_items=1,
                source_item_id=item_id,
            )
            month_start = month_start_utc(executed_at, timezone=budget_timezone)
            spent = month_to_date_spend_nanos(connection, since=month_start)
            estimate = plan.estimated_cost_nanos_usd if plan.ready else 0
            if spent + estimate > monthly_budget_nanos:
                raise FastItemError(
                    f"monthly LLM budget guard refused the fast item: month-to-date "
                    f"${_usd(spent)} plus a worst-case ${_usd(estimate)} exceeds the "
                    f"${_usd(monthly_budget_nanos)} budget; raise monthly_llm_budget_usd "
                    "in config/ops.toml or wait for the month to roll. Nothing was submitted"
                )
        active_provider = provider or AnthropicSynchronousProvider(api_key=api_key)
        report = run_extraction_batch(
            connection,
            window_start=observed_at,
            window_end=window_end,
            provider=active_provider,
            pricing=pricing,
            run_at=executed_at,
            clock=lambda: executed_at,
            max_items=1,
            source_item_id=int(item["source_item_id"]),
            run_tag="fast",
        )
        failures = (*report.errors, *report.ineligible)
        if failures:
            detail = "; ".join(f"{failure.code}: {failure.message}" for failure in failures)
            raise FastItemError(
                f"source item {item['source_item_id']} fast extraction failed: {detail}"
            )
        extraction_run_id = report.run_id or _terminal_run_id(
            connection, int(item["source_item_id"])
        )
        claims = _claims(connection, int(item["source_item_id"]), extraction_run_id)
        flagged = bool(
            connection.execute(
                "SELECT 1 FROM source_item_review_flags "
                "WHERE source_item_id = ? AND review_status = 'pending' LIMIT 1",
                (int(item["source_item_id"]),),
            ).fetchone()
        )
    players = tuple(sorted({player for claim in claims for player in claim.players}))
    return FastItemReport(
        source_item_id=int(item["source_item_id"]),
        source_id=source_id,
        source_grade=grade,
        run_id=extraction_run_id,
        claims=claims,
        players=players,
        elapsed_seconds=max(0.0, monotonic() - started),
        reused_existing=report.run_id is None and report.skipped_terminal_items > 0,
        review_flagged=flagged,
    )


def _usd(nanos: int) -> str:
    return f"{nanos / 1_000_000_000:.2f}"


def _resolve_item(
    connection: sqlite3.Connection,
    *,
    url: str | None,
    source_item_id: int | None,
) -> sqlite3.Row:
    if source_item_id is not None:
        rows = connection.execute(
            "SELECT source_item_id, source_id, observed_at FROM source_items "
            "WHERE source_item_id = ?",
            (source_item_id,),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT source_item_id, source_id, observed_at FROM source_items "
            "WHERE canonical_url = ? ORDER BY source_item_id",
            (url,),
        ).fetchall()
    if len(rows) != 1:
        target = f"source item {source_item_id}" if source_item_id is not None else f"URL {url!r}"
        raise FastItemError(
            f"{target} resolved to {len(rows)} collected items; exactly one is required"
        )
    return cast(sqlite3.Row, rows[0])


def _source_grade(source_id: str, *, catalog_path: Path) -> str:
    try:
        return catalog_source_grade(load_source_catalog(catalog_path), source_id)
    except CatalogError as error:
        raise FastItemError(
            f"source {source_id!r} has no trusted grade in {catalog_path}: {error}"
        ) from error


def _terminal_run_id(connection: sqlite3.Connection, source_item_id: int) -> str | None:
    row = connection.execute(
        "SELECT run_id FROM source_item_extractions WHERE source_item_id = ? "
        "AND status IN ('succeeded', 'flagged') ORDER BY ingested_at DESC LIMIT 1",
        (source_item_id,),
    ).fetchone()
    return None if row is None or row["run_id"] is None else str(row["run_id"])


def _claims(
    connection: sqlite3.Connection,
    source_item_id: int,
    run_id: str | None,
) -> tuple[FastClaim, ...]:
    rows = connection.execute(
        "SELECT claim_id, claim_type, claim_dimension FROM claims "
        "WHERE source_item_id = ? AND run_id IS ? ORDER BY claim_id",
        (source_item_id, run_id),
    ).fetchall()
    claims: list[FastClaim] = []
    for row in rows:
        names = tuple(
            str(player["canonical_name"] or player["name_raw"])
            for player in connection.execute(
                "SELECT ref.name_raw, p.canonical_name FROM claim_player_refs AS ref "
                "LEFT JOIN players AS p ON p.player_id = ref.player_id "
                "WHERE ref.claim_id = ? ORDER BY ref.ordinal",
                (str(row["claim_id"]),),
            )
        )
        claims.append(
            FastClaim(
                claim_id=str(row["claim_id"]),
                claim_type=str(row["claim_type"]),
                claim_dimension=str(row["claim_dimension"]),
                players=names,
            )
        )
    return tuple(claims)


__all__ = [
    "DEFAULT_SOURCE_CATALOG_PATH",
    "FastClaim",
    "FastItemError",
    "FastItemReport",
    "extract_fast_item",
]
