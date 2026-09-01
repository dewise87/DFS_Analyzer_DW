"""Player-level marginal outcome distributions and quantile-based fitting.

The first implementation uses a zero-location log-normal distribution for fantasy points
conditional on the player being active.  It is deliberately simple, has a lower endpoint
at zero, and supplies the right skew seen in NFL player outcomes.  The vendor mean is
preserved exactly; shape is identified by the ratio of two explicitly configured vendor
quantiles, and an inconsistent triplet is rejected when its quantile error exceeds the
visible tolerance.

``p_full_role_given_active`` is retained as a separate availability-gate parameter.  It
does not create an invented limited-role scoring distribution: until that second
conditional component is modeled, the marginal distribution is exactly an inactive
point mass at zero plus the fitted active distribution.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from random import Random
from statistics import NormalDist
from types import MappingProxyType
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_FIT_TOLERANCE: Final = 0.02
FITTER_VERSION: Final = "lognormal-mean-quantile-ratio-v1"
_STANDARD_NORMAL: Final = NormalDist()


class DistributionError(ValueError):
    """Base error for an invalid player-distribution operation."""


class DistributionConfigurationError(DistributionError):
    """Raised when a vendor floor/ceiling interpretation is not configured."""


class DistributionFitError(DistributionError):
    """Raised when inputs cannot produce a converged conditional distribution."""


@dataclass(frozen=True)
class QuantileInterpretation:
    """Configured meanings of one source/position floor and ceiling pair."""

    floor_quantile: float
    ceiling_quantile: float

    def __post_init__(self) -> None:
        values = (self.floor_quantile, self.ceiling_quantile)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("configured quantiles must be finite")
        if not 0.0 < self.floor_quantile < self.ceiling_quantile < 1.0:
            raise ValueError(
                "configured quantiles must satisfy 0 < floor_quantile "
                "< ceiling_quantile < 1"
            )


QuantileConfiguration = Mapping[tuple[str, str], QuantileInterpretation]

# Production entries stay empty until a vendor's documented semantics or position-level
# historical calibration establishes them.  Callers may pass an explicit table to the
# fitter; there is intentionally no cross-source or cross-position fallback.
SOURCE_POSITION_QUANTILES: Final[QuantileConfiguration] = MappingProxyType({})


class PlayerOutcomeDistribution(BaseModel):
    """Inactive/active mixture with a zero-location log-normal active component.

    ``conditional_location`` is fixed at zero, ``conditional_scale`` is ``exp(mu)``
    (the component's median), and ``conditional_shape`` is the log-space standard
    deviation.  All conditional methods mean "given active".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    distribution_family: Literal["lognormal"] = "lognormal"
    p_active: float = Field(ge=0, le=1, allow_inf_nan=False)
    p_full_role_given_active: float = Field(ge=0, le=1, allow_inf_nan=False)
    conditional_location: float = Field(ge=0, allow_inf_nan=False)
    conditional_scale: float = Field(gt=0, allow_inf_nan=False)
    conditional_shape: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_finite_mean(self) -> Self:
        if self.conditional_location != 0.0:
            raise ValueError("lognormal conditional_location must be zero")
        try:
            mean = self.conditional_location + self.conditional_scale * math.exp(
                0.5 * self.conditional_shape**2
            )
        except OverflowError as error:
            raise ValueError("conditional parameters must have a finite mean") from error
        if not math.isfinite(mean):
            raise ValueError("conditional parameters must have a finite mean")
        return self

    @property
    def inactive_probability(self) -> float:
        """Probability mass at exactly zero from the inactive component."""

        return 1.0 - self.p_active

    @property
    def conditional_mean(self) -> float:
        """Expected fantasy points given that the player is active."""

        return self.conditional_location + self.conditional_scale * math.exp(
            0.5 * self.conditional_shape**2
        )

    @property
    def mean(self) -> float:
        """Expected fantasy points over the inactive/active mixture."""

        return self.p_active * self.conditional_mean

    def conditional_quantile(self, q: float) -> float:
        """Return the active-component quantile for a probability in ``[0, 1]``."""

        probability = _probability(q)
        if probability == 0.0:
            return self.conditional_location
        if probability == 1.0:
            return math.inf
        z_score = _STANDARD_NORMAL.inv_cdf(probability)
        exponent = self.conditional_shape * z_score
        return self.conditional_location + self.conditional_scale * _safe_exp(exponent)

    def quantile(self, q: float) -> float:
        """Return a generalized-inverse quantile of the unconditional mixture."""

        probability = _probability(q)
        if probability <= self.inactive_probability:
            return 0.0
        if self.p_active == 0.0:
            return 0.0
        conditional_probability = (
            probability - self.inactive_probability
        ) / self.p_active
        return self.conditional_quantile(conditional_probability)

    def conditional_cdf(self, value: float) -> float:
        """Return the active-component CDF at ``value``."""

        outcome = _finite_value(value, "value")
        residual = outcome - self.conditional_location
        if residual <= 0.0:
            return 0.0
        z_score = (
            math.log(residual) - math.log(self.conditional_scale)
        ) / self.conditional_shape
        return _STANDARD_NORMAL.cdf(z_score)

    def cdf(self, value: float) -> float:
        """Return the right-continuous CDF of the unconditional mixture."""

        outcome = _finite_value(value, "value")
        if outcome < 0.0:
            return 0.0
        return self.inactive_probability + self.p_active * self.conditional_cdf(outcome)

    def cdf_left(self, value: float) -> float:
        """Return the CDF immediately to the left, exposing the atom at zero."""

        outcome = _finite_value(value, "value")
        if outcome <= 0.0:
            return 0.0
        return self.cdf(outcome)

    def conditional_log_density(self, value: float) -> float:
        """Return log density under the active component, or ``-inf`` off support."""

        outcome = _finite_value(value, "value")
        residual = outcome - self.conditional_location
        if residual <= 0.0:
            return -math.inf
        log_residual = math.log(residual)
        log_scale = math.log(self.conditional_scale)
        standardized = (log_residual - log_scale) / self.conditional_shape
        return (
            -log_residual
            - math.log(self.conditional_shape)
            - 0.5 * math.log(2.0 * math.pi)
            - 0.5 * standardized**2
        )

    def sample(self, n: int, rng: Random) -> tuple[float, ...]:
        """Draw ``n`` outcomes using an explicitly supplied random generator."""

        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise ValueError("n must be a non-negative integer")
        log_scale = math.log(self.conditional_scale)
        outcomes: list[float] = []
        for _ in range(n):
            if rng.random() >= self.p_active:
                outcomes.append(0.0)
            else:
                try:
                    outcome = rng.lognormvariate(log_scale, self.conditional_shape)
                except OverflowError as error:
                    raise DistributionError(
                        "sample exceeded the supported floating-point range"
                    ) from error
                if not math.isfinite(outcome):
                    raise DistributionError(
                        "sample exceeded the supported floating-point range"
                    )
                outcomes.append(self.conditional_location + outcome)
        return tuple(outcomes)


class DistributionFitResult(BaseModel):
    """Persistence-ready diagnostics from one configured distribution fit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    distribution: PlayerOutcomeDistribution
    source: str
    position: str
    input_mean: float = Field(gt=0, allow_inf_nan=False)
    input_floor: float = Field(gt=0, allow_inf_nan=False)
    input_ceiling: float = Field(gt=0, allow_inf_nan=False)
    floor_quantile: float = Field(gt=0, lt=1, allow_inf_nan=False)
    ceiling_quantile: float = Field(gt=0, lt=1, allow_inf_nan=False)
    fit_tolerance: float = Field(gt=0, allow_inf_nan=False)
    fit_max_relative_error: float = Field(ge=0, allow_inf_nan=False)
    fit_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fitter_version: Literal["lognormal-mean-quantile-ratio-v1"] = FITTER_VERSION

    @model_validator(mode="after")
    def validate_diagnostics(self) -> Self:
        if self.source != _normalized_source(self.source):
            raise ValueError("fit source must be normalized")
        if self.position != _normalized_position(self.position):
            raise ValueError("fit position must be normalized")
        if not self.input_floor < self.input_mean < self.input_ceiling:
            raise ValueError("inputs must satisfy input_floor < input_mean < input_ceiling")
        if self.floor_quantile >= self.ceiling_quantile:
            raise ValueError("floor_quantile must be below ceiling_quantile")
        interpretation = QuantileInterpretation(
            self.floor_quantile,
            self.ceiling_quantile,
        )
        expected_config_hash = fit_configuration_sha256(
            source=self.source,
            position=self.position,
            interpretation=interpretation,
            tolerance=self.fit_tolerance,
            fitter_version=self.fitter_version,
        )
        if self.fit_config_sha256 != expected_config_hash:
            raise ValueError("fit_config_sha256 does not match the fit configuration")
        errors = _fit_relative_errors(
            self.distribution,
            mean=self.input_mean,
            floor=self.input_floor,
            ceiling=self.input_ceiling,
            interpretation=interpretation,
        )
        actual_max_error = max(errors)
        if not math.isclose(
            self.fit_max_relative_error,
            actual_max_error,
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            raise ValueError("fit_max_relative_error does not match the fitted distribution")
        if actual_max_error > self.fit_tolerance:
            raise ValueError("fit_max_relative_error must not exceed fit_tolerance")
        return self


def fit_player_distribution(
    *,
    source: str,
    position: str,
    mean: float,
    floor: float,
    ceiling: float,
    p_active: float,
    p_full_role_given_active: float,
    quantile_configuration: QuantileConfiguration = SOURCE_POSITION_QUANTILES,
    tolerance: float = DEFAULT_FIT_TOLERANCE,
) -> PlayerOutcomeDistribution:
    """Fit a zero-location conditional log-normal to configured vendor inputs.

    The three vendor values are interpreted as conditional-on-active.  Quantile meanings
    are looked up by the exact normalized ``(source, position)`` key; guessing another
    position or a generic vendor default is forbidden.  The input mean is exact, while
    floor and ceiling must both round-trip within ``tolerance``.
    """

    return fit_player_distribution_with_diagnostics(
        source=source,
        position=position,
        mean=mean,
        floor=floor,
        ceiling=ceiling,
        p_active=p_active,
        p_full_role_given_active=p_full_role_given_active,
        quantile_configuration=quantile_configuration,
        tolerance=tolerance,
    ).distribution


def fit_player_distribution_with_diagnostics(
    *,
    source: str,
    position: str,
    mean: float,
    floor: float,
    ceiling: float,
    p_active: float,
    p_full_role_given_active: float,
    quantile_configuration: QuantileConfiguration = SOURCE_POSITION_QUANTILES,
    tolerance: float = DEFAULT_FIT_TOLERANCE,
) -> DistributionFitResult:
    """Fit a distribution and return every diagnostic needed by the store row."""

    key = (_normalized_source(source), _normalized_position(position))
    try:
        interpretation = quantile_configuration[key]
    except KeyError as error:
        raise DistributionConfigurationError(
            f"no floor/ceiling quantiles configured for source={key[0]!r}, "
            f"position={key[1]!r}"
        ) from error

    conditional_mean = _finite_value(mean, "mean")
    lower = _finite_value(floor, "floor")
    upper = _finite_value(ceiling, "ceiling")
    fit_tolerance = _finite_value(tolerance, "tolerance")
    if not 0.0 < lower < conditional_mean < upper:
        raise DistributionFitError("inputs must satisfy 0 < floor < mean < ceiling")
    if fit_tolerance <= 0.0:
        raise DistributionFitError("tolerance must be positive")

    lower_z = _STANDARD_NORMAL.inv_cdf(interpretation.floor_quantile)
    upper_z = _STANDARD_NORMAL.inv_cdf(interpretation.ceiling_quantile)
    shape = (math.log(upper) - math.log(lower)) / (upper_z - lower_z)
    scale = conditional_mean * _safe_exp(-0.5 * shape**2)
    try:
        distribution = PlayerOutcomeDistribution(
            p_active=p_active,
            p_full_role_given_active=p_full_role_given_active,
            conditional_location=0.0,
            conditional_scale=scale,
            conditional_shape=shape,
        )
    except ValueError as error:
        raise DistributionFitError(f"fitted parameters are invalid: {error}") from error

    errors = _fit_relative_errors(
        distribution,
        mean=conditional_mean,
        floor=lower,
        ceiling=upper,
        interpretation=interpretation,
    )
    max_relative_error = max(errors)
    if max_relative_error > fit_tolerance:
        raise DistributionFitError(
            "lognormal fit did not converge within tolerance "
            f"{fit_tolerance:g}; maximum relative error was {max_relative_error:.6g}"
        )
    return DistributionFitResult(
        distribution=distribution,
        source=key[0],
        position=key[1],
        input_mean=conditional_mean,
        input_floor=lower,
        input_ceiling=upper,
        floor_quantile=interpretation.floor_quantile,
        ceiling_quantile=interpretation.ceiling_quantile,
        fit_tolerance=fit_tolerance,
        fit_max_relative_error=max_relative_error,
        fit_config_sha256=fit_configuration_sha256(
            source=key[0],
            position=key[1],
            interpretation=interpretation,
            tolerance=fit_tolerance,
        ),
    )


def fit_configuration_sha256(
    *,
    source: str,
    position: str,
    interpretation: QuantileInterpretation,
    tolerance: float = DEFAULT_FIT_TOLERANCE,
    fitter_version: str = FITTER_VERSION,
) -> str:
    """Hash the exact configured semantics and algorithm version for persistence."""

    fit_tolerance = _finite_value(tolerance, "tolerance")
    if fit_tolerance <= 0.0:
        raise DistributionConfigurationError("tolerance must be positive")
    version = fitter_version.strip()
    if not version:
        raise DistributionConfigurationError("fitter_version must not be empty")
    payload = {
        "ceiling_quantile": interpretation.ceiling_quantile.hex(),
        "distribution_family": "lognormal",
        "fitter_version": version,
        "floor_quantile": interpretation.floor_quantile.hex(),
        "position": _normalized_position(position),
        "schema_version": 1,
        "source": _normalized_source(source),
        "tolerance": fit_tolerance.hex(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalized_source(value: str) -> str:
    normalized = value.strip().casefold()
    if not normalized:
        raise DistributionConfigurationError("source must not be empty")
    return normalized


def _normalized_position(value: str) -> str:
    normalized = value.strip().upper()
    if not normalized:
        raise DistributionConfigurationError("position must not be empty")
    return normalized


def _finite_value(value: float, label: str) -> float:
    if isinstance(value, bool):
        raise DistributionError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise DistributionError(f"{label} must be a finite number") from error
    if not math.isfinite(number):
        raise DistributionError(f"{label} must be a finite number")
    return number


def _probability(value: float) -> float:
    probability = _finite_value(value, "q")
    if not 0.0 <= probability <= 1.0:
        raise DistributionError("q must be between 0 and 1")
    return probability


def _fit_relative_errors(
    distribution: PlayerOutcomeDistribution,
    *,
    mean: float,
    floor: float,
    ceiling: float,
    interpretation: QuantileInterpretation,
) -> tuple[float, float, float]:
    return (
        _relative_error(distribution.conditional_mean, mean),
        _relative_error(
            distribution.conditional_quantile(interpretation.floor_quantile), floor
        ),
        _relative_error(
            distribution.conditional_quantile(interpretation.ceiling_quantile), ceiling
        ),
    )


def _relative_error(actual: float, expected: float) -> float:
    return abs(actual - expected) / abs(expected)


def _safe_exp(value: float) -> float:
    try:
        return math.exp(value)
    except OverflowError:
        return math.inf
