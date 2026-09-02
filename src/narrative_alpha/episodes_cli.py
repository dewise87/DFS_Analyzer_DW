"""Operator CLI for deterministic narrative episode builds and audits."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from narrative_alpha.narrative.episodes import (
    DEFAULT_WINDOW,
    EpisodeError,
    build_episodes,
    episode_audit_payload,
    load_episode_audits,
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
        prog="na-episodes",
        description="Build and inspect deterministic Stage 2 narrative episodes.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="build one append-only as-of snapshot")
    build_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    build_parser.add_argument("--as-of", type=_timestamp, required=True)
    build_parser.add_argument(
        "--window-hours",
        type=_positive_float,
        default=DEFAULT_WINDOW.total_seconds() / 3600.0,
    )

    show_parser = subparsers.add_parser("show", help="show episode claims and relations")
    show_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    selector = show_parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--player", type=int)
    selector.add_argument("--episode")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        with connect_database(arguments.database) as connection:
            apply_migrations(connection)
            if arguments.command == "build":
                report = build_episodes(
                    connection,
                    as_of=arguments.as_of,
                    window=timedelta(hours=arguments.window_hours),
                )
                payload: object = {
                    "as_of": _canonical_timestamp(report.as_of),
                    "claims_considered": report.claims_considered,
                    "episode_count": report.episode_count,
                    "episodes_inserted": report.episodes_inserted,
                    "membership_count": report.membership_count,
                    "memberships_inserted": report.memberships_inserted,
                    "method_version": report.method_version,
                    "reused_existing": report.reused_existing,
                    "run_id": report.run_id,
                    "team_scoped_claims": report.team_scoped_claims,
                    "unavailable_text_claims": report.unavailable_text_claims,
                    "unclustered_claims": report.unclustered_claims,
                    "unresolved_player_claims": report.unresolved_player_claims,
                    "unresolved_player_refs": report.unresolved_player_refs,
                    "window_hours": report.window_hours,
                }
            elif arguments.command == "show":
                audits = load_episode_audits(
                    connection,
                    player_id=arguments.player,
                    episode_id=arguments.episode,
                )
                payload = {
                    "count": len(audits),
                    "episodes": [episode_audit_payload(audit) for audit in audits],
                }
            else:  # pragma: no cover - argparse constrains the command
                raise AssertionError(f"unhandled command {arguments.command!r}")
    except (
        EpisodeError,
        MigrationError,
        OSError,
        sqlite3.Error,
        StoreConfigurationError,
        ValidationError,
        ValueError,
    ) as error:
        print(
            json.dumps(
                {"error": {"code": "episodes_failed", "message": str(error)}},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


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
        raise argparse.ArgumentTypeError("must be a positive number") from error
    maximum_hours = timedelta.max.total_seconds() / 3600.0
    if parsed <= 0 or not math.isfinite(parsed) or parsed > maximum_hours:
        raise argparse.ArgumentTypeError("must be a positive number")
    return parsed


def _canonical_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
