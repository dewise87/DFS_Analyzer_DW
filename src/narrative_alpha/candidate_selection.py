"""Shared point-in-time candidate selection for decision build and replay."""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from narrative_alpha.portfolio import CandidatePlayer, DfsSite


class CandidateSelectionError(RuntimeError):
    """Raised when point-in-time store rows cannot form an optimizer scenario."""


class PointInTimeQuery(Protocol):
    """Minimal bounded-query contract supplied by the replay guardrail."""

    def query(
        self,
        sql: str,
        parameters: Mapping[str, object] | None = None,
        *,
        as_of: datetime | None,
    ) -> tuple[sqlite3.Row, ...]: ...


@dataclass(frozen=True, order=True)
class SelectedSourceArtifact:
    """One source file that contributed at least one selected candidate value."""

    sha256: str
    source: str


@dataclass(frozen=True)
class CandidateSelection:
    """Candidate players plus the exact input files that contributed to them."""

    players: tuple[CandidatePlayer, ...]
    projection_source_versions: tuple[str, ...]
    salary_artifacts: tuple[SelectedSourceArtifact, ...]
    projection_artifacts: tuple[SelectedSourceArtifact, ...]

    @property
    def salary_hashes(self) -> frozenset[str]:
        return frozenset(artifact.sha256 for artifact in self.salary_artifacts)

    @property
    def projection_hashes(self) -> frozenset[str]:
        return frozenset(artifact.sha256 for artifact in self.projection_artifacts)


def select_candidate_scenario(
    session: PointInTimeQuery,
    *,
    slate_id: int,
    site: DfsSite,
    as_of: datetime,
    salary_hashes: frozenset[str] | None = None,
    projection_hashes: frozenset[str] | None = None,
) -> CandidateSelection:
    """Select and blend the exact point-in-time candidates used by build and replay.

    Build leaves the optional hash filters unset and captures the hashes returned from
    the selected rows. Replay supplies the captured hash sets, constraining the same
    ranking, joins, and blend implementation to the frozen inputs.
    """

    if (salary_hashes is None) != (projection_hashes is None):
        raise CandidateSelectionError(
            "salary and projection hash filters must either both be set or both be omitted"
        )
    if salary_hashes is not None and (not salary_hashes or not projection_hashes):
        raise CandidateSelectionError(
            "candidate selection requires non-empty salary and projection hash filters"
        )

    salary_filter, salary_parameters = _hash_filter(
        "s.source_file_sha256", "salary_hash", salary_hashes
    )
    projection_filter, projection_parameters = _hash_filter(
        "ps.source_file_sha256", "projection_hash", projection_hashes
    )
    parameters: dict[str, object] = {
        "slate_id": slate_id,
        "site": site.value,
        **salary_parameters,
        **projection_parameters,
    }
    rows = session.query(
        f"""
        WITH ranked_salaries AS (
            SELECT s.*,
                   row_number() OVER (
                       PARTITION BY s.player_id
                       ORDER BY s.observed_at DESC, s.salary_id DESC
                   ) AS version_rank
            FROM salaries AS s
            WHERE s.slate_id = :slate_id
              {salary_filter}
              AND julianday(s.observed_at) <= julianday(:as_of)
              AND julianday(s.valid_from) <= julianday(:as_of)
              AND (s.valid_to IS NULL OR julianday(s.valid_to) > julianday(:as_of))
        ),
        ranked_projections AS (
            SELECT ps.*,
                   row_number() OVER (
                       PARTITION BY ps.source, ps.player_id
                       ORDER BY ps.observed_at DESC, ps.projection_snapshot_id DESC
                   ) AS version_rank
            FROM projection_snapshots AS ps
            WHERE ps.slate_id = :slate_id
              AND ps.site = :site
              {projection_filter}
              AND julianday(ps.observed_at) <= julianday(:as_of)
              AND julianday(ps.valid_from) <= julianday(:as_of)
              AND (ps.valid_to IS NULL OR julianday(ps.valid_to) > julianday(:as_of))
        )
        SELECT s.player_id, s.site_player_id, s.roster_positions_json, s.salary,
               s.source_file_sha256 AS salary_hash, s.source AS salary_source,
               p.canonical_name, p.position,
               team.abbreviation AS team,
               opponent.abbreviation AS opponent,
               g.external_game_id, g.kickoff_at,
               ps.projection_mean, ps.ownership_projection,
               ps.source AS projection_source,
               ps.source_version AS projection_source_version,
               ps.source_file_sha256 AS projection_hash
        FROM ranked_salaries AS s
        JOIN players AS p ON p.player_id = s.player_id
        JOIN teams AS team ON team.team_id = s.team_id
        JOIN teams AS opponent ON opponent.team_id = s.opponent_team_id
        JOIN games AS g ON g.game_id = s.game_id
        JOIN ranked_projections AS ps
          ON ps.player_id = s.player_id AND ps.version_rank = 1
        WHERE s.version_rank = 1
          AND julianday(p.observed_at) <= julianday(:as_of)
          AND julianday(p.valid_from) <= julianday(:as_of)
          AND (p.valid_to IS NULL OR julianday(p.valid_to) > julianday(:as_of))
          AND julianday(team.observed_at) <= julianday(:as_of)
          AND julianday(team.valid_from) <= julianday(:as_of)
          AND (team.valid_to IS NULL OR julianday(team.valid_to) > julianday(:as_of))
          AND julianday(opponent.observed_at) <= julianday(:as_of)
          AND julianday(opponent.valid_from) <= julianday(:as_of)
          AND (opponent.valid_to IS NULL OR julianday(opponent.valid_to) > julianday(:as_of))
          AND julianday(g.observed_at) <= julianday(:as_of)
          AND julianday(g.valid_from) <= julianday(:as_of)
          AND (g.valid_to IS NULL OR julianday(g.valid_to) > julianday(:as_of))
        ORDER BY s.player_id, ps.source, ps.source_file_sha256
        """,
        parameters,
        as_of=as_of,
    )
    if not rows:
        raise CandidateSelectionError("no candidate players were available at the cutoff")

    grouped: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[int(row["player_id"])].append(row)

    players = tuple(_candidate_from_rows(grouped[player_id]) for player_id in sorted(grouped))
    salary_artifacts = tuple(
        sorted(
            {
                SelectedSourceArtifact(
                    sha256=str(player_rows[0]["salary_hash"]),
                    source=str(player_rows[0]["salary_source"]),
                )
                for player_rows in grouped.values()
            }
        )
    )
    projection_artifacts = tuple(
        sorted(
            {
                SelectedSourceArtifact(
                    sha256=str(row["projection_hash"]),
                    source=str(row["projection_source"]),
                )
                for row in rows
            }
        )
    )
    source_versions = tuple(
        sorted(
            {
                f"{row['projection_source']}:"
                f"{row['projection_source_version'] or 'unknown'}:"
                f"{row['projection_hash']}"
                for row in rows
            }
        )
    )
    return CandidateSelection(
        players=players,
        projection_source_versions=source_versions,
        salary_artifacts=salary_artifacts,
        projection_artifacts=projection_artifacts,
    )


