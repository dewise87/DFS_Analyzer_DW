"""Deterministic, evidence-backed human memo for one production slate decision."""

from __future__ import annotations

import csv
import io
import math
import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from narrative_alpha.candidate_selection import SelectedSourceArtifact
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.portfolio import (
    HEURISTIC_NOTICE,
    CandidatePlayer,
    DfsSite,
    HeuristicReport,
    HeuristicThresholds,
    Lineup,
    LineupPlayer,
    build_heuristic_report,
    render_heuristic_report,
)
from narrative_alpha.replay import PointInTimeSession, ReplayArtifactError, ReplayError
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


class SlateMemoLineup(BaseModel):
    """One reproducible lineup summary plus its exact ordered roster."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lineup_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    total_projection: float = Field(ge=0, allow_inf_nan=False)
    total_salary: int = Field(gt=0)
    projected_ownership_sum: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    players: tuple[LineupPlayer, ...] = Field(min_length=1)


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
    _validate_build_result(build_result, session, slate)

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
) -> None:
    request = build_result.request
    if request.slate_id != slate.slate_id or request.site.value != slate.site:
        raise SlateMemoError("optimizer request does not match the point-in-time slate")
    if request.slate_type.value != slate.slate_type:
        raise SlateMemoError("optimizer request slate type does not match the stored slate")
    if not build_result.replay.report.output_matches:
        raise SlateMemoError("BuildResult replay did not match generated lineup bytes")
    if build_result.replay.request != request:
        raise SlateMemoError("BuildResult request differs from its verified replay request")
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
        selected = session.candidate_scenario(
            slate_id=slate.slate_id,
            site=DfsSite(slate.site),
            salary_artifacts=salary_artifacts,
            projection_artifacts=projection_artifacts,
            availability_artifacts=availability_artifacts,
            as_of=build_result.snapshot.decision_at,
        )
    except (ReplayError, ValueError) as error:
        raise SlateMemoError(str(error)) from error
    if frozenset(selected.salary_artifacts) != salary_artifacts:
        raise SlateMemoError(
            "not every salary manifest source/hash pair contributed memo rows"
        )
    if frozenset(selected.projection_artifacts) != projection_artifacts:
        raise SlateMemoError(
            "not every projection manifest source/hash pair contributed memo rows"
        )
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
            if candidate is None or not _lineup_player_matches(player, candidate):
                raise SlateMemoError(
                    f"lineup player {player.player_id} cannot be reproduced from the store"
                )


def _lineup_player_matches(player: LineupPlayer, candidate: CandidatePlayer) -> bool:
    return (
        player.site_player_id == candidate.site_player_id
        and player.name == candidate.name
        and player.team == candidate.team
        and player.opponent == candidate.opponent
        and player.position == candidate.position
        and player.salary == candidate.salary
        and player.projection == candidate.projection
        and player.projected_ownership == candidate.projected_ownership
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
    ownership = tuple(player.projected_ownership for player in lineup.players)
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
