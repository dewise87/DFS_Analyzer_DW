"""Operator CLI for deterministic Stage 3 narrative features."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from narrative_alpha.narrative.features import (
    DEFAULT_HEAT_CONFIG_PATH,
    FeatureError,
    build_features,
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
        prog="na-features",
        description="Build deterministic Stage 3 player/slate narrative features.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build", help="build one append-only as-of snapshot")
    build_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    build_parser.add_argument("--config", type=Path, default=DEFAULT_HEAT_CONFIG_PATH)
    build_parser.add_argument("--slate-id", type=_positive_integer, required=True)
    build_parser.add_argument("--site", choices=("dk", "fd"), required=True)
    build_parser.add_argument("--as-of", type=_timestamp, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        with connect_database(arguments.database) as connection:
            apply_migrations(connection)
            if arguments.command != "build":  # pragma: no cover - argparse constrains this
                raise AssertionError(f"unhandled command {arguments.command!r}")
            report = build_features(
                connection,
                slate_id=arguments.slate_id,
                site=arguments.site,
                as_of=arguments.as_of,
                config_path=arguments.config,
            )
    except (
        FeatureError,
        MigrationError,
        OSError,
        sqlite3.Error,
        StoreConfigurationError,
        ValidationError,
        ValueError,
    ) as error:
        print(
            json.dumps(
                {"error": {"code": "features_failed", "message": str(error)}},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    print(
        json.dumps(
            {
                "as_of": _canonical_timestamp(report.as_of),
                "episode_count": report.episode_count,
                "feature_version": report.feature_version,
                "features_inserted": report.features_inserted,
                "player_count": report.player_count,
                "reused_existing": report.reused_existing,
                "run_id": report.run_id,
                "site": report.site,
                "slate_id": report.slate_id,
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


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _canonical_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
