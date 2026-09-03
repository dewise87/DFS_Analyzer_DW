"""Governance caps, roster calibration, and append-only ownership scenarios."""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

import numpy as np
from numpy.typing import NDArray

from narrative_alpha import __version__
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.ownership.model import (
    FittedOwnershipModel,
    OwnershipModelError,
    OwnershipScenarioInput,
    predict_ownership,
)
from narrative_alpha.ownership_config import (
    GovernanceStatus,
    OwnershipModelConfig,
    SlateKind,
)


class OwnershipScenarioError(OwnershipModelError):
    """Raised when scenarios cannot be safely capped, calibrated, or stored."""


@dataclass(frozen=True)
class OwnershipScenario:
    player_id: int
    slate_id: int
    site: str
    contest_archetype: str
    role: str
    position: str
    decision_snapshot_id: str
    baseline_ownership: float
    ownership_p10: float
    ownership_p50: float
    ownership_p90: float
    delta_p50: float
    prob_delta_positive: float
    governance_status: GovernanceStatus
    status_multiplier: float
    applied_ownership: float
    calibrated_to_roster_totals: bool
    model_run_id: str
    model_version: str
    config_sha256: str
    feature_version: str
    ownership_scenario_id: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class ScenarioBuildReport:
    run_id: str
    decision_snapshot_id: str
    site: str
    contest_archetype: str
    governance_status: GovernanceStatus
    scenarios: tuple[OwnershipScenario, ...]


def apply_governance_cap(
    baseline: float,
    modeled: float,
    *,
    status: GovernanceStatus,
    slate_kind: SlateKind,
    config: OwnershipModelConfig,
) -> float:
    """Scale then cap a modeled delta in probability space."""

    if not 0 <= baseline <= 1 or not 0 <= modeled <= 1:
        raise OwnershipScenarioError("cap inputs must be ownership fractions")
    cap = config.cap(slate_kind, status)
    delta = (modeled - baseline) * cap.multiplier
    bounded_delta = min(max(delta, -cap.maximum_delta), cap.maximum_delta)
    return min(max(baseline + bounded_delta, 0.0), 1.0)


def calibrate_probabilities(
    probabilities: Sequence[float],
    groups: Sequence[str],
    targets: Mapping[str, float],
    *,
    tolerance: float = 1e-10,
    maximum_iterations: int = 200,
    lower_bounds: Sequence[float] | None = None,
    upper_bounds: Sequence[float] | None = None,
) -> tuple[float, ...]:
    """Apply one logistic IPF offset per disjoint group to match exact totals."""

    if len(probabilities) != len(groups):
        raise OwnershipScenarioError("probabilities and calibration groups must align")
    lower_limits = (
        tuple(0.0 for _ in probabilities) if lower_bounds is None else tuple(lower_bounds)
    )
    upper_limits = (
        tuple(1.0 for _ in probabilities) if upper_bounds is None else tuple(upper_bounds)
    )
    if len(lower_limits) != len(probabilities) or len(upper_limits) != len(probabilities):
        raise OwnershipScenarioError("calibration bounds must align with probabilities")
    if any(
        not 0 <= minimum <= maximum <= 1
        for minimum, maximum in zip(lower_limits, upper_limits, strict=True)
    ):
        raise OwnershipScenarioError("calibration bounds must be ordered fractions")
    grouped: dict[str, list[int]] = defaultdict(list)
    for index, (probability, group) in enumerate(zip(probabilities, groups, strict=True)):
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise OwnershipScenarioError("calibration probabilities must be finite fractions")
        grouped[group].append(index)
    if set(grouped) != set(targets):
        raise OwnershipScenarioError("every calibration group must have exactly one target")

    result = list(float(value) for value in probabilities)
    epsilon = 1e-12
    for group, indices in sorted(grouped.items()):
        target = float(targets[group])
        if not math.isfinite(target) or target < 0 or target > len(indices):
            raise OwnershipScenarioError(
                f"calibration target {target} is impossible for group {group!r}"
            )
        minimum_total = math.fsum(lower_limits[index] for index in indices)
        maximum_total = math.fsum(upper_limits[index] for index in indices)
        if target < minimum_total - tolerance or target > maximum_total + tolerance:
            raise OwnershipScenarioError(
                f"calibration target {target} violates governance bounds for group {group!r}"
            )
        if abs(target - minimum_total) <= tolerance:
            for index in indices:
                result[index] = lower_limits[index]
            continue
        if abs(maximum_total - target) <= tolerance:
            for index in indices:
                result[index] = upper_limits[index]
            continue
        logits = np.asarray(
            [
                math.log(
                    min(max(result[index], epsilon), 1.0 - epsilon)
                    / (1.0 - min(max(result[index], epsilon), 1.0 - epsilon))
                )
                for index in indices
            ],
            dtype=np.float64,
        )
        offset_lower, offset_upper = -40.0, 40.0
        calibrated = np.zeros(len(indices), dtype=np.float64)
        for _ in range(maximum_iterations):
            midpoint = (offset_lower + offset_upper) / 2.0
            calibrated = np.clip(
                _expit(logits + midpoint),
                np.asarray([lower_limits[index] for index in indices]),
                np.asarray([upper_limits[index] for index in indices]),
            )
            total = float(np.sum(calibrated))
            if abs(total - target) <= tolerance:
                break
            if total < target:
                offset_lower = midpoint
            else:
                offset_upper = midpoint
        else:
            raise OwnershipScenarioError(
                f"calibration did not converge for group {group!r}"
            )
        for index, value in zip(indices, calibrated, strict=True):
            result[index] = float(value)
    return tuple(result)


