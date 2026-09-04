"""Explicitly non-simulated lineup arithmetic for early contest review."""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from narrative_alpha.store import ContestRow

if TYPE_CHECKING:
    from narrative_alpha.build import BuildResult

HEURISTIC_NOTICE: Literal[
    "HEURISTIC ONLY — NOT SIMULATOR-BACKED. These values make no probability claims."
] = "HEURISTIC ONLY — NOT SIMULATOR-BACKED. These values make no probability claims."


class HeuristicReportError(ValueError):
    """Raised when a build and contest cannot support honest heuristic arithmetic."""


@dataclass(frozen=True)
class HeuristicThresholds:
    """Every tunable numeric assumption used by the heuristic report."""

    heuristic_cash_line_projection_points: float = 150.0

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.heuristic_cash_line_projection_points)
            or self.heuristic_cash_line_projection_points <= 0
        ):
            raise ValueError("heuristic_cash_line_projection_points must be finite and positive")


class HeuristicLineupRow(BaseModel):
    """One lineup's labeled arithmetic; none of these fields is a simulated estimate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lineup_id: str
    heuristic_lineup_projection_sum: float = Field(allow_inf_nan=False)
    heuristic_salary_used: int = Field(ge=0)
    heuristic_projected_ownership_sum: float | None = Field(default=None, allow_inf_nan=False)
    heuristic_naive_cash_line_proxy: float = Field(ge=0, allow_inf_nan=False)
    heuristic_naive_ev_cents: float = Field(allow_inf_nan=False)


class HeuristicReport(BaseModel):
    """Structured heuristic report with a mandatory simulator disclaimer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    notice: Literal[
        "HEURISTIC ONLY — NOT SIMULATOR-BACKED. These values make no probability claims."
    ] = HEURISTIC_NOTICE
    contest_external_contest_id: str
    contest_site: str
    heuristic_cash_line_projection_points: float = Field(gt=0, allow_inf_nan=False)
    heuristic_average_prize_per_entry_cents: float = Field(ge=0, allow_inf_nan=False)
    lineups: tuple[HeuristicLineupRow, ...] = Field(min_length=1)


def build_heuristic_report(
    build_result: BuildResult,
    contest: ContestRow,
    *,
    thresholds: HeuristicThresholds | None = None,
) -> HeuristicReport:
    """Apply transparent, non-probabilistic arithmetic to a build's lineups.

    The cash-line proxy is ``projection / configured cash-line projection``. The naive EV
    scales the contest's prize pool per field entry by that proxy, then subtracts the entry
    fee. It is intentionally simple and is not a payout probability or simulated result.
    """

    selected_thresholds = thresholds or HeuristicThresholds()
    if contest.total_prizes_cents is None:
        raise HeuristicReportError("contest total_prizes_cents is required for naive EV")
    if not build_result.lineups:
        raise HeuristicReportError("BuildResult contains no lineups")

    average_prize = contest.total_prizes_cents / contest.field_size
    report_rows: list[HeuristicLineupRow] = []
    for lineup in build_result.lineups:
        if lineup.site.value != contest.site or lineup.slate_id != contest.slate_id:
            raise HeuristicReportError(
                f"lineup {lineup.lineup_id} does not belong to contest site/slate"
            )
        ownership_values = tuple(
            player.projected_ownership_captain
            if player.slot in {"CPT", "MVP"}
            else player.projected_ownership
            for player in lineup.players
        )
        ownership_sum = (
            None
            if any(value is None for value in ownership_values)
            else round(sum(value for value in ownership_values if value is not None), 6)
        )
        projection_sum = round(sum(player.projection for player in lineup.players), 6)
        salary_used = sum(player.salary for player in lineup.players)
        cash_line_proxy = projection_sum / selected_thresholds.heuristic_cash_line_projection_points
        naive_ev = cash_line_proxy * average_prize - contest.entry_fee_cents
        report_rows.append(
            HeuristicLineupRow(
                lineup_id=lineup.lineup_id,
                heuristic_lineup_projection_sum=projection_sum,
                heuristic_salary_used=salary_used,
                heuristic_projected_ownership_sum=ownership_sum,
                heuristic_naive_cash_line_proxy=round(cash_line_proxy, 6),
                heuristic_naive_ev_cents=round(naive_ev, 2),
            )
        )

    return HeuristicReport(
        contest_external_contest_id=contest.external_contest_id,
        contest_site=contest.site,
        heuristic_cash_line_projection_points=(
            selected_thresholds.heuristic_cash_line_projection_points
        ),
        heuristic_average_prize_per_entry_cents=round(average_prize, 6),
        lineups=tuple(report_rows),
    )


def render_heuristic_report(report: HeuristicReport) -> str:
    """Render a stable, human-readable report whose numeric headers stay explicit."""

    output = io.StringIO(newline="")
    output.write(report.notice + "\n")
    output.write(f"contest_external_contest_id={report.contest_external_contest_id}\n")
    output.write(f"contest_site={report.contest_site}\n")
    output.write(
        "heuristic_cash_line_projection_points="
        f"{report.heuristic_cash_line_projection_points:.6f}\n"
    )
    output.write(
        "heuristic_average_prize_per_entry_cents="
        f"{report.heuristic_average_prize_per_entry_cents:.6f}\n"
    )
    output.write(
        "heuristic_naive_cash_line_proxy_formula="
        "heuristic_lineup_projection_sum / heuristic_cash_line_projection_points\n"
    )
    output.write(
        "heuristic_naive_ev_cents_formula="
        "(heuristic_naive_cash_line_proxy * "
        "heuristic_average_prize_per_entry_cents) - entry_fee_cents\n\n"
    )
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (
            "lineup_id",
            "heuristic_lineup_projection_sum",
            "heuristic_salary_used",
            "heuristic_projected_ownership_sum",
            "heuristic_naive_cash_line_proxy",
            "heuristic_naive_ev_cents",
        )
    )
    for row in report.lineups:
        writer.writerow(
            (
                row.lineup_id,
                f"{row.heuristic_lineup_projection_sum:.6f}",
                row.heuristic_salary_used,
                ""
                if row.heuristic_projected_ownership_sum is None
                else f"{row.heuristic_projected_ownership_sum:.6f}",
                f"{row.heuristic_naive_cash_line_proxy:.6f}",
                f"{row.heuristic_naive_ev_cents:.2f}",
            )
        )
    return output.getvalue()
