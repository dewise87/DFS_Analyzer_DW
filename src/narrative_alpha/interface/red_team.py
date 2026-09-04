"""Stage 5 red-team review: the five questions, answered from the store, before a human.

§5.3 Stage 5 asks five things of the largest proposed changes. A model could be asked to
answer them; this module does not. Every answer here is a bounded point-in-time query
against rows the decision already froze, so the section renders identically on every
replay and cannot invent a reason a player moved.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from narrative_alpha.ownership_routing import AppliedOwnershipDelta, OwnershipRouting
from narrative_alpha.portfolio import CandidatePlayer, Lineup
from narrative_alpha.replay import PointInTimeSession

#: §5.3 Stage 5 reviews "the largest proposed changes"; ten is what fits on one screen
#: beside the rosters without the operator scrolling past the thing they must judge.
RED_TEAM_LIMIT = 10


class RedTeamAnswer(BaseModel):
    """One applied delta with all five Stage 5 questions answered deterministically."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    player_id: int
    name: str
    team: str
    position: str
    baseline_ownership: float = Field(ge=0, le=1)
    applied_ownership: float = Field(ge=0, le=1)
    delta_points: float
    episode_count: int = Field(ge=0)
    evidence_ref_count: int = Field(ge=0)

    # 1 — contrary evidence inside the same episode window.
    contrary_claim_count: int = Field(ge=0)
    contrary_claim_ids: tuple[str, ...] = ()

    # 2 — did the purchased baseline already move?
    baseline_observation_count: int = Field(ge=0)
    baseline_first_ownership: float | None = None
    baseline_last_ownership: float | None = None
    baseline_move_points: float | None = None

    # 3 — duplicate-source illusion.
    episode_item_count: int = Field(ge=0)
    unique_source_count: int = Field(ge=0)
    unique_source_family_count: int = Field(ge=0)
    duplicate_item_count: int = Field(ge=0)
    derivative_claim_count: int = Field(ge=0)

    # 4 — confounders present as of the decision.
    confounders: tuple[str, ...] = ()

    # 5 — the "do nothing" case.
    lineups_containing_player: int = Field(ge=0)
    lineup_ownership_sum_delta_points: float
    optimizer_reads_ownership: bool
    do_nothing_case: str


def build_red_team_review(
    session: PointInTimeSession,
    routing: OwnershipRouting,
    *,
    slate_id: int,
    site: str,
    decision_at: datetime,
    candidates: Mapping[int, CandidatePlayer],
    lineups: Sequence[Lineup],
    optimizer_reads_ownership: bool,
    limit: int = RED_TEAM_LIMIT,
) -> tuple[RedTeamAnswer, ...]:
    """Answer Stage 5 for the ``limit`` largest applied deltas, or nothing when unrouted."""

    if not routing.applied:
        return ()
    return tuple(
        _answer(
            session,
            delta,
            slate_id=slate_id,
            site=site,
            decision_at=decision_at,
            candidates=candidates,
            lineups=lineups,
            optimizer_reads_ownership=optimizer_reads_ownership,
        )
        for delta in routing.largest_deltas(limit)
    )


