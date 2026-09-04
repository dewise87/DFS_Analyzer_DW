"""Historical comparison hooks; these write diagnostics and never alter calibration state."""

from __future__ import annotations

import csv
import io
import math
import re
import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from narrative_alpha.identity.normalization import name_without_suffix, normalize_name
from narrative_alpha.ingest.results import (
    ContestArchetype,
    ContestMetadata,
    parse_contest_standings,
)
from narrative_alpha.ingest.salaries import SalarySite, SalarySlateType
from narrative_alpha.report_cli import write_report_atomic
from narrative_alpha.simulation.models import EXPERIMENTAL_NOTICE, SimulationReport
from narrative_alpha.snapshots import CaptureKind, load_manifest, sha256_file
from narrative_alpha.snapshots.core import MANIFEST_FILENAME, snapshot_week_path


class SimulationCalibrationError(RuntimeError):
    """Raised when a requested historical comparison lacks its immutable labels."""


@dataclass(frozen=True)
class CalibrationResult:
    simulation_run_id: int
    report_path: Path
    comparison_path: Path


def calibrate_week(
    connection: sqlite3.Connection,
    *,
    season: int,
    week: int,
    snapshot_root: Path,
) -> tuple[CalibrationResult, ...]:
    """Compare every saved simulation for a week with its captured contest standings."""

    runs = connection.execute(
        """
        SELECT sr.*, c.external_contest_id, c.site, c.slate_id, c.archetype,
               c.entry_limit, c.entry_fee_cents, c.field_size, c.payout_curve_id,
               s.slate_type
        FROM simulation_runs AS sr
        JOIN contests AS c ON c.contest_id = sr.contest_id
        JOIN slates AS s ON s.slate_id = c.slate_id
        WHERE s.season = ? AND s.week = ?
        ORDER BY sr.simulation_run_id
        """,
        (season, week),
    ).fetchall()
    if not runs:
        raise SimulationCalibrationError(
            f"no simulation_runs exist for season {season} week {week:02d}"
        )

    results: list[CalibrationResult] = []
    for run in runs:
        result = _calibrate_run(
            connection,
            run=run,
            season=season,
            week=week,
            snapshot_root=snapshot_root,
        )
        results.append(result)
    return tuple(results)


