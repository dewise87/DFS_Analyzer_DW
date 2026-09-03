"""The one Stage 2 episode-snapshot operation shared by operator lanes."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime

from narrative_alpha.narrative.episodes import EpisodeBuildReport
from narrative_alpha.ops.runs import OpsStepStatus

EpisodeStep = Callable[..., EpisodeBuildReport]


def build_episode_snapshot(
    build_episodes: EpisodeStep,
    connection: sqlite3.Connection,
    *,
    as_of: datetime,
    built_at: datetime,
) -> tuple[OpsStepStatus, dict[str, object], str | None]:
    """Build and commit one append-only Stage 2 snapshot.

    Both the batch and slate lanes deliberately use this one adapter so their snapshots
    call the same Stage 2 library function and record the same audit counts.
    """

    report = build_episodes(connection, as_of=as_of, built_at=built_at)
    connection.commit()
    return (
        "succeeded",
        {
            "method_version": report.method_version,
            "claims_considered": report.claims_considered,
            "episode_count": report.episode_count,
            "episodes_inserted": report.episodes_inserted,
            "membership_count": report.membership_count,
            "memberships_inserted": report.memberships_inserted,
            "unclustered_claims": report.unclustered_claims,
            "unresolved_player_claims": report.unresolved_player_claims,
            "reused_existing": report.reused_existing,
        },
        None,
    )
