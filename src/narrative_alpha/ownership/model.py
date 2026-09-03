"""Bounded binomial logit-offset model fitted by MAP and Laplace approximation."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import minimize  # type: ignore[import-untyped]

from narrative_alpha.ownership_config import OwnershipModelConfig


class OwnershipModelError(ValueError):
    """Raised when a model cannot be fitted or used honestly."""


@dataclass(frozen=True)
class OwnershipTrainingRow:
    """One labeled player-contest row with predictors frozen at its decision cutoff."""

    player_id: int
    season: int
    week: int
    slate_id: int
    decision_snapshot_id: str
    decision_at: str
    site: str
    contest_archetype: str
    role: str
    position: str
    baseline_ownership: float
    h_signed_z: float
    h_dfs_z: float
    h_velocity_z: float
    actual_ownership: float
    roster_count: int
    lineup_count: int
    label_source: str
    feature_id: str = "synthetic"
    feature_version: str = "synthetic"
    ownership_baseline_id: int | None = None
    actual_ownership_id: int | None = None

    def __post_init__(self) -> None:
        probabilities = (self.baseline_ownership, self.actual_ownership)
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities):
            raise OwnershipModelError("ownership probabilities must be finite fractions")
        features = (self.h_signed_z, self.h_dfs_z, self.h_velocity_z)
        if any(not math.isfinite(value) for value in features):
            raise OwnershipModelError("standardized ownership features must be finite")
        if self.lineup_count <= 0 or not 0 <= self.roster_count <= self.lineup_count:
            raise OwnershipModelError("roster_count must be within a positive lineup_count")


@dataclass(frozen=True)
class OwnershipScenarioInput:
    player_id: int
    slate_id: int
    decision_snapshot_id: str
    site: str
    contest_archetype: str
    role: str
    position: str
    baseline_ownership: float
    h_signed_z: float
    h_dfs_z: float
    h_velocity_z: float
    feature_id: str
    feature_version: str
    ownership_baseline_id: int


OwnershipPredictor = OwnershipTrainingRow | OwnershipScenarioInput


@dataclass(frozen=True)
class FittedOwnershipModel:
    model_version: str
    config_version: str
    config_sha256: str
    feature_version: str
    site: str
    contest_archetype: str
    amplitude: float
    probability_epsilon: float
    parameter_names: tuple[str, ...]
    map_parameters: tuple[float, ...]
    covariance: tuple[tuple[float, ...], ...]
    training_rows: int
    training_weeks: tuple[tuple[int, int], ...]
    converged: bool
    objective: float
    run_id: str | None = None
    # Quasi-binomial dispersion: the Pearson chi-square per degree of freedom, floored at
    # one, by which the Laplace covariance is inflated. Label counts come from contests of
    # tens of thousands of lineups, so a pure binomial likelihood would make the posterior
    # far tighter than dependent rows sharing slates and stories can justify (§12.2.7).
    dispersion: float = 1.0

    @property
    def coefficients(self) -> dict[str, float]:
        return dict(zip(self.parameter_names, self.map_parameters, strict=True))


@dataclass(frozen=True)
class PosteriorPrediction:
    baseline_ownership: float
    ownership_p10: float
    ownership_p50: float
    ownership_p90: float
    delta_p50: float
    prob_delta_positive: float


def fit_ownership_model(
    rows: Sequence[OwnershipTrainingRow],
    *,
    config: OwnershipModelConfig,
    contest_archetype: str,
    site: str,
    allow_synthetic: bool = False,
    roles: Sequence[str] | None = None,
) -> FittedOwnershipModel:
    """Fit the three-slope model; synthetic seams must be opened explicitly."""

    canonical_rows = tuple(rows)
    synthetic = tuple(row for row in canonical_rows if _is_synthetic_source(row.label_source))
    if synthetic and not allow_synthetic:
        raise OwnershipModelError(
            f"refusing {len(synthetic)} synthetic fixture/test label row(s); "
            "allow_synthetic=True is test-only"
        )
    mismatched = tuple(
        row
        for row in canonical_rows
        if row.contest_archetype != contest_archetype or row.site != site
    )
    if mismatched:
        raise OwnershipModelError("all training rows must match one site/archetype cohort")

    selected_roles = tuple(sorted(set(roles or ()) | {row.role for row in canonical_rows}))
    if not selected_roles:
        selected_roles = ("classic",)
    parameter_names = (
        "beta_signed",
        "beta_dfs",
        "beta_velocity",
        f"intercept_archetype:{contest_archetype}",
        *(f"intercept_role:{role}" for role in selected_roles),
    )
    design = _design_matrix(canonical_rows, selected_roles)
    baseline = np.asarray(
        [
            _clip_probability(row.baseline_ownership, config.probability_epsilon)
            for row in canonical_rows
        ],
        dtype=np.float64,
    )
    successes = np.asarray([row.roster_count for row in canonical_rows], dtype=np.float64)
    trials = np.asarray([row.lineup_count for row in canonical_rows], dtype=np.float64)
    prior_scales = np.asarray(
        [
            config.priors.beta_signed_scale,
            config.priors.beta_dfs_scale,
            config.priors.beta_velocity_scale,
            *([config.priors.intercept_scale] * (1 + len(selected_roles))),
        ],
        dtype=np.float64,
    )
    prior_precision = 1.0 / np.square(prior_scales)
    baseline_logit = _logit_array(baseline)

    def objective(theta: NDArray[np.float64]) -> float:
        eta, _, _ = _linear_predictor(
            theta, design, baseline_logit, amplitude=config.amplitude
        )
        negative_log_likelihood = np.sum(trials * np.logaddexp(0.0, eta) - successes * eta)
        negative_log_prior = 0.5 * np.sum(prior_precision * np.square(theta))
        return float(negative_log_likelihood + negative_log_prior)

    def gradient(theta: NDArray[np.float64]) -> NDArray[np.float64]:
        eta, jacobian, _ = _linear_predictor(
            theta, design, baseline_logit, amplitude=config.amplitude
        )
        probability = _expit(eta)
        return jacobian.T @ (trials * probability - successes) + prior_precision * theta

    initial = np.zeros(len(parameter_names), dtype=np.float64)
    result = minimize(
        objective,
        initial,
        jac=gradient,
        method="L-BFGS-B",
        bounds=((0.0, None), *((None, None),) * (len(parameter_names) - 1)),
        options={"maxiter": 2000, "ftol": 1e-12, "gtol": 1e-8},
    )
    if not result.success or not np.all(np.isfinite(result.x)):
        raise OwnershipModelError(f"MAP optimization failed: {result.message}")

    theta = np.asarray(result.x, dtype=np.float64)
    eta, jacobian, second = _linear_predictor(
        theta, design, baseline_logit, amplitude=config.amplitude
    )
    probability = _expit(eta)
    residual = trials * probability - successes
    weights = trials * probability * (1.0 - probability)
    hessian = jacobian.T @ (weights[:, np.newaxis] * jacobian)
    slope_design = design[:, :3]
    hessian[:3, :3] += slope_design.T @ (
        (residual * second)[:, np.newaxis] * slope_design
    )
    hessian += np.diag(prior_precision)
    dispersion = _dispersion(residual, weights, degrees_of_freedom=len(canonical_rows) - len(theta))
    covariance = _stable_inverse(hessian) * dispersion
    return FittedOwnershipModel(
        model_version=config.model_version,
        config_version=config.config_version,
        config_sha256=config.config_sha256,
        feature_version=config.feature_version,
        site=site,
        contest_archetype=contest_archetype,
        amplitude=config.amplitude,
        probability_epsilon=config.probability_epsilon,
        parameter_names=parameter_names,
        map_parameters=tuple(float(value) for value in theta),
        covariance=tuple(tuple(float(value) for value in row) for row in covariance),
        training_rows=len(canonical_rows),
        training_weeks=tuple(sorted({(row.season, row.week) for row in canonical_rows})),
        converged=True,
        objective=float(result.fun),
        dispersion=dispersion,
    )


def posterior_parameter_draws(
    model: FittedOwnershipModel,
    *,
    draw_count: int,
    seed: int,
) -> NDArray[np.float64]:
    """Draw from the Laplace approximation, respecting the half-normal support."""

    if draw_count <= 0:
        raise OwnershipModelError("draw_count must be positive")
    rng = np.random.default_rng(seed)
    mean = np.asarray(model.map_parameters, dtype=np.float64)
    covariance = np.asarray(model.covariance, dtype=np.float64)
    accepted: list[NDArray[np.float64]] = []
    remaining = draw_count
    attempts = 0
    while remaining > 0 and attempts < 20:
        candidates = rng.multivariate_normal(mean, covariance, size=max(remaining * 2, 64))
        valid = candidates[candidates[:, 0] >= 0.0]
        accepted.append(valid[:remaining])
        remaining -= min(remaining, len(valid))
        attempts += 1
    if remaining:
        fallback = rng.multivariate_normal(mean, covariance, size=remaining)
        fallback[:, 0] = np.abs(fallback[:, 0])
        accepted.append(fallback)
    return np.concatenate(accepted, axis=0)[:draw_count]


def predict_ownership(
    model: FittedOwnershipModel,
    rows: Sequence[OwnershipPredictor],
    *,
    draw_count: int,
    seed: int,
) -> tuple[PosteriorPrediction, ...]:
    """Summarize player probabilities over Laplace posterior parameter draws."""

    selected = tuple(rows)
    if not selected:
        return ()
    for row in selected:
        if row.site != model.site or row.contest_archetype != model.contest_archetype:
            raise OwnershipModelError("prediction rows do not match the fitted site/archetype")
    role_names = tuple(
        name.removeprefix("intercept_role:")
        for name in model.parameter_names
        if name.startswith("intercept_role:")
    )
    design = _design_matrix(selected, role_names)
    baselines = np.asarray(
        [
            _clip_probability(row.baseline_ownership, model.probability_epsilon)
            for row in selected
        ],
        dtype=np.float64,
    )
    parameter_draws = posterior_parameter_draws(model, draw_count=draw_count, seed=seed)
    heat_raw = parameter_draws[:, :3] @ design[:, :3].T
    heat_deltas = model.amplitude * np.tanh(heat_raw / model.amplitude)
    intercepts = parameter_draws[:, 3:] @ design[:, 3:].T
    probabilities = _expit(
        _logit_array(baselines)[np.newaxis, :] + intercepts + heat_deltas
    )
    quantiles = np.quantile(probabilities, (0.1, 0.5, 0.9), axis=0)
    positive = np.mean(probabilities > baselines[np.newaxis, :], axis=0)
    return tuple(
        PosteriorPrediction(
            baseline_ownership=float(row.baseline_ownership),
            ownership_p10=float(quantiles[0, index]),
            ownership_p50=float(quantiles[1, index]),
            ownership_p90=float(quantiles[2, index]),
            delta_p50=float(quantiles[1, index] - row.baseline_ownership),
            prob_delta_positive=float(positive[index]),
        )
        for index, row in enumerate(selected)
    )


def is_synthetic_source(source: str) -> bool:
    """Public source classifier shared by the database gate and library seam."""

    return _is_synthetic_source(source)


def _is_synthetic_source(source: str) -> bool:
    normalized = source.strip().casefold()
    tokens = tuple(part for part in re.split(r"[^a-z0-9]+", normalized) if part)
    synthetic_tokens = {"fixture", "fixtures", "test", "tests", "pytest", "synthetic"}
    return normalized.startswith(("fixture", "test", "synthetic")) or any(
        token in synthetic_tokens for token in tokens
    )


def _design_matrix(
    rows: Sequence[OwnershipPredictor], roles: Sequence[str]
) -> NDArray[np.float64]:
    role_indices = {role: index for index, role in enumerate(roles)}
    matrix = np.zeros((len(rows), 4 + len(roles)), dtype=np.float64)
    for index, row in enumerate(rows):
        if row.role not in role_indices:
            raise OwnershipModelError(f"model has no intercept for role {row.role!r}")
        matrix[index, :4] = (row.h_signed_z, row.h_dfs_z, row.h_velocity_z, 1.0)
        matrix[index, 4 + role_indices[row.role]] = 1.0
    return matrix


def _linear_predictor(
    theta: NDArray[np.float64],
    design: NDArray[np.float64],
    baseline_logit: NDArray[np.float64],
    *,
    amplitude: float,
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    raw = design[:, :3] @ theta[:3]
    scaled = raw / amplitude
    tangent = np.tanh(scaled)
    first = 1.0 - np.square(tangent)
    second = (-2.0 / amplitude) * first * tangent
    intercept = design[:, 3:] @ theta[3:]
    jacobian = design.copy()
    jacobian[:, :3] *= first[:, np.newaxis]
    return baseline_logit + intercept + amplitude * tangent, jacobian, second


def _dispersion(
    residual: NDArray[np.float64],
    variance: NDArray[np.float64],
    *,
    degrees_of_freedom: int,
) -> float:
    """Pearson chi-square per degree of freedom, never below one.

    Below one would mean the data are *less* variable than a binomial, which for shared
    slates and repeated players is not credible; above one it widens every interval by
    the same factor, which is the cheapest honest answer to overdispersion without a
    hierarchical model (that is Phase 4's job).
    """

    if degrees_of_freedom <= 0 or residual.size == 0:
        return 1.0
    pearson = float(np.sum(np.square(residual) / np.maximum(variance, 1e-12)))
    return max(1.0, pearson / degrees_of_freedom)


def _stable_inverse(matrix: NDArray[np.float64]) -> NDArray[np.float64]:
    symmetric = (matrix + matrix.T) / 2.0
    eigenvalues, eigenvectors = np.linalg.eigh(symmetric)
    floor = max(float(np.max(eigenvalues)) * 1e-10, 1e-10)
    clipped = np.maximum(eigenvalues, floor)
    return (eigenvectors * (1.0 / clipped)) @ eigenvectors.T


def _clip_probability(value: float, epsilon: float) -> float:
    return min(max(float(value), epsilon), 1.0 - epsilon)


def _logit_array(values: NDArray[np.float64]) -> NDArray[np.float64]:
    return np.log(values) - np.log1p(-values)


def _expit(values: NDArray[np.float64]) -> NDArray[np.float64]:
    output = np.empty_like(values, dtype=np.float64)
    positive = values >= 0
    output[positive] = 1.0 / (1.0 + np.exp(-values[positive]))
    exponent = np.exp(values[~positive])
    output[~positive] = exponent / (1.0 + exponent)
    return output
