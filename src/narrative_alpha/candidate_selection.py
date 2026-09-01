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
    salary_artifacts: frozenset[SelectedSourceArtifact] | None = None,
    projection_artifacts: frozenset[SelectedSourceArtifact] | None = None,
) -> CandidateSelection:
    """Select and blend the exact point-in-time candidates used by build and replay.

    Build leaves the optional artifact filters unset and captures the source/hash pairs
    returned from the selected rows. Replay supplies those exact pairs, constraining the
    same ranking, joins, and blend implementation to the frozen inputs.
    """

    if (salary_artifacts is None) != (projection_artifacts is None):
        raise CandidateSelectionError(
            "salary and projection artifact filters must either both be set or both be omitted"
        )
    if salary_artifacts is not None and (
        not salary_artifacts or not projection_artifacts
    ):
        raise CandidateSelectionError(
            "candidate selection requires non-empty salary and projection artifact filters"
        )

    salary_filter, salary_parameters = _artifact_filter(
        "s.source_file_sha256",
        "s.source",
        "salary_artifact",
        salary_artifacts,
    )
    projection_filter, projection_parameters = _artifact_filter(
        "ps.source_file_sha256",
        "ps.source",
        "projection_artifact",
        projection_artifacts,
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
                       ORDER BY rtrim(s.observed_at, 'Z') DESC, s.salary_id DESC
                   ) AS version_rank
            FROM salaries AS s
            WHERE s.slate_id = :slate_id
              {salary_filter}
              AND rtrim(s.observed_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(s.valid_from, 'Z') <= rtrim(:as_of, 'Z')
              AND (
                  s.valid_to IS NULL
                  OR rtrim(s.valid_to, 'Z') > rtrim(:as_of, 'Z')
              )
        ),
        ranked_projections AS (
            SELECT ps.*,
                   row_number() OVER (
                       PARTITION BY ps.source, ps.player_id
                       ORDER BY rtrim(ps.observed_at, 'Z') DESC,
                                ps.projection_snapshot_id DESC
                   ) AS version_rank
            FROM projection_snapshots AS ps
            WHERE ps.slate_id = :slate_id
              AND ps.site = :site
              {projection_filter}
              AND rtrim(ps.observed_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(ps.valid_from, 'Z') <= rtrim(:as_of, 'Z')
              AND (
                  ps.valid_to IS NULL
                  OR rtrim(ps.valid_to, 'Z') > rtrim(:as_of, 'Z')
              )
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
          AND rtrim(p.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(p.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (
              p.valid_to IS NULL
              OR rtrim(p.valid_to, 'Z') > rtrim(:as_of, 'Z')
          )
          AND rtrim(team.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(team.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (
              team.valid_to IS NULL
              OR rtrim(team.valid_to, 'Z') > rtrim(:as_of, 'Z')
          )
          AND rtrim(opponent.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(opponent.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (
              opponent.valid_to IS NULL
              OR rtrim(opponent.valid_to, 'Z') > rtrim(:as_of, 'Z')
          )
          AND rtrim(g.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(g.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (
              g.valid_to IS NULL
              OR rtrim(g.valid_to, 'Z') > rtrim(:as_of, 'Z')
          )
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
    selected_salary_artifacts = tuple(
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
    selected_projection_artifacts = tuple(
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
        salary_artifacts=selected_salary_artifacts,
        projection_artifacts=selected_projection_artifacts,
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


def _artifact_filter(
    hash_column: str,
    source_column: str,
    prefix: str,
    artifacts: frozenset[SelectedSourceArtifact] | None,
) -> tuple[str, dict[str, object]]:
    if artifacts is None:
        return "", {}
    parameters: dict[str, object] = {}
    predicates: list[str] = []
    for index, artifact in enumerate(sorted(artifacts)):
        source_key = f"{prefix}_source_{index}"
        hash_key = f"{prefix}_hash_{index}"
        parameters[source_key] = artifact.source
        parameters[hash_key] = artifact.sha256
        predicates.append(
            f"({source_column} = :{source_key} AND {hash_column} = :{hash_key})"
        )
    return "AND (" + " OR ".join(predicates) + ")", parameters
