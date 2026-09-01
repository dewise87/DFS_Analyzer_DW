"""Point-in-time reconstruction and byte-stable decision snapshot replay."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from narrative_alpha.portfolio import (
    CandidatePlayer,
    CandidatePlayerScenario,
    DfsSite,
    OptimizationRequest,
    OptimizerAdapter,
)
from narrative_alpha.store import DecisionManifestHash, DecisionSnapshotRow, SlateRow


class ReplayError(RuntimeError):
    """Base replay failure with no unbounded or post-decision fallback."""


class MissingAsOfBound(ReplayError):
    """Raised when a replay read is attempted without an as-of timestamp."""


class UnboundedReplayQuery(ReplayError):
    """Raised when replay SQL does not contain the mandatory ``:as_of`` bind."""


class ReplayArtifactError(ReplayError):
    """Raised for absent, unsafe, malformed, or hash-mismatched snapshot artifacts."""


class ReplayReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_snapshot_id: str
    decision_at: datetime
    expected_output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    actual_output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_matches: bool
    lineup_count: int = Field(ge=1)

    @field_validator("decision_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _as_utc(value)


@dataclass(frozen=True)
class ReplayResult:
    report: ReplayReport
    output_bytes: bytes


@dataclass(frozen=True)
class _ReplayCandidateScenario:
    players: tuple[CandidatePlayer, ...]
    projection_source_versions: tuple[str, ...]
    salary_hashes: frozenset[str]
    projection_hashes: frozenset[str]


class PointInTimeSession:
    """The only SQLite read boundary used by replay.

    Even fixed internal queries must bind ``:as_of``. The public ``query`` method rejects
    both missing timestamps and SQL that omits the bound, making an accidental current-state
    read fail closed.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def query(
        self,
        sql: str,
        parameters: Mapping[str, object] | None = None,
        *,
        as_of: datetime | None,
    ) -> tuple[sqlite3.Row, ...]:
        if as_of is None:
            raise MissingAsOfBound("replay reads require an explicit as_of timestamp")
        if ":as_of" not in sql:
            raise UnboundedReplayQuery("replay SQL must include the :as_of bind")
        values = dict(parameters or {})
        values["as_of"] = _timestamp(as_of)
        rows = self._connection.execute(sql, values).fetchall()
        if rows and not isinstance(rows[0], sqlite3.Row):
            raise ReplayError("replay requires sqlite3.Row connection row_factory")
        return tuple(rows)

    def decision_snapshot(
        self, decision_snapshot_id: str, *, as_of: datetime | None
    ) -> DecisionSnapshotRow:
        rows = self.query(
            """
            SELECT *
            FROM decision_snapshots
            WHERE decision_snapshot_id = :decision_snapshot_id
              AND julianday(decision_at) <= julianday(:as_of)
            """,
            {"decision_snapshot_id": decision_snapshot_id},
            as_of=as_of,
        )
        if len(rows) != 1:
            raise ReplayError(
                f"decision snapshot {decision_snapshot_id!r} was not available as of decision_at"
            )
        snapshot = DecisionSnapshotRow.from_db(rows[0])
        if snapshot.decision_at != _require_as_of(as_of):
            raise ReplayError("requested decision_at does not equal the snapshot decision_at")
        return snapshot

    def slate(self, slate_id: int, *, as_of: datetime | None) -> SlateRow:
        rows = self.query(
            """
            SELECT *
            FROM slates
            WHERE slate_id = :slate_id
              AND julianday(observed_at) <= julianday(:as_of)
              AND julianday(valid_from) <= julianday(:as_of)
              AND (valid_to IS NULL OR julianday(valid_to) > julianday(:as_of))
            """,
            {"slate_id": slate_id},
            as_of=as_of,
        )
        if len(rows) != 1:
            raise ReplayError(f"slate {slate_id} is unavailable at the replay cutoff")
        return SlateRow.from_db(rows[0])

    def candidate_scenario(
        self,
        *,
        slate_id: int,
        site: DfsSite,
        salary_hashes: frozenset[str],
        projection_hashes: frozenset[str],
        as_of: datetime | None,
    ) -> _ReplayCandidateScenario:
        if not salary_hashes or not projection_hashes:
            raise ReplayArtifactError(
                "decision manifest requires salary and projection artifact hashes"
            )
        salary_clause, salary_parameters = _hash_clause("salary_hash", salary_hashes)
        projection_clause, projection_parameters = _hash_clause(
            "projection_hash", projection_hashes
        )
        parameters: dict[str, object] = {
            "slate_id": slate_id,
            "site": site.value,
            **salary_parameters,
            **projection_parameters,
        }
        rows = self.query(
            f"""
            WITH ranked_salaries AS (
                SELECT s.*,
                       row_number() OVER (
                           PARTITION BY s.player_id
                           ORDER BY s.observed_at DESC, s.salary_id DESC
                       ) AS version_rank
                FROM salaries AS s
                WHERE s.slate_id = :slate_id
                  AND s.source_file_sha256 IN ({salary_clause})
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
                  AND ps.source_file_sha256 IN ({projection_clause})
                  AND julianday(ps.observed_at) <= julianday(:as_of)
                  AND julianday(ps.valid_from) <= julianday(:as_of)
                  AND (ps.valid_to IS NULL OR julianday(ps.valid_to) > julianday(:as_of))
            )
            SELECT s.player_id, s.site_player_id, s.roster_positions_json, s.salary,
                   s.source_file_sha256 AS salary_hash,
                   p.canonical_name, p.position,
                   team.abbreviation AS team,
                   opponent.abbreviation AS opponent,
                   g.external_game_id, g.kickoff_at,
                   avg(ps.projection_mean) AS projection,
                   avg(ps.ownership_projection) AS projected_ownership
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
            GROUP BY s.player_id, s.site_player_id, s.roster_positions_json, s.salary,
                     s.source_file_sha256, p.canonical_name, p.position,
                     team.abbreviation, opponent.abbreviation,
                     g.external_game_id, g.kickoff_at
            ORDER BY s.player_id
            """,
            parameters,
            as_of=as_of,
        )
        source_rows = self.query(
            f"""
            WITH ranked AS (
                SELECT ps.*,
                       row_number() OVER (
                           PARTITION BY ps.source, ps.player_id
                           ORDER BY ps.observed_at DESC, ps.projection_snapshot_id DESC
                       ) AS version_rank
                FROM projection_snapshots AS ps
                WHERE ps.slate_id = :slate_id
                  AND ps.site = :site
                  AND ps.source_file_sha256 IN ({projection_clause})
                  AND julianday(ps.observed_at) <= julianday(:as_of)
                  AND julianday(ps.valid_from) <= julianday(:as_of)
                  AND (ps.valid_to IS NULL OR julianday(ps.valid_to) > julianday(:as_of))
            )
            SELECT DISTINCT source, source_version, source_file_sha256
            FROM ranked
            WHERE version_rank = 1
            ORDER BY source, source_version, source_file_sha256
            """,
            parameters,
            as_of=as_of,
        )
        players = tuple(_candidate_from_row(row) for row in rows)
        if not players:
            raise ReplayError("no candidate players were available at the replay cutoff")
        source_versions = tuple(
            f"{row['source']}:{row['source_version'] or 'unknown'}:{row['source_file_sha256']}"
            for row in source_rows
        )
        return _ReplayCandidateScenario(
            players=players,
            projection_source_versions=source_versions,
            salary_hashes=frozenset(str(row["salary_hash"]) for row in rows),
            projection_hashes=frozenset(str(row["source_file_sha256"]) for row in source_rows),
        )