def build_scenarios(
    model: FittedOwnershipModel,
    inputs: Sequence[OwnershipScenarioInput],
    *,
    config: OwnershipModelConfig,
    slate_kind: SlateKind,
    status: GovernanceStatus,
) -> tuple[OwnershipScenario, ...]:
    """Produce §12.2.9 records, then calibrate only the governed p50 application."""

    selected = tuple(inputs)
    if not selected:
        raise OwnershipScenarioError("no scenario inputs are available")
    if model.run_id is None:
        raise OwnershipScenarioError("scenario provenance requires a persisted model fit")
    predictions = predict_ownership(
        model,
        selected,
        draw_count=config.posterior_draws,
        seed=config.posterior_seed,
    )
    capped = tuple(
        apply_governance_cap(
            row.baseline_ownership,
            prediction.ownership_p50,
            status=status,
            slate_kind=slate_kind,
            config=config,
        )
        for row, prediction in zip(selected, predictions, strict=True)
    )
    groups, targets = _calibration_targets(
        selected, slate_kind=slate_kind, config=config
    )
    maximum_delta = config.cap(slate_kind, status).maximum_delta
    calibrated = calibrate_probabilities(
        capped,
        groups,
        targets,
        tolerance=config.calibration.tolerance,
        maximum_iterations=config.calibration.maximum_iterations,
        lower_bounds=tuple(
            max(0.0, row.baseline_ownership - maximum_delta) for row in selected
        ),
        upper_bounds=tuple(
            min(1.0, row.baseline_ownership + maximum_delta) for row in selected
        ),
    )
    multiplier = config.cap(slate_kind, status).multiplier
    return tuple(
        OwnershipScenario(
            player_id=row.player_id,
            slate_id=row.slate_id,
            site=row.site,
            contest_archetype=row.contest_archetype,
            role=row.role,
            position=row.position,
            decision_snapshot_id=row.decision_snapshot_id,
            baseline_ownership=row.baseline_ownership,
            ownership_p10=prediction.ownership_p10,
            ownership_p50=prediction.ownership_p50,
            ownership_p90=prediction.ownership_p90,
            delta_p50=prediction.delta_p50,
            prob_delta_positive=prediction.prob_delta_positive,
            governance_status=status,
            status_multiplier=multiplier,
            applied_ownership=applied,
            calibrated_to_roster_totals=True,
            model_run_id=model.run_id,
            model_version=model.model_version,
            config_sha256=model.config_sha256,
            feature_version=model.feature_version,
        )
        for row, prediction, applied in zip(selected, predictions, calibrated, strict=True)
    )


