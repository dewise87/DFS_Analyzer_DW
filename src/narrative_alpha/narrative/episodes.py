"""Deterministic Stage 2 clustering of Stage 1 claims into narrative episodes."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from narrative_alpha import __version__
from narrative_alpha.identity.normalization import CANONICAL_TEAM_CODES, normalize_team_code
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.narrative.collectors import normalize_item_text
from narrative_alpha.store import EpisodeClaimRow, ModelRunRow, NarrativeEpisodeRow

METHOD_VERSION = "deterministic-token-set-jaccard-v1"
DEFAULT_WINDOW = timedelta(hours=72)
# A regular collector never leaves a 72-hour gap, so the rolling gap alone would let one
# team/dimension session run the whole season. An episode is also closed this long after it
# opened, and claims older than the lookback are not loaded at all.
DEFAULT_MAX_EPISODE_SPAN = timedelta(hours=168)
DEFAULT_LOOKBACK = timedelta(hours=336)
LINK_SIMILARITY_THRESHOLD = 0.35
DERIVATIVE_SIMILARITY_THRESHOLD = 0.8
DERIVATION_SOURCE = "narrative_alpha.episodes"
DEFAULT_PROMPT_VERSION_ID = "stage1-extraction-v1"
# Headline boilerplate dominates token-set Jaccard on 50-150 character items. Function
# words and team references carry no story identity and are dropped before comparison.
_STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "he",
        "her",
        "him",
        "his",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "our",
        "she",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
        "would",
        "you",
        "your",
        "nfl",
        "week",
        "season",
        "team",
        "teams",
        "game",
        "games",
        "says",
        "said",
        "say",
        "report",
        "reports",
        "reported",
        "per",
        "source",
        "sources",
    ]
)

_TEAM_REFERENCE_GROUPS: dict[str, tuple[str, ...]] = {
    "ARI": ("Arizona", "Cardinals", "Arizona Cardinals", "Cards", "Redbirds"),
    "ATL": ("Atlanta", "Falcons", "Atlanta Falcons"),
    "BAL": ("Baltimore", "Ravens", "Baltimore Ravens"),
    "BUF": ("Buffalo", "Bills", "Buffalo Bills", "The Bills"),
    "CAR": ("Carolina", "Panthers", "Carolina Panthers"),
    "CHI": ("Chicago", "Bears", "Chicago Bears"),
    "CIN": ("Cincinnati", "Bengals", "Cincinnati Bengals"),
    "CLE": ("Cleveland", "Browns", "Cleveland Browns"),
    "DAL": ("Dallas", "Cowboys", "Dallas Cowboys", "Cowgirls"),
    "DEN": ("Denver", "Broncos", "Denver Broncos"),
    "DET": ("Detroit", "Lions", "Detroit Lions"),
    "GB": ("Green Bay", "Packers", "Green Bay Packers", "Pack"),
    "HOU": ("Houston", "Texans", "Houston Texans"),
    "IND": ("Indianapolis", "Colts", "Indianapolis Colts"),
    "JAX": ("Jacksonville", "Jaguars", "Jacksonville Jaguars", "Jags"),
    "KC": ("Kansas City", "Chiefs", "Kansas City Chiefs"),
    "LV": ("Las Vegas", "Raiders", "Las Vegas Raiders", "Oakland", "Oakland Raiders"),
    "LAC": (
        "Chargers",
        "Los Angeles Chargers",
        "LA Chargers",
        "L.A. Chargers",
        "Bolts",
        "San Diego",
        "San Diego Chargers",
    ),
    "LAR": (
        "Rams",
        "Los Angeles Rams",
        "LA Rams",
        "L.A. Rams",
        "St. Louis",
        "St. Louis Rams",
    ),
    "MIA": ("Miami", "Dolphins", "Miami Dolphins", "Fins"),
    "MIN": ("Minnesota", "Vikings", "Minnesota Vikings", "Vikes", "Skol"),
    "NE": ("New England", "Patriots", "New England Patriots", "Pats"),
    "NO": ("New Orleans", "Saints", "New Orleans Saints"),
    "NYG": ("Giants", "New York Giants", "NY Giants", "N.Y. Giants"),
    "NYJ": ("Jets", "New York Jets", "NY Jets", "N.Y. Jets"),
    "PHI": ("Philadelphia", "Eagles", "Philadelphia Eagles"),
    "PIT": ("Pittsburgh", "Steelers", "Pittsburgh Steelers"),
    "SEA": ("Seattle", "Seahawks", "Seattle Seahawks", "Hawks"),
    "SF": ("San Francisco", "49ers", "San Francisco 49ers", "Niners"),
    "TB": ("Tampa Bay", "Buccaneers", "Tampa Bay Buccaneers", "Bucs"),
    "TEN": ("Tennessee", "Titans", "Tennessee Titans"),
    "WAS": (
        "Washington",
        "Commanders",
        "Washington Commanders",
        "Washington Football Team",
    ),
}
_AMBIGUOUS_TEAM_REFERENCES = frozenset(
    reference.casefold()
    for reference in ("LA", "L.A.", "Los Angeles", "NY", "N.Y.", "New York", "Birds", "Cats")
)
_TEAM_REFERENCE_CODES = {
    reference.casefold(): code
    for code, references in _TEAM_REFERENCE_GROUPS.items()
    for reference in references
}

SubjectType = Literal["player", "team", "unclustered"]
Relation = Literal[
    "origin",
    "independent",
    "corroborating",
    "derivative",
    "contradicting",
]


class EpisodeError(RuntimeError):
    """Base error for invalid inputs or inconsistent stored episode snapshots."""


class EpisodeSnapshotConflictError(EpisodeError):
    """Raised when an existing method/as-of snapshot differs from a rebuild."""


@dataclass(frozen=True)
class EpisodeBuildReport:
    """Counts and audit warnings from one deterministic build."""

    as_of: datetime
    method_version: str
    window_hours: float
    claims_considered: int
    episode_count: int
    membership_count: int
    episodes_inserted: int
    memberships_inserted: int
    unresolved_player_claims: int
    unresolved_player_refs: int
    team_scoped_claims: int
    unclustered_claims: int
    unavailable_text_claims: int
    reused_existing: bool
    run_id: str | None
    dropped_team_references: tuple[str, ...] = ()


@dataclass(frozen=True)
class EpisodeClaimAudit:
    """A relation row plus the retained source text needed for eye-level review."""

    row: EpisodeClaimRow
    item_observed_at: datetime
    title: str | None
    canonical_text: str | None
    outcome_direction: str
    roster_behavior_direction: str


@dataclass(frozen=True)
class EpisodeAudit:
    """One episode and all of its deterministically ordered claim relations."""

    row: NarrativeEpisodeRow
    claims: tuple[EpisodeClaimAudit, ...]


@dataclass(frozen=True)
class _LoadedClaim:
    claim_id: str
    source_item_id: int
    source_id: str
    source_family: str
    claim_dimension: str
    outcome_direction: str
    roster_behavior_direction: str
    item_observed_at: datetime
    # When the story happened as far as the store can tell: the publication time when the
    # feed carried one (never later than observation), else the collector's fetch time.
    # Availability is still gated on observed_at/ingested_at.
    event_at: datetime
    content_sha256: str
    canonical_text: str | None
    tokens: frozenset[str]
    resolved_player_ids: tuple[int, ...]
    unresolved_ref_count: int
    team_refs: tuple[str, ...]


@dataclass(frozen=True)
class _Subject:
    subject_type: SubjectType
    value: int | str


@dataclass(frozen=True)
class _CandidateMember:
    claim: _LoadedClaim
    relation: Relation
    similarity_score: float
    linked_claim_id: str | None
    method: str


@dataclass(frozen=True)
class _CandidateEpisode:
    episode_id: str
    subject: _Subject
    claim_dimension: str
    opened_at: datetime
    last_item_at: datetime
    origin_claim_id: str
    window_hours: float
    unique_source_count: int
    unique_source_family_count: int
    source_entropy: float
    reach_proxy: int
    velocity_per_6h: float
    recency_hours: float
    n_events: int
    item_count: int
    members: tuple[_CandidateMember, ...]


def build_episodes(
    connection: sqlite3.Connection,
    *,
    as_of: datetime,
    window: timedelta = DEFAULT_WINDOW,
    method_version: str = METHOD_VERSION,
    built_at: datetime | None = None,
    max_episode_span: timedelta = DEFAULT_MAX_EPISODE_SPAN,
    lookback: timedelta = DEFAULT_LOOKBACK,
    prompt_version_id: str = DEFAULT_PROMPT_VERSION_ID,
) -> EpisodeBuildReport:
    """Build one immutable method/as-of snapshot from prospectively eligible claims.

    A repeated build is a no-op only when the stored graph exactly matches the graph the
    current inputs produce. This turns late/backfilled pre-cutoff claims or reused method
    versions with changed parameters into loud conflicts instead of mixed snapshots.
    """

    cutoff = ensure_utc(as_of)
    build_time = ensure_utc(built_at or datetime.now(UTC))
    if build_time < cutoff:
        raise EpisodeError("built_at cannot precede the episode as_of cutoff")
    if window <= timedelta(0):
        raise EpisodeError("episode window must be positive")
    window_hours = window.total_seconds() / 3600.0
    if not math.isfinite(window_hours):
        raise EpisodeError("episode window must be finite")
    method = method_version.strip()
    if not method:
        raise EpisodeError("method_version must not be blank")
    if max_episode_span < window:
        raise EpisodeError("max_episode_span must be at least the rolling window")
    if lookback < max_episode_span:
        raise EpisodeError("lookback must be at least max_episode_span")
    prompt_version = prompt_version_id.strip()
    if not prompt_version:
        raise EpisodeError("prompt_version_id must not be blank")

    claims, dropped_team_references = _load_claims(
        connection, cutoff, lookback=lookback, prompt_version_id=prompt_version
    )
    candidates, subject_counts = _cluster_claims(
        claims,
        as_of=cutoff,
        window=window,
        max_episode_span=max_episode_span,
        method_version=method,
        prompt_version_id=prompt_version,
    )
    existing_count = int(
        connection.execute(
            "SELECT count(*) FROM narrative_episodes "
            "WHERE method_version = ? AND prompt_version_id = ? AND as_of = ?",
            (method, prompt_version, utc_timestamp(cutoff)),
        ).fetchone()[0]
    )
    if existing_count:
        stored = _stored_signature(connection, method, cutoff, prompt_version_id=prompt_version)
        if stored != _candidate_signature(candidates):
            raise EpisodeSnapshotConflictError(
                f"stored episode snapshot {method!r} at {utc_timestamp(cutoff)} "
                "differs from the deterministic rebuild"
            )
        return _build_report(
            claims,
            candidates,
            subject_counts,
            cutoff=cutoff,
            method_version=method,
            window_hours=window_hours,
            reused_existing=True,
            run_id=None,
            dropped_team_references=dropped_team_references,
        )

    # An empty build has no graph to persist. It remains a deterministic, explicit report.
    if not candidates:
        return _build_report(
            claims,
            candidates,
            subject_counts,
            cutoff=cutoff,
            method_version=method,
            window_hours=window_hours,
            reused_existing=False,
            run_id=None,
            dropped_team_references=dropped_team_references,
        )

    config_sha256 = hashlib.sha256(
        json.dumps(
            {
                "as_of": utc_timestamp(cutoff),
                "derivative_similarity_threshold": DERIVATIVE_SIMILARITY_THRESHOLD,
                "link_similarity_threshold": LINK_SIMILARITY_THRESHOLD,
                "lookback_hours": lookback.total_seconds() / 3600.0,
                "max_episode_span_hours": max_episode_span.total_seconds() / 3600.0,
                "method_version": method,
                "prompt_version_id": prompt_version,
                "window_hours": window_hours,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    run_id = f"stage2-{uuid4().hex}"
    run = ModelRunRow(
        run_id=run_id,
        run_type="stage_2_episodes",
        started_at=build_time,
        completed_at=None,
        status="running",
        code_version=__version__,
        config_sha256=config_sha256,
        parent_run_id=None,
        error_message=None,
        created_at=build_time,
    )

    connection.execute("SAVEPOINT narrative_episode_build")
    try:
        _insert_row(connection, "model_runs", run)
        for candidate in candidates:
            episode_row = _episode_row(
                candidate,
                as_of=cutoff,
                built_at=build_time,
                method_version=method,
                prompt_version_id=prompt_version,
                run_id=run_id,
            )
            _insert_row(connection, "narrative_episodes", episode_row)
            for member in candidate.members:
                _insert_row(
                    connection,
                    "episode_claims",
                    _episode_claim_row(
                        candidate,
                        member,
                        as_of=cutoff,
                        built_at=build_time,
                        method_version=method,
                        run_id=run_id,
                    ),
                )
        origin_count = int(
            connection.execute(
                """
                SELECT count(*)
                FROM episode_claims AS member
                JOIN narrative_episodes AS episode USING (episode_id)
                WHERE episode.method_version = ? AND episode.as_of = ?
                  AND member.relation = 'origin'
                  AND member.claim_id = episode.origin_claim_id
                """,
                (method, utc_timestamp(cutoff)),
            ).fetchone()[0]
        )
        if origin_count != len(candidates):
            raise EpisodeError("stored episode graph does not have exactly one valid origin each")
        cursor = connection.execute(
            """
            UPDATE model_runs
            SET completed_at = ?, status = 'succeeded'
            WHERE run_id = ? AND status = 'running'
            """,
            (utc_timestamp(max(build_time, ensure_utc(datetime.now(UTC)))), run_id),
        )
        if cursor.rowcount != 1:
            raise EpisodeError(f"could not mark Stage 2 run {run_id!r} succeeded")
    except Exception:
        connection.execute("ROLLBACK TO narrative_episode_build")
        connection.execute("RELEASE narrative_episode_build")
        raise
    else:
        connection.execute("RELEASE narrative_episode_build")

    return _build_report(
        claims,
        candidates,
        subject_counts,
        cutoff=cutoff,
        method_version=method,
        window_hours=window_hours,
        reused_existing=False,
        run_id=run_id,
        dropped_team_references=dropped_team_references,
    )


def load_episode_audits(
    connection: sqlite3.Connection,
    *,
    player_id: int | None = None,
    episode_id: str | None = None,
) -> tuple[EpisodeAudit, ...]:
    """Load episode graphs for the audit CLI; exactly one selector is required."""

    if (player_id is None) == (episode_id is None):
        raise EpisodeError("select exactly one player_id or episode_id")
    if player_id is not None:
        episode_rows = connection.execute(
            """
            SELECT * FROM narrative_episodes
            WHERE subject_type = 'player' AND subject_player_id = ?
            ORDER BY as_of DESC, opened_at DESC, episode_id
            """,
            (player_id,),
        ).fetchall()
    else:
        episode_rows = connection.execute(
            "SELECT * FROM narrative_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchall()

    audits: list[EpisodeAudit] = []
    for episode_db_row in episode_rows:
        episode = NarrativeEpisodeRow.from_db(episode_db_row)
        claim_rows = connection.execute(
            """
            SELECT
                member.*,
                item.observed_at AS item_observed_at,
                item.title AS item_title,
                item.cleaned_text AS item_cleaned_text,
                claim.outcome_direction,
                claim.roster_behavior_direction
            FROM episode_claims AS member
            JOIN source_items AS item USING (source_item_id)
            JOIN claims AS claim USING (claim_id)
            WHERE member.episode_id = ?
            ORDER BY item.observed_at, member.claim_id
            """,
            (episode.episode_id,),
        ).fetchall()
        claims: list[EpisodeClaimAudit] = []
        for claim_db_row in claim_rows:
            relation_values = {key: claim_db_row[key] for key in EpisodeClaimRow.model_fields}
            cleaned_text = claim_db_row["item_cleaned_text"]
            title = claim_db_row["item_title"]
            claims.append(
                EpisodeClaimAudit(
                    row=EpisodeClaimRow.model_validate(relation_values),
                    item_observed_at=_parse_timestamp(str(claim_db_row["item_observed_at"])),
                    title=None if title is None else str(title),
                    canonical_text=(
                        None
                        if cleaned_text is None
                        else normalize_item_text(
                            None if title is None else str(title), str(cleaned_text)
                        )
                    ),
                    outcome_direction=str(claim_db_row["outcome_direction"]),
                    roster_behavior_direction=str(claim_db_row["roster_behavior_direction"]),
                )
            )
        audits.append(EpisodeAudit(row=episode, claims=tuple(claims)))
    return tuple(audits)


def episode_audit_payload(audit: EpisodeAudit) -> dict[str, object]:
    """Return a stable JSON-compatible rendering of one audit graph."""

    episode = audit.row.db_values()
    members: list[dict[str, object]] = []
    for claim in audit.claims:
        payload: dict[str, object] = dict(claim.row.db_values())
        payload.update(
            {
                "canonical_text": claim.canonical_text,
                "item_observed_at": utc_timestamp(claim.item_observed_at),
                "outcome_direction": claim.outcome_direction,
                "roster_behavior_direction": claim.roster_behavior_direction,
                "title": claim.title,
            }
        )
        members.append(payload)
    return {"episode": episode, "claims": members}


def _load_claims(
    connection: sqlite3.Connection,
    as_of: datetime,
    *,
    lookback: timedelta = DEFAULT_LOOKBACK,
    prompt_version_id: str = DEFAULT_PROMPT_VERSION_ID,
) -> tuple[tuple[_LoadedClaim, ...], tuple[str, ...]]:
    cutoff = utc_timestamp(as_of)
    earliest = utc_timestamp(as_of - lookback)
    rows = connection.execute(
        """
        SELECT
            claim.claim_id,
            claim.source_item_id,
            claim.claim_dimension,
            claim.outcome_direction,
            claim.roster_behavior_direction,
            claim.team_refs_json,
            item.source_id,
            item.title,
            item.cleaned_text,
            item.content_sha256,
            item.observed_at AS item_observed_at,
            item.published_at AS item_published_at,
            extraction.source_family
        FROM claims AS claim
        JOIN source_items AS item ON item.source_item_id = claim.source_item_id
        JOIN source_item_extractions AS extraction
          ON extraction.extraction_id = claim.extraction_id
        WHERE extraction.status = 'succeeded'
          AND extraction.prompt_version_id = ?
          AND claim.observed_at <= ? AND claim.ingested_at <= ?
          AND claim.valid_from <= ? AND (claim.valid_to IS NULL OR ? < claim.valid_to)
          AND item.observed_at <= ? AND item.ingested_at <= ?
          AND item.valid_from <= ? AND (item.valid_to IS NULL OR ? < item.valid_to)
          AND item.observed_at >= ?
        ORDER BY item.observed_at, claim.claim_id
        """,
        (prompt_version_id, *((cutoff,) * 8), earliest),
    ).fetchall()
    if not rows:
        return (), ()

    eligible_claim_ids = {str(row["claim_id"]) for row in rows}
    resolved: dict[str, set[int]] = defaultdict(set)
    unresolved: Counter[str] = Counter()
    for ref in connection.execute(
        """
        SELECT claim_id, player_id, unresolved_id
        FROM claim_player_refs
        WHERE observed_at <= ? AND ingested_at <= ? AND valid_from <= ?
          AND (valid_to IS NULL OR ? < valid_to)
        ORDER BY claim_id, ordinal
        """,
        (cutoff,) * 4,
    ):
        claim_id = str(ref["claim_id"])
        if claim_id not in eligible_claim_ids:
            continue
        if ref["player_id"] is not None:
            resolved[claim_id].add(int(ref["player_id"]))
        if ref["unresolved_id"] is not None:
            unresolved[claim_id] += 1

    loaded: list[_LoadedClaim] = []
    dropped_references: set[str] = set()
    for row in rows:
        claim_id = str(row["claim_id"])
        title = row["title"]
        cleaned_text = row["cleaned_text"]
        canonical_text = (
            None
            if cleaned_text is None
            else normalize_item_text(None if title is None else str(title), str(cleaned_text))
        )
        content_sha256 = str(row["content_sha256"])
        if canonical_text is not None:
            actual_sha256 = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
            if actual_sha256 != content_sha256:
                raise EpisodeError(
                    f"source item {row['source_item_id']} canonical text hash drifted"
                )
        raw_team_refs = json.loads(str(row["team_refs_json"]))
        if not isinstance(raw_team_refs, list):
            raise EpisodeError(f"claim {claim_id} team_refs_json is not an array")
        team_refs, dropped = _canonical_team_refs(raw_team_refs)
        dropped_references.update(dropped)
        item_observed_at = _parse_timestamp(str(row["item_observed_at"]))
        published_raw = row["item_published_at"]
        event_at = item_observed_at
        if published_raw is not None:
            event_at = min(item_observed_at, _parse_timestamp(str(published_raw)))
        loaded.append(
            _LoadedClaim(
                claim_id=claim_id,
                source_item_id=int(row["source_item_id"]),
                source_id=str(row["source_id"]),
                source_family=str(row["source_family"]),
                claim_dimension=str(row["claim_dimension"]),
                outcome_direction=str(row["outcome_direction"]),
                roster_behavior_direction=str(row["roster_behavior_direction"]),
                item_observed_at=item_observed_at,
                event_at=event_at,
                content_sha256=content_sha256,
                canonical_text=canonical_text,
                tokens=_tokens(canonical_text),
                resolved_player_ids=tuple(sorted(resolved[claim_id])),
                unresolved_ref_count=unresolved[claim_id],
                team_refs=team_refs,
            )
        )
    return tuple(loaded), tuple(sorted(dropped_references))


def _cluster_claims(
    claims: tuple[_LoadedClaim, ...],
    *,
    as_of: datetime,
    window: timedelta,
    method_version: str,
    max_episode_span: timedelta = DEFAULT_MAX_EPISODE_SPAN,
    prompt_version_id: str = DEFAULT_PROMPT_VERSION_ID,
) -> tuple[tuple[_CandidateEpisode, ...], dict[str, set[str]]]:
    grouped: dict[tuple[str, str, str], list[_LoadedClaim]] = defaultdict(list)
    team_scoped: set[str] = set()
    unclustered: set[str] = set()
    for claim in claims:
        subjects = _subjects_for_claim(claim)
        for subject in subjects:
            grouped[(subject.subject_type, str(subject.value), claim.claim_dimension)].append(claim)
            if subject.subject_type == "team":
                team_scoped.add(claim.claim_id)
            elif subject.subject_type == "unclustered":
                unclustered.add(claim.claim_id)

    candidates: list[_CandidateEpisode] = []
    for group_key in sorted(grouped):
        subject_type_text, subject_value_text, claim_dimension = group_key
        subject = _subject_from_group(subject_type_text, subject_value_text)
        ordered = sorted(
            grouped[group_key],
            key=lambda claim: (claim.event_at, claim.claim_id),
        )
        session: list[_LoadedClaim] = []
        for claim in ordered:
            if session and (
                claim.event_at - session[-1].event_at > window
                or claim.event_at - session[0].event_at > max_episode_span
            ):
                candidates.append(
                    _make_candidate(
                        subject,
                        claim_dimension,
                        session,
                        as_of=as_of,
                        window=window,
                        method_version=method_version,
                        prompt_version_id=prompt_version_id,
                    )
                )
                session = []
            session.append(claim)
        if session:
            candidates.append(
                _make_candidate(
                    subject,
                    claim_dimension,
                    session,
                    as_of=as_of,
                    window=window,
                    method_version=method_version,
                    prompt_version_id=prompt_version_id,
                )
            )
    candidates.sort(key=lambda episode: episode.episode_id)
    return tuple(candidates), {"team_scoped": team_scoped, "unclustered": unclustered}


def _subjects_for_claim(claim: _LoadedClaim) -> tuple[_Subject, ...]:
    subjects: set[_Subject] = {
        _Subject("player", player_id) for player_id in claim.resolved_player_ids
    }
    needs_non_player_subject = claim.unresolved_ref_count > 0 or not claim.resolved_player_ids
    if needs_non_player_subject:
        if len(claim.team_refs) == 1:
            subjects.add(_Subject("team", claim.team_refs[0]))
        else:
            subjects.add(_Subject("unclustered", f"claim:{claim.claim_id}"))
    return tuple(sorted(subjects, key=lambda subject: (subject.subject_type, str(subject.value))))


def _canonical_team_refs(raw_team_refs: list[object]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Canonical codes plus the raw references that could not be mapped (for the report)."""

    codes: set[str] = set()
    dropped: set[str] = set()
    for raw_reference in raw_team_refs:
        reference = str(raw_reference).strip()
        if not reference:
            continue
        if reference.casefold() in _AMBIGUOUS_TEAM_REFERENCES:
            dropped.add(reference)
            continue
        mapped = _TEAM_REFERENCE_CODES.get(reference.casefold())
        if mapped is not None:
            codes.add(mapped)
            continue
        # Only an explicit code (case-insensitive) may pass through normalization; a stray
        # word such as "no" must not become NO.
        if reference.upper() == reference:
            normalized_code = normalize_team_code(reference)
            if normalized_code in CANONICAL_TEAM_CODES:
                codes.add(normalized_code)
                continue
        dropped.add(reference)
    return tuple(sorted(codes)), tuple(sorted(dropped))


