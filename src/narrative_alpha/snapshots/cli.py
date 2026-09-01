"""Command-line interface for append-only snapshot capture."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from narrative_alpha.snapshots.core import (
    DEFAULT_SNAPSHOT_ROOT,
    capture_files,
    collect_status,
    format_utc,
    initialize_week,
    verify_week,
)
from narrative_alpha.snapshots.fetch import fetch_odds, fetch_weather
from narrative_alpha.snapshots.models import CaptureKind


def build_parser() -> argparse.ArgumentParser:
    """Build the ``na-snapshot`` argument parser."""

    parser = argparse.ArgumentParser(
        prog="na-snapshot",
        description="Capture and verify point-in-time DFS input snapshots.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="initialize a season/week directory")
    _add_week_arguments(init_parser)
    _add_root_argument(init_parser)

    capture_parser = subparsers.add_parser(
        "capture", help="copy files into a new append-only capture"
    )
    _add_week_arguments(capture_parser)
    _add_root_argument(capture_parser)
    capture_parser.add_argument(
        "--kind",
        required=True,
        choices=[kind.value for kind in CaptureKind],
        help="type of input being captured",
    )
    capture_parser.add_argument("--source", required=True, help="source/vendor label")
    capture_parser.add_argument("files", nargs="+", type=Path, help="local files to capture")

    fetch_parser = subparsers.add_parser(
        "fetch", help="fetch odds or weather into a new append-only capture"
    )
    _add_week_arguments(fetch_parser)
    _add_root_argument(fetch_parser)
    fetch_parser.add_argument(
        "--kind",
        required=True,
        choices=[CaptureKind.ODDS.value, CaptureKind.WEATHER.value],
        help="remote input to fetch",
    )
    fetch_parser.add_argument(
        "--games",
        type=Path,
        help=(
            "games CSV with stadium (or home_team) and timezone-aware kickoff "
            "columns (weather only)"
        ),
    )

    verify_parser = subparsers.add_parser("verify", help="verify hashes and manifest coverage")
    _add_week_arguments(verify_parser)
    _add_root_argument(verify_parser)

    status_parser = subparsers.add_parser("status", help="show last capture time by week and kind")
    _add_root_argument(status_parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the snapshot CLI and return its process exit code."""

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            week_path = initialize_week(args.root, args.season, args.week)
            print(f"Initialized {week_path}")
            return 0

        if args.command == "capture":
            capture_path = capture_files(
                args.root,
                args.season,
                args.week,
                args.kind,
                args.source,
                args.files,
            )
            print(f"Captured {len(args.files)} file(s) in {capture_path}")
            return 0

        if args.command == "fetch":
            if args.kind == CaptureKind.ODDS:
                fetch_report = fetch_odds(
                    args.root,
                    args.season,
                    args.week,
                    api_key=os.environ.get("ODDS_API_KEY"),
                )
            else:
                if args.games is None:
                    raise ValueError("--games is required when --kind weather")
                fetch_report = fetch_weather(
                    args.root,
                    args.season,
                    args.week,
                    args.games,
                )
            print(
                f"Captured {fetch_report.files_captured} response file(s) in "
                f"{fetch_report.capture_path}"
            )
            if fetch_report.errors:
                print(
                    f"ERROR: {len(fetch_report.errors)} fetch error(s); see manifest errors section"
                )
                return 1
            return 0

        if args.command == "verify":
            verification = verify_week(args.root, args.season, args.week)
            if verification.ok:
                print(
                    f"OK: verified {verification.files_checked} file(s) across "
                    f"{verification.manifests_checked} manifest(s) in {verification.week_path}"
                )
                return 0
            for problem in verification.problems:
                print(f"ERROR: {problem}")
            print(
                f"FAILED: {len(verification.problems)} problem(s) while checking "
                f"{verification.week_path}"
            )
            return 1

        if args.command == "status":
            status = collect_status(args.root)
            if not status.weeks:
                print(f"No snapshot weeks found under {args.root}")
            for week_status in status.weeks:
                print(f"{week_status.season} week_{week_status.week:02d}")
                for kind in CaptureKind:
                    last_capture = week_status.last_captured.get(kind)
                    value = "MISSING" if last_capture is None else format_utc(last_capture)
                    print(f"  {kind.value}: {value}")
            for problem in status.problems:
                print(f"WARNING: {problem}")
            return 1 if status.problems else 0
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}")
        return 2

    parser.error(f"unknown command: {args.command}")


def _add_week_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--season", required=True, type=int, help="NFL season year")
    parser.add_argument("--week", required=True, type=int, help="NFL week number")


def _add_root_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_SNAPSHOT_ROOT,
        help=f"snapshot root directory (default: {DEFAULT_SNAPSHOT_ROOT})",
    )


if __name__ == "__main__":
    raise SystemExit(main())
