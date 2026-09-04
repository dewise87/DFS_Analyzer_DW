"""Deterministic, evidence-backed human memo for one production slate decision."""

from __future__ import annotations

import csv
import io
import math
import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from narrative_alpha.candidate_selection import CandidateSelection, SelectedSourceArtifact
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.interface.red_team import (
    RED_TEAM_LIMIT,
    RedTeamAnswer,
    build_red_team_review,
    render_red_team_review,
)
from narrative_alpha.ownership_routing import (
    AppliedOwnershipDelta,
    OwnershipRouting,
    material_delta,
    pinned_routing_from_manifest,
)
from narrative_alpha.portfolio import (
    HEURISTIC_NOTICE,
    SHOWDOWN_SITE_RULES,
    CandidatePlayer,
    ContestPolicy,
    DfsSite,
    HeuristicReport,
    HeuristicThresholds,
    Lineup,
    LineupPlayer,
    SlateType,
    build_heuristic_report,
    render_heuristic_report,
)
from narrative_alpha.replay import (
    PointInTimeSession,
    ReplayArtifactError,
    ReplayError,
    ownership_config_from_manifest,
)
from narrative_alpha.store import ContestRow, DecisionSnapshotRow, SlateRow

if TYPE_CHECKING:
    from narrative_alpha.build import BuildResult

SLATE_MEMO_NOTICE: Literal[
    "DECISION INPUT SUMMARY — projections and ownership are point-in-time inputs, "
    "not realized outcomes; no EV or probability claim is simulator-backed."
] = (
    "DECISION INPUT SUMMARY — projections and ownership are point-in-time inputs, "
    "not realized outcomes; no EV or probability claim is simulator-backed."
)


class SlateMemoError(RuntimeError):
    """Raised when a memo value cannot be reproduced from the frozen decision."""


class SlateMemoInputArtifact(BaseModel):
    """One exact external artifact that fed the decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_kind: Literal["salary", "projection"]
    source: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    logical_path: str

    @field_validator("source", "logical_path")
    @classmethod
    def require_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized


class SlateMemoAppliedDelta(BaseModel):
    """One governed ownership move with the episodes and evidence behind it (§8.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    player_id: int
    role: str
    position: str
    baseline_ownership: float = Field(ge=0, le=1)
    applied_ownership: float = Field(ge=0, le=1)
    delta_points: float
    ownership_p10: float = Field(ge=0, le=1)
    ownership_p50: float = Field(ge=0, le=1)
    ownership_p90: float = Field(ge=0, le=1)
    prob_delta_positive: float = Field(ge=0, le=1)
    feature_id: str
    episode_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]


class SlateMemoOwnershipRouting(BaseModel):
    """Stage 4's verdict for this decision: applied or vendor baseline, and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    applied: bool
    reason: str
    contest_archetype: str
    role: str
    scenario_run_id: str | None = None
    scenario_decision_snapshot_id: str | None = None
    model_run_id: str | None = None
    model_version: str | None = None
    model_eval_id: str | None = None
    governance_status: str | None = None
    status_multiplier: float | None = Field(default=None, ge=0, le=1)
    config_sha256: str | None = None
    feature_version: str | None = None
    scenario_set_sha256: str | None = None
    applied_row_count: int = Field(default=0, ge=0)
    material_delta_count: int = Field(default=0, ge=0)
    applied_deltas: tuple[SlateMemoAppliedDelta, ...] = ()
    red_team: tuple[RedTeamAnswer, ...] = ()

    @model_validator(mode="after")
    def validate_applied_provenance(self) -> SlateMemoOwnershipRouting:
        if not self.applied:
            if self.scenario_run_id is not None or self.applied_deltas:
                raise ValueError("a baseline routing carries no scenario set")
            return self
        if self.scenario_run_id is None or self.model_eval_id is None:
            raise ValueError("an applied routing must name its scenario set and evaluation")
        untraceable = tuple(
            delta.player_id
            for delta in self.applied_deltas
            if abs(delta.delta_points) > material_delta() * 100 and not delta.episode_ids
        )
        if untraceable:
            raise ValueError(f"applied deltas without episode provenance: {sorted(untraceable)}")
        return self


class SlateMemoLineup(BaseModel):
    """One reproducible lineup summary plus its exact ordered roster."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lineup_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    total_projection: float = Field(ge=0, allow_inf_nan=False)
    total_salary: int = Field(gt=0)
    projected_ownership_sum: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    players: tuple[LineupPlayer, ...] = Field(min_length=1)


