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

from narrative_alpha.ingest.availability import inactive_salary_status
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
    availability_artifacts: tuple[SelectedSourceArtifact, ...]
    ownership_artifacts: tuple[SelectedSourceArtifact, ...] = ()

    @property
    def salary_hashes(self) -> frozenset[str]:
        return frozenset(artifact.sha256 for artifact in self.salary_artifacts)

    @property
    def projection_hashes(self) -> frozenset[str]:
        return frozenset(artifact.sha256 for artifact in self.projection_artifacts)

    @property
    def availability_hashes(self) -> frozenset[str]:
        return frozenset(artifact.sha256 for artifact in self.availability_artifacts)


def select_candidate_scenario(
    session: PointInTimeQuery,
    *,
    slate_id: int,
    site: DfsSite,
    as_of: datetime,
    slate_type: str = "classic",
    salary_artifacts: frozenset[SelectedSourceArtifact] | None = None,
    projection_artifacts: frozenset[SelectedSourceArtifact] | None = None,
    availability_artifacts: frozenset[SelectedSourceArtifact] | None = None,
    ownership_artifacts: frozenset[SelectedSourceArtifact] | None = None,
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
    if salary_artifacts is not None and (not salary_artifacts or not projection_artifacts):
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
    availability_filter, availability_parameters = _artifact_filter(
        "pa.source_file_sha256",
        "pa.source",
        "availability_artifact",
        availability_artifacts,
        empty_means_none=True,
    )
    parameters: dict[str, object] = {
        "slate_id": slate_id,
        "site": site.value,
        **salary_parameters,
        **projection_parameters,
        **availability_parameters,
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
              AND rtrim(s.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
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
              AND rtrim(ps.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(ps.valid_from, 'Z') <= rtrim(:as_of, 'Z')
              AND (
                  ps.valid_to IS NULL
                  OR rtrim(ps.valid_to, 'Z') > rtrim(:as_of, 'Z')
              )
        ),
        ranked_availability AS (
            SELECT pa.*,
                   row_number() OVER (
                       PARTITION BY pa.slate_id, pa.site, pa.player_id
                       ORDER BY rtrim(pa.observed_at, 'Z') DESC, pa.availability_id DESC
                   ) AS version_rank
            FROM player_availability AS pa
            WHERE pa.slate_id = :slate_id
              AND pa.site = :site
              {availability_filter}
              AND rtrim(pa.observed_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(pa.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(pa.valid_from, 'Z') <= rtrim(:as_of, 'Z')
              AND (
                  pa.valid_to IS NULL
                  OR rtrim(pa.valid_to, 'Z') > rtrim(:as_of, 'Z')
              )
        )
        SELECT s.player_id, s.site_player_id, s.roster_positions_json, s.salary,
               s.player_status,
               s.source_file_sha256 AS salary_hash, s.source AS salary_source,
               p.canonical_name, p.position,
               team.abbreviation AS team,
               opponent.abbreviation AS opponent,
               g.external_game_id, g.kickoff_at,
               ps.projection_mean, ps.ownership_projection,
               ps.source AS projection_source,
               ps.source_version AS projection_source_version,
               ps.source_file_sha256 AS projection_hash,
               pa.availability_status, pa.source AS availability_source,
               pa.source_file_sha256 AS availability_hash
        FROM ranked_salaries AS s
        JOIN players AS p ON p.player_id = s.player_id
        JOIN teams AS team ON team.team_id = s.team_id
        JOIN teams AS opponent ON opponent.team_id = s.opponent_team_id
        JOIN games AS g ON g.game_id = s.game_id
        JOIN ranked_projections AS ps
          ON ps.player_id = s.player_id AND ps.version_rank = 1
        LEFT JOIN ranked_availability AS pa
          ON pa.player_id = s.player_id AND pa.version_rank = 1
        WHERE s.version_rank = 1
          AND rtrim(p.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(p.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(p.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (
              p.valid_to IS NULL
              OR rtrim(p.valid_to, 'Z') > rtrim(:as_of, 'Z')
          )
          AND rtrim(team.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(team.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(team.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (
              team.valid_to IS NULL
              OR rtrim(team.valid_to, 'Z') > rtrim(:as_of, 'Z')
          )
          AND rtrim(opponent.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(opponent.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(opponent.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (
              opponent.valid_to IS NULL
              OR rtrim(opponent.valid_to, 'Z') > rtrim(:as_of, 'Z')
          )
          AND rtrim(g.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(g.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
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

    ownership_by_role, selected_ownership_artifacts = _ownership_by_role(
        session,
        slate_id=slate_id,
        site=site,
        as_of=as_of,
        roles=("captain", "flex") if slate_type == "showdown" else ("classic",),
        player_ids=frozenset(grouped),
        artifacts=ownership_artifacts,
    )
    if slate_type == "showdown":
        missing = tuple(
            (player_id, role)
            for player_id in sorted(grouped)
            for role in ("captain", "flex")
            if role not in ownership_by_role.get(player_id, {})
        )
        if missing:
            detail = ", ".join(f"player {player_id} {role}" for player_id, role in missing[:10])
            suffix = "" if len(missing) <= 10 else f", +{len(missing) - 10} more"
            raise CandidateSelectionError(
                "showdown candidate selection requires as-of ownership_baselines for "
                f"both captain and flex roles; missing {detail}{suffix}"
            )
    players = tuple(
        _candidate_from_rows(
            grouped[player_id],
            baseline_ownership=ownership_by_role.get(player_id),
        )
        for player_id in sorted(grouped)
    )
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
    selected_availability_artifacts = tuple(
        sorted(
            {
                SelectedSourceArtifact(
                    sha256=str(row["availability_hash"]),
                    source=str(row["availability_source"]),
                )
                for row in rows
                if row["availability_hash"] is not None and row["availability_source"] is not None
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
        availability_artifacts=selected_availability_artifacts,
        ownership_artifacts=selected_ownership_artifacts,
    )


def _candidate_from_rows(
    rows: list[sqlite3.Row],
    *,
    baseline_ownership: Mapping[str, float] | None = None,
) -> CandidatePlayer:
    first = rows[0]
    try:
        slots = json.loads(str(first["roster_positions_json"]))
    except (TypeError, json.JSONDecodeError) as error:
        raise CandidateSelectionError("stored salary roster positions are invalid JSON") from error
    if not isinstance(slots, list) or not all(isinstance(slot, str) for slot in slots):
        raise CandidateSelectionError("stored salary roster positions must be a JSON string array")

    projection = math.fsum(float(row["projection_mean"]) for row in rows) / len(rows)
    ownership_values = [
        float(row["ownership_projection"])
        for row in rows
        if row["ownership_projection"] is not None
    ]
    projected_ownership = (
        None if not ownership_values else math.fsum(ownership_values) / len(ownership_values)
    )
    if baseline_ownership is not None:
        projected_ownership = baseline_ownership.get("classic", baseline_ownership.get("flex"))
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
        projected_ownership_captain=(
            None if baseline_ownership is None else baseline_ownership.get("captain")
        ),
        game_id=str(first["external_game_id"]),
        game_start=first["kickoff_at"],
        # A governed official availability decision overrides a salary-feed label.
        # Otherwise an explicit OUT must not reach the optimizer just because a
        # vendor retained a nonzero projection for that player.
        is_injured=(
            str(first["availability_status"]) == "unavailable"
            if first["availability_status"] is not None
            else inactive_salary_status(first["player_status"])
        ),
    )


def _ownership_by_role(
    session: PointInTimeQuery,
    *,
    slate_id: int,
    site: DfsSite,
    as_of: datetime,
    roles: tuple[str, ...],
    player_ids: frozenset[int],
    artifacts: frozenset[SelectedSourceArtifact] | None,
) -> tuple[dict[int, dict[str, float]], tuple[SelectedSourceArtifact, ...]]:
    """Use the latest dedicated baseline per role, pinning every consumed source file.

    Dedicated ownership captures take precedence over ownership embedded in a
    projection export. An empty artifact filter preserves older classic decisions
    that used only the latter; omitted filters discover inputs for a new build.
    """

    artifact_filter, parameters = _artifact_filter(
        "ob.source_file_sha256",
        "ob.source",
        "ownership_artifact",
        artifacts,
        empty_means_none=True,
    )
    role_parameters = {f"role_{index}": role for index, role in enumerate(roles)}
    role_binds = ", ".join(f":{key}" for key in role_parameters)
    rows = session.query(
        f"""
        WITH ranked AS (
            SELECT ob.*,
                   row_number() OVER (
                       PARTITION BY ob.player_id, ob.role
                       ORDER BY rtrim(ob.observed_at, 'Z') DESC,
                                rtrim(ob.ingested_at, 'Z') DESC,
                                ob.ownership_baseline_id DESC
                   ) AS baseline_rank
            FROM ownership_baselines AS ob
            WHERE ob.slate_id = :slate_id AND ob.site = :site
              AND ob.role IN ({role_binds})
              {artifact_filter}
              AND rtrim(ob.observed_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(ob.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(ob.valid_from, 'Z') <= rtrim(:as_of, 'Z')
              AND (ob.valid_to IS NULL OR rtrim(ob.valid_to, 'Z') > rtrim(:as_of, 'Z'))
        )
        SELECT player_id, role, ownership, source, source_file_sha256
        FROM ranked
        WHERE baseline_rank = 1
        ORDER BY player_id, role
        """,
        {"slate_id": slate_id, "site": site.value, **parameters, **role_parameters},
        as_of=as_of,
    )
    ownership: dict[int, dict[str, float]] = defaultdict(dict)
    selected_artifacts: set[SelectedSourceArtifact] = set()
    for row in rows:
        player_id = int(row["player_id"])
        if player_id not in player_ids:
            continue
        ownership[player_id][str(row["role"])] = float(row["ownership"])
        selected_artifacts.add(
            SelectedSourceArtifact(sha256=str(row["source_file_sha256"]), source=str(row["source"]))
        )
    return dict(ownership), tuple(sorted(selected_artifacts))


def _artifact_filter(
    hash_column: str,
    source_column: str,
    prefix: str,
    artifacts: frozenset[SelectedSourceArtifact] | None,
    *,
    empty_means_none: bool = False,
) -> tuple[str, dict[str, object]]:
    if artifacts is None:
        return "", {}
    if not artifacts and empty_means_none:
        return "AND 0", {}
    parameters: dict[str, object] = {}
    predicates: list[str] = []
    for index, artifact in enumerate(sorted(artifacts)):
        source_key = f"{prefix}_source_{index}"
        hash_key = f"{prefix}_hash_{index}"
        parameters[source_key] = artifact.source
        parameters[hash_key] = artifact.sha256
        predicates.append(f"({source_column} = :{source_key} AND {hash_column} = :{hash_key})")
    return "AND (" + " OR ".join(predicates) + ")", parameters
