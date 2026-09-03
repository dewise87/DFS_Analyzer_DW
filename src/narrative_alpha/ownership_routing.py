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
reaches ``ops.results`` → ``report_cli`` → ``build``, and ``build`` imports this module.
``MATERIAL_DELTA`` therefore mirrors ``config/ownership_model.toml``'s
``evaluation.material_delta_points``, and a test pins the two together.
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
from narrative_alpha.portfolio import DfsSite

MANIFEST_ARTIFACT_KIND: Literal["ownership_scenarios"] = "ownership_scenarios"
MANIFEST_PATH_PREFIX = "store/ownership_scenarios/"
SCENARIO_SET_SCHEMA_VERSION = "ownership-routing-v1"

# Mirrors config/ownership_model.toml [evaluation] material_delta_points = 2.0. A delta
# larger than this is a claim about a player and must cite evidence; anything smaller is
# the roster-total calibration's mechanical wobble (§12.2.6), which cites no episode
# because it asserts nothing about that player.
MATERIAL_DELTA = 0.02
# Float tolerance for the two comparisons below: a cap of 0.02 and a delta of exactly
# 0.02 must not trip on the last bit of a subtraction.
DELTA_TOLERANCE = 1e-9
# Mirrors config/ownership_model.toml [caps.classic.*] maximum_points. Stage 4 is the
# permission layer, so it re-asserts the magnitude cap on every stored row rather than
# trusting that whatever wrote the row respected it. A test pins these to the config.
CLASSIC_CAPS: Mapping[str, float] = {
    "UNVALIDATED": 0.02,
    "TESTING": 0.05,
    "PROVISIONAL": 0.10,
    "VALIDATED": 0.10,
}