class SlateMemoContestPolicy(BaseModel):
    """The exact policy selection that constrained this decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: str
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    contest_archetype: str
    ownership_sum_points_min: float | None = None
    ownership_sum_points_max: float | None = None
    lineup_uniqueness: int = Field(ge=1, le=9)
    max_player_exposure: float = Field(gt=0, le=1)
    objective: str

    @model_validator(mode="after")
    def complete_band(self) -> SlateMemoContestPolicy:
        if (self.ownership_sum_points_min is None) != (self.ownership_sum_points_max is None):
            raise ValueError("ownership-sum policy band must have both bounds or neither")
        return self


class SlateMemo(BaseModel):
    """Structured slate memo carrying the section 7.1 identifiers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime
    decision_at: datetime
    decision_snapshot_id: str
    run_id: str
    manifest_hash_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    slate: SlateRow
    scenario_id: str
    projection_source_versions: tuple[str, ...] = Field(min_length=1)
    input_artifacts: tuple[SlateMemoInputArtifact, ...] = Field(min_length=2)
    lineups: tuple[SlateMemoLineup, ...] = Field(min_length=1)
    honest_labeling_notice: Literal[
        "DECISION INPUT SUMMARY — projections and ownership are point-in-time inputs, "
        "not realized outcomes; no EV or probability claim is simulator-backed."
    ] = SLATE_MEMO_NOTICE
    ownership_routing: SlateMemoOwnershipRouting
    contest_policy: SlateMemoContestPolicy
    attached_contest: ContestRow | None = None
    heuristic_report: HeuristicReport | None = None

    @field_validator("as_of", "decision_at")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def validate_attached_evidence(self) -> SlateMemo:
        if self.as_of != self.decision_at:
            raise ValueError("memo as_of must equal decision_at")
        if (self.attached_contest is None) != (self.heuristic_report is None):
            raise ValueError("attached contest and heuristic report must be present together")
        if (
            self.attached_contest is not None
            and self.heuristic_report is not None
            and (
                self.attached_contest.external_contest_id
                != self.heuristic_report.contest_external_contest_id
                or self.attached_contest.site != self.heuristic_report.contest_site
            )
        ):
            raise ValueError("heuristic report does not match the attached contest")
        return self


