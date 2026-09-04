"""Stage 4 channel routing: governed ownership scenarios reaching the optimizer.

§5.3 Stage 4 says a model may propose channel effects but *deterministic permissions and
magnitude caps* govern what is applied. Slice 29 produced the proposals: capped,
roster-calibrated `ownership_scenarios` rows that nothing consumed. This module is the
permission layer between those rows and `candidate_selection`.

Three rules decide whether a scenario set replaces the vendor ownership:

1. a scenario set for this slate/site/archetype exists as of ``decision_at``;
2. its matching out-of-week evaluation record says the model beat the untouched vendor
   baseline (§12.2.7 item 8 — otherwise the baseline is the answer);
3. every material applied delta traces to a narrative episode and its evidence (§8.3).

Rules 1 and 2 fall back to the vendor baseline with a stated reason the memo prints. Rule
3 refuses: a scenario set that moved a player materially with no episode behind it is a
broken set, and a silent revert would hide that (§1.5 rule 7).

This module deliberately imports nothing from ``narrative_alpha.ownership``: that package
reaches ``ops.results`` → ``report_cli`` → ``build``, and ``build`` imports this module. The
threshold and the caps are not mirrored here — :mod:`narrative_alpha.ownership_config` is a
leaf both sides import, and :data:`ROUTING_CONFIG` is the one copy of
``config/ownership_model.toml`` this process reads.

It is also where the two reads Stage 4 shares with the audit view live: the evaluation gate
(:func:`latest_evaluation_status`) and the provenance join (:func:`episode_provenance`),
public so ``narrative/audit.py`` renders from the same rows the routing decided on.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from posixpath import basename
from typing import Literal, cast

from narrative_alpha.candidate_selection import (
    CandidateSelection,
    PointInTimeQuery,
    SelectedSourceArtifact,
    select_candidate_scenario,
)
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.ownership_config import (
    OwnershipModelConfig,
    load_ownership_config,
)
from narrative_alpha.portfolio import DfsSite

MANIFEST_ARTIFACT_KIND: Literal["ownership_scenarios"] = "ownership_scenarios"
MANIFEST_PATH_PREFIX = "store/ownership_scenarios/"
SCENARIO_SET_SCHEMA_VERSION = "ownership-routing-v1"

#: The shipped ownership configuration, read once at import from the path every lane
#: already resolves against the repository root. Stage 4's material threshold and its
#: magnitude caps are that file's bytes, not a copy of them: a routing that read a stale
#: mirror would permit a move the model's own governance forbids.
ROUTING_CONFIG: OwnershipModelConfig | None = None


def routing_config() -> OwnershipModelConfig:
    """The shipped configuration, read on first use and kept for the process.

    Read lazily rather than at import: this module is under `build`, which nearly every
    command imports, and a typo in the ownership file must break a build loudly — not
    `na-ops status`.
    """

    global ROUTING_CONFIG
    if ROUTING_CONFIG is None:
        ROUTING_CONFIG = load_ownership_config()
    return ROUTING_CONFIG


#: A routed decision freezes the configuration bytes it was governed under beside its
#: other artifacts, so a later edit to the shipped file cannot make it unreplayable.
OWNERSHIP_CONFIG_ARTIFACT_KIND: Literal["ownership_config"] = "ownership_config"
#: Named constants keep the classic compatibility surface while Stage 4 chooses the
#: configured cap family from the slate it is actually routing.
ROUTING_SLATE_KIND: Literal["classic"] = "classic"
SHOWDOWN_ROUTING_SLATE_KIND: Literal["showdown"] = "showdown"
# Float tolerance for the two comparisons below: a cap of 0.02 and a delta of exactly
# 0.02 must not trip on the last bit of a subtraction.
DELTA_TOLERANCE = 1e-9


def material_delta(config: OwnershipModelConfig | None = None) -> float:
    """The configured threshold above which a move is a claim that must cite evidence.

    Below it a move is the roster-total calibration's mechanical wobble (§12.2.6), which
    cites no episode because it asserts nothing about that player.
    """

    return (config or routing_config()).evaluation.material_delta


class OwnershipRoutingError(RuntimeError):
    """Raised when a scenario set cannot be routed to the optimizer safely."""


@dataclass(frozen=True)
class EvaluationVerdict:
    """The newest out-of-week evaluation of one model configuration, and what it said."""

    model_eval_id: str
    beat_baseline: bool


@dataclass(frozen=True, order=True)
class EvidenceRef:
    """One Stage 1 evidence excerpt behind one episode behind one applied delta."""

    episode_id: str
    claim_id: str
    relation: str
    source_id: str
    source_family: str
    source_item_id: int
    source_text_sha256: str
    extract_start: int
    extract_end: int
    verbatim_extract: str | None


@dataclass(frozen=True)
class ProvenanceEvidence:
    """One verbatim excerpt behind one claim, with the offsets that locate it."""

    ordinal: int
    source_item_id: int
    source_text_sha256: str
    extract_start: int
    extract_end: int
    verbatim_extract: str | None
    observed_at: str


@dataclass(frozen=True)
class ProvenanceClaim:
    """One Stage 1 claim inside one episode, with its taxonomy and its excerpts."""

    claim_id: str
    relation: str
    similarity_score: float
    linked_claim_id: str | None
    source_id: str
    source_family: str
    source_item_id: int
    claim_type: str
    claim_dimension: str
    outcome_direction: str
    roster_behavior_direction: str
    evidence_class: str
    evidence_basis: str
    falsifiable: bool
    item_title: str | None
    item_observed_at: str
    evidence: tuple[ProvenanceEvidence, ...]


@dataclass(frozen=True)
class ProvenanceEpisode:
    """One Stage 2 episode as of a cutoff, with everything claimed inside it."""

    episode_id: str
    claim_dimension: str
    opened_at: str
    last_item_at: str
    as_of: str
    method_version: str
    window_hours: float
    unique_source_count: int
    unique_source_family_count: int
    source_entropy: float
    velocity_per_6h: float
    recency_hours: float
    n_events: int
    item_count: int
    claims: tuple[ProvenanceClaim, ...]

    @property
    def evidence_refs(self) -> tuple[EvidenceRef, ...]:
        """This episode's excerpts flattened to what an applied delta must cite (§8.3)."""

        return tuple(
            EvidenceRef(
                episode_id=self.episode_id,
                claim_id=claim.claim_id,
                relation=claim.relation,
                source_id=claim.source_id,
                source_family=claim.source_family,
                source_item_id=excerpt.source_item_id,
                source_text_sha256=excerpt.source_text_sha256,
                extract_start=excerpt.extract_start,
                extract_end=excerpt.extract_end,
                verbatim_extract=excerpt.verbatim_extract,
            )
            for claim in self.claims
            for excerpt in claim.evidence
        )