def replay_decision(
    connection: sqlite3.Connection,
    *,
    decision_snapshot_id: str,
    decision_at: datetime,
    artifact_root: Path,
    adapter: OptimizerAdapter,
) -> ReplayResult:
    """Rebuild and hash one decision using only its captured pre-decision inputs."""

    cutoff = _as_utc(decision_at)
    session = PointInTimeSession(connection)
    snapshot = session.decision_snapshot(decision_snapshot_id, as_of=cutoff)
    request_artifact = _single_artifact(snapshot, "optimizer_request")
    expected_output = _single_artifact(snapshot, "generated_lineups")
    salary_hashes = _artifact_hashes(snapshot, "salary")
    projection_hashes = _artifact_hashes(snapshot, "projection")

    request_bytes = _read_verified_artifact(artifact_root, request_artifact)
    try:
        original_request = OptimizationRequest.model_validate_json(request_bytes)
    except ValidationError as error:
        raise ReplayArtifactError("optimizer request artifact is invalid") from error
    if original_request.slate_id != snapshot.slate_id:
        raise ReplayArtifactError("optimizer request slate does not match decision snapshot")

    slate = session.slate(snapshot.slate_id, as_of=cutoff)
    if original_request.site.value != slate.site:
        raise ReplayArtifactError("optimizer request site does not match point-in-time slate")
    if original_request.slate_type.value != slate.slate_type:
        raise ReplayArtifactError("optimizer request slate type does not match stored slate")

    rebuilt = session.candidate_scenario(
        slate_id=snapshot.slate_id,
        site=original_request.site,
        salary_hashes=salary_hashes,
        projection_hashes=projection_hashes,
        as_of=cutoff,
    )
    if rebuilt.salary_hashes != salary_hashes:
        raise ReplayArtifactError("not every salary manifest hash contributed replay rows")
    if rebuilt.projection_hashes != projection_hashes:
        raise ReplayArtifactError("not every projection manifest hash contributed replay rows")
    original_player_ids = {
        player.player_id for player in original_request.candidate_player_scenario.players
    }
    replay_player_ids = {player.player_id for player in rebuilt.players}
    if replay_player_ids != original_player_ids:
        raise ReplayArtifactError(
            "point-in-time candidate player IDs differ from the optimizer request artifact"
        )

    scenario = CandidatePlayerScenario(
        scenario_id=original_request.candidate_player_scenario.scenario_id,
        players=rebuilt.players,
        projection_source_versions=rebuilt.projection_source_versions,
    )
    request = original_request.model_copy(update={"candidate_player_scenario": scenario})
    lineups = adapter.build_lineups(request)
    output = adapter.export_upload_csv(lineups, request.site, request.upload_entries)
    actual_hash = hashlib.sha256(output).hexdigest()
    report = ReplayReport(
        decision_snapshot_id=snapshot.decision_snapshot_id,
        decision_at=cutoff,
        expected_output_sha256=expected_output.sha256,
        actual_output_sha256=actual_hash,
        output_matches=actual_hash == expected_output.sha256,
        lineup_count=len(lineups),
    )
    return ReplayResult(report=report, output_bytes=output)