def _candidate_from_rows(rows: list[sqlite3.Row]) -> CandidatePlayer:
    first = rows[0]
    try:
        slots = json.loads(str(first["roster_positions_json"]))
    except (TypeError, json.JSONDecodeError) as error:
        raise CandidateSelectionError("stored salary roster positions are invalid JSON") from error
    if not isinstance(slots, list) or not all(isinstance(slot, str) for slot in slots):
        raise CandidateSelectionError(
            "stored salary roster positions must be a JSON string array"
        )

    projection = math.fsum(float(row["projection_mean"]) for row in rows) / len(rows)
    ownership_values = [
        float(row["ownership_projection"])
        for row in rows
        if row["ownership_projection"] is not None
    ]
    projected_ownership = (
        None
        if not ownership_values
        else math.fsum(ownership_values) / len(ownership_values)
    )
    position = str(first["position"] or slots[0]).upper()
    return CandidatePlayer(
        player_id=int(first["player_id"]),
        site_player_id=str(first["site_player_id"]),
        name=str(first["canonical_name"]),
        team=str(first["team"]),
        opponent=str(first["opponent"]),
        position=position,
        eligible_roster_slots=tuple(slots),
        salary=int(first["salary"]),
        projection=projection,
        projected_ownership=projected_ownership,
        game_id=str(first["external_game_id"]),
        game_start=first["kickoff_at"],
    )


def _hash_filter(
    column: str,
    prefix: str,
    hashes: frozenset[str] | None,
) -> tuple[str, dict[str, object]]:
    if hashes is None:
        return "", {}
    parameters: dict[str, object] = {
        f"{prefix}_{index}": value for index, value in enumerate(sorted(hashes))
    }
    placeholders = ", ".join(f":{key}" for key in parameters)
    return f"AND {column} IN ({placeholders})", parameters