def _calibrate_run(
    connection: sqlite3.Connection,
    *,
    run: sqlite3.Row,
    season: int,
    week: int,
    snapshot_root: Path,
) -> CalibrationResult:
    contest_id = int(run["contest_id"])
    ledger_count = int(
        connection.execute(
            "SELECT count(*) FROM contest_entries WHERE contest_id = ?", (contest_id,)
        ).fetchone()[0]
    )
    if ledger_count == 0:
        raise SimulationCalibrationError(
            f"contest {run['external_contest_id']} has no entry ledger; calibration requires it"
        )
    label = connection.execute(
        """
        SELECT ao.source_file_sha256, ao.observed_at
        FROM actual_ownership AS ao
        WHERE ao.external_contest_id = ? AND ao.site = ? AND ao.slate_id = ?
        ORDER BY rtrim(ao.observed_at, 'Z') DESC, ao.actual_ownership_id DESC
        LIMIT 1
        """,
        (run["external_contest_id"], run["site"], run["slate_id"]),
    ).fetchone()
    if label is None:
        raise SimulationCalibrationError(
            f"contest {run['external_contest_id']} has no ingested standings labels"
        )
    settled = int(
        connection.execute(
            """
            SELECT count(*) FROM contest_entry_results AS cer
            JOIN contest_entries AS ce ON ce.contest_entry_id = cer.contest_entry_id
            WHERE ce.contest_id = ? AND cer.source_file_sha256 = ?
            """,
            (contest_id, label["source_file_sha256"]),
        ).fetchone()[0]
    )
    if settled == 0:
        raise SimulationCalibrationError(
            f"contest {run['external_contest_id']} has no standings settlement for its ledger"
        )

    captured = _captured_standings(
        snapshot_root,
        season=season,
        week=week,
        digest=str(label["source_file_sha256"]),
    )
    observed_at = datetime.fromisoformat(str(label["observed_at"]).replace("Z", "+00:00"))
    metadata = ContestMetadata(
        contest_id=str(run["external_contest_id"]),
        site=SalarySite(str(run["site"])),
        slate_id=int(run["slate_id"]),
        slate_type=SalarySlateType(str(run["slate_type"])),
        contest_archetype=ContestArchetype(str(run["archetype"])),
        entry_limit=int(run["entry_limit"]),
        entry_fee_cents=int(run["entry_fee_cents"]),
        expected_field_size=int(run["field_size"]),
        payout_curve_id=None if run["payout_curve_id"] is None else str(run["payout_curve_id"]),
        observed_at=observed_at,
    )
    standings = parse_contest_standings(captured, metadata)
    report = SimulationReport.model_validate_json(str(run["metrics_json"]))
    actual_ownership = _actual_ownership(
        connection,
        external_contest_id=str(run["external_contest_id"]),
        site=str(run["site"]),
        slate_id=int(run["slate_id"]),
        observed_at=str(label["observed_at"]),
    )
    score_values = np.asarray([entry.points for entry in standings.entries], dtype=np.float64)
    score_comparison = tuple(
        (quantile, simulated, _lower_quantile(score_values, quantile))
        for quantile, simulated in report.simulated_score_quantiles
    )
    realized_duplicates = _duplication_distribution(
        _resolve_standings_lineups(
            connection,
            tuple(entry.lineup for entry in standings.entries),
            slate_id=int(run["slate_id"]),
            source=f"{run['site']}-contest-standings",
            observed_at=observed_at,
        )
    )
    content = _render_comparison(
        report,
        actual_ownership=actual_ownership,
        score_comparison=score_comparison,
        realized_duplicates=realized_duplicates,
        ledger_count=ledger_count,
        standings_sha256=str(label["source_file_sha256"]),
    )
    report_path = Path(str(run["report_path"]))
    comparison_path = report_path.with_name(report_path.stem + "-calibration.txt")
    write_report_atomic(comparison_path, content)
    return CalibrationResult(
        simulation_run_id=int(run["simulation_run_id"]),
        report_path=report_path,
        comparison_path=comparison_path,
    )


def _actual_ownership(
    connection: sqlite3.Connection,
    *,
    external_contest_id: str,
    site: str,
    slate_id: int,
    observed_at: str,
) -> dict[int, float]:
    rows = connection.execute(
        """
        SELECT player_id, actual_ownership FROM actual_ownership
        WHERE external_contest_id = ? AND site = ? AND slate_id = ?
          AND role = 'classic' AND observed_at = ?
        ORDER BY player_id
        """,
        (external_contest_id, site, slate_id, observed_at),
    ).fetchall()
    return {int(row["player_id"]): float(row["actual_ownership"]) for row in rows}


def _captured_standings(snapshot_root: Path, *, season: int, week: int, digest: str) -> Path:
    week_path = snapshot_week_path(snapshot_root, season, week)
    if week_path.is_dir():
        for capture_path in sorted(path for path in week_path.iterdir() if path.is_dir()):
            manifest_path = capture_path / MANIFEST_FILENAME
            if not manifest_path.is_file():
                continue
            manifest = load_manifest(manifest_path)
            for record in manifest.files:
                if record.kind is CaptureKind.STANDINGS and record.sha256 == digest:
                    path = capture_path / record.path
                    if sha256_file(path) != digest:
                        raise SimulationCalibrationError(
                            f"captured standings hash mismatch: {path}"
                        )
                    return path
    raise SimulationCalibrationError(f"captured standings {digest} is absent under {week_path}")