def _subject_from_group(subject_type: str, value: str) -> _Subject:
    if subject_type == "player":
        return _Subject("player", int(value))
    if subject_type == "team":
        return _Subject("team", value)
    if subject_type == "unclustered":
        return _Subject("unclustered", value)
    raise AssertionError(f"unknown episode subject type {subject_type!r}")


def _make_candidate(
    subject: _Subject,
    claim_dimension: str,
    claims: list[_LoadedClaim],
    *,
    as_of: datetime,
    window: timedelta,
    method_version: str,
    prompt_version_id: str = DEFAULT_PROMPT_VERSION_ID,
) -> _CandidateEpisode:
    members: list[_CandidateMember] = []
    for claim in claims:
        if not members:
            members.append(_CandidateMember(claim, "origin", 1.0, None, "deterministic-origin"))
            continue
        members.append(_relation_to_prior(claim, members))

    opened_at = min(member.claim.event_at for member in members)
    last_item_at = max(member.claim.event_at for member in members)
    unique_items: dict[int, _LoadedClaim] = {}
    item_relations: dict[int, set[Relation]] = defaultdict(set)
    for member in members:
        unique_items.setdefault(member.claim.source_item_id, member.claim)
        item_relations[member.claim.source_item_id].add(member.relation)
    source_counts = Counter(item.source_id for item in unique_items.values())
    item_count = len(unique_items)
    source_entropy = max(
        0.0,
        -sum(
            (count / item_count) * math.log(count / item_count) for count in source_counts.values()
        ),
    )
    # One family per unique source (the family of its latest item), so a source that was
    # reclassified mid-season cannot make families outnumber sources.
    family_by_source: dict[str, str] = {}
    for item in sorted(unique_items.values(), key=lambda item: (item.event_at, item.claim_id)):
        family_by_source[item.source_id] = item.source_family
    source_families = set(family_by_source.values())
    event_relations = {"origin", "independent", "corroborating"}
    n_events = sum(bool(relations & event_relations) for relations in item_relations.values())
    non_derivative_items = [
        unique_items[item_id]
        for item_id, relations in item_relations.items()
        if relations != {"derivative"}
    ]
    last_non_derivative = max(item.event_at for item in non_derivative_items)
    duration_hours = (last_item_at - opened_at).total_seconds() / 3600.0
    velocity_per_6h = item_count / max(duration_hours / 6.0, 1.0)
    origin_claim_id = members[0].claim.claim_id
    episode_id = _episode_id(
        method_version=method_version,
        as_of=as_of,
        subject=subject,
        claim_dimension=claim_dimension,
        origin_claim_id=origin_claim_id,
        prompt_version_id=prompt_version_id,
    )
    return _CandidateEpisode(
        episode_id=episode_id,
        subject=subject,
        claim_dimension=claim_dimension,
        opened_at=opened_at,
        last_item_at=last_item_at,
        origin_claim_id=origin_claim_id,
        window_hours=window.total_seconds() / 3600.0,
        unique_source_count=len(source_counts),
        unique_source_family_count=len(source_families),
        source_entropy=source_entropy,
        reach_proxy=len(source_counts),
        velocity_per_6h=velocity_per_6h,
        recency_hours=(as_of - last_non_derivative).total_seconds() / 3600.0,
        n_events=n_events,
        item_count=item_count,
        members=tuple(members),
    )