@dataclass(frozen=True)
class PlayerProvenance:
    """One player's feature row and the episodes and excerpts standing behind it."""

    feature_id: str
    episode_ids: tuple[str, ...]
    episodes: tuple[ProvenanceEpisode, ...]
    evidence_refs: tuple[EvidenceRef, ...]


@dataclass(frozen=True)
class AppliedOwnershipDelta:
    """One player's governed move from the vendor baseline, with its provenance."""

    player_id: int
    role: str
    position: str
    baseline_ownership: float
    applied_ownership: float
    ownership_p10: float
    ownership_p50: float
    ownership_p90: float
    delta_p50: float
    prob_delta_positive: float
    feature_id: str
    episode_ids: tuple[str, ...]
    evidence_refs: tuple[EvidenceRef, ...]
    # What the scenario set proposed. Equal to `applied_ownership` unless the routing
    # held this player at the vendor baseline because the move cited no episode.
    proposed_ownership: float | None = None
    held_at_baseline: bool = False
    # The threshold this delta was judged against: the configuration the decision froze,
    # so a replay years later reads the same "material" the build did.
    material_threshold: float | None = None

    @property
    def delta(self) -> float:
        return self.applied_ownership - self.baseline_ownership

    @property
    def delta_points(self) -> float:
        return self.delta * 100.0

    @property
    def material(self) -> bool:
        threshold = material_delta() if self.material_threshold is None else self.material_threshold
        return abs(self.delta) > threshold + DELTA_TOLERANCE


@dataclass(frozen=True)
class PinnedOwnershipRouting:
    """What a frozen decision's manifest recorded about Stage 4 routing.

    ``scenario_run_id`` of ``None`` is not "unknown" — it is the positive statement that
    the decision applied the vendor baseline, so replay must not go looking for a set
    that landed afterwards.
    """

    scenario_run_id: str | None
    sha256: str | None


#: The manifest record of a decision that applied the vendor baseline. It is a positive
#: statement, not "unknown", so a replay of such a decision never goes looking for a
#: scenario set that landed afterwards.
NO_PINNED_ROUTING = PinnedOwnershipRouting(scenario_run_id=None, sha256=None)


