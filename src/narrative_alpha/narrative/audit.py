"""The Phase 2 signal/evidence audit view: why the optimizer saw that ownership number.

§8.3 requires deterministic traceability from an applied adjustment back to its evidence.
:func:`player_audit` is that lineage read, for one player at one frozen decision: the
vendor baseline, the applied ownership and its governance status, every heat feature with
its version, every episode behind those features, every claim in those episodes, and every
verbatim evidence excerpt behind those claims — each read as of ``decision_at`` and
nothing later.

Three sibling reads answer the same question from the other three directions a reader
arrives from — an episode id (:func:`episode_audit`), a phrase they remember seeing
(:func:`search_evidence`), and the decision's whole Stage 4 set rather than one player's
row (:func:`decision_scenarios`). They share this module because they share its one rule:
the decision's ``decision_at`` is the cutoff, and every query is bound to it.

Read-only by construction. Every query goes through :class:`PointInTimeSession`, which
refuses SQL that omits the ``:as_of`` bind, so a claim observed after the decision cannot
appear here however the caller asks. There is no write path in this module.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.narrative.source_catalog import (
    CatalogError,
    SourceGrade,
    catalog_source_grade,
    load_source_catalog,
    source_family_grade,
)
from narrative_alpha.ownership_routing import (
    MANIFEST_ARTIFACT_KIND,
    pinned_routing_from_manifest,
    stored_ownership_routing,
)
from narrative_alpha.replay import PointInTimeSession, ReplayError

DEFAULT_SOURCE_CATALOG_PATH = Path("config/narrative_sources.toml")

# How many excerpts an evidence search returns when the caller names no limit, and the
# ceiling it may raise that to. A search is a way in, not a bulk export: a reader who
# wants everything behind a player asks for the audit, which is bounded by the decision.
DEFAULT_EVIDENCE_SEARCH_LIMIT = 20
MAX_EVIDENCE_SEARCH_LIMIT = 100

#: The Appendix B heat channels, in the order the memo and the design doc list them.
HEAT_CHANNELS = (
    "h_signed",
    "h_absolute",
    "h_mainstream",
    "h_dfs",
    "h_team_fan",
    "h_velocity_6h",
    "h_acceleration",
    "h_consensus",
    "h_source_entropy",
    "h_novelty_share",
)


class AuditError(RuntimeError):
    """Raised when an audit cannot be assembled from the frozen decision."""


class AuditModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuditEvidence(AuditModel):
    """One bounded verbatim excerpt, with the source it came from and when it was seen."""

    ordinal: int = Field(ge=0)
    source_item_id: int
    source_id: str
    source_family: str
    source_grade: SourceGrade
    source_grade_basis: str
    extract_start: int = Field(ge=0)
    extract_end: int = Field(gt=0)
    verbatim_extract: str | None
    source_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime


class AuditClaim(AuditModel):
    """One Stage 1 claim as an episode member, with its own evidence excerpts."""

    claim_id: str
    relation: str
    similarity_score: float = Field(ge=0, le=1)
    linked_claim_id: str | None
    claim_type: str
    claim_dimension: str
    outcome_direction: str
    roster_behavior_direction: str
    evidence_class: str
    evidence_basis: str
    falsifiable: bool
    source_id: str
    source_family: str
    source_grade: SourceGrade
    source_grade_basis: str
    item_title: str | None
    item_observed_at: datetime
    evidence: tuple[AuditEvidence, ...]


class AuditEpisode(AuditModel):
    """One Stage 2 cluster and every claim it holds at the decision instant."""

    episode_id: str
    claim_dimension: str
    opened_at: datetime
    last_item_at: datetime
    as_of: datetime
    method_version: str
    window_hours: float
    unique_source_count: int
    unique_source_family_count: int
    source_entropy: float
    velocity_per_6h: float
    recency_hours: float
    n_events: int
    item_count: int
    claims: tuple[AuditClaim, ...]


class AuditChannel(AuditModel):
    """One heat channel: the raw value the formula produced and its slate z-score."""

    name: str
    raw_value: float
    standardized_value: float


class AuditFeatures(AuditModel):
    """The Appendix B row the ownership model read for this player at this decision."""

    feature_id: str
    feature_version: str
    formula_version: str
    feature_config_sha256: str
    episode_method_version: str
    as_of: datetime
    site: str
    role: str
    salary: int
    baseline_ownership: float | None
    baseline_ownership_change_6h: float | None
    projection_change_6h: float | None
    unique_episode_count: int
    unique_source_count: int
    source_overlap_index: float
    episode_ids: tuple[str, ...]
    channels: tuple[AuditChannel, ...]


class AuditOwnership(AuditModel):
    """The number the optimizer saw, the vendor number it started from, and which won."""

    vendor_baseline: float | None
    vendor_baseline_source: str | None
    vendor_baseline_observed_at: datetime | None
    applied: bool
    reason: str
    applied_ownership: float | None = None
    ownership_p10: float | None = None
    ownership_p50: float | None = None
    ownership_p90: float | None = None
    delta_points: float | None = None
    prob_delta_positive: float | None = None
    governance_status: str | None = None
    status_multiplier: float | None = None
    scenario_run_id: str | None = None
    model_run_id: str | None = None
    model_version: str | None = None
    config_sha256: str | None = None
    feature_version: str | None = None
    # What existed as of the decision, whether or not it was applied — so a baseline
    # decision can say whether there was a set at all and what its evaluation said.
    scenario_set_available: bool = False
    available_scenario_run_id: str | None = None
    available_scenario_status: str | None = None
    evaluation_model_eval_id: str | None = None
    evaluation_beat_baseline: bool | None = None


class PlayerAudit(AuditModel):
    """Everything behind one player's ownership at one decision, as of that decision."""

    decision_snapshot_id: str
    decision_at: datetime
    slate_id: int
    site: str
    slate_type: str
    season: int
    week: int
    player_id: int
    player_name: str
    position: str | None
    team: str | None
    salary: int | None
    ownership: AuditOwnership
    features: AuditFeatures | None
    episodes: tuple[AuditEpisode, ...]
    notes: tuple[str, ...]


class EvidenceHit(AuditModel):
    """One excerpt that matched a search, with the claim and episode carrying it."""

    episode_id: str
    claim_id: str
    claim_type: str
    claim_dimension: str
    evidence_class: str
    item_title: str | None
    evidence: AuditEvidence