def build_slate_memo(
    build_result: BuildResult,
    store: sqlite3.Connection | PointInTimeSession,
    *,
    contest_id: int | None = None,
    heuristic_thresholds: HeuristicThresholds | None = None,
) -> SlateMemo:
    """Build a memo only after reproducing its snapshot, candidates, and lineups."""

    session = store if isinstance(store, PointInTimeSession) else PointInTimeSession(store)
    cutoff = build_result.snapshot.decision_at
    try:
        stored_snapshot = session.decision_snapshot(
            build_result.snapshot.decision_snapshot_id,
            as_of=cutoff,
        )
        slate = session.slate(stored_snapshot.slate_id, as_of=cutoff)
    except ReplayError as error:
        raise SlateMemoError(str(error)) from error
    _validate_snapshot(build_result.snapshot, stored_snapshot)
    selected, routing = _validate_build_result(build_result, session, slate)

    artifacts = _input_artifacts(stored_snapshot)
    memo_lineups = tuple(_memo_lineup(lineup) for lineup in build_result.lineups)
    contest = None
    heuristic = None
    if contest_id is not None:
        contest = _load_contest(
            session,
            contest_id=contest_id,
            slate=slate,
            as_of=cutoff,
        )
        try:
            heuristic = build_heuristic_report(
                build_result,
                contest,
                thresholds=heuristic_thresholds,
            )
        except ValueError as error:
            raise SlateMemoError(str(error)) from error
    if stored_snapshot.run_id is None:
        raise SlateMemoError("decision snapshot has no run_id")
    memo_routing = _memo_routing(
        session,
        routing,
        slate=slate,
        decision_at=cutoff,
        candidates={player.player_id: player for player in selected.players},
        lineups=build_result.lineups,
        optimizer_reads_ownership=build_result.request.ownership_sum_range is not None,
    )
    policy = build_result.contest_policy.for_archetype(build_result.request.contest_archetype)
    return SlateMemo(
        as_of=cutoff,
        decision_at=cutoff,
        decision_snapshot_id=stored_snapshot.decision_snapshot_id,
        run_id=stored_snapshot.run_id,
        manifest_hash_set_sha256=stored_snapshot.manifest_hash_set_sha256,
        slate=slate,
        scenario_id=build_result.request.candidate_player_scenario.scenario_id,
        projection_source_versions=tuple(
            sorted(build_result.request.candidate_player_scenario.projection_source_versions)
        ),
        input_artifacts=artifacts,
        lineups=memo_lineups,
        ownership_routing=memo_routing,
        contest_policy=_memo_policy(
            policy,
            policy_version=build_result.contest_policy.policy_version,
            policy_sha256=build_result.contest_policy.sha256,
            contest_archetype=build_result.request.contest_archetype.value,
        ),
        attached_contest=contest,
        heuristic_report=heuristic,
    )