@dataclass(frozen=True)
class OwnershipRouting:
    """What Stage 4 decided for one decision, and why, in the memo's own words."""

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
    status_multiplier: float | None = None
    config_sha256: str | None = None
    feature_version: str | None = None
    scenario_source: str | None = None
    generated_at: str | None = None
    feature_as_of: str | None = None
    deltas: tuple[AppliedOwnershipDelta, ...] = ()

    @property
    def sha256(self) -> str:
        """Hash the exact rows this routing applied, for the decision manifest."""

        payload = {
            "schema": SCENARIO_SET_SCHEMA_VERSION,
            "contest_archetype": self.contest_archetype,
            "role": self.role,
            "scenario_run_id": self.scenario_run_id,
            "scenario_decision_snapshot_id": self.scenario_decision_snapshot_id,
            "model_run_id": self.model_run_id,
            "model_eval_id": self.model_eval_id,
            "governance_status": self.governance_status,
            "status_multiplier": self.status_multiplier,
            "config_sha256": self.config_sha256,
            "feature_version": self.feature_version,
            "rows": [
                {
                    "player_id": delta.player_id,
                    "role": delta.role,
                    "baseline_ownership": delta.baseline_ownership,
                    "applied_ownership": delta.applied_ownership,
                    "held_at_baseline": delta.held_at_baseline,
                }
                for delta in self.deltas
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def manifest_path(self) -> str:
        return f"{MANIFEST_PATH_PREFIX}{self.scenario_run_id}"

    @property
    def material_deltas(self) -> tuple[AppliedOwnershipDelta, ...]:
        return tuple(delta for delta in self.deltas if delta.material)

    @property
    def held_deltas(self) -> tuple[AppliedOwnershipDelta, ...]:
        return tuple(delta for delta in self.deltas if delta.held_at_baseline)

    def largest_deltas(self, limit: int) -> tuple[AppliedOwnershipDelta, ...]:
        """The ``limit`` largest applied moves by absolute points, ties broken by id."""

        ordered = sorted(
            self.deltas,
            key=lambda delta: (-abs(delta.delta), delta.player_id, delta.role),
        )
        return tuple(ordered[:limit])


@dataclass(frozen=True)
class RoutedCandidateSelection:
    """The candidates the optimizer sees, and the routing that shaped their ownership."""

    selection: CandidateSelection
    routing: OwnershipRouting


def select_routed_candidate_scenario(
    session: PointInTimeQuery,
    *,
    slate_id: int,
    site: DfsSite,
    slate_type: str,
    contest_archetype: str,
    as_of: datetime,
    salary_artifacts: frozenset[SelectedSourceArtifact] | None = None,
    projection_artifacts: frozenset[SelectedSourceArtifact] | None = None,
    availability_artifacts: frozenset[SelectedSourceArtifact] | None = None,
    pinned: PinnedOwnershipRouting | None = None,
    config: OwnershipModelConfig | None = None,
) -> RoutedCandidateSelection:
    """Select point-in-time candidates, then route governed ownership onto them.

    ``config`` is the ownership configuration to govern under: the one a replayed
    decision froze, or the shipped file when a build is discovering a set now.

    Build leaves ``pinned`` unset and discovers the newest eligible scenario set; replay
    passes exactly what the manifest froze, so the same rows come back and the request
    bytes are identical.
    """

    selection = select_candidate_scenario(
        session,
        slate_id=slate_id,
        site=site,
        slate_type=slate_type,
        as_of=as_of,
        salary_artifacts=salary_artifacts,
        projection_artifacts=projection_artifacts,
        availability_artifacts=availability_artifacts,
    )
    routing = select_ownership_routing(
        session,
        slate_id=slate_id,
        site=site,
        slate_type=slate_type,
        contest_archetype=contest_archetype,
        as_of=as_of,
        candidate_player_ids=frozenset(player.player_id for player in selection.players),
        pinned=pinned,
        config=config,
    )
    if not routing.applied:
        return RoutedCandidateSelection(selection=selection, routing=routing)

    applied = {(delta.player_id, delta.role): delta.applied_ownership for delta in routing.deltas}
    if slate_type == "showdown":
        players = tuple(
            player.model_copy(
                update={
                    "projected_ownership": applied[(player.player_id, "flex")],
                    "projected_ownership_captain": applied[(player.player_id, "captain")],
                }
            )
            for player in selection.players
        )
    else:
        players = tuple(
            player.model_copy(
                update={"projected_ownership": applied[(player.player_id, "classic")]}
            )
            for player in selection.players
        )
    return RoutedCandidateSelection(
        selection=CandidateSelection(
            players=players,
            projection_source_versions=selection.projection_source_versions,
            salary_artifacts=selection.salary_artifacts,
            projection_artifacts=selection.projection_artifacts,
            availability_artifacts=selection.availability_artifacts,
        ),
        routing=routing,
    )


def select_ownership_routing(
    session: PointInTimeQuery,
    *,
    slate_id: int,
    site: DfsSite,
    slate_type: str,
    contest_archetype: str,
    as_of: datetime,
    candidate_player_ids: frozenset[int],
    pinned: PinnedOwnershipRouting | None = None,
    config: OwnershipModelConfig | None = None,
) -> OwnershipRouting:
    """Decide whether a scenario set may replace the vendor ownership, and say why."""

    governance = config or routing_config()
    pinned_run_id = None if pinned is None else pinned.scenario_run_id
    if slate_type not in {ROUTING_SLATE_KIND, SHOWDOWN_ROUTING_SLATE_KIND}:
        raise OwnershipRoutingError(f"unsupported ownership routing slate type {slate_type!r}")
    roles = ("classic",) if slate_type == ROUTING_SLATE_KIND else ("captain", "flex")
    role = roles[0] if len(roles) == 1 else "captain+flex"
    if pinned is not None and pinned_run_id is None:
        return _baseline(
            contest_archetype,
            role,
            "the frozen decision manifest carries no ownership scenario set, so the "
            "vendor baseline was applied",
        )

    header = _scenario_set(
        session,
        slate_id=slate_id,
        site=site.value,
        contest_archetype=contest_archetype,
        roles=roles,
        as_of=as_of,
        pinned_run_id=pinned_run_id,
    )
    if header is None:
        if pinned_run_id is not None:
            raise OwnershipRoutingError(
                f"ownership scenario set {pinned_run_id!r} named by the decision manifest is "
                f"not available as of {utc_timestamp(as_of)}"
            )
        return _baseline(
            contest_archetype,
            role,
            f"no ownership scenario set exists for slate {slate_id} {site.value} "
            f"{contest_archetype} as of the decision; the vendor baseline was applied",
        )

    if str(header["config_sha256"]) != governance.config_sha256:
        # The set was capped and calibrated under one configuration; Stage 4 re-asserts
        # caps under another. Governing rows under a configuration they were not written
        # under is not a permission check, so refuse. A build says regenerate; a replay
        # cannot get here, because it governs under the bytes the decision froze.
        raise OwnershipRoutingError(
            f"ownership scenario set {str(header['run_id'])!r} was written under ownership "
            f"configuration {str(header['config_sha256'])!r}, but Stage 4 is governing "
            f"under {governance.config_sha256!r} ({governance.config_version}); "
            + (
                "the frozen decision cannot be routed under a different configuration"
                if pinned_run_id is not None
                else "regenerate the set with `na-ownership scenarios` under the current file"
            )
        )

    evaluation = latest_evaluation_status(
        session,
        site=site.value,
        contest_archetype=contest_archetype,
        feature_version=str(header["feature_version"]),
        config_sha256=str(header["config_sha256"]),
        as_of=as_of,
    )
    if evaluation is None:
        return _refuse_or_baseline(
            pinned_run_id,
            contest_archetype,
            role,
            "no out-of-week ownership evaluation exists for this model and configuration, "
            "so nothing has shown it beats the vendor baseline; the vendor baseline was "
            "applied",
        )
    if not evaluation.beat_baseline:
        return _refuse_or_baseline(
            pinned_run_id,
            contest_archetype,
            role,
            f"the newest out-of-week evaluation {evaluation.model_eval_id!r} did not "
            "beat the untouched vendor baseline; the vendor baseline was applied",
        )

    feature_as_of = _scenario_feature_instant(
        session,
        decision_snapshot_id=str(header["decision_snapshot_id"]),
        as_of=as_of,
    )
    collected: list[AppliedOwnershipDelta] = []
    for routed_role in roles:
        rows = _scenario_rows(
            session,
            run_id=str(header["run_id"]),
            role=routed_role,
            as_of=as_of,
        )
        covered = {int(row["player_id"]) for row in rows}
        uncovered = sorted(candidate_player_ids - covered)
        if uncovered:
            # Showdown's captain and flex totals are calibrated separately, so either
            # role being partial sends the whole set back to its vendor baselines.
            return _refuse_or_baseline(
                pinned_run_id,
                contest_archetype,
                role,
                f"ownership scenario set {str(header['run_id'])!r} {routed_role} role "
                f"covers {len(covered & candidate_player_ids)} of "
                f"{len(candidate_player_ids)} candidate player(s) — missing "
                f"{_listed_ids(uncovered)}; the vendor baseline was applied",
            )
        provenance = feature_provenance(
            session,
            slate_id=slate_id,
            site=site.value,
            role=routed_role,
            feature_as_of=feature_as_of,
            feature_version=str(header["feature_version"]),
            as_of=as_of,
        )
        collected.extend(
            _delta(
                row,
                provenance.get(int(row["player_id"])),
                threshold=governance.evaluation.material_delta,
            )
            for row in rows
            if int(row["player_id"]) in candidate_player_ids
        )
    deltas = tuple(collected)
    governance_status = str(header["governance_status"])
    capped = governance.cap_for(slate_type, governance_status)
    if capped is None:
        raise OwnershipRoutingError(
            f"ownership scenario set {str(header['run_id'])!r} carries governance status "
            f"{governance_status!r}, which Stage 4 has no cap for"
        )
    cap = capped.maximum_delta
    # The permission layer re-asserts the magnitude cap (§12.2.5) on the stored rows: a
    # row that moved past its own status's cap is a broken set, whoever wrote it.
    over_cap = tuple(delta for delta in deltas if abs(delta.delta) > cap + DELTA_TOLERANCE)
    if over_cap:
        raise OwnershipRoutingError(
            f"ownership scenario set {str(header['run_id'])!r} moves {len(over_cap)} "
            f"player-role row(s) past the {governance_status} cap of "
            f"{cap * 100:.1f} point(s) for {slate_type}: "
            + ", ".join(
                f"player {delta.player_id} {delta.role} {delta.delta_points:+.2f}pt"
                for delta in over_cap[:10]
            )
        )
    # A material move whose feature row cites episodes that resolve to no evidence is a
    # broken set: the provenance chain exists on paper and not in the store.
    broken = tuple(
        delta
        for delta in deltas
        if delta.material and delta.episode_ids and not delta.evidence_refs
    )
    if broken:
        raise OwnershipRoutingError(
            f"ownership scenario set {str(header['run_id'])!r} moves {len(broken)} player(s) "
            f"by more than {governance.evaluation.material_delta * 100:.1f} point(s) on "
            "episodes with no evidence "
            "excerpt behind them, so the delta cannot be traced to evidence (§8.3): "
            + ", ".join(
                f"player {delta.player_id} {delta.delta_points:+.2f}pt" for delta in broken[:10]
            )
        )
    # A material move with no episode at all is not a claim about the player: it is the
    # intercept, the standardization of "no heat", and the roster-total calibration
    # (§12.2.6) landing on someone the narrative never touched. Nothing is applied without
    # provenance (§8.3), so that player stays at the vendor baseline — and the routing
    # says so, rather than refusing the whole slate for a move the model did not mean.
    deltas = tuple(
        replace(delta, applied_ownership=delta.baseline_ownership, held_at_baseline=True)
        if delta.material and not delta.episode_ids
        else delta
        for delta in deltas
    )
    held = tuple(delta for delta in deltas if delta.held_at_baseline)
    held_note = (
        ""
        if not held
        else (
            f"; {len(held)} player-role row(s) held at the vendor baseline because the set "
            "moved them "
            f"more than {governance.evaluation.material_delta * 100:.1f} point(s) with no "
            "narrative episode behind "
            "the move: "
            + ", ".join(
                f"player {delta.player_id} {delta.role} "
                f"{((delta.proposed_ownership or 0.0) - delta.baseline_ownership) * 100:+.2f}pt"
                for delta in held[:10]
            )
        )
    )
    return OwnershipRouting(
        applied=True,
        reason=(
            f"applied ownership scenario set {str(header['run_id'])!r} at governance status "
            f"{governance_status} "
            f"(multiplier {float(header['status_multiplier']):.2f}); evaluation "
            f"{evaluation.model_eval_id!r} beat the untouched vendor baseline" + held_note
        ),
        contest_archetype=contest_archetype,
        role=role,
        scenario_run_id=str(header["run_id"]),
        scenario_decision_snapshot_id=str(header["decision_snapshot_id"]),
        model_run_id=str(header["model_run_id"]),
        model_version=str(header["model_version"]),
        model_eval_id=evaluation.model_eval_id,
        governance_status=str(header["governance_status"]),
        status_multiplier=float(header["status_multiplier"]),
        config_sha256=str(header["config_sha256"]),
        feature_version=str(header["feature_version"]),
        scenario_source=str(header["source"]),
        generated_at=str(header["observed_at"]),
        feature_as_of=feature_as_of,
        deltas=deltas,
    )


def pinned_routing_from_manifest(
    manifest: Sequence[object],
) -> PinnedOwnershipRouting:
    """Read the frozen decision's Stage 4 record, absence included, from its manifest.

    ``manifest`` items are ``DecisionManifestHash`` rows; they are typed structurally so
    this module stays below the store models in the import graph.
    """

    artifacts = [
        item for item in manifest if getattr(item, "artifact_kind", None) == MANIFEST_ARTIFACT_KIND
    ]
    if not artifacts:
        return NO_PINNED_ROUTING
    if len(artifacts) > 1:
        raise OwnershipRoutingError("decision manifest names more than one ownership scenario set")
    path = str(getattr(artifacts[0], "path", ""))
    if not path.startswith(MANIFEST_PATH_PREFIX):
        raise OwnershipRoutingError(
            f"ownership scenario manifest path {path!r} is not under {MANIFEST_PATH_PREFIX!r}"
        )
    run_id = basename(path)
    if not run_id:
        raise OwnershipRoutingError("ownership scenario manifest path names no run")
    return PinnedOwnershipRouting(
        scenario_run_id=run_id,
        sha256=str(getattr(artifacts[0], "sha256", "")),
    )


def verify_pinned_routing(routing: OwnershipRouting, pinned: PinnedOwnershipRouting) -> None:
    """Fail closed when replayed routing is not byte-for-byte what was frozen."""

    if pinned.scenario_run_id is None:
        if routing.applied:
            raise OwnershipRoutingError(
                "the frozen decision applied the vendor baseline but replay routed a "
                f"scenario set ({routing.scenario_run_id})"
            )
        return
    if not routing.applied or routing.scenario_run_id != pinned.scenario_run_id:
        raise OwnershipRoutingError(
            f"replay did not route the frozen ownership scenario set "
            f"{pinned.scenario_run_id!r}: {routing.reason}"
        )
    if pinned.sha256 is not None and routing.sha256 != pinned.sha256:
        raise OwnershipRoutingError(
            f"ownership scenario set {pinned.scenario_run_id!r} does not hash to the "
            "decision manifest's recorded value"
        )


def _baseline(contest_archetype: str, role: str, reason: str) -> OwnershipRouting:
    return OwnershipRouting(
        applied=False, reason=reason, contest_archetype=contest_archetype, role=role
    )


def _refuse_or_baseline(
    pinned_run_id: str | None, contest_archetype: str, role: str, reason: str
) -> OwnershipRouting:
    """Fall back for a fresh build; refuse when a frozen decision says it was applied."""

    if pinned_run_id is not None:
        raise OwnershipRoutingError(
            f"the frozen decision applied ownership scenario set {pinned_run_id!r}, but "
            f"replay cannot: {reason}"
        )
    return _baseline(contest_archetype, role, reason)


def _listed_ids(values: Sequence[int], limit: int = 10) -> str:
    shown = ", ".join(str(value) for value in values[:limit])
    return shown if len(values) <= limit else f"{shown}, +{len(values) - limit} more"


def _scenario_set(
    session: PointInTimeQuery,
    *,
    slate_id: int,
    site: str,
    contest_archetype: str,
    roles: tuple[str, ...],
    as_of: datetime,
    pinned_run_id: str | None,
) -> sqlite3.Row | None:
    rows = session.query(
        """
        SELECT DISTINCT os.run_id, os.decision_snapshot_id, os.contest_archetype, os.role,
               os.governance_status, os.status_multiplier, os.model_run_id,
               os.model_version, os.config_sha256, os.feature_version, os.source,
               os.observed_at
        FROM ownership_scenarios AS os
        JOIN model_runs AS run ON run.run_id = os.run_id
        WHERE os.slate_id = :slate_id AND os.site = :site
          AND os.contest_archetype = :contest_archetype
          AND (:pinned_run_id IS NULL OR os.run_id = :pinned_run_id)
          AND run.status = 'succeeded'
          AND rtrim(os.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(os.created_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(run.completed_at, 'Z') <= rtrim(:as_of, 'Z')
        ORDER BY rtrim(os.observed_at, 'Z') DESC, os.run_id DESC
        """,
        {
            "slate_id": slate_id,
            "site": site,
            "contest_archetype": contest_archetype,
            "pinned_run_id": pinned_run_id,
        },
        as_of=as_of,
    )
    required_roles = set(roles)
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["run_id"]), []).append(row)
    for run_rows in grouped.values():
        if {str(row["role"]) for row in run_rows} >= required_roles:
            shared = {
                (
                    str(row["decision_snapshot_id"]),
                    str(row["governance_status"]),
                    float(row["status_multiplier"]),
                    str(row["model_run_id"]),
                    str(row["model_version"]),
                    str(row["config_sha256"]),
                    str(row["feature_version"]),
                    str(row["source"]),
                    str(row["observed_at"]),
                )
                for row in run_rows
                if str(row["role"]) in required_roles
            }
            if len(shared) != 1:
                raise OwnershipRoutingError(
                    f"ownership scenario set {str(run_rows[0]['run_id'])!r} has "
                    "inconsistent captain/flex provenance"
                )
            return run_rows[0]
    return None