class EvidenceSearch(AuditModel):
    """A capped substring search over the excerpts visible at one decision."""

    decision_snapshot_id: str
    decision_at: datetime
    query: str
    limit: int
    truncated: bool
    hits: tuple[EvidenceHit, ...]


class DecisionScenarioRow(AuditModel):
    """One player's row in the scenario set this decision saw."""

    player_id: int
    player_name: str | None
    role: str
    position: str
    baseline_ownership: float
    applied_ownership: float
    ownership_p10: float
    ownership_p50: float
    ownership_p90: float
    delta_p50: float
    delta_points: float
    prob_delta_positive: float
    calibrated_to_roster_totals: bool


class DecisionRoutingRecord(AuditModel):
    """The row the build wrote beside the snapshot saying what Stage 4 did, and why."""

    applied: bool
    reason: str
    scenario_run_id: str | None
    scenario_set_sha256: str | None
    governance_status: str | None
    status_multiplier: float | None
    model_eval_id: str | None
    held_at_baseline: int
    created_at: datetime


class DecisionScenarios(AuditModel):
    """One decision's ownership scenario set and the routing record beside it."""

    decision_snapshot_id: str
    decision_at: datetime
    slate_id: int
    site: str
    #: ``manifest_pinned`` — the set the manifest names, so the optimizer read it;
    #: ``available_not_applied`` — a set existed at the cutoff and Stage 4 declined it;
    #: ``none`` — no set existed at all.
    set_status: str
    applied: bool
    scenario_run_id: str | None
    contest_archetype: str | None
    governance_status: str | None
    status_multiplier: float | None
    model_run_id: str | None
    model_version: str | None
    config_sha256: str | None
    feature_version: str | None
    routing_record: DecisionRoutingRecord | None
    rows: tuple[DecisionScenarioRow, ...]
    notes: tuple[str, ...]


def player_audit(
    connection: sqlite3.Connection,
    *,
    player_id: int,
    decision_snapshot_id: str,
    catalog_path: Path | None = DEFAULT_SOURCE_CATALOG_PATH,
) -> PlayerAudit:
    """Assemble one player's signal and evidence lineage as of a frozen decision."""

    session = PointInTimeSession(connection)
    decision_at = _decision_instant(connection, decision_snapshot_id)
    try:
        snapshot = session.decision_snapshot(decision_snapshot_id, as_of=decision_at)
        slate = session.slate(snapshot.slate_id, as_of=decision_at)
    except ReplayError as error:
        raise AuditError(str(error)) from error

    notes: list[str] = []
    player = _player_row(session, player_id=player_id, as_of=decision_at)
    salary = _salary_row(
        session, player_id=player_id, slate_id=slate.slate_id, as_of=decision_at
    )
    if salary is None:
        notes.append(
            f"player {player_id} has no point-in-time salary row on slate {slate.slate_id} "
            f"at {utc_timestamp(decision_at)}, so they were not an optimizer candidate"
        )
    grades = _GradeBook(catalog_path)
    if grades.note is not None:
        notes.append(grades.note)

    features, feature_note = _features(
        session,
        player_id=player_id,
        slate_id=slate.slate_id,
        site=slate.site,
        as_of=decision_at,
    )
    if feature_note is not None:
        notes.append(feature_note)

    ownership = _ownership(
        session,
        snapshot_manifest=snapshot.manifest_hashes_json,
        player_id=player_id,
        slate_id=slate.slate_id,
        site=slate.site,
        as_of=decision_at,
    )

    episode_ids: tuple[str, ...]
    if features is not None:
        episode_ids = features.episode_ids
    else:
        episode_ids = _episodes_at_newest_snapshot(
            session, player_id=player_id, as_of=decision_at
        )
        if episode_ids:
            notes.append(
                "no feature row exists for this player at the decision instant; the "
                "episodes below are the newest Stage 2 snapshot at or before it"
            )
    episodes = _episodes(session, episode_ids, as_of=decision_at, grades=grades)
    if not episodes:
        notes.append(
            f"no narrative episode was behind this player as of {utc_timestamp(decision_at)}; "
            "the heat channels above are the slate-population values for a quiet player, "
            "not a claim about them"
        )
    return PlayerAudit(
        decision_snapshot_id=decision_snapshot_id,
        decision_at=decision_at,
        slate_id=slate.slate_id,
        site=slate.site,
        slate_type=slate.slate_type,
        season=slate.season,
        week=slate.week,
        player_id=player_id,
        player_name=str(player["canonical_name"]),
        position=None if player["position"] is None else str(player["position"]),
        team=None if salary is None else str(salary["team"]),
        salary=None if salary is None else int(salary["salary"]),
        ownership=ownership,
        features=features,
        episodes=episodes,
        notes=tuple(notes),
    )