def render_red_team_review(answers: Sequence[RedTeamAnswer]) -> str:
    """Render the Stage 5 block; each answer is one labelled line, nothing summarized."""

    if not answers:
        return "red_team_status=not applicable — the decision used the vendor baseline\n"
    lines = [f"red_team_status=available — {len(answers)} largest applied delta(s)\n"]
    if not any(answer.optimizer_reads_ownership for answer in answers):
        # Said once, at the block level, because it is the largest fact on the page: with
        # no ownership_sum_range on the request, Stage 4 changed the request bytes and the
        # reported ownership sums, and not one roster.
        lines.append(
            "red_team_scope=this request set no ownership_sum_range, so the optimizer never "
            "read ownership: Stage 4 routing changed the decision's request bytes and its "
            "reported ownership sums, and no roster\n"
        )
    for answer in answers:
        lines.append(
            f"player_id={answer.player_id} name={answer.name} team={answer.team} "
            f"position={answer.position}\n"
        )
        lines.append(
            f"  delta                 {answer.baseline_ownership * 100:.2f}pt -> "
            f"{answer.applied_ownership * 100:.2f}pt ({answer.delta_points:+.2f}pt) "
            f"from {answer.episode_count} episode(s), "
            f"{answer.evidence_ref_count} evidence ref(s)\n"
        )
        lines.append(
            f"  contrary_evidence     {answer.contrary_claim_count} contradicting claim(s)"
            + (
                "\n"
                if not answer.contrary_claim_ids
                else " — " + ", ".join(answer.contrary_claim_ids) + "\n"
            )
        )
        lines.append(f"  baseline_already_moved {_baseline_sentence(answer)}\n")
        lines.append(
            f"  duplicate_sources     {answer.episode_item_count} item(s) from "
            f"{answer.unique_source_count} source(s) in "
            f"{answer.unique_source_family_count} family/families; "
            f"{answer.duplicate_item_count} duplicate item(s), "
            f"{answer.derivative_claim_count} derivative claim(s)\n"
        )
        lines.append(
            "  confounders           "
            + (
                "none recorded as of the decision"
                if not answer.confounders
                else "; ".join(answer.confounders)
            )
            + "\n"
        )
        lines.append(f"  do_nothing            {answer.do_nothing_case}\n")
    return "".join(lines)


def _baseline_sentence(answer: RedTeamAnswer) -> str:
    first = answer.baseline_first_ownership
    last = answer.baseline_last_ownership
    move = answer.baseline_move_points
    if move is None or first is None or last is None:
        return (
            f"{answer.baseline_observation_count} vendor baseline observation(s) before the "
            "decision, so no vendor move can be measured"
        )
    return (
        f"the vendor baseline moved {move:+.2f}pt between its last two captures "
        f"({first * 100:.2f}pt -> {last * 100:.2f}pt; "
        f"{answer.baseline_observation_count} capture(s) from this source before the decision)"
    )


def _answer(
    session: PointInTimeSession,
    delta: AppliedOwnershipDelta,
    *,
    slate_id: int,
    site: str,
    decision_at: datetime,
    candidates: Mapping[int, CandidatePlayer],
    lineups: Sequence[Lineup],
    optimizer_reads_ownership: bool,
) -> RedTeamAnswer:
    candidate = candidates.get(delta.player_id)
    episodes = _episode_totals(session, delta.episode_ids, as_of=decision_at)
    contrary, derivative = _claim_relations(session, delta.episode_ids, as_of=decision_at)
    baseline = _baseline_history(
        session,
        player_id=delta.player_id,
        slate_id=slate_id,
        site=site,
        role=delta.role,
        as_of=decision_at,
    )
    confounders = _confounders(
        session,
        player_id=delta.player_id,
        slate_id=slate_id,
        site=site,
        as_of=decision_at,
    )
    appearances = sum(
        1
        for lineup in lineups
        if any(
            player.player_id == delta.player_id
            and (
                delta.role == "classic"
                or (delta.role == "captain" and player.slot in {"CPT", "MVP"})
                or (delta.role == "flex" and player.slot not in {"CPT", "MVP"})
            )
            for player in lineup.players
        )
    )
    return RedTeamAnswer(
        player_id=delta.player_id,
        name="unknown" if candidate is None else candidate.name,
        team="UNK" if candidate is None else candidate.team,
        position=delta.position,
        baseline_ownership=delta.baseline_ownership,
        applied_ownership=delta.applied_ownership,
        delta_points=round(delta.delta_points, 6),
        episode_count=len(delta.episode_ids),
        evidence_ref_count=len(delta.evidence_refs),
        contrary_claim_count=len(contrary),
        contrary_claim_ids=contrary,
        baseline_observation_count=baseline[0],
        baseline_first_ownership=baseline[1],
        baseline_last_ownership=baseline[2],
        baseline_move_points=baseline[3],
        episode_item_count=episodes[0],
        unique_source_count=episodes[1],
        unique_source_family_count=episodes[2],
        duplicate_item_count=max(episodes[0] - episodes[1], 0),
        derivative_claim_count=derivative,
        confounders=confounders,
        lineups_containing_player=appearances,
        lineup_ownership_sum_delta_points=round(delta.delta_points * appearances, 6),
        optimizer_reads_ownership=optimizer_reads_ownership,
        do_nothing_case=_do_nothing(delta, appearances, optimizer_reads_ownership),
    )