def latest_evaluation_status(
    session: PointInTimeQuery,
    *,
    site: str,
    contest_archetype: str,
    feature_version: str,
    config_sha256: str,
    as_of: datetime,
) -> EvaluationVerdict | None:
    """The newest out-of-week ownership evaluation for this model and configuration.

    The one gate: Stage 4 asks it before routing a set to the optimizer, `na-ownership
    scenarios` asks it before writing rows at all, and the audit view asks it to say which
    number reached the optimizer. ``None`` means no evaluation exists yet — which is not
    the same as a losing one, and the three callers say so in their own words.
    """

    rows = session.query(
        """
        SELECT model_eval_id, beat_baseline
        FROM model_evals
        WHERE evaluation_kind = 'ownership' AND ownership_site = :site
          AND ownership_archetype = :contest_archetype
          AND feature_version = :feature_version AND config_sha256 = :config_sha256
          AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(:as_of, 'Z'))
        ORDER BY rtrim(observed_at, 'Z') DESC, model_eval_id DESC
        LIMIT 1
        """,
        {
            "site": site,
            "contest_archetype": contest_archetype,
            "feature_version": feature_version,
            "config_sha256": config_sha256,
        },
        as_of=as_of,
    )
    if not rows:
        return None
    return EvaluationVerdict(
        model_eval_id=str(rows[0]["model_eval_id"]),
        beat_baseline=bool(rows[0]["beat_baseline"]),
    )