def render_player_audit(audit: PlayerAudit) -> str:
    """Render the same model the dashboard page renders, as stable operator text."""

    output = io.StringIO(newline="")
    output.write("SIGNAL AND EVIDENCE AUDIT\n")
    output.write(f"decision_snapshot_id={audit.decision_snapshot_id}\n")
    output.write(f"decision_at={utc_timestamp(audit.decision_at)}\n")
    output.write(f"slate_id={audit.slate_id}\n")
    output.write(f"site={audit.site}\n")
    output.write(f"slate_type={audit.slate_type}\n")
    output.write(f"season={audit.season}\n")
    output.write(f"week={audit.week}\n")
    output.write(f"player_id={audit.player_id}\n")
    output.write(f"player_name={audit.player_name}\n")
    output.write(f"position={_text(audit.position)}\n")
    output.write(f"team={_text(audit.team)}\n")
    output.write(f"salary={_text(audit.salary)}\n")

    ownership = audit.ownership
    output.write("\nOWNERSHIP THE OPTIMIZER SAW\n")
    output.write(
        "ownership_source=" + ("scenario_model" if ownership.applied else "vendor_baseline") + "\n"
    )
    output.write(f"reason={ownership.reason}\n")
    output.write(f"vendor_baseline={_fraction(ownership.vendor_baseline)}\n")
    output.write(f"vendor_baseline_source={_text(ownership.vendor_baseline_source)}\n")
    output.write(
        "vendor_baseline_observed_at="
        + ("unavailable" if ownership.vendor_baseline_observed_at is None
           else utc_timestamp(ownership.vendor_baseline_observed_at))
        + "\n"
    )
    output.write(f"applied_ownership={_fraction(ownership.applied_ownership)}\n")
    output.write(f"ownership_p10={_fraction(ownership.ownership_p10)}\n")
    output.write(f"ownership_p50={_fraction(ownership.ownership_p50)}\n")
    output.write(f"ownership_p90={_fraction(ownership.ownership_p90)}\n")
    output.write(
        "delta_points="
        + ("unavailable" if ownership.delta_points is None else f"{ownership.delta_points:+.6f}")
        + "\n"
    )
    output.write(f"prob_delta_positive={_number(ownership.prob_delta_positive)}\n")
    output.write(f"governance_status={_text(ownership.governance_status)}\n")
    output.write(f"status_multiplier={_number(ownership.status_multiplier)}\n")
    output.write(f"scenario_run_id={_text(ownership.scenario_run_id)}\n")
    output.write(f"model_run_id={_text(ownership.model_run_id)}\n")
    output.write(f"model_version={_text(ownership.model_version)}\n")
    output.write(f"config_sha256={_text(ownership.config_sha256)}\n")
    output.write(f"scenario_feature_version={_text(ownership.feature_version)}\n")
    output.write(f"scenario_set_available={_flag(ownership.scenario_set_available)}\n")
    output.write(f"available_scenario_run_id={_text(ownership.available_scenario_run_id)}\n")
    output.write(f"available_scenario_status={_text(ownership.available_scenario_status)}\n")
    output.write(f"evaluation_model_eval_id={_text(ownership.evaluation_model_eval_id)}\n")
    output.write(
        "evaluation_beat_baseline="
        + ("unavailable" if ownership.evaluation_beat_baseline is None
           else _flag(ownership.evaluation_beat_baseline))
        + "\n"
    )

    output.write("\nHEAT FEATURES\n")
    if audit.features is None:
        output.write("feature_status=unavailable — no Appendix B row at this decision\n")
    else:
        features = audit.features
        output.write("feature_status=available\n")
        output.write(f"feature_id={features.feature_id}\n")
        output.write(f"feature_version={features.feature_version}\n")
        output.write(f"formula_version={features.formula_version}\n")
        output.write(f"feature_config_sha256={features.feature_config_sha256}\n")
        output.write(f"episode_method_version={features.episode_method_version}\n")
        output.write(f"feature_as_of={utc_timestamp(features.as_of)}\n")
        output.write(f"role={features.role}\n")
        output.write(f"feature_salary={features.salary}\n")
        output.write(f"feature_baseline_ownership={_fraction(features.baseline_ownership)}\n")
        output.write(
            f"baseline_ownership_change_6h={_number(features.baseline_ownership_change_6h)}\n"
        )
        output.write(f"projection_change_6h={_number(features.projection_change_6h)}\n")
        output.write(f"unique_episode_count={features.unique_episode_count}\n")
        output.write(f"unique_source_count={features.unique_source_count}\n")
        output.write(f"source_overlap_index={features.source_overlap_index:.6f}\n")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(("channel", "raw_value", "standardized_value"))
        for channel in features.channels:
            writer.writerow(
                (
                    channel.name,
                    f"{channel.raw_value:.6f}",
                    f"{channel.standardized_value:.6f}",
                )
            )

    output.write("\nEPISODES, CLAIMS, AND EVIDENCE\n")
    if not audit.episodes:
        output.write("episode_status=none — no narrative episode was behind this player\n")
    for episode in audit.episodes:
        output.write(f"episode_id={episode.episode_id}\n")
        output.write(
            f"  dimension={episode.claim_dimension} method={episode.method_version} "
            f"window_hours={episode.window_hours:.2f}\n"
        )
        output.write(
            f"  opened_at={utc_timestamp(episode.opened_at)} "
            f"last_item_at={utc_timestamp(episode.last_item_at)} "
            f"as_of={utc_timestamp(episode.as_of)}\n"
        )
        output.write(
            f"  items={episode.item_count} events={episode.n_events} "
            f"sources={episode.unique_source_count} "
            f"families={episode.unique_source_family_count} "
            f"entropy={episode.source_entropy:.6f} "
            f"velocity_per_6h={episode.velocity_per_6h:.6f} "
            f"recency_hours={episode.recency_hours:.6f}\n"
        )
        if not episode.claims:
            output.write("  claim_status=none visible at this decision\n")
        for claim in episode.claims:
            output.write(
                f"  claim {claim.claim_id} relation={claim.relation} "
                f"similarity={claim.similarity_score:.6f} "
                f"link={_text(claim.linked_claim_id)}\n"
            )
            output.write(
                f"    source={claim.source_id} family={claim.source_family} "
                f"grade={claim.source_grade} ({claim.source_grade_basis}) "
                f"observed_at={utc_timestamp(claim.item_observed_at)}\n"
            )
            output.write(
                f"    type={claim.claim_type} dimension={claim.claim_dimension} "
                f"outcome={claim.outcome_direction} "
                f"roster_behavior={claim.roster_behavior_direction} "
                f"evidence_class={claim.evidence_class} basis={claim.evidence_basis} "
                f"falsifiable={_flag(claim.falsifiable)}\n"
            )
            output.write(f"    title={_text(claim.item_title)}\n")
            if not claim.evidence:
                output.write("    evidence_status=none retained at this decision\n")
            for evidence in claim.evidence:
                output.write(
                    f"    evidence[{evidence.ordinal}] item={evidence.source_item_id} "
                    f"chars {evidence.extract_start}:{evidence.extract_end} "
                    f"observed_at={utc_timestamp(evidence.observed_at)} "
                    f"sha256={evidence.source_text_sha256}\n"
                )
                output.write(
                    "      excerpt="
                    + (
                        "redacted — the excerpt was tombstoned; offsets and hash remain"
                        if evidence.verbatim_extract is None
                        else evidence.verbatim_extract
                    )
                    + "\n"
                )

    output.write("\nNOTES\n")
    if not audit.notes:
        output.write("no gaps to report\n")
    for note in audit.notes:
        output.write(f"- {note}\n")
    return output.getvalue()


def _text(value: object) -> str:
    return "unavailable" if value is None else str(value)


def _flag(value: bool) -> str:
    return "yes" if value else "no"


def _number(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:.6f}"


def _fraction(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:.6f}"