def render_slate_memo(memo: SlateMemo) -> str:
    """Render a stable operator-facing memo with exact input and roster tables."""

    output = io.StringIO(newline="")
    output.write("SLATE DECISION MEMO\n")
    output.write(f"as_of={utc_timestamp(memo.as_of)}\n")
    output.write(f"decision_at={utc_timestamp(memo.decision_at)}\n")
    output.write(f"decision_snapshot_id={memo.decision_snapshot_id}\n")
    output.write(f"run_id={memo.run_id}\n")
    output.write(f"manifest_hash_set_sha256={memo.manifest_hash_set_sha256}\n")
    output.write(f"slate_id={memo.slate.slate_id}\n")
    output.write(f"external_slate_id={memo.slate.external_slate_id}\n")
    output.write(f"site={memo.slate.site}\n")
    output.write(f"slate_type={memo.slate.slate_type}\n")
    output.write(f"season={memo.slate.season}\n")
    output.write(f"week={memo.slate.week}\n")
    output.write(f"slate_name={memo.slate.name}\n")
    output.write(f"starts_at={utc_timestamp(memo.slate.starts_at)}\n")
    output.write(f"locks_at={utc_timestamp(memo.slate.locks_at)}\n")
    output.write(f"scenario_id={memo.scenario_id}\n")
    output.write("honest_labeling_notice=" + memo.honest_labeling_notice + "\n\n")

    policy = memo.contest_policy
    output.write("DECISION INPUT\n")
    output.write(f"contest_policy_version={policy.policy_version}\n")
    output.write(f"contest_policy_sha256={policy.policy_sha256}\n")
    output.write(f"contest_archetype={policy.contest_archetype}\n")
    output.write(f"objective={policy.objective}\n")
    if policy.ownership_sum_points_min is None:
        output.write("ownership_sum_points=none\n")
    else:
        output.write(
            f"ownership_sum_points={policy.ownership_sum_points_min:.6f}-"
            f"{policy.ownership_sum_points_max:.6f}\n"
        )
    output.write(f"lineup_uniqueness={policy.lineup_uniqueness}\n")
    output.write(f"max_player_exposure={policy.max_player_exposure:.6f}\n\n")

    output.write("INPUT PROVENANCE\n")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("artifact_kind", "source", "sha256", "logical_path"))
    for artifact in memo.input_artifacts:
        writer.writerow(
            (
                artifact.artifact_kind,
                artifact.source,
                artifact.sha256,
                artifact.logical_path,
            )
        )
    output.write("projection_source_versions\n")
    writer.writerow(("source_version",))
    for source_version in memo.projection_source_versions:
        writer.writerow((source_version,))

    output.write("\nLINEUPS\n")
    writer.writerow(
        (
            "lineup_id",
            "total_projection",
            "total_salary",
            "projected_ownership_sum",
        )
    )
    for lineup in memo.lineups:
        writer.writerow(
            (
                lineup.lineup_id,
                f"{lineup.total_projection:.6f}",
                lineup.total_salary,
                "unavailable"
                if lineup.projected_ownership_sum is None
                else f"{lineup.projected_ownership_sum:.6f}",
            )
        )

    if memo.slate.slate_type == "showdown":
        output.write("\nCAPTAIN CHOICES\n")
        writer.writerow(
            (
                "lineup_id",
                "slot",
                "player_id",
                "site_player_id",
                "name",
                "projected_ownership_flex",
                "projected_ownership_captain",
            )
        )
        for lineup in memo.lineups:
            captain = next(player for player in lineup.players if player.slot in {"CPT", "MVP"})
            writer.writerow(
                (
                    lineup.lineup_id,
                    captain.slot,
                    captain.player_id,
                    captain.site_player_id,
                    captain.name,
                    "unavailable"
                    if captain.projected_ownership is None
                    else f"{captain.projected_ownership:.6f}",
                    "unavailable"
                    if captain.projected_ownership_captain is None
                    else f"{captain.projected_ownership_captain:.6f}",
                )
            )

    output.write("\nROSTERS\n")
    writer.writerow(
        (
            "lineup_id",
            "slot",
            "player_id",
            "site_player_id",
            "name",
            "team",
            "opponent",
            "position",
            "salary",
            "projection",
            "projected_ownership",
            "game_id",
        )
    )
    for lineup in memo.lineups:
        for player in lineup.players:
            writer.writerow(
                (
                    lineup.lineup_id,
                    player.slot,
                    player.player_id,
                    player.site_player_id,
                    player.name,
                    player.team,
                    player.opponent,
                    player.position,
                    player.salary,
                    f"{player.projection:.6f}",
                    "unavailable"
                    if player.projected_ownership is None
                    else f"{player.projected_ownership:.6f}",
                    player.game_id,
                )
            )

    output.write("\nOWNERSHIP ROUTING (Stage 4)\n")
    routing = memo.ownership_routing
    output.write(
        "ownership_source=" + ("scenario_model" if routing.applied else "vendor_baseline") + "\n"
    )
    output.write(f"ownership_routing_reason={routing.reason}\n")
    output.write(f"ownership_contest_archetype={routing.contest_archetype}\n")
    output.write(f"ownership_role={routing.role}\n")
    output.write("ownership_scenario_run_id=" + _optional_scalar(routing.scenario_run_id) + "\n")
    output.write(
        "ownership_scenario_decision_snapshot_id="
        + _optional_scalar(routing.scenario_decision_snapshot_id)
        + "\n"
    )
    output.write("ownership_model_run_id=" + _optional_scalar(routing.model_run_id) + "\n")
    output.write("ownership_model_version=" + _optional_scalar(routing.model_version) + "\n")
    output.write("ownership_model_eval_id=" + _optional_scalar(routing.model_eval_id) + "\n")
    output.write(
        "ownership_governance_status=" + _optional_scalar(routing.governance_status) + "\n"
    )
    output.write(
        "ownership_status_multiplier="
        + (
            "unavailable"
            if routing.status_multiplier is None
            else f"{routing.status_multiplier:.6f}"
        )
        + "\n"
    )
    output.write("ownership_config_sha256=" + _optional_scalar(routing.config_sha256) + "\n")
    output.write("ownership_feature_version=" + _optional_scalar(routing.feature_version) + "\n")
    output.write(
        "ownership_scenario_set_sha256=" + _optional_scalar(routing.scenario_set_sha256) + "\n"
    )
    output.write(f"ownership_applied_rows={routing.applied_row_count}\n")
    output.write(f"ownership_material_deltas={routing.material_delta_count}\n")

    output.write("\nAPPLIED OWNERSHIP DELTAS\n")
    if not routing.applied_deltas:
        output.write("applied_delta_status=none — the vendor baseline reached the optimizer\n")
    else:
        output.write(
            f"applied_delta_status=showing the {len(routing.applied_deltas)} largest of "
            f"{routing.applied_row_count} applied row(s); every row is in "
            "ownership_scenarios under ownership_scenario_run_id\n"
        )
        writer.writerow(
            (
                "player_id",
                "role",
                "position",
                "baseline_ownership",
                "applied_ownership",
                "delta_points",
                "ownership_p10",
                "ownership_p50",
                "ownership_p90",
                "prob_delta_positive",
                "feature_id",
                "episode_ids",
                "evidence_refs",
            )
        )
        for delta in routing.applied_deltas:
            writer.writerow(
                (
                    delta.player_id,
                    delta.role,
                    delta.position,
                    f"{delta.baseline_ownership:.6f}",
                    f"{delta.applied_ownership:.6f}",
                    f"{delta.delta_points:+.6f}",
                    f"{delta.ownership_p10:.6f}",
                    f"{delta.ownership_p50:.6f}",
                    f"{delta.ownership_p90:.6f}",
                    f"{delta.prob_delta_positive:.6f}",
                    delta.feature_id,
                    "|".join(delta.episode_ids),
                    "|".join(delta.evidence_refs),
                )
            )

    output.write("\nRED TEAM (Stage 5)\n")
    output.write(render_red_team_review(routing.red_team))

    output.write("\nATTACHED CONTEST\n")
    if memo.attached_contest is None:
        output.write("contest_status=unavailable — no contest attached\n")
    else:
        contest = memo.attached_contest
        output.write("contest_status=available\n")
        output.write(f"contest_id={contest.contest_id}\n")
        output.write(f"contest_external_contest_id={contest.external_contest_id}\n")
        output.write(f"contest_site={contest.site}\n")
        output.write(f"contest_slate_id={contest.slate_id}\n")
        output.write(f"contest_archetype={contest.archetype}\n")
        output.write(f"contest_field_size={contest.field_size}\n")
        output.write(f"contest_entry_limit={contest.entry_limit}\n")
        output.write(f"contest_entry_fee_cents={contest.entry_fee_cents}\n")
        output.write(
            "contest_total_prizes_cents=" + _optional_scalar(contest.total_prizes_cents) + "\n"
        )
        output.write("contest_payout_curve_id=" + _optional_scalar(contest.payout_curve_id) + "\n")
        output.write(f"contest_source={contest.source}\n")
        output.write("contest_published_at=" + _optional_timestamp(contest.published_at) + "\n")
        output.write(f"contest_observed_at={utc_timestamp(contest.observed_at)}\n")
        output.write(f"contest_ingested_at={utc_timestamp(contest.ingested_at)}\n")
        output.write("contest_effective_at=" + _optional_timestamp(contest.effective_at) + "\n")
        output.write(f"contest_valid_from={utc_timestamp(contest.valid_from)}\n")
        output.write("contest_valid_to=" + _optional_timestamp(contest.valid_to) + "\n")
        output.write("contest_source_version=" + _optional_scalar(contest.source_version) + "\n")
        output.write("contest_run_id=" + _optional_scalar(contest.run_id) + "\n")

    output.write("\nHEURISTIC EV\n")
    if memo.heuristic_report is None:
        output.write("heuristic_ev_status=unavailable — no contest attached\n")
        output.write(HEURISTIC_NOTICE + "\n")
    else:
        output.write(render_heuristic_report(memo.heuristic_report))
    return output.getvalue()