def _scenario_rows(
    session: PointInTimeQuery, *, run_id: str, role: str, as_of: datetime
) -> tuple[sqlite3.Row, ...]:
    return session.query(
        """
        SELECT player_id, role, position, baseline_ownership, applied_ownership,
               ownership_p10, ownership_p50, ownership_p90, delta_p50,
               prob_delta_positive
        FROM ownership_scenarios
        WHERE run_id = :run_id AND role = :role
          AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(created_at, 'Z') <= rtrim(:as_of, 'Z')
        ORDER BY player_id
        """,
        {"run_id": run_id, "role": role},
        as_of=as_of,
    )


def _scenario_feature_instant(
    session: PointInTimeQuery, *, decision_snapshot_id: str, as_of: datetime
) -> str:
    rows = session.query(
        """
        SELECT decision_at
        FROM decision_snapshots
        WHERE decision_snapshot_id = :decision_snapshot_id
          AND rtrim(decision_at, 'Z') <= rtrim(:as_of, 'Z')
        """,
        {"decision_snapshot_id": decision_snapshot_id},
        as_of=as_of,
    )
    if len(rows) != 1:
        raise OwnershipRoutingError(
            f"ownership scenarios cite decision {decision_snapshot_id!r}, which is not "
            "available at this cutoff"
        )
    return str(rows[0]["decision_at"])