def _relation_to_prior(
    claim: _LoadedClaim,
    prior_members: list[_CandidateMember],
) -> _CandidateMember:
    # Text similarity decides whether a link exists; direction only decides its label. A
    # byte-identical copy is derivative whatever its extracted directions say (the first
    # live corpus had unknown/neutral directions on half its claims), and a near-copy from
    # the same source is a repost, not a second event.
    best_link: tuple[float, str, _CandidateMember, str] | None = None
    best_opposing_link: tuple[float, str, _CandidateMember, str] | None = None
    max_similarity = 0.0
    claim_hash = claim.content_sha256
    claim_tokens = claim.tokens
    claim_outcome_direction = claim.outcome_direction
    claim_roster_behavior_direction = claim.roster_behavior_direction
    for prior in prior_members:
        prior_claim = prior.claim
        if claim_hash == prior_claim.content_sha256:
            similarity, similarity_method = 1.0, "exact-canonical-content-sha256"
        elif not claim_tokens or not prior_claim.tokens:
            similarity, similarity_method = 0.0, "token-set-jaccard-unavailable-text"
        else:
            similarity = len(claim_tokens & prior_claim.tokens) / len(
                claim_tokens | prior_claim.tokens
            )
            similarity_method = "token-set-jaccard"
        if similarity > max_similarity:
            max_similarity = similarity
        if similarity < LINK_SIMILARITY_THRESHOLD:
            continue
        prior_claim_id = prior_claim.claim_id
        if (
            best_link is None
            or similarity > best_link[0]
            or (similarity == best_link[0] and prior_claim_id < best_link[1])
        ):
            best_link = (similarity, prior_claim_id, prior, similarity_method)
        directions_oppose = (
            (claim_outcome_direction == "increase" and prior_claim.outcome_direction == "decrease")
            or (
                claim_outcome_direction == "decrease"
                and prior_claim.outcome_direction == "increase"
            )
            or (
                claim_roster_behavior_direction == "increase"
                and prior_claim.roster_behavior_direction == "decrease"
            )
            or (
                claim_roster_behavior_direction == "decrease"
                and prior_claim.roster_behavior_direction == "increase"
            )
        )
        if directions_oppose and (
            best_opposing_link is None
            or similarity > best_opposing_link[0]
            or (similarity == best_opposing_link[0] and prior_claim_id < best_opposing_link[1])
        ):
            best_opposing_link = (similarity, prior_claim_id, prior, similarity_method)
    if best_link is None:
        return _CandidateMember(
            claim=claim,
            relation="independent",
            similarity_score=max_similarity,
            linked_claim_id=None,
            method="rolling-window-no-text-link",
        )

    # A contradiction outranks everything: "will start" against "will not start" is a
    # near-copy by tokens and the opposite claim by direction, and the direction is the
    # point. Otherwise a near-copy is derivative and any other link corroborates.
    if best_opposing_link is not None:
        similarity, _, linked, similarity_method = best_opposing_link
        relation: Relation = "contradicting"
    else:
        similarity, _, linked, similarity_method = best_link
        relation = (
            "derivative" if similarity >= DERIVATIVE_SIMILARITY_THRESHOLD else "corroborating"
        )
    return _CandidateMember(
        claim=claim,
        relation=relation,
        similarity_score=similarity,
        linked_claim_id=linked.claim.claim_id,
        method=similarity_method,
    )


