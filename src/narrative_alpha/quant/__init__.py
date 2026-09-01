"""L3: projection blend, player distributions, ownership model, dependence."""

from narrative_alpha.quant.distributions import (
    DEFAULT_FIT_TOLERANCE,
    FITTER_VERSION,
    SOURCE_POSITION_QUANTILES,
    DistributionConfigurationError,
    DistributionError,
    DistributionFitError,
    DistributionFitResult,
    PlayerOutcomeDistribution,
    QuantileConfiguration,
    QuantileInterpretation,
    fit_configuration_sha256,
    fit_player_distribution,
    fit_player_distribution_with_diagnostics,
)
from narrative_alpha.quant.scoring import (
    PITCalibration,
    continuous_ranked_probability_score,
    crps,
    log_score,
    pit_histogram,
    randomized_pit,
)

__all__ = [
    "DEFAULT_FIT_TOLERANCE",
    "FITTER_VERSION",
    "SOURCE_POSITION_QUANTILES",
    "DistributionConfigurationError",
    "DistributionError",
    "DistributionFitError",
    "DistributionFitResult",
    "PITCalibration",
    "PlayerOutcomeDistribution",
    "QuantileConfiguration",
    "QuantileInterpretation",
    "continuous_ranked_probability_score",
    "crps",
    "fit_configuration_sha256",
    "fit_player_distribution",
    "fit_player_distribution_with_diagnostics",
    "log_score",
    "pit_histogram",
    "randomized_pit",
]