def feature_provenance(
    session: PointInTimeQuery,
    *,
    slate_id: int,
    site: str,
    role: str,
    feature_as_of: str,
    feature_version: str,
    as_of: datetime,
) -> Mapping[int, PlayerProvenance]:
    """Join each player's heat features back to their episodes and evidence excerpts.

    The one provenance read: Stage 4 asks it which episodes stand behind a delta before
    applying one, and the audit view asks it the same question afterwards so a reader sees
    the rows the routing decided on rather than a second query that could disagree.
    """

    parameters = {
        "slate_id": slate_id,
        "site": site,
        "role": role,
        "feature_as_of": feature_as_of,
        "feature_version": feature_version,
    }
    features = session.query(
        """
        SELECT nf.player_id, nf.feature_id, nf.episode_ids_json
        FROM narrative_features AS nf
        WHERE nf.slate_id = :slate_id AND nf.site = :site
          AND nf.role = CASE WHEN :role = 'captain' THEN 'flex' ELSE :role END
          AND nf.as_of = :feature_as_of AND nf.feature_version = :feature_version
          AND rtrim(nf.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(nf.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(nf.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (nf.valid_to IS NULL OR rtrim(nf.valid_to, 'Z') > rtrim(:as_of, 'Z'))
        ORDER BY nf.player_id
        """,
        parameters,
        as_of=as_of,
    )
    cited: dict[int, tuple[str, tuple[str, ...]]] = {}
    for row in features:
        episode_ids = json.loads(str(row["episode_ids_json"]))
        if not isinstance(episode_ids, list):
            raise OwnershipRoutingError(
                f"narrative feature {row['feature_id']} stores non-array episode ids"
            )
        cited[int(row["player_id"])] = (
            str(row["feature_id"]),
            tuple(str(value) for value in episode_ids),
        )
    everything = {
        episode.episode_id: episode
        for episode in episode_provenance(
            session,
            tuple(sorted({value for _, ids in cited.values() for value in ids})),
            as_of=as_of,
        )
    }
    return {
        player_id: PlayerProvenance(
            feature_id=feature_id,
            episode_ids=episode_ids,
            episodes=tuple(everything[value] for value in episode_ids if value in everything),
            evidence_refs=tuple(
                sorted(
                    reference
                    for value in episode_ids
                    if value in everything
                    for reference in everything[value].evidence_refs
                )
            ),
        )
        for player_id, (feature_id, episode_ids) in cited.items()
    }


