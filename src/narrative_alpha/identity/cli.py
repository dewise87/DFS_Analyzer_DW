"""Command-line manual review for unresolved player identities."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from narrative_alpha.identity.crosswalk import CrosswalkError, PlayerCrosswalk
from narrative_alpha.identity.nflverse import NflverseRosterError, refresh_roster_release
from narrative_alpha.store import apply_migrations, connect_database

DEFAULT_DATABASE_PATH = Path("data/db/narrative_alpha.sqlite3")
DEFAULT_NFLVERSE_ARCHIVE = Path("data/archive/nflverse")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="na-crosswalk")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    commands = parser.add_subparsers(dest="command", required=True)
    resolve = commands.add_parser("resolve", help="list or decide pending identities")
    resolve.add_argument("--unresolved-id", type=int)
    resolve.add_argument("--player-id", type=int)
    resolve.add_argument("--ignore", action="store_true")
    resolve.add_argument("--note")
    refresh = commands.add_parser(
        "nflverse-refresh",
        help="review the rolling nflverse roster without changing the pin table",
    )
    refresh.add_argument("--season", type=int, required=True)
    refresh.add_argument("--reviewed-at", type=date.fromisoformat, required=True)
    refresh.add_argument("--archive", type=Path, default=DEFAULT_NFLVERSE_ARCHIVE)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "nflverse-refresh":
            report = refresh_roster_release(
                args.season,
                args.archive,
                reviewed_at=args.reviewed_at,
            )
            print(report.render(), end="")
            return 0
        with connect_database(args.database) as connection:
            apply_migrations(connection)
            crosswalk = PlayerCrosswalk(connection)
            if args.unresolved_id is None:
                if args.player_id is not None or args.ignore:
                    raise CrosswalkError("--player-id/--ignore require --unresolved-id")
                unresolved = crosswalk.list_unresolved()
                if not unresolved:
                    print("No unresolved player identities.")
                for row in unresolved:
                    candidates = ", ".join(
                        f"{candidate['player_id']}:{candidate['canonical_name']}"
                        for candidate in row.candidates_json
                    )
                    print(
                        f"{row.unresolved_id}\t{row.source}\t{row.name_raw}\t"
                        f"{row.team}\t{row.position or '-'}\t{candidates or '-'}"
                    )
                return 0
            if args.ignore:
                if args.player_id is not None:
                    raise CrosswalkError("--ignore and --player-id are mutually exclusive")
                crosswalk.ignore(args.unresolved_id, note=args.note)
                print(f"Ignored unresolved identity {args.unresolved_id}.")
                return 0
            if args.player_id is None:
                raise CrosswalkError("a decision requires --player-id or --ignore")
            crosswalk.resolve(args.unresolved_id, args.player_id, note=args.note)
            print(f"Resolved identity {args.unresolved_id} to canonical player {args.player_id}.")
            return 0
    except (CrosswalkError, NflverseRosterError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
