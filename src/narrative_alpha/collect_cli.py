"""Operator CLI for policy-gated narrative feed capture and retention."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.narrative import (
    CollectionError,
    SourceSeedPlan,
    apply_source_seed,
    check_catalog_feeds,
    collect_source,
    feed_check_payload,
    load_source_catalog,
    plan_source_seed,
    purge_expired_content,
    seed_plan_payload,
    tombstone_removed_item,
)
from narrative_alpha.store import (
    MigrationError,
    StoreConfigurationError,
    apply_migrations,
    connect_database,
)

DEFAULT_DATABASE_PATH = Path("data/db/narrative_alpha.sqlite3")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="na-collect", description="Collect reviewed public feeds as prospective evidence."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    seed_parser = subparsers.add_parser(
        "seed", help="attest and append source catalog versions, or check feed health"
    )
    seed_parser.add_argument("--catalog", type=Path, default=Path("config/narrative_sources.toml"))
    seed_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    seed_parser.add_argument("--terms-reviewed-at", type=_timestamp)
    seed_parser.add_argument("--dry-run", action="store_true")
    seed_parser.add_argument("--check-feeds", action="store_true")

    run_parser = subparsers.add_parser("run", help="collect enabled reviewed feeds")
    run_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    run_parser.add_argument("--source-id", action="append")
    run_parser.add_argument("--observed-at", type=_timestamp)
    run_parser.add_argument("--policy-max-age-days", type=int, default=365)

    purge_parser = subparsers.add_parser(
        "purge", help="purge expired text and apply platform deletion reports"
    )
    purge_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    purge_parser.add_argument("--source-id", action="append")
    purge_parser.add_argument("--as-of", type=_timestamp)
    purge_parser.add_argument("--removed-item-id", action="append", type=int, default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "seed":
            return _seed(arguments)
        if arguments.command == "run":
            return _run(arguments)
        if arguments.command == "purge":
            return _purge(arguments)
    except (
        CollectionError,
        MigrationError,
        OSError,
        sqlite3.Error,
        StoreConfigurationError,
        ValueError,
    ) as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2
    raise AssertionError(f"unhandled command {arguments.command!r}")


def _seed(arguments: argparse.Namespace) -> int:
    if arguments.check_feeds:
        if arguments.dry_run:
            raise ValueError("--dry-run and --check-feeds are separate non-writing modes")
        catalog = load_source_catalog(arguments.catalog)
        report = check_catalog_feeds(catalog)
        print(json.dumps(feed_check_payload(report), indent=2, sort_keys=True))
        return 0 if report.ok else 2
    if arguments.terms_reviewed_at is None:
        raise ValueError(
            "--terms-reviewed-at is required; it attests that the operator reviewed the terms"
        )

    if arguments.dry_run:
        plan = _dry_run_seed_plan(arguments)
        print(
            json.dumps(
                {"dry_run": True, "seed_plan": seed_plan_payload(plan)},
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    with connect_database(arguments.database) as connection:
        apply_migrations(connection)
        plan = plan_source_seed(
            connection,
            arguments.catalog,
            terms_reviewed_at=arguments.terms_reviewed_at,
        )
        # Render every tier term and its covered source count before any catalog write.
        print(
            json.dumps(
                {"dry_run": False, "seed_plan": seed_plan_payload(plan)},
                indent=2,
                sort_keys=True,
            )
        )
        result = apply_source_seed(connection, plan)

    print(
        json.dumps(
            {
                "policy_versions_inserted": result.policy_versions_inserted,
                "source_versions_inserted": result.source_versions_inserted,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _dry_run_seed_plan(arguments: argparse.Namespace) -> SourceSeedPlan:
    """Plan against a disposable database so dry-run never mutates the target."""

    with tempfile.TemporaryDirectory(prefix="na-collect-seed-") as directory:
        disposable_path = Path(directory) / "planning.sqlite3"
        with connect_database(disposable_path) as connection:
            if arguments.database.exists():
                source_uri = f"{arguments.database.resolve().as_uri()}?mode=ro"
                with sqlite3.connect(source_uri, uri=True) as source_connection:
                    source_connection.backup(connection)
            apply_migrations(connection)
            return plan_source_seed(
                connection,
                arguments.catalog,
                terms_reviewed_at=arguments.terms_reviewed_at,
            )


def _run(arguments: argparse.Namespace) -> int:
    if arguments.policy_max_age_days < 0:
        raise ValueError("policy max age must not be negative")
    observed_at = arguments.observed_at or datetime.now(UTC)
    reports: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    with connect_database(arguments.database) as connection:
        apply_migrations(connection)
        if arguments.source_id:
            source_ids = list(dict.fromkeys(arguments.source_id))
        else:
            cutoff = utc_timestamp(observed_at)
            source_ids = [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT source_id
                    FROM (
                        SELECT
                            source_id,
                            enabled,
                            row_number() OVER (
                                PARTITION BY source_id
                                ORDER BY observed_at DESC, source_record_id DESC
                            ) AS version_rank
                        FROM sources
                        WHERE rtrim(observed_at, 'Z') <= rtrim(?, 'Z')
                          AND rtrim(valid_from, 'Z') <= rtrim(?, 'Z')
                          AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(?, 'Z'))
                    )
                    WHERE version_rank = 1 AND enabled = 1
                    ORDER BY source_id
                    """,
                    (cutoff, cutoff, cutoff),
                )
            ]
        for source_id in source_ids:
            try:
                report = collect_source(
                    connection,
                    source_id,
                    observed_at=observed_at,
                    policy_max_age=timedelta(days=arguments.policy_max_age_days),
                )
            except CollectionError as error:
                errors.append({"source_id": source_id, "message": str(error)})
                continue
            reports.append(
                {
                    "attempts": report.attempts,
                    "duplicate_items": report.duplicate_items,
                    "fetched_items": report.fetched_items,
                    "inserted_items": report.inserted_items,
                    "observed_at": utc_timestamp(report.observed_at),
                    "source_id": report.source_id,
                }
            )
    print(json.dumps({"errors": errors, "reports": reports}, indent=2, sort_keys=True))
    return 2 if errors else 0


def _purge(arguments: argparse.Namespace) -> int:
    deleted = 0
    with connect_database(arguments.database) as connection:
        apply_migrations(connection)
        for item_id in dict.fromkeys(arguments.removed_item_id):
            deleted += int(tombstone_removed_item(connection, item_id, reported_at=arguments.as_of))
        report = purge_expired_content(
            connection, as_of=arguments.as_of, source_ids=arguments.source_id
        )
    print(
        json.dumps(
            {
                "as_of": utc_timestamp(report.as_of),
                "platform_deletions_tombstoned": deleted,
                "retention_tombstones_written": report.tombstones_written,
                "source_items_purged": report.source_items_purged,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
