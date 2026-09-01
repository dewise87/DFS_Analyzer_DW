"""Command-line interface for production decision builds."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from narrative_alpha.build import BuildError, build_decision
from narrative_alpha.identity import CrosswalkError
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.portfolio import ContestArchetype, DfsSite, OptimizerError
from narrative_alpha.store import MigrationError, StoreConfigurationError

DEFAULT_DATABASE_PATH = Path("data/db/narrative_alpha.sqlite3")
DEFAULT_ARTIFACT_DIRECTORY = Path("data/decisions")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="na-build")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--slate-id", type=int, required=True)
    parser.add_argument("--site", choices=tuple(site.value for site in DfsSite), required=True)
    parser.add_argument("--decision-at", type=_timestamp)
    parser.add_argument(
        "--artifact-directory",
        "--artifact-dir",
        "--artifact-root",
        type=Path,
        default=DEFAULT_ARTIFACT_DIRECTORY,
    )
    parser.add_argument("--number-of-lineups", type=int, default=1)
    parser.add_argument(
        "--contest-archetype",
        choices=tuple(archetype.value for archetype in ContestArchetype),
        default=ContestArchetype.CASH.value,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    # Resolve the optional wall-clock default at the CLI boundary. The value passed into
    # build_decision is thereafter the sole clock for every decision artifact and row.
    decision_at = arguments.decision_at or datetime.now(UTC)
    try:
        result = build_decision(
            arguments.database,
            slate_id=arguments.slate_id,
            site=arguments.site,
            decision_at=decision_at,
            artifact_directory=arguments.artifact_directory,
            number_of_lineups=arguments.number_of_lineups,
            contest_archetype=arguments.contest_archetype,
        )
    except BuildError as error:
        _print_error(error.structured())
        return 2
    except CrosswalkError as error:
        _print_error({"code": "unresolved_player_identity", "message": str(error)})
        return 2
    except OptimizerError as error:
        _print_error({"code": "optimizer_failed", "message": str(error)})
        return 2
    except (MigrationError, StoreConfigurationError, sqlite3.Error, ValueError) as error:
        _print_error({"code": "build_failed", "message": str(error)})
        return 2

    payload = {
        "artifact_directory": str(result.artifact_directory),
        "decision_at": utc_timestamp(result.snapshot.decision_at),
        "decision_snapshot_id": result.snapshot.decision_snapshot_id,
        "generated_lineups": str(result.generated_lineups_path),
        "lineup_count": len(result.lineups),
        "manifest": str(result.manifest_path),
        "manifest_hash_set_sha256": result.snapshot.manifest_hash_set_sha256,
        "optimizer_request": str(result.optimizer_request_path),
        "output_sha256": result.replay.report.actual_output_sha256,
        "replay_verified": result.replay.report.output_matches,
        "run_id": result.snapshot.run_id,
    }
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


def _print_error(error: dict[str, str]) -> None:
    print(json.dumps({"error": error}, sort_keys=True), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
