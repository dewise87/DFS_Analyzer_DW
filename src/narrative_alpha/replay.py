"""Point-in-time reconstruction and byte-stable decision snapshot replay."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from narrative_alpha.candidate_selection import (
    CandidateSelection,
    CandidateSelectionError,
    SelectedSourceArtifact,
    select_candidate_scenario,
)
from narrative_alpha.portfolio import (
    CandidatePlayerScenario,
    DfsSite,
    Lineup,
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
    request: OptimizationRequest
    lineups: tuple[Lineup, ...]


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
              AND rtrim(decision_at, 'Z') <= rtrim(:as_of, 'Z')
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
              AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(valid_from, 'Z') <= rtrim(:as_of, 'Z')
              AND (
                  valid_to IS NULL
                  OR rtrim(valid_to, 'Z') > rtrim(:as_of, 'Z')
              )
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
        salary_artifacts: frozenset[SelectedSourceArtifact],
        projection_artifacts: frozenset[SelectedSourceArtifact],
        as_of: datetime | None,
    ) -> CandidateSelection:
        """Compatibility wrapper around the shared build/replay selection seam."""

        if not salary_artifacts or not projection_artifacts:
            raise ReplayArtifactError(
                "decision manifest requires salary and projection artifacts"
            )
        try:
            return select_candidate_scenario(
                self,
                slate_id=slate_id,
                site=site,
                salary_artifacts=salary_artifacts,
                projection_artifacts=projection_artifacts,
                as_of=_require_as_of(as_of),
            )
        except CandidateSelectionError as error:
            raise ReplayError(str(error)) from error


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
    salary_artifacts = _source_artifacts(snapshot, "salary")
    projection_artifacts = _source_artifacts(snapshot, "projection")

    request_bytes = _read_verified_artifact(artifact_root, request_artifact)
    expected_output_bytes = _read_verified_artifact(artifact_root, expected_output)
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

    try:
        rebuilt = select_candidate_scenario(
            session,
            slate_id=snapshot.slate_id,
            site=original_request.site,
            salary_artifacts=salary_artifacts,
            projection_artifacts=projection_artifacts,
            as_of=cutoff,
        )
    except CandidateSelectionError as error:
        raise ReplayError(str(error)) from error
    if frozenset(rebuilt.salary_artifacts) != salary_artifacts:
        raise ReplayArtifactError(
            "not every salary manifest source/hash pair contributed replay rows"
        )
    if frozenset(rebuilt.projection_artifacts) != projection_artifacts:
        raise ReplayArtifactError(
            "not every projection manifest source/hash pair contributed replay rows"
        )
    original_scenario = original_request.candidate_player_scenario
    if rebuilt.players != original_scenario.players:
        raise ReplayArtifactError(
            "point-in-time candidate values differ from the optimizer request artifact"
        )
    if rebuilt.projection_source_versions != original_scenario.projection_source_versions:
        raise ReplayArtifactError(
            "point-in-time projection source versions differ from the optimizer request artifact"
        )

    scenario = CandidatePlayerScenario(
        scenario_id=original_scenario.scenario_id,
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
        output_matches=(
            actual_hash == expected_output.sha256 and output == expected_output_bytes
        ),
        lineup_count=len(lineups),
    )
    return ReplayResult(
        report=report,
        output_bytes=output,
        # Preserve the exact frozen request artifact for downstream audit/reporting.
        # ``lineups`` were rebuilt from the point-in-time store-backed request above;
        # consumers can therefore compare the two and fail if any candidate value drifted.
        request=original_request,
        lineups=lineups,
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


def _source_artifacts(
    snapshot: DecisionSnapshotRow, artifact_kind: str
) -> frozenset[SelectedSourceArtifact]:
    missing_sources = tuple(
        item.sha256
        for item in snapshot.manifest_hashes_json
        if item.artifact_kind == artifact_kind
        and (item.source is None or not item.source.strip())
    )
    if missing_sources:
        raise ReplayArtifactError(
            f"decision manifest {artifact_kind} artifacts have no source: "
            + ", ".join(sorted(missing_sources))
        )
    artifacts = frozenset(
        SelectedSourceArtifact(sha256=item.sha256, source=str(item.source))
        for item in snapshot.manifest_hashes_json
        if item.artifact_kind == artifact_kind
    )
    if not artifacts:
        raise ReplayArtifactError(f"decision manifest has no {artifact_kind} artifacts")
    return artifacts


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