class OwnershipRoutingError(RuntimeError):
    """Raised when a scenario set cannot be routed to the optimizer safely."""


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

    @property
    def delta(self) -> float:
        return self.applied_ownership - self.baseline_ownership

    @property
    def delta_points(self) -> float:
        return self.delta * 100.0

    @property
    def material(self) -> bool:
        return abs(self.delta) > MATERIAL_DELTA + DELTA_TOLERANCE


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

        ordered = sorted(self.deltas, key=lambda delta: (-abs(delta.delta), delta.player_id))
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
) -> RoutedCandidateSelection:
    """Select point-in-time candidates, then route governed ownership onto them.

    Build leaves ``pinned`` unset and discovers the newest eligible scenario set; replay
    passes exactly what the manifest froze, so the same rows come back and the request
    bytes are identical.
    """

    selection = select_candidate_scenario(
        session,
        slate_id=slate_id,
        site=site,
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
    )
    if not routing.applied:
        return RoutedCandidateSelection(selection=selection, routing=routing)

    applied = {delta.player_id: delta.applied_ownership for delta in routing.deltas}
    players = tuple(
        player.model_copy(update={"projected_ownership": applied[player.player_id]})
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
) -> OwnershipRouting:
    """Decide whether a scenario set may replace the vendor ownership, and say why."""

    pinned_run_id = None if pinned is None else pinned.scenario_run_id
    if slate_type != "classic":
        # A classic optimizer candidate carries one ownership value and no captain/flex
        # split, so a showdown scenario set has nowhere to land yet.
        return _refuse_or_baseline(
            pinned_run_id,
            contest_archetype,
            slate_type,
            f"{slate_type} routing is not wired to the optimizer, which carries one "
            "ownership value per candidate; the vendor baseline was applied",
        )
    role = "classic"
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
        role=role,
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

    evaluation = _latest_evaluation(
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
    if not bool(evaluation["beat_baseline"]):
        return _refuse_or_baseline(
            pinned_run_id,
            contest_archetype,
            role,
            f"the newest out-of-week evaluation {str(evaluation['model_eval_id'])!r} did not "
            "beat the untouched vendor baseline; the vendor baseline was applied",
        )

    rows = _scenario_rows(
        session,
        run_id=str(header["run_id"]),
        role=role,
        as_of=as_of,
    )
    covered = {int(row["player_id"]) for row in rows}
    uncovered = sorted(candidate_player_ids - covered)
    if uncovered:
        # Applying a set that misses candidates would mix modeled and vendor ownership
        # inside one roster-total calibration (§12.2.6), so the whole slate falls back.
        return _refuse_or_baseline(
            pinned_run_id,
            contest_archetype,
            role,
            f"ownership scenario set {str(header['run_id'])!r} covers "
            f"{len(covered & candidate_player_ids)} of {len(candidate_player_ids)} candidate "
            f"player(s) — missing {_listed_ids(uncovered)}; the vendor baseline was applied",
        )

    feature_as_of = _scenario_feature_instant(
        session,
        decision_snapshot_id=str(header["decision_snapshot_id"]),
        as_of=as_of,
    )
    provenance = _episode_provenance(
        session,
        slate_id=slate_id,
        site=site.value,
        role=role,
        feature_as_of=feature_as_of,
        feature_version=str(header["feature_version"]),
        as_of=as_of,
    )
    deltas = tuple(
        _delta(row, provenance.get(int(row["player_id"])))
        for row in rows
        if int(row["player_id"]) in candidate_player_ids
    )
    governance_status = str(header["governance_status"])
    cap = CLASSIC_CAPS.get(governance_status)
    if cap is None:
        raise OwnershipRoutingError(
            f"ownership scenario set {str(header['run_id'])!r} carries governance status "
            f"{governance_status!r}, which Stage 4 has no cap for"
        )
    # The permission layer re-asserts the magnitude cap (§12.2.5) on the stored rows: a
    # row that moved past its own status's cap is a broken set, whoever wrote it.
    over_cap = tuple(delta for delta in deltas if abs(delta.delta) > cap + DELTA_TOLERANCE)
    if over_cap:
        raise OwnershipRoutingError(
            f"ownership scenario set {str(header['run_id'])!r} moves {len(over_cap)} "
            f"player(s) past the {governance_status} cap of {cap * 100:.1f} point(s): "
            + ", ".join(
                f"player {delta.player_id} {delta.delta_points:+.2f}pt" for delta in over_cap[:10]
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
            f"by more than {MATERIAL_DELTA * 100:.1f} point(s) on episodes with no evidence "
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
            f"; {len(held)} player(s) held at the vendor baseline because the set moved them "
            f"more than {MATERIAL_DELTA * 100:.1f} point(s) with no narrative episode behind "
            "the move: "
            + ", ".join(
                f"player {delta.player_id} "
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
            f"{str(evaluation['model_eval_id'])!r} beat the untouched vendor baseline"
            + held_note
        ),
        contest_archetype=contest_archetype,
        role=role,
        scenario_run_id=str(header["run_id"]),
        scenario_decision_snapshot_id=str(header["decision_snapshot_id"]),
        model_run_id=str(header["model_run_id"]),
        model_version=str(header["model_version"]),
        model_eval_id=str(evaluation["model_eval_id"]),
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
        item
        for item in manifest
        if getattr(item, "artifact_kind", None) == MANIFEST_ARTIFACT_KIND
    ]
    if not artifacts:
        return NO_PINNED_ROUTING
    if len(artifacts) > 1:
        raise OwnershipRoutingError(
            "decision manifest names more than one ownership scenario set"
        )
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


def verify_pinned_routing(
    routing: OwnershipRouting, pinned: PinnedOwnershipRouting
) -> None:
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
    role: str,
    as_of: datetime,
    pinned_run_id: str | None,
) -> sqlite3.Row | None:
    rows = session.query(
        """
        SELECT DISTINCT os.run_id, os.decision_snapshot_id, os.contest_archetype,
               os.governance_status, os.status_multiplier, os.model_run_id,
               os.model_version, os.config_sha256, os.feature_version, os.source,
               os.observed_at
        FROM ownership_scenarios AS os
        JOIN model_runs AS run ON run.run_id = os.run_id
        WHERE os.slate_id = :slate_id AND os.site = :site
          AND os.contest_archetype = :contest_archetype AND os.role = :role
          AND (:pinned_run_id IS NULL OR os.run_id = :pinned_run_id)
          AND run.status = 'succeeded'
          AND rtrim(os.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(os.created_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(run.completed_at, 'Z') <= rtrim(:as_of, 'Z')
        ORDER BY rtrim(os.observed_at, 'Z') DESC, os.run_id DESC
        LIMIT 1
        """,
        {
            "slate_id": slate_id,
            "site": site,
            "contest_archetype": contest_archetype,
            "role": role,
            "pinned_run_id": pinned_run_id,
        },
        as_of=as_of,
    )
    return rows[0] if rows else None


def _latest_evaluation(
    session: PointInTimeQuery,
    *,
    site: str,
    contest_archetype: str,
    feature_version: str,
    config_sha256: str,
    as_of: datetime,
) -> sqlite3.Row | None:
    rows = session.query(
        """
        SELECT model_eval_id, beat_baseline, report_path, observed_at
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
    return rows[0] if rows else None


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


def _episode_provenance(
    session: PointInTimeQuery,
    *,
    slate_id: int,
    site: str,
    role: str,
    feature_as_of: str,
    feature_version: str,
    as_of: datetime,
) -> Mapping[int, tuple[str, tuple[str, ...], tuple[EvidenceRef, ...]]]:
    """Join each player's heat features back to their episodes and evidence excerpts."""

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
        WHERE nf.slate_id = :slate_id AND nf.site = :site AND nf.role = :role
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
    evidence = session.query(
        """
        SELECT nf.player_id, episode.value AS episode_id, ec.claim_id, ec.relation,
               ec.source_id, ec.source_family, ref.source_item_id,
               ref.source_text_sha256, ref.extract_start, ref.extract_end,
               ref.verbatim_extract
        FROM narrative_features AS nf,
             json_each(nf.episode_ids_json) AS episode
        JOIN narrative_episodes AS ne
          ON ne.episode_id = episode.value
         AND rtrim(ne.observed_at, 'Z') <= rtrim(:as_of, 'Z')
         AND rtrim(ne.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
         AND rtrim(ne.valid_from, 'Z') <= rtrim(:as_of, 'Z')
         AND (ne.valid_to IS NULL OR rtrim(ne.valid_to, 'Z') > rtrim(:as_of, 'Z'))
        JOIN episode_claims AS ec
          ON ec.episode_id = ne.episode_id
         AND rtrim(ec.observed_at, 'Z') <= rtrim(:as_of, 'Z')
         AND rtrim(ec.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
         AND rtrim(ec.valid_from, 'Z') <= rtrim(:as_of, 'Z')
         AND (ec.valid_to IS NULL OR rtrim(ec.valid_to, 'Z') > rtrim(:as_of, 'Z'))
        JOIN claim_evidence_refs AS ref
          ON ref.claim_id = ec.claim_id
         AND rtrim(ref.observed_at, 'Z') <= rtrim(:as_of, 'Z')
         AND rtrim(ref.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
         AND rtrim(ref.valid_from, 'Z') <= rtrim(:as_of, 'Z')
         AND (ref.valid_to IS NULL OR rtrim(ref.valid_to, 'Z') > rtrim(:as_of, 'Z'))
        WHERE nf.slate_id = :slate_id AND nf.site = :site AND nf.role = :role
          AND nf.as_of = :feature_as_of AND nf.feature_version = :feature_version
          AND rtrim(nf.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(nf.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
        ORDER BY nf.player_id, episode.value, ec.claim_id, ref.ordinal
        """,
        parameters,
        as_of=as_of,
    )
    refs: dict[int, list[EvidenceRef]] = {}
    for row in evidence:
        refs.setdefault(int(row["player_id"]), []).append(
            EvidenceRef(
                episode_id=str(row["episode_id"]),
                claim_id=str(row["claim_id"]),
                relation=str(row["relation"]),
                source_id=str(row["source_id"]),
                source_family=str(row["source_family"]),
                source_item_id=int(row["source_item_id"]),
                source_text_sha256=str(row["source_text_sha256"]),
                extract_start=int(row["extract_start"]),
                extract_end=int(row["extract_end"]),
                verbatim_extract=(
                    None
                    if row["verbatim_extract"] is None
                    else str(row["verbatim_extract"])
                ),
            )
        )
    provenance: dict[int, tuple[str, tuple[str, ...], tuple[EvidenceRef, ...]]] = {}
    for row in features:
        player_id = int(row["player_id"])
        episode_ids = json.loads(str(row["episode_ids_json"]))
        if not isinstance(episode_ids, list):
            raise OwnershipRoutingError(
                f"narrative feature {row['feature_id']} stores non-array episode ids"
            )
        provenance[player_id] = (
            str(row["feature_id"]),
            tuple(str(value) for value in episode_ids),
            tuple(sorted(refs.get(player_id, ()))),
        )
    return provenance


def _delta(
    row: sqlite3.Row,
    provenance: tuple[str, tuple[str, ...], tuple[EvidenceRef, ...]] | None,
) -> AppliedOwnershipDelta:
    if provenance is None:
        raise OwnershipRoutingError(
            f"ownership scenario for player {int(row['player_id'])} has no narrative "
            "feature row at the instant it was built, so its delta cannot be traced"
        )
    feature_id, episode_ids, evidence = provenance
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
        feature_id=feature_id,
        episode_ids=episode_ids,
        evidence_refs=evidence,
        proposed_ownership=float(row["applied_ownership"]),
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
