"""Proper scoring and calibration helpers for player outcome distributions."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from random import Random
from statistics import NormalDist

from narrative_alpha.quant.distributions import (
    DistributionError,
    PlayerOutcomeDistribution,
)

_STANDARD_NORMAL = NormalDist()


@dataclass(frozen=True)
class PITCalibration:
    """Histogram summary for randomized probability-integral-transform values."""

    values: tuple[float, ...]
    bin_edges: tuple[float, ...]
    bin_counts: tuple[int, ...]
    expected_count: float
    pearson_chi_square: float
    max_abs_frequency_deviation: float


def crps(distribution: PlayerOutcomeDistribution, realized: float) -> float:
    """Return the exact CRPS using ``E|X-y| - 0.5 E|X-X'|``.

    The closed form includes both cross-terms between the inactive atom and the
    log-normal active component.  Lower scores are better.
    """

    outcome = _finite_outcome(realized)
    active_probability = distribution.p_active
    inactive_probability = distribution.inactive_probability
    active_mean = distribution.conditional_mean
    expected_absolute_error = (
        inactive_probability * abs(outcome)
        + active_probability * _active_expected_absolute_error(distribution, outcome)
    )

    # The expression below is the log-normal Gini mean difference.
    lognormal_mean = active_mean - distribution.conditional_location
    half_active_pair_distance = lognormal_mean * (
        2.0 * _STANDARD_NORMAL.cdf(distribution.conditional_shape / math.sqrt(2.0))
        - 1.0
    )
    half_mixture_pair_distance = (
        active_probability**2 * half_active_pair_distance
        + active_probability * inactive_probability * active_mean
    )
    score = expected_absolute_error - half_mixture_pair_distance
    if not math.isfinite(score):
        raise DistributionError("CRPS exceeded the supported floating-point range")
    # Floating-point cancellation can yield a tiny negative value at a degenerate limit.
    if score < -1e-12 * max(1.0, expected_absolute_error):
        raise DistributionError("CRPS calculation produced a negative value")
    return max(0.0, score)


def continuous_ranked_probability_score(
    distribution: PlayerOutcomeDistribution,
    realized: float,
) -> float:
    """Long-form alias for :func:`crps`."""

    return crps(distribution, realized)


def log_score(distribution: PlayerOutcomeDistribution, realized: float) -> float:
    """Return negative log predictive mass/density; lower scores are better.

    At zero the inactive probability is a discrete mass and must not be scored as a
    continuous density.  Unsupported observations receive positive infinity.

    DK/FD fantasy points can be negative (lost fumbles, interceptions, a bad defense), and
    the zero-floored family gives those outcomes no density, so a real slate can produce
    ``+inf`` here.  Callers aggregating log score must count off-support outcomes
    separately rather than averaging an infinity into a summary number; CRPS stays finite
    for the same outcome and is the safer headline shape metric until the family carries a
    negative tail.
    """

    outcome = _finite_outcome(realized)
    if outcome == 0.0:
        mass = distribution.inactive_probability
        return math.inf if mass == 0.0 else -math.log(mass)
    if outcome < 0.0 or distribution.p_active == 0.0:
        return math.inf
    conditional_log_density = distribution.conditional_log_density(outcome)
    if conditional_log_density == -math.inf:
        return math.inf
    return -(math.log(distribution.p_active) + conditional_log_density)


def randomized_pit(
    distribution: PlayerOutcomeDistribution,
    realized: float,
    rng: Random,
) -> float:
    """Return a randomized PIT value, including correct handling of the zero atom."""

    outcome = _finite_outcome(realized)
    lower = distribution.cdf_left(outcome)
    upper = distribution.cdf(outcome)
    return lower + rng.random() * (upper - lower)


def pit_histogram(
    distributions: Sequence[PlayerOutcomeDistribution],
    realized: Sequence[float],
    *,
    bins: int,
    rng: Random,
) -> PITCalibration:
    """Build a deterministic-with-supplied-RNG PIT histogram and uniformity diagnostics."""

    if not distributions:
        raise DistributionError("PIT calibration requires at least one distribution")
    if len(distributions) != len(realized):
        raise DistributionError("distributions and realized outcomes must have equal length")
    if isinstance(bins, bool) or not isinstance(bins, int) or bins < 2:
        raise DistributionError("bins must be an integer of at least 2")

    values = tuple(
        randomized_pit(distribution, outcome, rng)
        for distribution, outcome in zip(distributions, realized, strict=True)
    )
    counts = [0] * bins
    for value in values:
        index = min(int(value * bins), bins - 1)
        counts[index] += 1

    sample_size = len(values)
    expected_count = sample_size / bins
    chi_square = sum(
        (count - expected_count) ** 2 / expected_count for count in counts
    )
    expected_frequency = 1.0 / bins
    max_deviation = max(
        abs(count / sample_size - expected_frequency) for count in counts
    )
    return PITCalibration(
        values=values,
        bin_edges=tuple(index / bins for index in range(bins + 1)),
        bin_counts=tuple(counts),
        expected_count=expected_count,
        pearson_chi_square=chi_square,
        max_abs_frequency_deviation=max_deviation,
    )


def _active_expected_absolute_error(
    distribution: PlayerOutcomeDistribution,
    outcome: float,
) -> float:
    residual_outcome = outcome - distribution.conditional_location
    lognormal_mean = (
        distribution.conditional_scale
        * math.exp(0.5 * distribution.conditional_shape**2)
    )
    if residual_outcome <= 0.0:
        return distribution.conditional_location + lognormal_mean - outcome

    standardized = (
        math.log(residual_outcome) - math.log(distribution.conditional_scale)
    ) / distribution.conditional_shape
    cdf = _STANDARD_NORMAL.cdf(standardized)
    truncated_mean_probability = _STANDARD_NORMAL.cdf(
        standardized - distribution.conditional_shape
    )
    expected_distance = (
        residual_outcome * (2.0 * cdf - 1.0)
        + lognormal_mean * (1.0 - 2.0 * truncated_mean_probability)
    )
    if not math.isfinite(expected_distance):
        raise DistributionError("CRPS exceeded the supported floating-point range")
    return expected_distance


def _finite_outcome(value: float) -> float:
    if isinstance(value, bool):
        raise DistributionError("realized outcome must be finite")
    try:
        outcome = float(value)
    except (TypeError, ValueError) as error:
        raise DistributionError("realized outcome must be finite") from error
    if not math.isfinite(outcome):
        raise DistributionError("realized outcome must be finite")
    return outcome