def _tokens(text: str | None) -> frozenset[str]:
    if text is None:
        return frozenset()
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    without_marks = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    tokens = re.findall(r"[^\W_]+", without_marks, flags=re.UNICODE)
    return frozenset(
        token for token in tokens if token not in _STOP_WORDS and token not in _TEAM_TOKENS
    )


_TEAM_TOKENS = frozenset(
    token
    for references in _TEAM_REFERENCE_GROUPS.values()
    for reference in references
    for token in re.findall(r"[^\W_]+", reference.casefold())
) | frozenset(code.casefold() for code in _TEAM_REFERENCE_GROUPS)


def _episode_id(
    *,
    method_version: str,
    as_of: datetime,
    subject: _Subject,
    claim_dimension: str,
    origin_claim_id: str,
    prompt_version_id: str = DEFAULT_PROMPT_VERSION_ID,
) -> str:
    payload = json.dumps(
        {
            "as_of": utc_timestamp(as_of),
            "claim_dimension": claim_dimension,
            "method_version": method_version,
            "origin_claim_id": origin_claim_id,
            "prompt_version_id": prompt_version_id,
            "subject_type": subject.subject_type,
            "subject_value": subject.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "episode-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _episode_row(
    candidate: _CandidateEpisode,
    *,
    as_of: datetime,
    built_at: datetime,
    method_version: str,
    run_id: str,
    prompt_version_id: str = DEFAULT_PROMPT_VERSION_ID,
) -> NarrativeEpisodeRow:
    return NarrativeEpisodeRow(
        episode_id=candidate.episode_id,
        subject_type=candidate.subject.subject_type,
        subject_player_id=(
            int(candidate.subject.value) if candidate.subject.subject_type == "player" else None
        ),
        subject_team_code=(
            str(candidate.subject.value) if candidate.subject.subject_type == "team" else None
        ),
        unclustered_key=(
            str(candidate.subject.value)
            if candidate.subject.subject_type == "unclustered"
            else None
        ),
        claim_dimension=candidate.claim_dimension,  # type: ignore[arg-type]
        opened_at=candidate.opened_at,
        last_item_at=candidate.last_item_at,
        origin_claim_id=candidate.origin_claim_id,
        method_version=method_version,
        prompt_version_id=prompt_version_id,
        as_of=as_of,
        window_hours=candidate.window_hours,
        unique_source_count=candidate.unique_source_count,
        unique_source_family_count=candidate.unique_source_family_count,
        source_entropy=candidate.source_entropy,
        reach_proxy=candidate.reach_proxy,
        velocity_per_6h=candidate.velocity_per_6h,
        recency_hours=candidate.recency_hours,
        n_events=candidate.n_events,
        item_count=candidate.item_count,
        source=DERIVATION_SOURCE,
        published_at=None,
        observed_at=built_at,
        ingested_at=built_at,
        effective_at=as_of,
        valid_from=built_at,
        valid_to=None,
        source_version=method_version,
        run_id=run_id,
    )


def _episode_claim_row(
    candidate: _CandidateEpisode,
    member: _CandidateMember,
    *,
    as_of: datetime,
    built_at: datetime,
    method_version: str,
    run_id: str,
) -> EpisodeClaimRow:
    return EpisodeClaimRow(
        episode_id=candidate.episode_id,
        claim_id=member.claim.claim_id,
        source_item_id=member.claim.source_item_id,
        source_id=member.claim.source_id,
        source_family=member.claim.source_family,
        relation=member.relation,
        similarity_score=member.similarity_score,
        linked_claim_id=member.linked_claim_id,
        method=member.method,
        method_version=method_version,
        as_of=as_of,
        source=DERIVATION_SOURCE,
        published_at=None,
        observed_at=built_at,
        ingested_at=built_at,
        effective_at=as_of,
        valid_from=built_at,
        valid_to=None,
        source_version=method_version,
        run_id=run_id,
    )


def _insert_row(
    connection: sqlite3.Connection,
    table: str,
    row: ModelRunRow | NarrativeEpisodeRow | EpisodeClaimRow,
) -> None:
    values = row.db_values()
    columns = ", ".join(values)
    placeholders = ", ".join(f":{column}" for column in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        values,
    )


def _build_report(
    claims: tuple[_LoadedClaim, ...],
    candidates: tuple[_CandidateEpisode, ...],
    subject_counts: dict[str, set[str]],
    *,
    cutoff: datetime,
    method_version: str,
    window_hours: float,
    reused_existing: bool,
    run_id: str | None,
    dropped_team_references: tuple[str, ...] = (),
) -> EpisodeBuildReport:
    memberships = sum(len(candidate.members) for candidate in candidates)
    unresolved_claim_ids = {claim.claim_id for claim in claims if claim.unresolved_ref_count}
    return EpisodeBuildReport(
        as_of=cutoff,
        method_version=method_version,
        window_hours=window_hours,
        claims_considered=len(claims),
        episode_count=len(candidates),
        membership_count=memberships,
        episodes_inserted=0 if reused_existing else len(candidates),
        memberships_inserted=0 if reused_existing else memberships,
        unresolved_player_claims=len(unresolved_claim_ids),
        unresolved_player_refs=sum(claim.unresolved_ref_count for claim in claims),
        team_scoped_claims=len(subject_counts["team_scoped"]),
        unclustered_claims=len(subject_counts["unclustered"]),
        unavailable_text_claims=sum(claim.canonical_text is None for claim in claims),
        reused_existing=reused_existing,
        run_id=run_id,
        dropped_team_references=dropped_team_references,
    )


def _candidate_signature(candidates: tuple[_CandidateEpisode, ...]) -> str:
    payload = [_candidate_payload(candidate) for candidate in candidates]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _candidate_payload(candidate: _CandidateEpisode) -> dict[str, object]:
    return {
        "episode": {
            "as_of": None,  # supplied by the snapshot selector, not repeated in candidates
            "claim_dimension": candidate.claim_dimension,
            "episode_id": candidate.episode_id,
            "item_count": candidate.item_count,
            "last_item_at": utc_timestamp(candidate.last_item_at),
            "n_events": candidate.n_events,
            "opened_at": utc_timestamp(candidate.opened_at),
            "origin_claim_id": candidate.origin_claim_id,
            "reach_proxy": candidate.reach_proxy,
            "recency_hours": candidate.recency_hours,
            "source_entropy": candidate.source_entropy,
            "subject_player_id": (
                candidate.subject.value if candidate.subject.subject_type == "player" else None
            ),
            "subject_team_code": (
                candidate.subject.value if candidate.subject.subject_type == "team" else None
            ),
            "subject_type": candidate.subject.subject_type,
            "unclustered_key": (
                candidate.subject.value if candidate.subject.subject_type == "unclustered" else None
            ),
            "unique_source_count": candidate.unique_source_count,
            "unique_source_family_count": candidate.unique_source_family_count,
            "velocity_per_6h": candidate.velocity_per_6h,
            "window_hours": candidate.window_hours,
        },
        "members": [
            {
                "claim_id": member.claim.claim_id,
                "episode_id": candidate.episode_id,
                "linked_claim_id": member.linked_claim_id,
                "method": member.method,
                "relation": member.relation,
                "similarity_score": member.similarity_score,
                "source_family": member.claim.source_family,
                "source_id": member.claim.source_id,
                "source_item_id": member.claim.source_item_id,
            }
            for member in candidate.members
        ],
    }


def _stored_signature(
    connection: sqlite3.Connection,
    method_version: str,
    as_of: datetime,
    *,
    prompt_version_id: str = DEFAULT_PROMPT_VERSION_ID,
) -> str:
    cutoff = utc_timestamp(as_of)
    episode_rows = connection.execute(
        """
        SELECT * FROM narrative_episodes
        WHERE method_version = ? AND prompt_version_id = ? AND as_of = ?
        ORDER BY episode_id
        """,
        (method_version, prompt_version_id, cutoff),
    ).fetchall()
    payload: list[dict[str, object]] = []
    for episode in episode_rows:
        members = connection.execute(
            """
            SELECT * FROM episode_claims
            WHERE episode_id = ?
            ORDER BY (SELECT min(coalesce(item.published_at, item.observed_at), item.observed_at)
                      FROM source_items AS item
                      WHERE item.source_item_id = episode_claims.source_item_id), claim_id
            """,
            (episode["episode_id"],),
        ).fetchall()
        payload.append(
            {
                "episode": {
                    "as_of": None,
                    "claim_dimension": episode["claim_dimension"],
                    "episode_id": episode["episode_id"],
                    "item_count": episode["item_count"],
                    "last_item_at": episode["last_item_at"],
                    "n_events": episode["n_events"],
                    "opened_at": episode["opened_at"],
                    "origin_claim_id": episode["origin_claim_id"],
                    "reach_proxy": episode["reach_proxy"],
                    "recency_hours": episode["recency_hours"],
                    "source_entropy": episode["source_entropy"],
                    "subject_player_id": episode["subject_player_id"],
                    "subject_team_code": episode["subject_team_code"],
                    "subject_type": episode["subject_type"],
                    "unclustered_key": episode["unclustered_key"],
                    "unique_source_count": episode["unique_source_count"],
                    "unique_source_family_count": episode["unique_source_family_count"],
                    "velocity_per_6h": episode["velocity_per_6h"],
                    "window_hours": episode["window_hours"],
                },
                "members": [
                    {
                        "claim_id": member["claim_id"],
                        "episode_id": member["episode_id"],
                        "linked_claim_id": member["linked_claim_id"],
                        "method": member["method"],
                        "relation": member["relation"],
                        "similarity_score": member["similarity_score"],
                        "source_family": member["source_family"],
                        "source_id": member["source_id"],
                        "source_item_id": member["source_item_id"],
                    }
                    for member in members
                ],
            }
        )
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ensure_utc(parsed)