def _validate_snapshot(expected: DecisionSnapshotRow, actual: DecisionSnapshotRow) -> None:
    expected_values = expected.model_dump(mode="python", exclude={"manifest_hashes_json"})
    actual_values = actual.model_dump(mode="python", exclude={"manifest_hashes_json"})
    expected_artifacts = {
        (item.artifact_kind, item.sha256, item.path, item.source)
        for item in expected.manifest_hashes_json
    }
    actual_artifacts = {
        (item.artifact_kind, item.sha256, item.path, item.source)
        for item in actual.manifest_hashes_json
    }
    if expected_values != actual_values or expected_artifacts != actual_artifacts:
        raise SlateMemoError("BuildResult snapshot does not match the stored decision snapshot")


def _validate_build_result(
    build_result: BuildResult,
    session: PointInTimeSession,
    slate: SlateRow,
) -> tuple[CandidateSelection, OwnershipRouting]:
    request = build_result.request
    if request.slate_id != slate.slate_id or request.site.value != slate.site:
        raise SlateMemoError("optimizer request does not match the point-in-time slate")
    if request.slate_type.value != slate.slate_type:
        raise SlateMemoError("optimizer request slate type does not match the stored slate")
    if not build_result.replay.report.output_matches:
        raise SlateMemoError("BuildResult replay did not match generated lineup bytes")
    if build_result.replay.request != request:
        raise SlateMemoError("BuildResult request differs from its verified replay request")
    if build_result.contest_policy != build_result.replay.contest_policy:
        raise SlateMemoError("BuildResult contest policy differs from its verified replay policy")
    if build_result.replay.lineups != build_result.lineups:
        raise SlateMemoError("BuildResult lineups differ from its verified replay lineups")

    salary_artifacts = _source_artifacts(build_result.snapshot, "salary")
    projection_artifacts = _source_artifacts(build_result.snapshot, "projection")
    availability_artifacts = frozenset(
        SelectedSourceArtifact(sha256=item.sha256, source=item.source or "")
        for item in build_result.snapshot.manifest_hashes_json
        if item.artifact_kind == "availability"
    )
    try:
        selected, routing = session.candidate_scenario(
            slate_id=slate.slate_id,
            site=DfsSite(slate.site),
            salary_artifacts=salary_artifacts,
            projection_artifacts=projection_artifacts,
            availability_artifacts=availability_artifacts,
            slate_type=slate.slate_type,
            contest_archetype=request.contest_archetype.value,
            ownership_routing=pinned_routing_from_manifest(
                build_result.snapshot.manifest_hashes_json
            ),
            ownership_config=ownership_config_from_manifest(
                build_result.snapshot.manifest_hashes_json, build_result.artifact_root
            ),
            as_of=build_result.snapshot.decision_at,
        )
    except (ReplayError, ValueError) as error:
        raise SlateMemoError(str(error)) from error
    recorded = build_result.ownership_routing
    if recorded.applied != routing.applied or recorded.scenario_run_id != routing.scenario_run_id:
        raise SlateMemoError(
            "the frozen decision's ownership routing cannot be reproduced from the store"
        )
    # The re-derivation proves the rows; the reason comes from the build's own record (a
    # live BuildResult, or the stored routing row a reload carries), because a replay of
    # an unrouted decision can only say "no set pinned" and that is not why.
    routing = replace(routing, reason=recorded.reason)
    if frozenset(selected.salary_artifacts) != salary_artifacts:
        raise SlateMemoError("not every salary manifest source/hash pair contributed memo rows")
    if frozenset(selected.projection_artifacts) != projection_artifacts:
        raise SlateMemoError("not every projection manifest source/hash pair contributed memo rows")
    scenario = request.candidate_player_scenario
    if selected.players != scenario.players:
        raise SlateMemoError(
            "optimizer candidate values cannot be reproduced from the frozen store rows"
        )
    if selected.projection_source_versions != scenario.projection_source_versions:
        raise SlateMemoError(
            "optimizer projection source versions cannot be reproduced from the store"
        )

    candidates = {player.player_id: player for player in selected.players}
    if len(build_result.lineups) != request.number_of_lineups:
        raise SlateMemoError("lineup count does not match the optimizer request")
    for lineup in build_result.lineups:
        if lineup.site != request.site or lineup.slate_id != request.slate_id:
            raise SlateMemoError(f"lineup {lineup.lineup_id} does not match the request")
        for player in lineup.players:
            candidate = candidates.get(player.player_id)
            if candidate is None or not _lineup_player_matches(
                player,
                candidate,
                site=request.site,
                slate_type=request.slate_type,
            ):
                raise SlateMemoError(
                    f"lineup player {player.player_id} cannot be reproduced from the store"
                )
    return selected, routing


