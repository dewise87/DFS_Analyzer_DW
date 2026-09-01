"""Command-line interface for operator-copied contests and payout tables."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from narrative_alpha.contests import (
    ContestEntryError,
    ManualContest,
    PayoutBand,
    add_contest,
    parse_payout_csv,
)
from narrative_alpha.portfolio import ContestArchetype, DfsSite
from narrative_alpha.store import (
    MigrationError,
    StoreConfigurationError,
    apply_migrations,
    connect_database,
)

DEFAULT_DATABASE_PATH = Path("data/db/narrative_alpha.sqlite3")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="na-contest", description="Record manually copied contest lobby metadata."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_parser = subparsers.add_parser("add", help="add one contest and its payout table")
    add_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    add_parser.add_argument("--external-contest-id", "--contest-id", required=True)
    add_parser.add_argument(
        "--site", choices=tuple(site.value for site in DfsSite), required=True
    )
    add_parser.add_argument("--slate-id", type=int, required=True)
    add_parser.add_argument(
        "--archetype",
        "--contest-archetype",
        choices=tuple(archetype.value for archetype in ContestArchetype),
        required=True,
    )
    add_parser.add_argument("--field-size", type=int, required=True)
    add_parser.add_argument("--entry-limit", type=int, required=True)
    add_parser.add_argument("--entry-fee-cents", type=int, required=True)
    add_parser.add_argument("--total-prizes-cents", type=int)
    add_parser.add_argument("--payout-curve-id", required=True)
    payout_source = add_parser.add_mutually_exclusive_group(required=True)
    payout_source.add_argument("--payouts-csv", "--payout-csv", type=Path)
    payout_source.add_argument(
        "--payout",
        action="append",
        type=_payout,
        metavar="RANK_FROM:RANK_TO:PRIZE_CENTS",
        help="repeat for each inclusive payout band",
    )
    add_parser.add_argument("--observed-at", type=_timestamp, required=True)
    add_parser.add_argument("--published-at", type=_timestamp)
    add_parser.add_argument("--effective-at", type=_timestamp)
    add_parser.add_argument("--source", default="manual-site-lobby")
    add_parser.add_argument("--source-version", default="manual-contest-v1")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command != "add":
        raise AssertionError(f"unhandled command {arguments.command!r}")

    try:
        payouts = (
            parse_payout_csv(arguments.payouts_csv)
            if arguments.payouts_csv is not None
            else tuple(arguments.payout or ())
        )
        contest = ManualContest(
            external_contest_id=arguments.external_contest_id,
            site=arguments.site,
            slate_id=arguments.slate_id,
            archetype=arguments.archetype,
            field_size=arguments.field_size,
            entry_limit=arguments.entry_limit,
            entry_fee_cents=arguments.entry_fee_cents,
            total_prizes_cents=arguments.total_prizes_cents,
            payout_curve_id=arguments.payout_curve_id,
            source=arguments.source,
            published_at=arguments.published_at,
            observed_at=arguments.observed_at,
            effective_at=arguments.effective_at,
            source_version=arguments.source_version,
        )
        with connect_database(arguments.database) as connection:
            apply_migrations(connection)
            result = add_contest(connection, contest, payouts)
    except (
        ContestEntryError,
        MigrationError,
        OSError,
        sqlite3.Error,
        StoreConfigurationError,
        ValidationError,
        ValueError,
    ) as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "contest_id": result.contest.contest_id,
                "external_contest_id": result.contest.external_contest_id,
                "payout_curve_id": result.contest.payout_curve_id,
                "payout_rows_added": len(result.payouts),
                "site": result.contest.site,
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


def _payout(value: str) -> PayoutBand:
    try:
        rank_from, rank_to, prize_cents = (int(part) for part in value.split(":"))
        return PayoutBand(
            rank_from=rank_from,
            rank_to=rank_to,
            prize_cents=prize_cents,
        )
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError(
            "must be RANK_FROM:RANK_TO:PRIZE_CENTS"
        ) from error


if __name__ == "__main__":
    raise SystemExit(main())