def _do_nothing(
    delta: AppliedOwnershipDelta, appearances: int, optimizer_reads_ownership: bool
) -> str:
    """State exactly what reverting this one player to the vendor baseline would change."""

    if appearances == 0:
        rostered = "this player is in none of the generated lineups"
    else:
        rostered = (
            f"this player is in {appearances} generated lineup(s), whose reported ownership "
            f"sum would move {-delta.delta_points * appearances:+.2f}pt in total"
        )
    if not optimizer_reads_ownership:
        role_effect = "; the captain swap would stay unchanged" if delta.role == "captain" else ""
        return (
            f"{rostered}; this request set no ownership_sum_range, so the optimizer "
            f"objective never read ownership and no roster would change{role_effect}"
        )
    role_effect = (
        "; reverting captain ownership could change the captain swap"
        if delta.role == "captain"
        else ""
    )
    return (
        f"{rostered}; this request constrained the lineup ownership sum, so reverting to "
        f"the baseline could change which rosters satisfy that constraint{role_effect}"
    )


def _episode_totals(
    session: PointInTimeSession, episode_ids: Sequence[str], *, as_of: datetime
) -> tuple[int, int, int]:
    if not episode_ids:
        return 0, 0, 0
    placeholders = ", ".join(f":episode_{index}" for index in range(len(episode_ids)))
    parameters = {f"episode_{index}": value for index, value in enumerate(episode_ids)}
    # Distinct over the whole episode set, not summed per episode: a source that appears
    # in three of the player's episodes is one source, and counting it three times would
    # hide exactly the duplicate-source illusion this question exists to expose.
    rows = session.query(
        f"""
        SELECT count(DISTINCT ec.source_item_id) AS item_count,
               count(DISTINCT ec.source_id) AS unique_source_count,
               count(DISTINCT ec.source_family) AS unique_source_family_count
        FROM episode_claims AS ec
        WHERE ec.episode_id IN ({placeholders})
          AND rtrim(ec.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ec.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ec.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (ec.valid_to IS NULL OR rtrim(ec.valid_to, 'Z') > rtrim(:as_of, 'Z'))
        """,
        parameters,
        as_of=as_of,
    )
    row = rows[0]
    return (
        int(row["item_count"]),
        int(row["unique_source_count"]),
        int(row["unique_source_family_count"]),
    )


def _claim_relations(
    session: PointInTimeSession, episode_ids: Sequence[str], *, as_of: datetime
) -> tuple[tuple[str, ...], int]:
    if not episode_ids:
        return (), 0
    placeholders = ", ".join(f":episode_{index}" for index in range(len(episode_ids)))
    parameters = {f"episode_{index}": value for index, value in enumerate(episode_ids)}
    rows = session.query(
        f"""
        SELECT claim_id, relation
        FROM episode_claims
        WHERE episode_id IN ({placeholders})
          AND relation IN ('contradicting', 'derivative')
          AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(:as_of, 'Z'))
        ORDER BY relation, claim_id
        """,
        parameters,
        as_of=as_of,
    )
    contrary = tuple(
        str(row["claim_id"]) for row in rows if str(row["relation"]) == "contradicting"
    )
    derivative = sum(1 for row in rows if str(row["relation"]) == "derivative")
    return contrary, derivative