def resolve_audit_player(
    connection: sqlite3.Connection,
    *,
    selector: str,
    decision_snapshot_id: str,
) -> int:
    """Accept a player id or an exact canonical name as of the decision, never a guess."""

    decision_at = _decision_instant(connection, decision_snapshot_id)
    stripped = selector.strip()
    if not stripped:
        raise AuditError("--player must name a player id or an exact canonical name")
    if stripped.isdigit():
        return int(stripped)
    rows = PointInTimeSession(connection).query(
        """
        SELECT player_id, canonical_name
        FROM players
        WHERE canonical_name = :name
          AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(:as_of, 'Z'))
        ORDER BY player_id
        """,
        {"name": stripped},
        as_of=decision_at,
    )
    identifiers = sorted({int(row["player_id"]) for row in rows})
    if not identifiers:
        raise AuditError(
            f"no player is named {stripped!r} as of {utc_timestamp(decision_at)}; pass the "
            "canonical name exactly, or the player id"
        )
    if len(identifiers) > 1:
        listed = ", ".join(str(value) for value in identifiers)
        raise AuditError(
            f"{stripped!r} matches player ids {listed} at that decision; pass the id"
        )
    return identifiers[0]


class _GradeBook:
    """Source grades, from the reviewed catalog when it is readable, else the family default.

    The catalog is configuration, not a point-in-time record, so which basis was used is
    reported beside every grade rather than left for the reader to assume.
    """

    def __init__(self, catalog_path: Path | None) -> None:
        self._catalog = None
        self.note: str | None = None
        if catalog_path is None:
            return
        try:
            self._catalog = load_source_catalog(catalog_path)
        except (CatalogError, OSError) as error:
            self.note = (
                f"source grades fall back to the source-family default: the catalog at "
                f"{catalog_path} could not be read ({error})"
            )

    def grade(self, *, source_id: str, source_family: str) -> tuple[SourceGrade, str]:
        if self._catalog is not None:
            try:
                return catalog_source_grade(self._catalog, source_id), "catalog"
            except CatalogError:
                pass
        return source_family_grade(source_family), "source_family_default"


def list_audit_candidates(
    connection: sqlite3.Connection, *, decision_snapshot_id: str
) -> tuple[tuple[int, str, str | None], ...]:
    """The players a decision could audit: salaried at its cutoff, read as-of that cutoff.

    Returns ``(player_id, canonical_name, position)`` in name order. Bound through the
    same session as every other read here, so a salary or roster row that arrived after
    the decision cannot appear in the list any more than in the audit.
    """

    as_of = _decision_instant(connection, decision_snapshot_id)
    rows = PointInTimeSession(connection).query(
        """
        SELECT DISTINCT p.player_id, p.canonical_name, p.position
        FROM decision_snapshots AS ds
        JOIN salaries AS s
          ON s.slate_id = ds.slate_id
         AND rtrim(s.observed_at, 'Z') <= rtrim(:as_of, 'Z')
         AND rtrim(s.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
         AND rtrim(s.valid_from, 'Z') <= rtrim(:as_of, 'Z')
         AND (s.valid_to IS NULL OR rtrim(s.valid_to, 'Z') > rtrim(:as_of, 'Z'))
        JOIN players AS p
          ON p.player_id = s.player_id
         AND rtrim(p.observed_at, 'Z') <= rtrim(:as_of, 'Z')
         AND rtrim(p.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
         AND rtrim(p.valid_from, 'Z') <= rtrim(:as_of, 'Z')
         AND (p.valid_to IS NULL OR rtrim(p.valid_to, 'Z') > rtrim(:as_of, 'Z'))
        WHERE ds.decision_snapshot_id = :decision_snapshot_id
        ORDER BY p.canonical_name, p.player_id
        """,
        {"decision_snapshot_id": decision_snapshot_id},
        as_of=as_of,
    )
    return tuple(
        (
            int(row["player_id"]),
            str(row["canonical_name"]),
            None if row["position"] is None else str(row["position"]),
        )
        for row in rows
    )


def episode_audit(
    connection: sqlite3.Connection,
    *,
    episode_id: str,
    decision_snapshot_id: str,
    catalog_path: Path | None = DEFAULT_SOURCE_CATALOG_PATH,
) -> AuditEpisode:
    """One episode with its claims and excerpts, read as of a decision and nothing later.

    The same rows :func:`player_audit` nests under a player, reached from the other end:
    a reader who has an episode id from a memo or a feature row and wants the evidence
    without first working out which player it belongs to.
    """

    decision_at = _decision_instant(connection, decision_snapshot_id)
    episodes = _episodes(
        PointInTimeSession(connection),
        (episode_id,),
        as_of=decision_at,
        grades=_GradeBook(catalog_path),
    )
    if not episodes:
        raise AuditError(
            f"episode {episode_id!r} was not visible at {utc_timestamp(decision_at)}; it "
            "either does not exist or was built after this decision"
        )
    return episodes[0]


