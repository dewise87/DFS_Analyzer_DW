"""L6: MCP/chat tools, slate memo, static dashboard, alerts, decision log."""

from narrative_alpha.interface.red_team import (
    RED_TEAM_LIMIT,
    RedTeamAnswer,
    build_red_team_review,
    render_red_team_review,
)
from narrative_alpha.interface.slate_memo import (
    SLATE_MEMO_NOTICE,
    SlateMemo,
    SlateMemoAppliedDelta,
    SlateMemoContestPolicy,
    SlateMemoError,
    SlateMemoInputArtifact,
    SlateMemoLineup,
    SlateMemoOwnershipRouting,
    build_slate_memo,
    render_slate_memo,
)

__all__ = [
    "RED_TEAM_LIMIT",
    "SLATE_MEMO_NOTICE",
    "RedTeamAnswer",
    "SlateMemo",
    "SlateMemoAppliedDelta",
    "SlateMemoContestPolicy",
    "SlateMemoError",
    "SlateMemoInputArtifact",
    "SlateMemoLineup",
    "SlateMemoOwnershipRouting",
    "build_red_team_review",
    "build_slate_memo",
    "render_red_team_review",
    "render_slate_memo",
]