def _render_comparison(
    report: SimulationReport,
    *,
    actual_ownership: dict[int, float],
    score_comparison: Sequence[tuple[float, float, float]],
    realized_duplicates: tuple[tuple[int, float], ...],
    ledger_count: int,
    standings_sha256: str,
) -> str:
    output = io.StringIO(newline="")
    # A calibration hook is evidence for a human review; running it does not make the
    # assumptions calibrated and cannot remove this label from an existing report.
    if report.notice is not None:
        output.write(EXPERIMENTAL_NOTICE + "\n")
    output.write("NARRATIVE ALPHA SIMULATION CALIBRATION COMPARISON\n")
    output.write(f"simulation_decision_snapshot_id={report.decision_snapshot_id}\n")
    output.write(f"contest_external_id={report.contest_external_id}\n")
    output.write(f"config_version={report.config_version}\n")
    output.write(f"config_sha256={report.config_sha256}\n")
    output.write(f"draws={report.draws}\n")
    output.write(f"field_replicates={report.field_replicates}\n")
    output.write(f"seed={report.seed}\n")
    output.write(f"ledger_entries={ledger_count}\n")
    output.write(f"standings_sha256={standings_sha256}\n")
    output.write("calibration_state_changed=false\n")
    writer = csv.writer(output, lineterminator="\n")

    output.write("\nOWNERSHIP MARGINAL COMPARISON\n")
    if report.notice is not None:
        output.write(EXPERIMENTAL_NOTICE + "\n")
    writer.writerow(
        (
            "player_id",
            "name",
            "target",
            "simulated",
            "realized",
            "simulation_error",
            "realized_error",
        )
    )
    for marginal in report.ownership_marginals:
        realized = actual_ownership.get(marginal.player_id)
        writer.writerow(
            (
                marginal.player_id,
                marginal.name,
                f"{marginal.calibrated_target:.6f}",
                f"{marginal.achieved:.6f}",
                "unavailable" if realized is None else f"{realized:.6f}",
                f"{marginal.absolute_error:.6f}",
                "unavailable"
                if realized is None
                else f"{abs(realized - marginal.calibrated_target):.6f}",
            )
        )

    output.write("\nSCORE DISTRIBUTION QUANTILES\n")
    if report.notice is not None:
        output.write(EXPERIMENTAL_NOTICE + "\n")
    writer.writerow(("quantile", "simulated", "realized", "difference"))
    for quantile, simulated, realized in score_comparison:
        writer.writerow(
            (
                f"{quantile:.6f}",
                f"{simulated:.6f}",
                f"{realized:.6f}",
                f"{simulated - realized:.6f}",
            )
        )

    output.write("\nDUPLICATION COUNT DISTRIBUTION\n")
    if report.notice is not None:
        output.write(EXPERIMENTAL_NOTICE + "\n")
    writer.writerow(("duplicates_excluding_self", "simulated_probability", "realized_probability"))
    simulated_distribution = dict(report.simulated_field_duplication_distribution)
    realized_distribution = dict(realized_duplicates)
    for count in sorted(set(simulated_distribution) | set(realized_distribution)):
        writer.writerow(
            (
                count,
                f"{simulated_distribution.get(count, 0.0):.6f}",
                f"{realized_distribution.get(count, 0.0):.6f}",
            )
        )
    return output.getvalue()


def _duplication_distribution(
    lineups: Sequence[Sequence[int]],
) -> tuple[tuple[int, float], ...]:
    normalized = tuple(
        tuple(sorted({int(player_id) for player_id in lineup})) for lineup in lineups
    )
    counts = Counter(normalized)
    frequencies = Counter(counts[lineup] - 1 for lineup in normalized)
    return tuple(
        (count, frequency / len(normalized)) for count, frequency in sorted(frequencies.items())
    )


_LINEUP_SLOT = re.compile(r"(?:^|\s)(QB|RB|WR|TE|FLEX|DST|D/ST|DEF|CPT|MVP)\s+")


