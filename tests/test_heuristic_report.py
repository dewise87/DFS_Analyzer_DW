from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from narrative_alpha.build import BuildResult
from narrative_alpha.portfolio import (
    DfsSite,
    HeuristicThresholds,
    Lineup,
    LineupPlayer,
    build_heuristic_report,
    lineup_sha256,
    render_heuristic_report,
)
from narrative_alpha.store import ContestRow

GOLDEN = Path(__file__).with_name("golden") / "heuristic_report.txt"
OBSERVED_AT = datetime(2026, 9, 13, 12, tzinfo=UTC)


def test_rendered_heuristic_report_matches_golden_file() -> None:
    lineup = _lineup()
    build_result = cast(BuildResult, SimpleNamespace(lineups=(lineup,)))
    report = build_heuristic_report(
        build_result,
        _contest(),
        thresholds=HeuristicThresholds(heuristic_cash_line_projection_points=150.0),
    )

    assert report.lineups[0].heuristic_projected_ownership_sum == 0.9
    assert render_heuristic_report(report) == GOLDEN.read_text(encoding="utf-8")


def _lineup() -> Lineup:
    slots = ("QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST")
    players = tuple(
        LineupPlayer(
            slot=slot,
            player_id=index,
            site_player_id=str(10_000 + index),
            name=f"Player {index}",
            team="AAA" if index % 2 else "BBB",
            opponent="BBB" if index % 2 else "AAA",
            position="RB" if slot == "FLEX" else slot,
            salary=5_000,
            projection=15.0,
            projected_ownership=0.1,
            game_id="game-1",
        )
        for index, slot in enumerate(slots, start=1)
    )
    return Lineup(
        lineup_id=lineup_sha256(DfsSite.DRAFTKINGS, 1, players),
        site=DfsSite.DRAFTKINGS,
        slate_id=1,
        players=players,
        total_salary=45_000,
        total_projection=135.0,
    )


def _contest() -> ContestRow:
    point_in_time = {
        "source": "manual-site-lobby",
        "published_at": None,
        "observed_at": OBSERVED_AT,
        "ingested_at": OBSERVED_AT,
        "effective_at": None,
        "valid_from": OBSERVED_AT,
        "valid_to": None,
        "source_version": "manual-contest-v1",
        "run_id": None,
    }
    return ContestRow(
        contest_id=1,
        external_contest_id="dk-manual-1",
        site="draftkings",
        slate_id=1,
        archetype="single_entry",
        field_size=100,
        entry_limit=1,
        entry_fee_cents=1_000,
        total_prizes_cents=1_000_000,
        payout_curve_id="dk-manual-1-payouts",
        **point_in_time,
    )