def _candidate_from_row(row: sqlite3.Row) -> CandidatePlayer:
    try:
        slots = json.loads(str(row["roster_positions_json"]))
    except (TypeError, json.JSONDecodeError) as error:
        raise ReplayError("stored salary roster positions are invalid JSON") from error
    if not isinstance(slots, list) or not all(isinstance(slot, str) for slot in slots):
        raise ReplayError("stored salary roster positions must be a JSON string array")
    position = str(row["position"] or slots[0]).upper()
    return CandidatePlayer(
        player_id=int(row["player_id"]),
        site_player_id=str(row["site_player_id"]),
        name=str(row["canonical_name"]),
        team=str(row["team"]),
        opponent=str(row["opponent"]),
        position=position,
        eligible_roster_slots=tuple(slots),
        salary=int(row["salary"]),
        projection=float(row["projection"]),
        projected_ownership=(
            None if row["projected_ownership"] is None else float(row["projected_ownership"])
        ),
        game_id=str(row["external_game_id"]),
        game_start=row["kickoff_at"],
    )


def _single_artifact(
    snapshot: DecisionSnapshotRow, artifact_kind: str
) -> DecisionManifestHash:
    artifacts = tuple(
        item for item in snapshot.manifest_hashes_json if item.artifact_kind == artifact_kind
    )
    if len(artifacts) != 1:
        raise ReplayArtifactError(
            f"decision manifest must contain exactly one {artifact_kind} artifact"
        )
    return artifacts[0]


def _artifact_hashes(snapshot: DecisionSnapshotRow, artifact_kind: str) -> frozenset[str]:
    hashes = frozenset(
        item.sha256
        for item in snapshot.manifest_hashes_json
        if item.artifact_kind == artifact_kind
    )
    if not hashes:
        raise ReplayArtifactError(f"decision manifest has no {artifact_kind} artifacts")
    return hashes


def _read_verified_artifact(root: Path, artifact: DecisionManifestHash) -> bytes:
    root_path = root.resolve()
    candidate = (root_path / artifact.path).resolve()
    try:
        candidate.relative_to(root_path)
    except ValueError as error:
        message = f"artifact path escapes artifact root: {artifact.path}"
        raise ReplayArtifactError(message) from error
    try:
        content = candidate.read_bytes()
    except OSError as error:
        raise ReplayArtifactError(f"cannot read artifact {artifact.path}: {error}") from error
    actual_hash = hashlib.sha256(content).hexdigest()
    if actual_hash != artifact.sha256:
        raise ReplayArtifactError(f"artifact hash mismatch for {artifact.path}")
    return content


def _hash_clause(prefix: str, hashes: frozenset[str]) -> tuple[str, dict[str, object]]:
    parameters: dict[str, object] = {
        f"{prefix}_{index}": value for index, value in enumerate(sorted(hashes))
    }
    return ", ".join(f":{key}" for key in parameters), parameters


def _require_as_of(value: datetime | None) -> datetime:
    if value is None:
        raise MissingAsOfBound("replay reads require an explicit as_of timestamp")
    return _as_utc(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReplayError("replay timestamps must include a timezone")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")
