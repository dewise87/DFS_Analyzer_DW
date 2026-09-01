"""Deterministic DraftKings and FanDuel classic upload CSV rendering."""

from __future__ import annotations

import csv
import io
from collections import defaultdict

from narrative_alpha.portfolio.adapter import OptimizerError
from narrative_alpha.portfolio.models import (
    CLASSIC_SITE_RULES,
    DfsSite,
    Lineup,
    LineupPlayer,
    UploadEntry,
)


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
    slots = CLASSIC_SITE_RULES[site].slots
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