def search_evidence(
    connection: sqlite3.Connection,
    *,
    decision_snapshot_id: str,
    query: str,
    limit: int = DEFAULT_EVIDENCE_SEARCH_LIMIT,
    catalog_path: Path | None = DEFAULT_SOURCE_CATALOG_PATH,
) -> EvidenceSearch:
    """Case-insensitive substring search over the excerpts visible at one decision.

    Capped, and the cap is reported rather than hidden: ``truncated`` says a further
    matching excerpt exists, so a caller narrowing a phrase knows it is looking at a
    window and not at the whole store. A tombstoned excerpt is cleared text, so it
    matches nothing — an absence the reader can check against the surviving offsets.
    """

    stripped = query.strip()
    if not stripped:
        raise AuditError("search_evidence needs a non-empty query")
    if limit < 1 or limit > MAX_EVIDENCE_SEARCH_LIMIT:
        raise AuditError(
            f"search_evidence limit must be between 1 and {MAX_EVIDENCE_SEARCH_LIMIT}"
        )
    decision_at = _decision_instant(connection, decision_snapshot_id)
    session = PointInTimeSession(connection)
    grades = _GradeBook(catalog_path)
    rows = session.query(
        """
        SELECT ec.episode_id, ec.claim_id, ec.source_id, ec.source_family,
               c.claim_type, c.claim_dimension, c.evidence_class,
               item.title, ref.ordinal, ref.source_item_id, ref.source_text_sha256,
               ref.extract_start, ref.extract_end, ref.verbatim_extract,
               ref.observed_at
        FROM claim_evidence_refs AS ref
        JOIN episode_claims AS ec ON ec.claim_id = ref.claim_id
        JOIN claims AS c ON c.claim_id = ref.claim_id
        JOIN narrative_episodes AS ep ON ep.episode_id = ec.episode_id
        JOIN source_items AS item ON item.source_item_id = ref.source_item_id
        WHERE ref.verbatim_extract IS NOT NULL
          AND instr(lower(ref.verbatim_extract), lower(:query)) > 0
          AND rtrim(ref.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ref.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ref.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (ref.valid_to IS NULL OR rtrim(ref.valid_to, 'Z') > rtrim(:as_of, 'Z'))
          AND rtrim(ec.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ec.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ec.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (ec.valid_to IS NULL OR rtrim(ec.valid_to, 'Z') > rtrim(:as_of, 'Z'))
          AND rtrim(c.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(c.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(c.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (c.valid_to IS NULL OR rtrim(c.valid_to, 'Z') > rtrim(:as_of, 'Z'))
          AND rtrim(ep.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ep.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ep.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (ep.valid_to IS NULL OR rtrim(ep.valid_to, 'Z') > rtrim(:as_of, 'Z'))
          AND rtrim(item.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(item.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(item.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (item.valid_to IS NULL OR rtrim(item.valid_to, 'Z') > rtrim(:as_of, 'Z'))
        ORDER BY ec.episode_id, ec.claim_id, ref.ordinal
        LIMIT :fetch
        """,
        {"query": stripped, "fetch": limit + 1},
        as_of=decision_at,
    )
    hits: list[EvidenceHit] = []
    for row in rows[:limit]:
        grade, basis = grades.grade(
            source_id=str(row["source_id"]), source_family=str(row["source_family"])
        )
        hits.append(
            EvidenceHit(
                episode_id=str(row["episode_id"]),
                claim_id=str(row["claim_id"]),
                claim_type=str(row["claim_type"]),
                claim_dimension=str(row["claim_dimension"]),
                evidence_class=str(row["evidence_class"]),
                item_title=None if row["title"] is None else str(row["title"]),
                evidence=AuditEvidence(
                    ordinal=int(row["ordinal"]),
                    source_item_id=int(row["source_item_id"]),
                    source_id=str(row["source_id"]),
                    source_family=str(row["source_family"]),
                    source_grade=grade,
                    source_grade_basis=basis,
                    extract_start=int(row["extract_start"]),
                    extract_end=int(row["extract_end"]),
                    verbatim_extract=str(row["verbatim_extract"]),
                    source_text_sha256=str(row["source_text_sha256"]),
                    observed_at=_stamp(str(row["observed_at"])),
                ),
            )
        )
    return EvidenceSearch(
        decision_snapshot_id=decision_snapshot_id,
        decision_at=decision_at,
        query=stripped,
        limit=limit,
        truncated=len(rows) > limit,
        hits=tuple(hits),
    )


def decision_scenarios(
    connection: sqlite3.Connection, *, decision_snapshot_id: str
) -> DecisionScenarios:
    """The Stage 4 scenario set one decision saw, and the routing record beside it.

    :func:`player_audit` answers this for one player; this answers it for the slate, so a
    reader can see the whole set rather than the row they already suspected. The manifest
    is the decision's own record: a set named there was applied, and its absence is the
    positive statement that the vendor baseline was, whatever landed afterwards.
    """

    decision_at = _decision_instant(connection, decision_snapshot_id)
    session = PointInTimeSession(connection)
    try:
        snapshot = session.decision_snapshot(decision_snapshot_id, as_of=decision_at)
        slate = session.slate(snapshot.slate_id, as_of=decision_at)
    except ReplayError as error:
        raise AuditError(str(error)) from error

    notes: list[str] = []
    stored = stored_ownership_routing(
        connection, decision_snapshot_id=decision_snapshot_id
    )
    record = None if stored is None else _routing_record(stored)
    if record is None:
        notes.append(
            "this decision carries no routing record; it was frozen before Stage 4 wrote "
            "one, so the reason for what follows is not recoverable"
        )

    pinned = pinned_routing_from_manifest(snapshot.manifest_hashes_json)
    run_id = pinned.scenario_run_id
    set_status = "manifest_pinned"
    if run_id is None:
        set_status = "none"
        available = session.query(
            """
            SELECT run_id
            FROM ownership_scenarios
            WHERE slate_id = :slate_id AND site = :site
              AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(created_at, 'Z') <= rtrim(:as_of, 'Z')
            ORDER BY rtrim(observed_at, 'Z') DESC, run_id DESC
            LIMIT 1
            """,
            {"slate_id": slate.slate_id, "site": slate.site},
            as_of=decision_at,
        )
        if available:
            run_id = str(available[0]["run_id"])
            set_status = "available_not_applied"
            notes.append(
                f"the manifest names no {MANIFEST_ARTIFACT_KIND} set, so the optimizer "
                f"read the vendor baseline; set {run_id} existed at the cutoff and the "
                "rows below are what it proposed, not what was applied"
            )
        else:
            notes.append(
                "no ownership scenario set existed for this slate and site as of the "
                "decision, so the vendor baseline reached the optimizer"
            )
    if run_id is None:
        return DecisionScenarios(
            decision_snapshot_id=decision_snapshot_id,
            decision_at=decision_at,
            slate_id=slate.slate_id,
            site=slate.site,
            set_status=set_status,
            applied=False,
            scenario_run_id=None,
            contest_archetype=None,
            governance_status=None,
            status_multiplier=None,
            model_run_id=None,
            model_version=None,
            config_sha256=None,
            feature_version=None,
            routing_record=record,
            rows=(),
            notes=tuple(notes),
        )

    rows = session.query(
        """
        SELECT os.player_id, os.role, os.position, os.baseline_ownership,
               os.applied_ownership, os.ownership_p10, os.ownership_p50,
               os.ownership_p90, os.delta_p50, os.prob_delta_positive,
               os.calibrated_to_roster_totals, os.contest_archetype,
               os.governance_status, os.status_multiplier, os.model_run_id,
               os.model_version, os.config_sha256, os.feature_version,
               p.canonical_name
        FROM ownership_scenarios AS os
        LEFT JOIN players AS p
          ON p.player_id = os.player_id
         AND rtrim(p.observed_at, 'Z') <= rtrim(:as_of, 'Z')
         AND rtrim(p.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
         AND rtrim(p.valid_from, 'Z') <= rtrim(:as_of, 'Z')
         AND (p.valid_to IS NULL OR rtrim(p.valid_to, 'Z') > rtrim(:as_of, 'Z'))
        WHERE os.run_id = :run_id
          AND rtrim(os.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(os.created_at, 'Z') <= rtrim(:as_of, 'Z')
        ORDER BY os.player_id, os.role
        """,
        {"run_id": run_id},
        as_of=decision_at,
    )
    if not rows:
        raise AuditError(
            f"ownership scenario set {run_id!r} has no row visible at "
            f"{utc_timestamp(decision_at)}; the manifest and the store disagree"
        )
    first = rows[0]
    return DecisionScenarios(
        decision_snapshot_id=decision_snapshot_id,
        decision_at=decision_at,
        slate_id=slate.slate_id,
        site=slate.site,
        set_status=set_status,
        applied=set_status == "manifest_pinned",
        scenario_run_id=run_id,
        contest_archetype=str(first["contest_archetype"]),
        governance_status=str(first["governance_status"]),
        status_multiplier=float(first["status_multiplier"]),
        model_run_id=str(first["model_run_id"]),
        model_version=str(first["model_version"]),
        config_sha256=str(first["config_sha256"]),
        feature_version=str(first["feature_version"]),
        routing_record=record,
        rows=tuple(
            DecisionScenarioRow(
                player_id=int(row["player_id"]),
                player_name=(
                    None if row["canonical_name"] is None else str(row["canonical_name"])
                ),
                role=str(row["role"]),
                position=str(row["position"]),
                baseline_ownership=float(row["baseline_ownership"]),
                applied_ownership=float(row["applied_ownership"]),
                ownership_p10=float(row["ownership_p10"]),
                ownership_p50=float(row["ownership_p50"]),
                ownership_p90=float(row["ownership_p90"]),
                delta_p50=float(row["delta_p50"]),
                delta_points=round(
                    (float(row["applied_ownership"]) - float(row["baseline_ownership"]))
                    * 100.0,
                    6,
                ),
                prob_delta_positive=float(row["prob_delta_positive"]),
                calibrated_to_roster_totals=bool(row["calibrated_to_roster_totals"]),
            )
            for row in rows
        ),
        notes=tuple(notes),
    )