def _memo_routing(
    session: PointInTimeSession,
    routing: OwnershipRouting,
    *,
    slate: SlateRow,
    decision_at: datetime,
    candidates: dict[int, CandidatePlayer],
    lineups: tuple[Lineup, ...],
    optimizer_reads_ownership: bool,
) -> SlateMemoOwnershipRouting:
    """Carry Stage 4's verdict and Stage 5's review into the memo, applied or not."""

    if not routing.applied:
        return SlateMemoOwnershipRouting(
            applied=False,
            reason=routing.reason,
            contest_archetype=routing.contest_archetype,
            role=routing.role,
        )
    red_team = build_red_team_review(
        session,
        routing,
        slate_id=slate.slate_id,
        site=slate.site,
        decision_at=decision_at,
        candidates=candidates,
        lineups=lineups,
        optimizer_reads_ownership=optimizer_reads_ownership,
        limit=RED_TEAM_LIMIT,
    )
    return SlateMemoOwnershipRouting(
        applied=True,
        reason=routing.reason,
        contest_archetype=routing.contest_archetype,
        role=routing.role,
        scenario_run_id=routing.scenario_run_id,
        scenario_decision_snapshot_id=routing.scenario_decision_snapshot_id,
        model_run_id=routing.model_run_id,
        model_version=routing.model_version,
        model_eval_id=routing.model_eval_id,
        governance_status=routing.governance_status,
        status_multiplier=routing.status_multiplier,
        config_sha256=routing.config_sha256,
        feature_version=routing.feature_version,
        scenario_set_sha256=routing.sha256,
        applied_row_count=len(routing.deltas),
        material_delta_count=len(routing.material_deltas),
        applied_deltas=tuple(
            _memo_delta(delta) for delta in routing.largest_deltas(RED_TEAM_LIMIT)
        ),
        red_team=red_team,
    )


