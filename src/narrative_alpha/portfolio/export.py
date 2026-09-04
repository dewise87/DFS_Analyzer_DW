"""Deterministic DraftKings and FanDuel classic upload CSV rendering."""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from pathlib import Path

from narrative_alpha.portfolio.adapter import OptimizerError
from narrative_alpha.portfolio.models import (
    DfsSite,
    Lineup,
    LineupPlayer,
    SlateType,
    UploadEntry,
    site_rules,
)


def parse_upload_entries(path: Path, site: DfsSite) -> tuple[UploadEntry, ...]:
    """Read reserved-entry metadata from an untouched site upload template."""

    try:
        rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"), newline="")))
    except UnicodeDecodeError as error:
        raise OptimizerError("upload template is not UTF-8") from error
    if not rows:
        raise OptimizerError("upload template is empty")
    normalized = tuple(cell.strip().casefold().replace(" ", "_") for cell in rows[0])
    expected = (
        ("entry_id", "contest_name", "contest_id", "entry_fee")
        if site is DfsSite.DRAFTKINGS
        else ("entry_id", "contest_id", "contest_name")
    )
    if normalized[: len(expected)] != expected:
        raise OptimizerError(
            "upload template must begin with the site's reserved-entry columns: "
            + ",".join(expected)
        )
    entries: list[UploadEntry] = []
    for row_number, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        if len(row) < len(expected):
            raise OptimizerError(f"upload template row {row_number} is too short")
        prefix = row[: len(expected)]
        if site is DfsSite.DRAFTKINGS:
            entry_id, contest_name, contest_id, entry_fee = prefix
        else:
            entry_id, contest_id, contest_name = prefix
            entry_fee = ""
        try:
            entries.append(
                UploadEntry(
                    entry_id=entry_id,
                    contest_id=contest_id,
                    contest_name=contest_name,
                    entry_fee=entry_fee,
                )
            )
        except ValueError as error:
            raise OptimizerError(f"invalid upload template row {row_number}: {error}") from error
    if not entries:
        raise OptimizerError("upload template contains no reserved entries")
    if len({entry.entry_id for entry in entries}) != len(entries):
        raise OptimizerError("upload template contains duplicate entry IDs")
    return tuple(entries)


def export_upload_csv(
    lineups: tuple[Lineup, ...],
    site: DfsSite,
    entries: tuple[UploadEntry, ...] = (),
) -> bytes:
    """Render stable UTF-8 bytes suitable for a site's classic upload template."""

    if not lineups:
        raise OptimizerError("cannot export an empty lineup collection")
    if any(lineup.site is not site for lineup in lineups):
        raise OptimizerError("all lineups must belong to the requested site")
    if entries and len(entries) != len(lineups):
        raise OptimizerError("upload entry count must equal lineup count")

    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    slate_type = (
        SlateType.SHOWDOWN
        if any(player.slot in {"CPT", "MVP"} for player in lineups[0].players)
        else SlateType.CLASSIC
    )
    if any(
        any(player.slot in {"CPT", "MVP"} for player in lineup.players)
        != (slate_type is SlateType.SHOWDOWN)
        for lineup in lineups
    ):
        raise OptimizerError("cannot mix classic and showdown lineups in one upload")
    slots = site_rules(site, slate_type).slots
    prefix: tuple[str, ...]
    if entries and site is DfsSite.DRAFTKINGS:
        prefix = ("Entry ID", "Contest Name", "Contest ID", "Entry Fee")
    elif entries:
        prefix = ("entry_id", "contest_id", "contest_name")
    else:
        prefix = ()
    writer.writerow((*prefix, *slots))

    for index, lineup in enumerate(lineups):
        ordered = _order_players(lineup, slots)
        metadata: tuple[str, ...]
        if entries:
            entry = entries[index]
            metadata = (
                (entry.entry_id, entry.contest_name, entry.contest_id, entry.entry_fee)
                if site is DfsSite.DRAFTKINGS
                else (entry.entry_id, entry.contest_id, entry.contest_name)
            )
        else:
            metadata = ()
        cells = tuple(_render_player(player, site) for player in ordered)
        writer.writerow((*metadata, *cells))
    return output.getvalue().encode("utf-8")


def _order_players(lineup: Lineup, slots: tuple[str, ...]) -> tuple[LineupPlayer, ...]:
    by_slot: dict[str, list[LineupPlayer]] = defaultdict(list)
    for player in lineup.players:
        by_slot[player.slot].append(player)
    ordered: list[LineupPlayer] = []
    for slot in slots:
        try:
            ordered.append(by_slot[slot].pop(0))
        except (KeyError, IndexError) as error:
            raise OptimizerError(f"lineup {lineup.lineup_id} is missing slot {slot}") from error
    if any(by_slot.values()):
        raise OptimizerError(f"lineup {lineup.lineup_id} has unexpected roster slots")
    return tuple(ordered)


def _render_player(player: LineupPlayer, site: DfsSite) -> str:
    if site is DfsSite.DRAFTKINGS:
        return f"{player.name} ({player.site_player_id})"
    return player.site_player_id