def _baseline_history(
    session: PointInTimeSession,
    *,
    player_id: int,
    slate_id: int,
    site: str,
    role: str,
    as_of: datetime,
) -> tuple[int, float | None, float | None, float | None]:
    rows = session.query(
        """
        SELECT ownership, source
        FROM ownership_baselines
        WHERE slate_id = :slate_id AND player_id = :player_id AND site = :site
          AND role = :role
          AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(:as_of, 'Z'))
        ORDER BY rtrim(observed_at, 'Z'), ownership_baseline_id
        """,
        {"slate_id": slate_id, "player_id": player_id, "site": site, "role": role},
        as_of=as_of,
    )
    if not rows:
        return 0, None, None, None
    # "Did the baseline already move" means the newest vendor's last two captures — the
    # Saturday and Sunday numbers — never first-ever versus last, and never one vendor's
    # number subtracted from another's.
    source = str(rows[-1]["source"])
    same_source = [float(row["ownership"]) for row in rows if str(row["source"]) == source]
    last = same_source[-1]
    if len(same_source) < 2:
        return len(same_source), None, last, None
    previous = same_source[-2]
    return len(same_source), previous, last, round((last - previous) * 100.0, 6)


def _confounders(
    session: PointInTimeSession,
    *,
    player_id: int,
    slate_id: int,
    site: str,
    as_of: datetime,
) -> tuple[str, ...]:
    """Name every non-narrative input that also changed before the decision."""

    found: list[str] = []
    availability = session.query(
        """
        SELECT availability_status
        FROM player_availability
        WHERE slate_id = :slate_id AND player_id = :player_id AND site = :site
          AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(:as_of, 'Z'))
        ORDER BY rtrim(observed_at, 'Z'), availability_id
        """,
        {"slate_id": slate_id, "player_id": player_id, "site": site},
        as_of=as_of,
    )
    if availability:
        statuses = [str(row["availability_status"]) for row in availability]
        found.append(f"availability: {len(statuses)} official row(s), newest {statuses[-1]}")
    odds = session.query(
        """
        SELECT o.total, o.home_spread
        FROM odds_snapshots AS o
        JOIN salaries AS s ON s.game_id = o.game_id
        WHERE s.slate_id = :slate_id AND s.player_id = :player_id
          AND rtrim(s.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(s.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (s.valid_to IS NULL OR rtrim(s.valid_to, 'Z') > rtrim(:as_of, 'Z'))
          AND rtrim(o.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(o.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(o.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (o.valid_to IS NULL OR rtrim(o.valid_to, 'Z') > rtrim(:as_of, 'Z'))
        ORDER BY rtrim(o.observed_at, 'Z'), o.odds_snapshot_id
        """,
        {"slate_id": slate_id, "player_id": player_id},
        as_of=as_of,
    )
    if odds:
        found.append(
            f"odds: {len(odds)} observation(s), "
            f"total {_move(odds, 'total')}, home spread {_move(odds, 'home_spread')}"
        )
    weather = session.query(
        """
        SELECT w.precipitation_probability, w.wind_gust_kph
        FROM weather_snapshots AS w
        JOIN salaries AS s ON s.game_id = w.game_id
        WHERE s.slate_id = :slate_id AND s.player_id = :player_id
          AND rtrim(s.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(s.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (s.valid_to IS NULL OR rtrim(s.valid_to, 'Z') > rtrim(:as_of, 'Z'))
          AND rtrim(w.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(w.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(w.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (w.valid_to IS NULL OR rtrim(w.valid_to, 'Z') > rtrim(:as_of, 'Z'))
        ORDER BY rtrim(w.observed_at, 'Z'), w.weather_snapshot_id
        """,
        {"slate_id": slate_id, "player_id": player_id},
        as_of=as_of,
    )
    if weather:
        newest = weather[-1]
        found.append(
            f"weather: {len(weather)} forecast(s), newest precipitation "
            f"{_optional_number(newest['precipitation_probability'])}, gust "
            f"{_optional_number(newest['wind_gust_kph'])}"
        )
    return tuple(found)


def _move(rows: Sequence[sqlite3.Row], column: str) -> str:
    values = [float(row[column]) for row in rows if row[column] is not None]
    if len(values) < 2:
        return "unchanged (fewer than two observations)"
    change = values[-1] - values[0]
    if math.isclose(change, 0.0, abs_tol=1e-9):
        return "unchanged"
    return f"{values[0]:.2f} -> {values[-1]:.2f} ({change:+.2f})"


def _optional_number(value: object) -> str:
    if value is None:
        return "unavailable"
    return f"{float(str(value)):.2f}"
