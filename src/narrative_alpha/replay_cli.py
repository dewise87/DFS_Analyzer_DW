"""Command-line interface for point-in-time decision replay."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from narrative_alpha.portfolio import PydfsAdapter
from narrative_alpha.replay import ReplayError, replay_decision
from narrative_alpha.store import apply_migrations, connect_database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="na-replay")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--decision-snapshot", required=True)
    parser.add_argument("--decision-at", type=_timestamp, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        with connect_database(arguments.database) as connection:
            apply_migrations(connection)
            result = replay_decision(
                connection,
                decision_snapshot_id=arguments.decision_snapshot,
                decision_at=arguments.decision_at,
                artifact_root=arguments.artifact_root,
                adapter=PydfsAdapter(),
            )
        if arguments.output is not None:
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_bytes(result.output_bytes)
        print(result.report.model_dump_json(indent=2))
        return 0 if result.report.output_matches else 1
    except ReplayError as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2


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