def _routing_record(row: sqlite3.Row) -> DecisionRoutingRecord:
    return DecisionRoutingRecord(
        applied=bool(row["applied"]),
        reason=str(row["reason"]),
        scenario_run_id=(
            None if row["scenario_run_id"] is None else str(row["scenario_run_id"])
        ),
        scenario_set_sha256=(
            None if row["scenario_set_sha256"] is None else str(row["scenario_set_sha256"])
        ),
        governance_status=(
            None if row["governance_status"] is None else str(row["governance_status"])
        ),
        status_multiplier=_optional_float(row["status_multiplier"]),
        model_eval_id=None if row["model_eval_id"] is None else str(row["model_eval_id"]),
        held_at_baseline=int(row["held_at_baseline"]),
        created_at=_stamp(str(row["created_at"])),
    )


def _decision_instant(connection: sqlite3.Connection, decision_snapshot_id: str) -> datetime:
    """The one unbounded read: the cutoff every other read in this module is bound to."""

    row = connection.execute(
        "SELECT decision_at FROM decision_snapshots WHERE decision_snapshot_id = ?",
        (decision_snapshot_id,),
    ).fetchone()
    if row is None:
        raise AuditError(f"unknown decision snapshot {decision_snapshot_id!r}")
    return ensure_utc(
        datetime.fromisoformat(str(row["decision_at"]).replace("Z", "+00:00"))
    )


def _player_row(
    session: PointInTimeSession, *, player_id: int, as_of: datetime
) -> sqlite3.Row:
    rows = session.query(
        """
        SELECT player_id, canonical_name, position
        FROM players
        WHERE player_id = :player_id
          AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(:as_of, 'Z'))
        """,
        {"player_id": player_id},
        as_of=as_of,
    )
    if len(rows) != 1:
        raise AuditError(
            f"player {player_id} is not uniquely available at {utc_timestamp(as_of)}"
        )
    return rows[0]


def _salary_row(
    session: PointInTimeSession, *, player_id: int, slate_id: int, as_of: datetime
) -> sqlite3.Row | None:
    rows = session.query(
        """
        SELECT s.salary, team.abbreviation AS team
        FROM salaries AS s
        JOIN teams AS team ON team.team_id = s.team_id
        WHERE s.slate_id = :slate_id AND s.player_id = :player_id
          AND rtrim(s.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(s.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (s.valid_to IS NULL OR rtrim(s.valid_to, 'Z') > rtrim(:as_of, 'Z'))
          AND rtrim(team.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(team.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (team.valid_to IS NULL OR rtrim(team.valid_to, 'Z') > rtrim(:as_of, 'Z'))
        ORDER BY rtrim(s.observed_at, 'Z') DESC, s.salary_id DESC
        LIMIT 1
        """,
        {"slate_id": slate_id, "player_id": player_id},
        as_of=as_of,
    )
    return rows[0] if rows else None


def _features(
    session: PointInTimeSession,
    *,
    player_id: int,
    slate_id: int,
    site: str,
    as_of: datetime,
) -> tuple[AuditFeatures | None, str | None]:
    rows = session.query(
        """
        SELECT *
        FROM narrative_features
        WHERE player_id = :player_id AND slate_id = :slate_id AND site = :site
          AND as_of = :as_of
          AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(:as_of, 'Z'))
        ORDER BY rtrim(observed_at, 'Z') DESC, feature_version DESC
        """,
        {"player_id": player_id, "slate_id": slate_id, "site": site},
        as_of=as_of,
    )
    if not rows:
        return None, (
            f"no Appendix B feature row exists for this player at {utc_timestamp(as_of)}; "
            "the ownership model had nothing to read for them"
        )
    row = rows[0]
    note = None
    if len(rows) > 1:
        versions = ", ".join(sorted({str(value["feature_version"]) for value in rows}))
        note = (
            f"{len(rows)} feature versions exist at this instant ({versions}); the newest "
            f"observation, {row['feature_version']}, is shown"
        )
    episode_ids = json.loads(str(row["episode_ids_json"]))
    if not isinstance(episode_ids, list):
        raise AuditError(f"feature {row['feature_id']} stores non-array episode ids")
    return (
        AuditFeatures(
            feature_id=str(row["feature_id"]),
            feature_version=str(row["feature_version"]),
            formula_version=str(row["formula_version"]),
            feature_config_sha256=str(row["feature_config_sha256"]),
            episode_method_version=str(row["episode_method_version"]),
            as_of=_stamp(str(row["as_of"])),
            site=str(row["site"]),
            role=str(row["role"]),
            salary=int(row["salary"]),
            baseline_ownership=_optional_float(row["baseline_ownership"]),
            baseline_ownership_change_6h=_optional_float(row["baseline_ownership_change_6h"]),
            projection_change_6h=_optional_float(row["projection_change_6h"]),
            unique_episode_count=int(row["unique_episode_count"]),
            unique_source_count=int(row["unique_source_count"]),
            source_overlap_index=float(row["source_overlap_index"]),
            episode_ids=tuple(str(value) for value in episode_ids),
            channels=tuple(
                AuditChannel(
                    name=channel,
                    raw_value=float(row[channel]),
                    standardized_value=float(row[f"{channel}_z"]),
                )
                for channel in HEAT_CHANNELS
            ),
        ),
        note,
    )