def _memo_policy(
    policy: ContestPolicy,
    *,
    policy_version: str,
    policy_sha256: str,
    contest_archetype: str,
) -> SlateMemoContestPolicy:
    points = policy.ownership_sum_points
    return SlateMemoContestPolicy(
        policy_version=policy_version,
        policy_sha256=policy_sha256,
        contest_archetype=contest_archetype,
        ownership_sum_points_min=None if points is None else points.min,
        ownership_sum_points_max=None if points is None else points.max,
        lineup_uniqueness=policy.lineup_uniqueness,
        max_player_exposure=policy.max_player_exposure,
        objective=policy.objective,
    )


def _memo_delta(delta: AppliedOwnershipDelta) -> SlateMemoAppliedDelta:
    return SlateMemoAppliedDelta(
        player_id=delta.player_id,
        role=delta.role,
        position=delta.position,
        baseline_ownership=delta.baseline_ownership,
        applied_ownership=delta.applied_ownership,
        delta_points=round(delta.delta_points, 6),
        ownership_p10=delta.ownership_p10,
        ownership_p50=delta.ownership_p50,
        ownership_p90=delta.ownership_p90,
        prob_delta_positive=delta.prob_delta_positive,
        feature_id=delta.feature_id,
        episode_ids=delta.episode_ids,
        evidence_refs=tuple(
            f"{ref.episode_id}/{ref.claim_id}/{ref.source_id}/item-{ref.source_item_id}"
            f"[{ref.extract_start}:{ref.extract_end}]"
            for ref in delta.evidence_refs
        ),
    )