def _resolve_standings_lineups(
    connection: sqlite3.Connection,
    lineups: Sequence[str],
    *,
    slate_id: int,
    source: str,
    observed_at: datetime,
) -> tuple[tuple[int, ...], ...]:
    """Resolve site lineup names against the same canonical/alias crosswalk as ingestion."""

    stamp = observed_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
    rows = connection.execute(
        """
        WITH ranked AS (
            SELECT s.*,
                   row_number() OVER (
                       PARTITION BY s.slate_id, s.player_id
                       ORDER BY rtrim(s.observed_at, 'Z') DESC, s.salary_id DESC
                   ) AS version_rank
            FROM salaries AS s
            WHERE s.slate_id = ?
              AND rtrim(s.observed_at, 'Z') <= rtrim(?, 'Z')
              AND rtrim(s.valid_from, 'Z') <= rtrim(?, 'Z')
              AND (s.valid_to IS NULL OR rtrim(s.valid_to, 'Z') > rtrim(?, 'Z'))
        )
        SELECT p.player_id, p.canonical_name, p.position
        FROM ranked AS s JOIN players AS p ON p.player_id = s.player_id
        WHERE s.version_rank = 1 ORDER BY p.player_id
        """,
        (slate_id, stamp, stamp, stamp),
    ).fetchall()
    if not rows:
        raise SimulationCalibrationError(
            f"slate {slate_id} has no salary crosswalk at the standings timestamp"
        )
    candidates = {
        int(row["player_id"]): (
            str(row["canonical_name"]),
            _position(None if row["position"] is None else str(row["position"])),
        )
        for row in rows
    }
    aliases = connection.execute(
        """
        SELECT player_id, alias FROM player_aliases
        WHERE source = ?
          AND (
              (manual_override = 1 AND valid_to IS NULL)
              OR (
                  rtrim(valid_from, 'Z') <= rtrim(?, 'Z')
                  AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(?, 'Z'))
              )
          )
        ORDER BY manual_override DESC, alias_id DESC
        """,
        (source, stamp, stamp),
    ).fetchall()
    names: dict[str, set[int]] = {}
    suffix_names: dict[str, set[int]] = {}
    for player_id, (name, _) in candidates.items():
        names.setdefault(normalize_name(name), set()).add(player_id)
        suffix_names.setdefault(name_without_suffix(name), set()).add(player_id)
    for row in aliases:
        player_id = int(row["player_id"])
        if player_id not in candidates:
            continue
        alias = str(row["alias"])
        names.setdefault(normalize_name(alias), set()).add(player_id)
        suffix_names.setdefault(name_without_suffix(alias), set()).add(player_id)

    resolved: list[tuple[int, ...]] = []
    for lineup in lineups:
        entries = _lineup_entries(lineup)
        player_ids: list[int] = []
        for slot, name in entries:
            possible = names.get(normalize_name(name), set())
            if not possible:
                possible = suffix_names.get(name_without_suffix(name), set())
            eligible = {
                player_id for player_id in possible if _slot_agrees(slot, candidates[player_id][1])
            }
            if len(eligible) != 1:
                raise SimulationCalibrationError(
                    f"standings lineup player {name!r} at {slot} did not resolve uniquely "
                    f"through the slate crosswalk; candidates={sorted(eligible)}"
                )
            player_ids.append(next(iter(eligible)))
        resolved.append(tuple(sorted(set(player_ids))))
    return tuple(resolved)


def _lineup_entries(lineup: str) -> tuple[tuple[str, str], ...]:
    normalized = " ".join(lineup.split())
    matches = tuple(_LINEUP_SLOT.finditer(normalized))
    if not matches or matches[0].start() != 0:
        raise SimulationCalibrationError(f"cannot parse standings lineup: {lineup!r}")
    entries: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        name = normalized[match.end() : end].strip()
        if not name:
            raise SimulationCalibrationError(f"standings lineup has an empty {match.group(1)}")
        entries.append((match.group(1), name))
    return tuple(entries)


def _slot_agrees(slot: str, position: str | None) -> bool:
    if slot in {"FLEX", "CPT", "MVP"}:
        return True
    return _position(slot) == position


def _position(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    return "DST" if normalized in {"D", "DEF", "D/ST"} else normalized


def _lower_quantile(values: np.ndarray[tuple[int], np.dtype[np.float64]], q: float) -> float:
    if values.size == 0:
        raise SimulationCalibrationError("standings contains no entrant scores")
    ordered = np.sort(values)
    return float(ordered[max(0, math.ceil(q * len(ordered)) - 1)])