def _ownership(
    session: PointInTimeSession,
    *,
    snapshot_manifest: tuple[object, ...],
    player_id: int,
    slate_id: int,
    site: str,
    as_of: datetime,
) -> AuditOwnership:
    """The vendor number, the applied number, and which of the two the optimizer got."""

    baseline = session.query(
        """
        SELECT ownership, source, observed_at
        FROM ownership_baselines
        WHERE slate_id = :slate_id AND player_id = :player_id AND site = :site
          AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(:as_of, 'Z'))
        ORDER BY rtrim(observed_at, 'Z') DESC, ownership_baseline_id DESC
        LIMIT 1
        """,
        {"slate_id": slate_id, "player_id": player_id, "site": site},
        as_of=as_of,
    )
    vendor = None if not baseline else float(baseline[0]["ownership"])
    vendor_source = None if not baseline else str(baseline[0]["source"])
    vendor_at = None if not baseline else _stamp(str(baseline[0]["observed_at"]))

    available = session.query(
        """
        SELECT run_id, governance_status, contest_archetype, feature_version,
               config_sha256
        FROM ownership_scenarios
        WHERE slate_id = :slate_id AND site = :site
          AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(created_at, 'Z') <= rtrim(:as_of, 'Z')
        ORDER BY rtrim(observed_at, 'Z') DESC, run_id DESC
        LIMIT 1
        """,
        {"slate_id": slate_id, "site": site},
        as_of=as_of,
    )
    verdict: sqlite3.Row | None = None
    if available:
        verdict_rows = session.query(
            """
            SELECT model_eval_id, beat_baseline
            FROM model_evals
            WHERE evaluation_kind = 'ownership' AND ownership_site = :site
              AND ownership_archetype = :contest_archetype
              AND feature_version = :feature_version
              AND config_sha256 = :config_sha256
              AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(ingested_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(valid_from, 'Z') <= rtrim(:as_of, 'Z')
              AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(:as_of, 'Z'))
            ORDER BY rtrim(observed_at, 'Z') DESC, model_eval_id DESC
            LIMIT 1
            """,
            {
                "site": site,
                "contest_archetype": str(available[0]["contest_archetype"]),
                "feature_version": str(available[0]["feature_version"]),
                "config_sha256": str(available[0]["config_sha256"]),
            },
            as_of=as_of,
        )
        verdict = verdict_rows[0] if verdict_rows else None

    common: dict[str, object] = {
        "vendor_baseline": vendor,
        "vendor_baseline_source": vendor_source,
        "vendor_baseline_observed_at": vendor_at,
        "scenario_set_available": bool(available),
        "available_scenario_run_id": None if not available else str(available[0]["run_id"]),
        "available_scenario_status": (
            None if not available else str(available[0]["governance_status"])
        ),
        "evaluation_model_eval_id": (
            None if verdict is None else str(verdict["model_eval_id"])
        ),
        "evaluation_beat_baseline": (
            None if verdict is None else bool(verdict["beat_baseline"])
        ),
    }

    # The manifest is the decision's own record of Stage 4: an `ownership_scenarios`
    # entry means a set was applied, and its absence means the vendor baseline was.
    pinned = pinned_routing_from_manifest(snapshot_manifest)
    if pinned.scenario_run_id is None:
        return AuditOwnership(
            applied=False,
            reason=_baseline_reason(bool(available), verdict),
            **common,  # type: ignore[arg-type]
        )
    rows = session.query(
        """
        SELECT baseline_ownership, applied_ownership, ownership_p10, ownership_p50,
               ownership_p90, delta_p50, prob_delta_positive, governance_status,
               status_multiplier, model_run_id, model_version, config_sha256,
               feature_version
        FROM ownership_scenarios
        WHERE run_id = :run_id AND player_id = :player_id
          AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(created_at, 'Z') <= rtrim(:as_of, 'Z')
        ORDER BY role
        LIMIT 1
        """,
        {"run_id": pinned.scenario_run_id, "player_id": player_id},
        as_of=as_of,
    )
    if not rows:
        raise AuditError(
            f"decision applied ownership scenario set {pinned.scenario_run_id!r}, which has "
            f"no row for player {player_id}; the manifest and the store disagree"
        )
    row = rows[0]
    return AuditOwnership(
        applied=True,
        reason=(
            f"the decision manifest names {MANIFEST_ARTIFACT_KIND} set "
            f"{pinned.scenario_run_id}, so the optimizer read its governed applied "
            "ownership instead of the vendor baseline"
        ),
        applied_ownership=float(row["applied_ownership"]),
        ownership_p10=float(row["ownership_p10"]),
        ownership_p50=float(row["ownership_p50"]),
        ownership_p90=float(row["ownership_p90"]),
        delta_points=round(
            (float(row["applied_ownership"]) - float(row["baseline_ownership"])) * 100.0, 6
        ),
        prob_delta_positive=float(row["prob_delta_positive"]),
        governance_status=str(row["governance_status"]),
        status_multiplier=float(row["status_multiplier"]),
        scenario_run_id=pinned.scenario_run_id,
        model_run_id=str(row["model_run_id"]),
        model_version=str(row["model_version"]),
        config_sha256=str(row["config_sha256"]),
        feature_version=str(row["feature_version"]),
        **common,  # type: ignore[arg-type]
    )


