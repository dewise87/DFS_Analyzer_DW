"""Operator CLI for slate and salary ingestion (`na-slate`)."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from narrative_alpha.identity import CrosswalkError
from narrative_alpha.ingest.slates import (
    SlateIngestError,
    SlateLoadReport,
    list_slates,
    load_salary_capture,
    newest_salary_capture,
    render_slates,
)
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.snapshots.core import DEFAULT_SNAPSHOT_ROOT
from narrative_alpha.store import (
    MigrationError,
    StoreConfigurationError,
    apply_migrations,
    connect_database,
)

DEFAULT_DATABASE_PATH = Path("data/db/narrative_alpha.sqlite3")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="na-slate",
        description="Ingest captured DK/FD salary exports into slates and salaries.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser(
        "ingest", help="load a week's salary capture into slate and salary rows"
    )
    _add_common_arguments(ingest)
    ingest.add_argument("--site", choices=("dk", "fd"), required=True)
    ingest.add_argument(
        "--capture",
        type=Path,
        help="capture directory (default: the newest salaries capture for the week)",
    )
    ingest.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_SNAPSHOT_ROOT,
        help=f"snapshot root directory (default: {DEFAULT_SNAPSHOT_ROOT})",
    )
    ingest.add_argument(
        "--slate-name",
        help="operator label for the slate (default: the derived external slate id)",
    )
    ingest.add_argument(
        "--starts-at",
        type=_timestamp,
        help=(
            "first kickoff, required only for exports that omit game times "
            "(FanDuel); use the same value for every re-download of the slate"
        ),
    )

    listing = subparsers.add_parser("list", help="show the week's slates and their ids")
    _add_common_arguments(listing)
    listing.add_argument("--site", choices=("dk", "fd"), default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        with connect_database(arguments.database) as connection:
            apply_migrations(connection)
            if arguments.command == "ingest":
                return _ingest(connection, arguments)
            if arguments.command == "list":  # pragma: no branch - argparse constrains this
                print(
                    render_slates(
                        list_slates(
                            connection,
                            season=arguments.season,
                            week=arguments.week,
                            site=arguments.site,
                        ),
                        season=arguments.season,
                        week=arguments.week,
                    ),
                    end="",
                )
                return 0
    except (
        CrosswalkError,
        MigrationError,
        OSError,
        SlateIngestError,
        StoreConfigurationError,
        ValidationError,
        ValueError,
        sqlite3.Error,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    raise AssertionError(  # pragma: no cover - argparse constrains this
        f"unhandled command {arguments.command!r}"
    )


def _ingest(connection: sqlite3.Connection, arguments: argparse.Namespace) -> int:
    capture_path = arguments.capture or newest_salary_capture(
        arguments.root, arguments.season, arguments.week
    )
    report = load_salary_capture(
        connection,
        capture_path,
        season=arguments.season,
        week=arguments.week,
        site=arguments.site,
        slate_name=arguments.slate_name,
        starts_at=arguments.starts_at,
    )
    print(render_ingest(report), end="")
    return 0 if report.ok else 1


def render_ingest(report: SlateLoadReport) -> str:
    """Render the load as fixed lines; nothing refused or queued is summarized away."""

    lines = [
        f"SLATE INGEST — {report.season} week {report.week:02d} {report.site}",
        f"  capture     {report.capture_path}",
        f"  observed at {utc_timestamp(report.observed_at)}",
        f"  files       {report.files_seen} salary file(s), "
        f"{report.rows_seen} row(s), {report.rows_rejected} rejected",
    ]
    for skipped in report.files_skipped:
        lines.append(f"  skipped     {skipped} (another site)")

    if not report.slates:
        lines.append("  no slate was written")
    for slate in report.slates:
        lines.extend(
            (
                "",
                f"  slate {slate.slate_id} — {slate.name}",
                f"    external id  {slate.external_slate_id}",
                f"    type         {slate.slate_type}"
                + ("  (new)" if slate.slate_inserted else "  (existing)"),
                f"    locks at     {utc_timestamp(slate.locks_at)}",
                f"    salaries     {slate.salary_rows_inserted} inserted, "
                f"{slate.duplicate_rows} already loaded",
            )
        )
        if slate.teams_inserted or slate.games_inserted:
            lines.append(
                f"    recorded     {slate.teams_inserted} new team(s), "
                f"{slate.games_inserted} new game(s)"
            )
        for matchup in slate.matchups_without_kickoff:
            lines.append(
                f"    ! {matchup} has no kickoff in the export; its salary rows carry "
                "no game_id and will not reach a lineup build"
            )
        for change in slate.salary_changes:
            lines.append(
                f"    ~ {change.name_raw} ({change.site_player_id}) "
                f"{change.previous_salary} -> {change.salary} since "
                f"{utc_timestamp(change.previous_observed_at)}"
            )
        for unresolved in slate.unresolved:
            lines.append(
                f"    ? unresolved {unresolved.name_raw} ({unresolved.site_player_id}) "
                f"{unresolved.position} {unresolved.team} — "
                f"na-crosswalk resolve --unresolved-id {unresolved.unresolved_id} "
                "--player-id <player_id>"
            )

    if report.errors:
        lines.append("")
        lines.append("  ERRORS")
        lines.extend(f"    ! {error}" for error in report.errors)
    lines.append("")
    return "\n".join(lines)


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--season", type=_positive_integer, required=True)
    parser.add_argument("--week", type=_positive_integer, required=True)


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


if __name__ == "__main__":
    raise SystemExit(main())