def episode_provenance(
    session: PointInTimeQuery,
    episode_ids: Sequence[str],
    *,
    as_of: datetime,
) -> tuple[ProvenanceEpisode, ...]:
    """Read the named episodes with their claims and verbatim evidence, as of ``as_of``.

    Three bounded reads rather than one row-multiplying join, so an episode with no claim
    and a claim with no surviving excerpt both come back — the audit view shows them, and
    Stage 4 refuses on the second (§8.3). Episodes are ordered as they opened, claims by
    relation then id, excerpts by their ordinal in the source text.
    """

    if not episode_ids:
        return ()
    ordered = tuple(dict.fromkeys(episode_ids))
    placeholders, parameters = _id_list("episode", ordered)
    episodes = session.query(
        f"""
        SELECT episode_id, claim_dimension, opened_at, last_item_at, as_of,
               method_version, window_hours, unique_source_count,
               unique_source_family_count, source_entropy, velocity_per_6h,
               recency_hours, n_events, item_count
        FROM narrative_episodes
        WHERE episode_id IN ({placeholders})
          AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(:as_of, 'Z'))
        ORDER BY rtrim(opened_at, 'Z'), episode_id
        """,
        parameters,
        as_of=as_of,
    )
    claims = session.query(
        f"""
        SELECT ec.episode_id, ec.claim_id, ec.relation, ec.similarity_score,
               ec.linked_claim_id, ec.source_id, ec.source_family, ec.source_item_id,
               c.claim_type, c.claim_dimension, c.outcome_direction,
               c.roster_behavior_direction, c.evidence_class, c.evidence_basis,
               c.falsifiable, item.title, item.observed_at
        FROM episode_claims AS ec
        JOIN claims AS c ON c.claim_id = ec.claim_id
        JOIN source_items AS item ON item.source_item_id = ec.source_item_id
        WHERE ec.episode_id IN ({placeholders})
          AND rtrim(ec.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ec.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ec.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (ec.valid_to IS NULL OR rtrim(ec.valid_to, 'Z') > rtrim(:as_of, 'Z'))
          AND rtrim(c.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(c.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(c.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (c.valid_to IS NULL OR rtrim(c.valid_to, 'Z') > rtrim(:as_of, 'Z'))
          AND rtrim(item.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(item.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(item.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (item.valid_to IS NULL OR rtrim(item.valid_to, 'Z') > rtrim(:as_of, 'Z'))
        ORDER BY ec.episode_id, ec.relation, ec.claim_id
        """,
        parameters,
        as_of=as_of,
    )
    excerpts: dict[str, list[ProvenanceEvidence]] = {}
    claim_ids = tuple(dict.fromkeys(str(row["claim_id"]) for row in claims))
    if claim_ids:
        claim_placeholders, claim_parameters = _id_list("claim", claim_ids)
        for row in session.query(
            f"""
            SELECT claim_id, ordinal, source_item_id, source_text_sha256, extract_start,
                   extract_end, verbatim_extract, observed_at
            FROM claim_evidence_refs
            WHERE claim_id IN ({claim_placeholders})
              AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(ingested_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(valid_from, 'Z') <= rtrim(:as_of, 'Z')
              AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(:as_of, 'Z'))
            ORDER BY claim_id, ordinal
            """,
            claim_parameters,
            as_of=as_of,
        ):
            excerpts.setdefault(str(row["claim_id"]), []).append(
                ProvenanceEvidence(
                    ordinal=int(row["ordinal"]),
                    source_item_id=int(row["source_item_id"]),
                    source_text_sha256=str(row["source_text_sha256"]),
                    extract_start=int(row["extract_start"]),
                    extract_end=int(row["extract_end"]),
                    # A tombstoned excerpt is cleared, not deleted; offsets and hash remain.
                    verbatim_extract=(
                        None if row["verbatim_extract"] is None else str(row["verbatim_extract"])
                    ),
                    observed_at=str(row["observed_at"]),
                )
            )
    by_episode: dict[str, list[ProvenanceClaim]] = {}
    for row in claims:
        by_episode.setdefault(str(row["episode_id"]), []).append(
            ProvenanceClaim(
                claim_id=str(row["claim_id"]),
                relation=str(row["relation"]),
                similarity_score=float(row["similarity_score"]),
                linked_claim_id=(
                    None if row["linked_claim_id"] is None else str(row["linked_claim_id"])
                ),
                source_id=str(row["source_id"]),
                source_family=str(row["source_family"]),
                source_item_id=int(row["source_item_id"]),
                claim_type=str(row["claim_type"]),
                claim_dimension=str(row["claim_dimension"]),
                outcome_direction=str(row["outcome_direction"]),
                roster_behavior_direction=str(row["roster_behavior_direction"]),
                evidence_class=str(row["evidence_class"]),
                evidence_basis=str(row["evidence_basis"]),
                falsifiable=bool(row["falsifiable"]),
                item_title=None if row["title"] is None else str(row["title"]),
                item_observed_at=str(row["observed_at"]),
                evidence=tuple(excerpts.get(str(row["claim_id"]), ())),
            )
        )
    return tuple(
        ProvenanceEpisode(
            episode_id=str(row["episode_id"]),
            claim_dimension=str(row["claim_dimension"]),
            opened_at=str(row["opened_at"]),
            last_item_at=str(row["last_item_at"]),
            as_of=str(row["as_of"]),
            method_version=str(row["method_version"]),
            window_hours=float(row["window_hours"]),
            unique_source_count=int(row["unique_source_count"]),
            unique_source_family_count=int(row["unique_source_family_count"]),
            source_entropy=float(row["source_entropy"]),
            velocity_per_6h=float(row["velocity_per_6h"]),
            recency_hours=float(row["recency_hours"]),
            n_events=int(row["n_events"]),
            item_count=int(row["item_count"]),
            claims=tuple(by_episode.get(str(row["episode_id"]), ())),
        )
        for row in episodes
    )