def _lineup_player_matches(
    player: LineupPlayer,
    candidate: CandidatePlayer,
    *,
    site: DfsSite,
    slate_type: SlateType,
) -> bool:
    captain = slate_type is SlateType.SHOWDOWN and player.slot in {"CPT", "MVP"}
    points_multiplier = SHOWDOWN_SITE_RULES[site].captain_points_multiplier if captain else 1.0
    salary_multiplier = SHOWDOWN_SITE_RULES[site].captain_salary_multiplier if captain else 1.0
    return (
        player.site_player_id == candidate.site_player_id
        and player.name == candidate.name
        and player.team == candidate.team
        and player.opponent == candidate.opponent
        and player.position == candidate.position
        and player.salary == round(candidate.salary * salary_multiplier)
        and player.projection == round(candidate.projection * points_multiplier, 6)
        and player.projected_ownership == candidate.projected_ownership
        and player.projected_ownership_captain == candidate.projected_ownership_captain
        and player.game_id == candidate.game_id
        and player.slot in candidate.eligible_roster_slots
    )


def _input_artifacts(
    snapshot: DecisionSnapshotRow,
) -> tuple[SlateMemoInputArtifact, ...]:
    artifacts: list[SlateMemoInputArtifact] = []
    kinds: set[str] = set()
    for item in snapshot.manifest_hashes_json:
        if item.artifact_kind not in {"salary", "projection"}:
            continue
        if item.source is None or not item.source.strip():
            raise SlateMemoError(
                f"{item.artifact_kind} manifest artifact {item.sha256} has no source"
            )
        artifacts.append(
            SlateMemoInputArtifact(
                artifact_kind=item.artifact_kind,
                source=item.source,
                sha256=item.sha256,
                logical_path=item.path,
            )
        )
        kinds.add(item.artifact_kind)
    if kinds != {"salary", "projection"}:
        raise SlateMemoError("decision manifest requires salary and projection artifacts")
    return tuple(
        sorted(
            artifacts,
            key=lambda item: (
                item.artifact_kind,
                item.source,
                item.sha256,
                item.logical_path,
            ),
        )
    )


def _load_contest(
    session: PointInTimeSession,
    *,
    contest_id: int,
    slate: SlateRow,
    as_of: datetime,
) -> ContestRow:
    if isinstance(contest_id, bool) or contest_id < 1:
        raise SlateMemoError("contest_id must be a positive integer")
    rows = session.query(
        """
        SELECT *
        FROM contests
        WHERE contest_id = :contest_id
          AND slate_id = :slate_id
          AND site = :site
          AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (
              valid_to IS NULL
              OR rtrim(valid_to, 'Z') > rtrim(:as_of, 'Z')
          )
        """,
        {
            "contest_id": contest_id,
            "slate_id": slate.slate_id,
            "site": slate.site,
        },
        as_of=as_of,
    )
    if len(rows) != 1:
        raise SlateMemoError(
            f"contest {contest_id} is unavailable for this site/slate at the cutoff"
        )
    return ContestRow.from_db(rows[0])


def _optional_timestamp(value: datetime | None) -> str:
    return "unavailable" if value is None else utc_timestamp(value)


def _optional_scalar(value: int | str | None) -> str:
    return "unavailable" if value is None else str(value)


def _memo_lineup(lineup: Lineup) -> SlateMemoLineup:
    ownership = tuple(
        player.projected_ownership_captain
        if player.slot in {"CPT", "MVP"}
        else player.projected_ownership
        for player in lineup.players
    )
    ownership_sum = (
        None
        if any(value is None for value in ownership)
        else round(math.fsum(value for value in ownership if value is not None), 6)
    )
    return SlateMemoLineup(
        lineup_id=lineup.lineup_id,
        total_projection=lineup.total_projection,
        total_salary=lineup.total_salary,
        projected_ownership_sum=ownership_sum,
        players=lineup.players,
    )


def _source_artifacts(
    snapshot: DecisionSnapshotRow,
    artifact_kind: Literal["salary", "projection"],
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
    if not artifacts:
        raise ReplayArtifactError(f"decision manifest has no {artifact_kind} artifacts")
    return artifacts
