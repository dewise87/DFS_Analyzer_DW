"""Point-in-time reconstruction and byte-stable decision snapshot replay."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from narrative_alpha.candidate_selection import (
    CandidateSelection,
    CandidateSelectionError,
    SelectedSourceArtifact,
)
from narrative_alpha.ownership_config import (
    OwnershipConfigError,
    OwnershipModelConfig,
    load_ownership_config_bytes,
)
from narrative_alpha.ownership_routing import (
    NO_PINNED_ROUTING,
    OWNERSHIP_CONFIG_ARTIFACT_KIND,
    OwnershipRouting,
    OwnershipRoutingError,
    PinnedOwnershipRouting,
    pinned_routing_from_manifest,
    select_routed_candidate_scenario,
    verify_pinned_routing,
)
from narrative_alpha.portfolio import (
    CONTEST_POLICY_ARTIFACT_KIND,
    SHOWDOWN_SITE_RULES,
    CandidatePlayerScenario,
    ContestArchetype,
    ContestPolicies,
    ContestPolicyError,
    DfsSite,
    Lineup,
    LineupPlayer,
    OptimizationRequest,
    OptimizerAdapter,
    SlateType,
    export_upload_csv,
    lineup_sha256,
    load_contest_policies_bytes,
    policy_request_fields,
    site_rules,
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
    ownership_routing: OwnershipRouting
    contest_policy: ContestPolicies


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
        availability_artifacts: frozenset[SelectedSourceArtifact] = frozenset(),
        slate_type: str = "classic",
        contest_archetype: str = ContestArchetype.CASH.value,
        ownership_routing: PinnedOwnershipRouting = NO_PINNED_ROUTING,
        ownership_config: OwnershipModelConfig | None = None,
    ) -> tuple[CandidateSelection, OwnershipRouting]:
        """Compatibility wrapper around the shared build/replay selection seam.

        The availability set defaults to empty, which selects exactly what a decision
        frozen before availability existed selected; a caller reading a manifest must
        pass what the manifest carries, or the selection will not match. The Stage 4
        routing defaults to "the vendor baseline was applied" for the same reason.
        """

        if not salary_artifacts or not projection_artifacts:
            raise ReplayArtifactError("decision manifest requires salary and projection artifacts")
        try:
            routed = select_routed_candidate_scenario(
                self,
                slate_id=slate_id,
                site=site,
                slate_type=slate_type,
                contest_archetype=contest_archetype,
                salary_artifacts=salary_artifacts,
                projection_artifacts=projection_artifacts,
                availability_artifacts=availability_artifacts,
                as_of=_require_as_of(as_of),
                pinned=ownership_routing,
                config=ownership_config,
            )
        except CandidateSelectionError as error:
            raise ReplayError(str(error)) from error
        except OwnershipRoutingError as error:
            raise ReplayArtifactError(str(error)) from error
        return routed.selection, routed.routing


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
    availability_artifacts = _source_artifacts(snapshot, "availability", required=False)
    contest_policy = _contest_policy(snapshot, artifact_root)

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

    pinned_routing = pinned_routing_from_manifest(snapshot.manifest_hashes_json)
    frozen_config = ownership_config_from_manifest(snapshot.manifest_hashes_json, artifact_root)
    try:
        routed = select_routed_candidate_scenario(
            session,
            slate_id=snapshot.slate_id,
            site=original_request.site,
            slate_type=slate.slate_type,
            contest_archetype=original_request.contest_archetype.value,
            salary_artifacts=salary_artifacts,
            projection_artifacts=projection_artifacts,
            availability_artifacts=availability_artifacts,
            as_of=cutoff,
            pinned=pinned_routing,
            config=frozen_config,
        )
        verify_pinned_routing(routed.routing, pinned_routing)
    except CandidateSelectionError as error:
        raise ReplayError(str(error)) from error
    except OwnershipRoutingError as error:
        raise ReplayArtifactError(str(error)) from error
    rebuilt = routed.selection
    if frozenset(rebuilt.salary_artifacts) != salary_artifacts:
        raise ReplayArtifactError(
            "not every salary manifest source/hash pair contributed replay rows"
        )
    if frozenset(rebuilt.projection_artifacts) != projection_artifacts:
        raise ReplayArtifactError(
            "not every projection manifest source/hash pair contributed replay rows"
        )
    if frozenset(rebuilt.availability_artifacts) != availability_artifacts:
        raise ReplayArtifactError(
            "not every availability manifest source/hash pair contributed replay rows"
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
    try:
        policy_fields = policy_request_fields(
            contest_policy,
            original_request.contest_archetype,
            scenario,
        )
    except ContestPolicyError as error:
        raise ReplayArtifactError(str(error)) from error
    for field, expected in policy_fields.as_update().items():
        if getattr(original_request, field) != expected:
            raise ReplayArtifactError(
                f"optimizer request {field} differs from frozen contest policy "
                f"{contest_policy.policy_version!r}"
            )
    request = original_request.model_copy(
        update={"candidate_player_scenario": scenario, **policy_fields.as_update()}
    )
    lineups = adapter.build_lineups(request)
    output = adapter.export_upload_csv(lineups, request.site, request.upload_entries)
    actual_hash = hashlib.sha256(output).hexdigest()
    report = ReplayReport(
        decision_snapshot_id=snapshot.decision_snapshot_id,
        decision_at=cutoff,
        expected_output_sha256=expected_output.sha256,
        actual_output_sha256=actual_hash,
        output_matches=(actual_hash == expected_output.sha256 and output == expected_output_bytes),
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
        ownership_routing=routed.routing,
        contest_policy=contest_policy,
    )


@dataclass(frozen=True)
class FrozenDecision:
    """A frozen decision read back from its own verified artifacts."""

    snapshot: DecisionSnapshotRow
    request: OptimizationRequest
    lineups: tuple[Lineup, ...]
    upload_bytes: bytes
    contest_policy: ContestPolicies


def read_frozen_decision(
    connection: sqlite3.Connection,
    *,
    decision_snapshot_id: str,
    decision_at: datetime,
    artifact_root: Path,
) -> FrozenDecision:
    """Read a frozen decision's lineups from its artifacts, verifying bytes, not rebuilding.

    Every artifact is checked against the manifest hash, the lineups are rebuilt from the
    upload CSV through the frozen request's own candidates, and that rebuild must re-export
    to exactly the frozen bytes. Nothing is optimized: this is for a caller that needs the
    lineups as an *input* (the fast lane pins the ones it leaves alone) and must not pay
    for a full replay to get them. Proving the decision reproduces is `na-replay`'s job.
    """

    cutoff = _require_as_of(decision_at)
    session = PointInTimeSession(connection)
    snapshot = session.decision_snapshot(decision_snapshot_id, as_of=cutoff)
    request_artifact = _single_artifact(snapshot, "optimizer_request")
    output_artifact = _single_artifact(snapshot, "generated_lineups")
    request_bytes = _read_verified_artifact(artifact_root, request_artifact)
    upload_bytes = _read_verified_artifact(artifact_root, output_artifact)
    contest_policy = _contest_policy(snapshot, artifact_root)
    try:
        request = OptimizationRequest.model_validate_json(request_bytes)
    except ValidationError as error:
        raise ReplayArtifactError(f"frozen optimizer request is not valid: {error}") from error
    try:
        policy_fields = policy_request_fields(
            contest_policy,
            request.contest_archetype,
            request.candidate_player_scenario,
        )
    except ContestPolicyError as error:
        raise ReplayArtifactError(str(error)) from error
    for field, expected in policy_fields.as_update().items():
        if getattr(request, field) != expected:
            raise ReplayArtifactError(
                f"frozen optimizer request {field} differs from contest policy "
                f"{contest_policy.policy_version!r}"
            )
    lineups = _lineups_from_upload(request, upload_bytes)
    try:
        rendered = export_upload_csv(lineups, request.site, request.upload_entries)
    except Exception as error:  # the export raises its own adapter error type
        raise ReplayArtifactError(f"frozen lineups do not re-export: {error}") from error
    if rendered != upload_bytes:
        raise ReplayArtifactError(
            "generated_lineups.csv does not round-trip through the frozen request's candidates"
        )
    return FrozenDecision(
        snapshot=snapshot,
        request=request,
        lineups=lineups,
        upload_bytes=upload_bytes,
        contest_policy=contest_policy,
    )


def _lineups_from_upload(request: OptimizationRequest, upload_bytes: bytes) -> tuple[Lineup, ...]:
    slots = site_rules(request.site, request.slate_type).slots
    by_site_id = {
        player.site_player_id: player for player in request.candidate_player_scenario.players
    }
    rows = list(csv.reader(io.StringIO(upload_bytes.decode("utf-8"), newline="")))
    if not rows:
        raise ReplayArtifactError("generated_lineups.csv is empty")
    header = tuple(rows[0])
    prefix = len(header) - len(slots)
    if prefix < 0 or header[prefix:] != tuple(slots):
        raise ReplayArtifactError(
            f"generated_lineups.csv header {header!r} does not end with the {request.site.value} "
            f"roster slots {slots!r}"
        )
    pinned = request.pinned_lineups
    lineups: list[Lineup] = []
    for index, row in enumerate(rows[1:]):
        cells = tuple(row[prefix:])
        if len(cells) != len(slots):
            raise ReplayArtifactError(
                f"generated_lineups.csv row {index + 1} has {len(cells)} cells"
            )
        players: list[LineupPlayer] = []
        for slot, cell in zip(slots, cells, strict=True):
            site_player_id = _site_player_id(cell, request.site)
            candidate = by_site_id.get(site_player_id)
            if candidate is None:
                raise ReplayArtifactError(
                    f"generated_lineups.csv names site player {site_player_id!r}, which the "
                    "frozen request's candidates do not contain"
                )
            captain = request.slate_type is SlateType.SHOWDOWN and slot in {"CPT", "MVP"}
            points_multiplier = (
                SHOWDOWN_SITE_RULES[request.site].captain_points_multiplier if captain else 1.0
            )
            salary_multiplier = (
                SHOWDOWN_SITE_RULES[request.site].captain_salary_multiplier if captain else 1.0
            )
            players.append(
                LineupPlayer(
                    slot=slot,
                    player_id=candidate.player_id,
                    site_player_id=site_player_id,
                    name=candidate.name,
                    team=candidate.team,
                    opponent=candidate.opponent,
                    position=candidate.position,
                    salary=round(candidate.salary * salary_multiplier),
                    projection=round(candidate.projection * points_multiplier, 6),
                    projected_ownership=candidate.projected_ownership,
                    projected_ownership_captain=candidate.projected_ownership_captain,
                    game_id=candidate.game_id,
                )
            )
        player_tuple = tuple(players)
        if index < len(pinned):
            # A pinned lineup was frozen verbatim, with the projections it carried when it
            # was first built; the CSV row must name exactly its players.
            frozen = pinned[index]
            if {player.player_id for player in frozen.players} != {
                player.player_id for player in player_tuple
            }:
                raise ReplayArtifactError(
                    f"generated_lineups.csv row {index + 1} does not match pinned lineup "
                    f"{frozen.lineup_id}"
                )
            lineups.append(frozen)
            continue
        lineups.append(
            Lineup(
                lineup_id=lineup_sha256(request.site, request.slate_id, player_tuple),
                site=request.site,
                slate_id=request.slate_id,
                players=player_tuple,
                total_salary=sum(player.salary for player in player_tuple),
                total_projection=round(sum(player.projection for player in player_tuple), 6),
            )
        )
    return tuple(lineups)


def _site_player_id(cell: str, site: DfsSite) -> str:
    if site is DfsSite.DRAFTKINGS:
        match = re.search(r"\(([^()]+)\)\s*$", cell)
        if match is None:
            raise ReplayArtifactError(f"upload cell {cell!r} carries no DraftKings player id")
        return match.group(1)
    return cell.strip()


def _single_artifact(snapshot: DecisionSnapshotRow, artifact_kind: str) -> DecisionManifestHash:
    artifacts = tuple(
        item for item in snapshot.manifest_hashes_json if item.artifact_kind == artifact_kind
    )
    if len(artifacts) != 1:
        raise ReplayArtifactError(
            f"decision manifest must contain exactly one {artifact_kind} artifact"
        )
    return artifacts[0]


def ownership_config_from_manifest(
    manifest: Sequence[DecisionManifestHash], artifact_root: Path
) -> OwnershipModelConfig | None:
    """The ownership configuration a routed decision froze, verified; None when unrouted.

    A decision that applied the vendor baseline froze no configuration, and governs
    under none on replay: the pinned routing short-circuits before any cap is read.
    """

    artifacts = tuple(
        item for item in manifest if item.artifact_kind == OWNERSHIP_CONFIG_ARTIFACT_KIND
    )
    if not artifacts:
        return None
    if len(artifacts) != 1:
        raise ReplayArtifactError("decision manifest carries more than one ownership config")
    raw = _read_verified_artifact(artifact_root, artifacts[0])
    try:
        config = load_ownership_config_bytes(raw, source=artifacts[0].path)
    except OwnershipConfigError as error:
        raise ReplayArtifactError(str(error)) from error
    if artifacts[0].source != config.config_version:
        raise ReplayArtifactError(
            "ownership config manifest version does not match the frozen config bytes"
        )
    return config


def _contest_policy(snapshot: DecisionSnapshotRow, artifact_root: Path) -> ContestPolicies:
    artifact = _single_artifact(snapshot, CONTEST_POLICY_ARTIFACT_KIND)
    raw = _read_verified_artifact(artifact_root, artifact)
    try:
        policies = load_contest_policies_bytes(raw, source=artifact.path)
    except ContestPolicyError as error:
        raise ReplayArtifactError(str(error)) from error
    if artifact.source != policies.policy_version:
        raise ReplayArtifactError(
            "contest policy manifest version does not match the frozen policy bytes"
        )
    return policies


def _source_artifacts(
    snapshot: DecisionSnapshotRow,
    artifact_kind: str,
    *,
    required: bool = True,
) -> frozenset[SelectedSourceArtifact]:
    missing_sources = tuple(
        item.sha256
        for item in snapshot.manifest_hashes_json
        if item.artifact_kind == artifact_kind and (item.source is None or not item.source.strip())
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
    if required and not artifacts:
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
