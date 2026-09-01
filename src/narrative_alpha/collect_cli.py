"""Operator CLI for policy-gated narrative feed capture and retention."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.narrative import (
    CollectionError,
    collect_source,
    purge_expired_content,
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


def _run(arguments: argparse.Namespace) -> int:
    if arguments.policy_max_age_days < 0:
        raise ValueError("policy max age must not be negative")
    reports: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    with connect_database(arguments.database) as connection:
        apply_migrations(connection)
        source_ids = arguments.source_id or [
            str(row[0])
            for row in connection.execute(
                "SELECT source_id FROM sources WHERE enabled = 1 ORDER BY source_id"
            )
        ]
        for source_id in source_ids:
            try:
                report = collect_source(
                    connection,
                    source_id,
                    observed_at=arguments.observed_at,
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
