"""Read-only Appendix C-style rendering of source credibility snapshots."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp


@dataclass(frozen=True)
class SourceCredibilityRow:
    source_id: str
    team: str
    claim_type: str
    claim_dimension: str
    n_graded: int
    correct_count: int
    incorrect_count: int
    indeterminate_count: int
    ungradable_count: int
    posterior_mean: float
    interval_low: float
    interval_high: float
    interval_mass: float
    precision: float | None
    coverage: float
    average_lead_time_minutes: float
    correction_rate: float
    last_claim_at: datetime
    decay_weight: float
    decay_half_life_days: float
    beta_prior_alpha: float
    beta_prior_beta: float
    weighted_correct: float
    weighted_incorrect: float


@dataclass(frozen=True)
class SourceCredibilityReport:
    season: int
    week: int
    grading_run_id: str | None
    as_of_at: datetime | None
    grading_config_version: str | None
    grading_config_sha256: str | None
    rows: tuple[SourceCredibilityRow, ...]


def build_source_credibility_report(
    connection: sqlite3.Connection,
    *,
    season: int,
    week: int,
) -> SourceCredibilityReport:
    """Read the newest complete ledger snapshot written for an exact grading week."""

    if season < 1 or not 1 <= week <= 99:
        raise ValueError("season and week must be positive NFL identifiers")
    snapshot = connection.execute(
        """
        SELECT grading_run_id, as_of_at, grading_config_version, grading_config_sha256
        FROM source_credibility
        WHERE season = ? AND week = ?
        ORDER BY rtrim(as_of_at, 'Z') DESC, rowid DESC
        LIMIT 1
        """,
        (season, week),
    ).fetchone()
    if snapshot is None:
        return SourceCredibilityReport(season, week, None, None, None, None, ())
    run_id = str(snapshot["grading_run_id"])
    rows = connection.execute(
        """
        SELECT * FROM source_credibility
        WHERE season = ? AND week = ? AND grading_run_id = ?
        ORDER BY source_id, team, claim_type, claim_dimension
        """,
        (season, week, run_id),
    ).fetchall()
    return SourceCredibilityReport(
        season=season,
        week=week,
        grading_run_id=run_id,
        as_of_at=_parse_timestamp(str(snapshot["as_of_at"])),
        grading_config_version=str(snapshot["grading_config_version"]),
        grading_config_sha256=str(snapshot["grading_config_sha256"]),
        rows=tuple(
            SourceCredibilityRow(
                source_id=str(row["source_id"]),
                team=str(row["team"]),
                claim_type=str(row["claim_type"]),
                claim_dimension=str(row["claim_dimension"]),
                n_graded=int(row["n_graded"]),
                correct_count=int(row["correct_count"]),
                incorrect_count=int(row["incorrect_count"]),
                indeterminate_count=int(row["indeterminate_count"]),
                ungradable_count=int(row["ungradable_count"]),
                posterior_mean=float(row["accuracy_posterior_mean"]),
                interval_low=float(row["accuracy_interval_low"]),
                interval_high=float(row["accuracy_interval_high"]),
                interval_mass=float(row["posterior_interval_mass"]),
                precision=None if row["precision"] is None else float(row["precision"]),
                coverage=float(row["coverage"]),
                average_lead_time_minutes=float(row["average_lead_time_minutes"]),
                correction_rate=float(row["correction_rate"]),
                last_claim_at=_parse_timestamp(str(row["last_claim_at"])),
                decay_weight=float(row["decay_weight"]),
                decay_half_life_days=float(row["decay_half_life_days"]),
                beta_prior_alpha=float(row["beta_prior_alpha"]),
                beta_prior_beta=float(row["beta_prior_beta"]),
                weighted_correct=float(row["weighted_correct"]),
                weighted_incorrect=float(row["weighted_incorrect"]),
            )
            for row in rows
        ),
    )


def render_source_credibility_report(report: SourceCredibilityReport) -> str:
    """Render every point accuracy beside n and its posterior interval."""

    lines = [
        "NARRATIVE ALPHA — SOURCE CREDIBILITY LEDGER",
        f"  season/week  {report.season} / {report.week:02d}",
    ]
    if report.grading_run_id is None or report.as_of_at is None:
        lines.extend(("  no ledger snapshot exists for this week", ""))
        return "\n".join(lines)
    lines.extend(
        (
            f"  grading run  {report.grading_run_id}",
            f"  as of        {utc_timestamp(report.as_of_at)}",
            f"  rules        {report.grading_config_version} sha256={report.grading_config_sha256}",
            "  discipline   Beta prior is included; every accuracy estimate carries n "
            "and its posterior interval",
            "",
        )
    )
    if not report.rows:
        lines.extend(("  no source cells were eligible", ""))
        return "\n".join(lines)
    lines.extend(("SOURCE x CLAIM TYPE (pooled across teams and dimensions; read this first)", ""))
    for pooled in pooled_rows(report.rows):
        mass = pooled.interval_mass * 100
        lines.extend(
            (
                f"{pooled.source_id} | {pooled.claim_type}",
                f"  accuracy posterior {pooled.posterior_mean:.3f}  n={pooled.n_graded}  "
                f"{mass:.0f}% interval [{pooled.interval_low:.3f}, {pooled.interval_high:.3f}]"
                f"  (decay-weighted n={pooled.weighted_correct + pooled.weighted_incorrect:.2f})",
                f"  outcomes correct={pooled.correct_count} incorrect={pooled.incorrect_count} "
                f"indeterminate={pooled.indeterminate_count} "
                f"ungradable={pooled.ungradable_count}",
                "",
            )
        )
    lines.extend(
        ("SOURCE x TEAM x CLAIM TYPE x DIMENSION (small cells; n is shown for a reason)", "")
    )
    for row in report.rows:
        mass = row.interval_mass * 100
        raw = "unavailable" if row.precision is None else f"{row.precision:.3f}"
        lines.extend(
            (
                f"{row.source_id} | {row.team} | {row.claim_type} | {row.claim_dimension}",
                f"  accuracy posterior {row.posterior_mean:.3f}  n={row.n_graded}  "
                f"{mass:.0f}% interval [{row.interval_low:.3f}, {row.interval_high:.3f}]"
                f"  (decay-weighted n={row.weighted_correct + row.weighted_incorrect:.2f})",
                f"  outcomes correct={row.correct_count} incorrect={row.incorrect_count} "
                f"indeterminate={row.indeterminate_count} ungradable={row.ungradable_count}",
                f"  raw accuracy {raw} (unshrunk, no interval; the posterior above is the "
                f"estimate)  determinate share={row.coverage:.3f}  "
                f"average lead={row.average_lead_time_minutes:.1f}m  "
                f"correction rate={row.correction_rate:.3f}",
                f"  decay weight={row.decay_weight:.3f} "
                f"(half-life {row.decay_half_life_days:g}d)  "
                f"last claim={utc_timestamp(row.last_claim_at)}",
                "",
            )
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class PooledCredibilityRow:
    """One (source, claim type) cell pooled from the fine cells of a snapshot."""

    source_id: str
    claim_type: str
    n_graded: int
    correct_count: int
    incorrect_count: int
    indeterminate_count: int
    ungradable_count: int
    weighted_correct: float
    weighted_incorrect: float
    posterior_mean: float
    interval_low: float
    interval_high: float
    interval_mass: float


def pooled_rows(rows: Sequence[SourceCredibilityRow]) -> tuple[PooledCredibilityRow, ...]:
    """Pool the fine cells by (source, claim type) into one Beta each (§12.4.2).

    The store keeps the fine cell; the report leads with the pooled one, because after a
    week the fine cells are n=1 and n=2 and would read as certainty they do not have.
    """

    from narrative_alpha.grading.core import posterior_from_weights

    grouped: dict[tuple[str, str], list[SourceCredibilityRow]] = {}
    for row in rows:
        grouped.setdefault((row.source_id, row.claim_type), []).append(row)
    pooled: list[PooledCredibilityRow] = []
    for (source_id, claim_type), cells in sorted(grouped.items()):
        weighted_correct = sum(cell.weighted_correct for cell in cells)
        weighted_incorrect = sum(cell.weighted_incorrect for cell in cells)
        mean, low, high = posterior_from_weights(
            cells[0].beta_prior_alpha,
            cells[0].beta_prior_beta,
            weighted_correct,
            weighted_incorrect,
            interval_mass=cells[0].interval_mass,
        )
        pooled.append(
            PooledCredibilityRow(
                source_id=source_id,
                claim_type=claim_type,
                n_graded=sum(cell.n_graded for cell in cells),
                correct_count=sum(cell.correct_count for cell in cells),
                incorrect_count=sum(cell.incorrect_count for cell in cells),
                indeterminate_count=sum(cell.indeterminate_count for cell in cells),
                ungradable_count=sum(cell.ungradable_count for cell in cells),
                weighted_correct=weighted_correct,
                weighted_incorrect=weighted_incorrect,
                posterior_mean=mean,
                interval_low=low,
                interval_high=high,
                interval_mass=cells[0].interval_mass,
            )
        )
    return tuple(pooled)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"stored credibility timestamp is naive: {value!r}")
    return ensure_utc(parsed.astimezone(UTC))
