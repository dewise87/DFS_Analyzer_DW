"""One canonical player per franchise defense, shared by every loader that meets a DST row.

No roster carries a team defense, and every site and vendor names it differently
("Green Bay Defense", "Packers", "GB D/ST"). Sending those rows through the person
crosswalk queues 32 by-hand resolutions a week and blocks the lineup build, so a defense
resolves deterministically here instead: key ``dst:<code>``, position ``DST``, inserted
once on first sight and reused thereafter.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from narrative_alpha.identity.normalization import normalize_team_code
from narrative_alpha.ingest.timestamps import utc_timestamp

DEFENSE_POSITIONS = frozenset({"DST", "D", "DEF", "D/ST"})
DEFENSE_PLAYER_SOURCE = "team-defense-identity"
DEFENSE_SOURCE_VERSION = "team-defense-v1"


def is_defense_position(position: str | None) -> bool:
    """True for the position labels sites and vendors use for a team defense."""

    return position is not None and position.strip().upper() in DEFENSE_POSITIONS


def resolve_team_defense(
    connection: sqlite3.Connection,
    team_code: str,
    *,
    observed_at: datetime,
    ingested_at: datetime,
    run_id: str | None = None,
) -> int:
    """Return the canonical defense ``player_id`` for a franchise, inserting it once."""

    canonical = normalize_team_code(team_code)
    key = f"dst:{canonical}"
    row = connection.execute(
        "SELECT player_id FROM players WHERE player_key = ? ORDER BY valid_from, player_id LIMIT 1",
        (key,),
    ).fetchone()
    if row is not None:
        return int(row[0])
    observed_text = utc_timestamp(observed_at)
    cursor = connection.execute(
        """
        INSERT INTO players(
            player_key, canonical_name, position, birth_date, source, published_at,
            observed_at, ingested_at, effective_at, valid_from, valid_to, source_version,
            run_id
        ) VALUES (?, ?, 'DST', NULL, ?, NULL, ?, ?, NULL, ?, NULL, ?, ?)
        """,
        (
            key,
            f"{canonical} DST",
            DEFENSE_PLAYER_SOURCE,
            observed_text,
            utc_timestamp(ingested_at),
            observed_text,
            DEFENSE_SOURCE_VERSION,
            run_id,
        ),
    )
    if cursor.lastrowid is None:  # pragma: no cover - SQLite always supplies a rowid
        raise sqlite3.DatabaseError(f"could not insert defense player {key}")
    return int(cursor.lastrowid)