def _id_list(prefix: str, values: Sequence[str]) -> tuple[str, dict[str, object]]:
    """Bind an id list by name; SQLite has no array parameter and this stays escaped."""

    placeholders = ", ".join(f":{prefix}_{index}" for index in range(len(values)))
    return placeholders, {f"{prefix}_{index}": value for index, value in enumerate(values)}


def _delta(
    row: sqlite3.Row, provenance: PlayerProvenance | None, *, threshold: float
) -> AppliedOwnershipDelta:
    if provenance is None:
        raise OwnershipRoutingError(
            f"ownership scenario for player {int(row['player_id'])} has no narrative "
            "feature row at the instant it was built, so its delta cannot be traced"
        )
    return AppliedOwnershipDelta(
        player_id=int(row["player_id"]),
        role=str(row["role"]),
        position=str(row["position"]),
        baseline_ownership=float(row["baseline_ownership"]),
        applied_ownership=float(row["applied_ownership"]),
        ownership_p10=float(row["ownership_p10"]),
        ownership_p50=float(row["ownership_p50"]),
        ownership_p90=float(row["ownership_p90"]),
        delta_p50=float(row["delta_p50"]),
        prob_delta_positive=float(row["prob_delta_positive"]),
        feature_id=provenance.feature_id,
        episode_ids=provenance.episode_ids,
        evidence_refs=provenance.evidence_refs,
        proposed_ownership=float(row["applied_ownership"]),
        material_threshold=threshold,
    )


def record_ownership_routing(
    connection: sqlite3.Connection,
    *,
    decision_snapshot_id: str,
    routing: OwnershipRouting,
    created_at: str,
) -> None:
    """Keep the routing decision and its reason beside the snapshot, permanently.

    The manifest records *what* was applied; this row records *why*, or why not. A replay
    can only re-derive "the manifest carries no set" for an unrouted decision, which is
    not the reason — "no set existed", "the evaluation lost", and "the set missed three
    candidates" are different facts, and the record must keep the real one.
    """

    connection.execute(
        """
        INSERT INTO decision_ownership_routing(
            decision_snapshot_id, applied, reason, scenario_run_id, scenario_set_sha256,
            governance_status, status_multiplier, model_eval_id, held_at_baseline,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision_snapshot_id,
            int(routing.applied),
            routing.reason,
            routing.scenario_run_id if routing.applied else None,
            routing.sha256 if routing.applied else None,
            routing.governance_status,
            routing.status_multiplier,
            routing.model_eval_id,
            len(routing.held_deltas),
            created_at,
        ),
    )


def stored_ownership_routing(
    connection: sqlite3.Connection, *, decision_snapshot_id: str
) -> sqlite3.Row | None:
    """The routing record a build wrote for this decision, or None for a decision frozen
    before Stage 4 existed."""

    row = connection.execute(
        "SELECT * FROM decision_ownership_routing WHERE decision_snapshot_id = ?",
        (decision_snapshot_id,),
    ).fetchone()
    return None if row is None else cast("sqlite3.Row", row)