def persist_scenarios(
    connection: sqlite3.Connection,
    scenarios: Sequence[OwnershipScenario],
    *,
    generated_at: datetime,
) -> ScenarioBuildReport:
    """Append a scenario run without changing any candidate or build input."""

    selected = tuple(scenarios)
    if not selected:
        raise OwnershipScenarioError("cannot persist an empty scenario run")
    first = selected[0]
    shared = {
        (row.decision_snapshot_id, row.site, row.contest_archetype, row.governance_status)
        for row in selected
    }
    if len(shared) != 1:
        raise OwnershipScenarioError(
            "one scenario run must have one decision/site/archetype/status"
        )
    at = ensure_utc(generated_at)
    stamp = utc_timestamp(at)
    run_id = f"ownership-scenarios-{uuid4().hex}"
    connection.execute("SAVEPOINT ownership_scenarios")
    stored: list[OwnershipScenario] = []
    try:
        connection.execute(
            """
            INSERT INTO model_runs(
                run_id, run_type, started_at, completed_at, status, code_version,
                config_sha256, parent_run_id, error_message, created_at
            ) VALUES (?, 'ownership_scenarios', ?, NULL, 'running', ?, ?, ?, NULL, ?)
            """,
            (
                run_id,
                stamp,
                __version__,
                first.config_sha256,
                first.model_run_id,
                stamp,
            ),
        )
        for row in selected:
            scenario_id = f"ownership-scenario-{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO ownership_scenarios(
                    ownership_scenario_id, player_id, slate_id, site,
                    contest_archetype, role, position, decision_snapshot_id,
                    baseline_ownership, ownership_p10, ownership_p50, ownership_p90,
                    delta_p50, prob_delta_positive, governance_status,
                    status_multiplier, applied_ownership, calibrated_to_roster_totals,
                    model_run_id, run_id, model_version, config_sha256, feature_version,
                    source, observed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, 'ownership-map-laplace', ?, ?)
                """,
                (
                    scenario_id,
                    row.player_id,
                    row.slate_id,
                    row.site,
                    row.contest_archetype,
                    row.role,
                    row.position,
                    row.decision_snapshot_id,
                    row.baseline_ownership,
                    row.ownership_p10,
                    row.ownership_p50,
                    row.ownership_p90,
                    row.delta_p50,
                    row.prob_delta_positive,
                    row.governance_status,
                    row.status_multiplier,
                    row.applied_ownership,
                    int(row.calibrated_to_roster_totals),
                    row.model_run_id,
                    run_id,
                    row.model_version,
                    row.config_sha256,
                    row.feature_version,
                    stamp,
                    stamp,
                ),
            )
            stored.append(
                OwnershipScenario(
                    **{
                        **row.__dict__,
                        "ownership_scenario_id": scenario_id,
                        "run_id": run_id,
                    }
                )
            )
        cursor = connection.execute(
            """
            UPDATE model_runs SET completed_at = ?, status = 'succeeded'
            WHERE run_id = ? AND status = 'running'
            """,
            (stamp, run_id),
        )
        if cursor.rowcount != 1:
            raise OwnershipScenarioError("could not complete ownership scenario run")
    except Exception:
        connection.execute("ROLLBACK TO ownership_scenarios")
        connection.execute("RELEASE ownership_scenarios")
        raise
    else:
        connection.execute("RELEASE ownership_scenarios")
    return ScenarioBuildReport(
        run_id=run_id,
        decision_snapshot_id=first.decision_snapshot_id,
        site=first.site,
        contest_archetype=first.contest_archetype,
        governance_status=first.governance_status,
        scenarios=tuple(stored),
    )


def _calibration_targets(
    rows: tuple[OwnershipScenarioInput, ...],
    *,
    slate_kind: SlateKind,
    config: OwnershipModelConfig,
) -> tuple[tuple[str, ...], dict[str, float]]:
    if slate_kind == "showdown":
        groups = tuple(row.role for row in rows)
        targets = {
            "captain": config.calibration.showdown_captain_slots,
            "flex": config.calibration.showdown_flex_slots,
        }
        return groups, targets

    groups = tuple(row.position for row in rows)
    baseline_by_position: dict[str, float] = defaultdict(float)
    for row in rows:
        baseline_by_position[row.position] += row.baseline_ownership
    baseline_total = math.fsum(baseline_by_position.values())
    if baseline_total <= 0:
        raise OwnershipScenarioError("classic baseline has no roster mass to calibrate")
    roster_total = (
        config.calibration.draftkings_classic_slots
        if rows[0].site == "draftkings"
        else config.calibration.fanduel_classic_slots
    )
    scale = roster_total / baseline_total
    targets = {position: value * scale for position, value in baseline_by_position.items()}
    return groups, targets


def _expit(values: NDArray[np.float64]) -> NDArray[np.float64]:
    output = np.empty_like(values)
    positive = values >= 0
    output[positive] = 1.0 / (1.0 + np.exp(-values[positive]))
    exponent = np.exp(values[~positive])
    output[~positive] = exponent / (1.0 + exponent)
    return output