def _baseline_reason(available: bool, verdict: sqlite3.Row | None) -> str:
    if not available:
        return (
            "the vendor baseline reached the optimizer: no ownership scenario set existed "
            "for this slate and site as of the decision"
        )
    if verdict is None:
        return (
            "the vendor baseline reached the optimizer: a scenario set existed but no "
            "out-of-week evaluation had shown it beats the baseline"
        )
    if not bool(verdict["beat_baseline"]):
        return (
            "the vendor baseline reached the optimizer: the newest out-of-week evaluation "
            f"{verdict['model_eval_id']!s} did not beat the untouched vendor baseline"
        )
    return (
        "the vendor baseline reached the optimizer: a set existed and its evaluation won, "
        "so Stage 4 declined it for another stated reason — the memo's OWNERSHIP ROUTING "
        "block carries that reason"
    )


def _episodes_at_newest_snapshot(
    session: PointInTimeSession, *, player_id: int, as_of: datetime
) -> tuple[str, ...]:
    rows = session.query(
        """
        SELECT episode_id
        FROM narrative_episodes
        WHERE subject_type = 'player' AND subject_player_id = :player_id
          AND as_of = (
              SELECT max(as_of) FROM narrative_episodes AS newest
              WHERE newest.subject_type = 'player'
                AND newest.subject_player_id = :player_id
                AND rtrim(newest.as_of, 'Z') <= rtrim(:as_of, 'Z')
                AND rtrim(newest.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          )
          AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(:as_of, 'Z'))
        ORDER BY episode_id
        """,
        {"player_id": player_id},
        as_of=as_of,
    )
    return tuple(str(row["episode_id"]) for row in rows)


def _episodes(
    session: PointInTimeSession,
    episode_ids: tuple[str, ...],
    *,
    as_of: datetime,
    grades: _GradeBook,
) -> tuple[AuditEpisode, ...]:
    if not episode_ids:
        return ()
    placeholders = ", ".join(f":episode_{index}" for index in range(len(episode_ids)))
    parameters = {f"episode_{index}": value for index, value in enumerate(episode_ids)}
    rows = session.query(
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
    return tuple(
        AuditEpisode(
            episode_id=str(row["episode_id"]),
            claim_dimension=str(row["claim_dimension"]),
            opened_at=_stamp(str(row["opened_at"])),
            last_item_at=_stamp(str(row["last_item_at"])),
            as_of=_stamp(str(row["as_of"])),
            method_version=str(row["method_version"]),
            window_hours=float(row["window_hours"]),
            unique_source_count=int(row["unique_source_count"]),
            unique_source_family_count=int(row["unique_source_family_count"]),
            source_entropy=float(row["source_entropy"]),
            velocity_per_6h=float(row["velocity_per_6h"]),
            recency_hours=float(row["recency_hours"]),
            n_events=int(row["n_events"]),
            item_count=int(row["item_count"]),
            claims=_claims(
                session, episode_id=str(row["episode_id"]), as_of=as_of, grades=grades
            ),
        )
        for row in rows
    )


def _claims(
    session: PointInTimeSession,
    *,
    episode_id: str,
    as_of: datetime,
    grades: _GradeBook,
) -> tuple[AuditClaim, ...]:
    rows = session.query(
        """
        SELECT ec.claim_id, ec.relation, ec.similarity_score, ec.linked_claim_id,
               ec.source_id, ec.source_family, c.claim_type, c.claim_dimension,
               c.outcome_direction, c.roster_behavior_direction, c.evidence_class,
               c.evidence_basis, c.falsifiable, item.title, item.observed_at
        FROM episode_claims AS ec
        JOIN claims AS c ON c.claim_id = ec.claim_id
        JOIN source_items AS item ON item.source_item_id = ec.source_item_id
        WHERE ec.episode_id = :episode_id
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
        ORDER BY ec.relation, ec.claim_id
        """,
        {"episode_id": episode_id},
        as_of=as_of,
    )
    claims: list[AuditClaim] = []
    for row in rows:
        grade, basis = grades.grade(
            source_id=str(row["source_id"]), source_family=str(row["source_family"])
        )
        claims.append(
            AuditClaim(
                claim_id=str(row["claim_id"]),
                relation=str(row["relation"]),
                similarity_score=float(row["similarity_score"]),
                linked_claim_id=(
                    None if row["linked_claim_id"] is None else str(row["linked_claim_id"])
                ),
                claim_type=str(row["claim_type"]),
                claim_dimension=str(row["claim_dimension"]),
                outcome_direction=str(row["outcome_direction"]),
                roster_behavior_direction=str(row["roster_behavior_direction"]),
                evidence_class=str(row["evidence_class"]),
                evidence_basis=str(row["evidence_basis"]),
                falsifiable=bool(row["falsifiable"]),
                source_id=str(row["source_id"]),
                source_family=str(row["source_family"]),
                source_grade=grade,
                source_grade_basis=basis,
                item_title=None if row["title"] is None else str(row["title"]),
                item_observed_at=_stamp(str(row["observed_at"])),
                evidence=_evidence(
                    session,
                    claim_id=str(row["claim_id"]),
                    source_id=str(row["source_id"]),
                    source_family=str(row["source_family"]),
                    as_of=as_of,
                    grades=grades,
                ),
            )
        )
    return tuple(claims)


def _evidence(
    session: PointInTimeSession,
    *,
    claim_id: str,
    source_id: str,
    source_family: str,
    as_of: datetime,
    grades: _GradeBook,
) -> tuple[AuditEvidence, ...]:
    rows = session.query(
        """
        SELECT ordinal, source_item_id, source_text_sha256, extract_start, extract_end,
               verbatim_extract, observed_at
        FROM claim_evidence_refs
        WHERE claim_id = :claim_id
          AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(:as_of, 'Z'))
        ORDER BY ordinal
        """,
        {"claim_id": claim_id},
        as_of=as_of,
    )
    grade, basis = grades.grade(source_id=source_id, source_family=source_family)
    return tuple(
        AuditEvidence(
            ordinal=int(row["ordinal"]),
            source_item_id=int(row["source_item_id"]),
            source_id=source_id,
            source_family=source_family,
            source_grade=grade,
            source_grade_basis=basis,
            extract_start=int(row["extract_start"]),
            extract_end=int(row["extract_end"]),
            # A tombstoned excerpt is cleared, not deleted; the offsets and hash remain.
            verbatim_extract=(
                None if row["verbatim_extract"] is None else str(row["verbatim_extract"])
            ),
            source_text_sha256=str(row["source_text_sha256"]),
            observed_at=_stamp(str(row["observed_at"])),
        )
        for row in rows
    )


def _stamp(value: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _optional_float(value: object) -> float | None:
    return None if value is None else float(str(value))
