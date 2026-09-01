import math
import random
from statistics import NormalDist, fmean, stdev

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from narrative_alpha.quant import (
    FITTER_VERSION,
    DistributionConfigurationError,
    DistributionError,
    DistributionFitError,
    PlayerOutcomeDistribution,
    QuantileInterpretation,
    crps,
    fit_configuration_sha256,
    fit_player_distribution,
    fit_player_distribution_with_diagnostics,
    log_score,
    pit_histogram,
    randomized_pit,
)

NORMAL = NormalDist()
FIXTURE_QUANTILES = {
    ("fixture-vendor", "WR"): QuantileInterpretation(0.1, 0.9),
    ("fixture-vendor", "QB"): QuantileInterpretation(0.2, 0.8),
}


def _distribution(
    *,
    p_active: float = 0.75,
    p_full_role: float = 0.8,
    scale: float = 10.0,
    shape: float = 0.5,
) -> PlayerOutcomeDistribution:
    return PlayerOutcomeDistribution(
        p_active=p_active,
        p_full_role_given_active=p_full_role,
        conditional_location=0.0,
        conditional_scale=scale,
        conditional_shape=shape,
    )


def _conditional_inputs(
    *,
    scale: float,
    shape: float,
    quantiles: QuantileInterpretation,
) -> tuple[float, float, float]:
    mean = scale * math.exp(0.5 * shape**2)
    floor = scale * math.exp(shape * NORMAL.inv_cdf(quantiles.floor_quantile))
    ceiling = scale * math.exp(shape * NORMAL.inv_cdf(quantiles.ceiling_quantile))
    return mean, floor, ceiling


def test_mixture_mean_quantiles_and_full_role_metadata() -> None:
    distribution = _distribution(p_active=0.7, p_full_role=0.2)
    otherwise_identical = _distribution(p_active=0.7, p_full_role=0.95)

    assert distribution.mean == pytest.approx(0.7 * distribution.conditional_mean)
    assert distribution.quantile(0.0) == 0.0
    assert distribution.quantile(0.3) == 0.0
    assert distribution.quantile(0.65) == pytest.approx(
        distribution.conditional_quantile(0.5)
    )
    assert math.isinf(distribution.quantile(1.0))
    assert distribution.mean == otherwise_identical.mean
    assert distribution.quantile(0.8) == otherwise_identical.quantile(0.8)


def test_sampling_is_seeded_nonnegative_and_respects_inactive_atom() -> None:
    distribution = _distribution(p_active=0.65)
    first = distribution.sample(5_000, random.Random(42))
    second = distribution.sample(5_000, random.Random(42))

    assert first == second
    assert all(value >= 0.0 for value in first)
    zero_fraction = sum(value == 0.0 for value in first) / len(first)
    assert zero_fraction == pytest.approx(0.35, abs=0.025)
    assert _distribution(p_active=0.0).sample(20, random.Random(1)) == (0.0,) * 20


@pytest.mark.parametrize("q", [-0.01, 1.01, math.nan, math.inf])
def test_quantile_rejects_invalid_probabilities(q: float) -> None:
    with pytest.raises(DistributionError):
        _distribution().quantile(q)


def test_model_rejects_invalid_probabilities_and_parameters() -> None:
    with pytest.raises(ValidationError):
        _distribution(p_active=1.1)
    with pytest.raises(ValidationError):
        PlayerOutcomeDistribution(
            p_active=1.0,
            p_full_role_given_active=1.0,
            conditional_location=0.1,
            conditional_scale=10.0,
            conditional_shape=0.5,
        )
    with pytest.raises(ValidationError):
        PlayerOutcomeDistribution(
            p_active=1.0,
            p_full_role_given_active=1.0,
            conditional_location=0.0,
            conditional_scale=math.inf,
            conditional_shape=0.5,
        )


def test_fitted_mean_and_configured_quantiles_round_trip() -> None:
    interpretation = FIXTURE_QUANTILES[("fixture-vendor", "WR")]
    mean, floor, ceiling = _conditional_inputs(
        scale=11.0,
        shape=0.55,
        quantiles=interpretation,
    )

    fitted = fit_player_distribution(
        source=" Fixture-Vendor ",
        position="wr",
        mean=mean,
        floor=floor,
        ceiling=ceiling,
        p_active=0.82,
        p_full_role_given_active=0.74,
        quantile_configuration=FIXTURE_QUANTILES,
        tolerance=1e-10,
    )

    assert fitted.conditional_location == 0.0
    assert fitted.conditional_mean == pytest.approx(mean, rel=1e-10)
    assert fitted.conditional_quantile(interpretation.floor_quantile) == pytest.approx(
        floor, rel=1e-10
    )
    assert fitted.conditional_quantile(interpretation.ceiling_quantile) == pytest.approx(
        ceiling, rel=1e-10
    )


def test_fit_diagnostics_are_persistence_ready_and_canonically_hashed() -> None:
    interpretation = FIXTURE_QUANTILES[("fixture-vendor", "WR")]
    mean, floor, ceiling = _conditional_inputs(
        scale=11.0,
        shape=0.55,
        quantiles=interpretation,
    )
    result = fit_player_distribution_with_diagnostics(
        source="Fixture-Vendor",
        position="wr",
        mean=mean,
        floor=floor,
        ceiling=ceiling,
        p_active=0.82,
        p_full_role_given_active=0.74,
        quantile_configuration=FIXTURE_QUANTILES,
        tolerance=1e-10,
    )

    assert result.source == "fixture-vendor"
    assert result.position == "WR"
    assert result.input_mean == mean
    assert result.floor_quantile == interpretation.floor_quantile
    assert result.fit_max_relative_error <= result.fit_tolerance
    assert result.fitter_version == FITTER_VERSION
    assert result.fit_config_sha256 == fit_configuration_sha256(
        source="fixture-vendor",
        position="WR",
        interpretation=interpretation,
        tolerance=1e-10,
    )


def test_default_tolerance_accepts_a_small_vendor_quantile_inconsistency() -> None:
    interpretation = FIXTURE_QUANTILES[("fixture-vendor", "WR")]
    mean, floor, ceiling = _conditional_inputs(
        scale=10.0,
        shape=0.5,
        quantiles=interpretation,
    )
    result = fit_player_distribution_with_diagnostics(
        source="fixture-vendor",
        position="WR",
        mean=mean,
        floor=floor * 1.01,
        ceiling=ceiling * 1.01,
        p_active=1.0,
        p_full_role_given_active=1.0,
        quantile_configuration=FIXTURE_QUANTILES,
    )

    assert 0.0 < result.fit_max_relative_error < result.fit_tolerance


def test_fitter_uses_source_and_position_specific_quantiles() -> None:
    shared_inputs = {"mean": 15.0, "floor": 7.0, "ceiling": 27.0}
    wr = fit_player_distribution(
        source="fixture-vendor",
        position="WR",
        p_active=1.0,
        p_full_role_given_active=1.0,
        quantile_configuration=FIXTURE_QUANTILES,
        tolerance=1.0,
        **shared_inputs,
    )
    qb = fit_player_distribution(
        source="fixture-vendor",
        position="QB",
        p_active=1.0,
        p_full_role_given_active=1.0,
        quantile_configuration=FIXTURE_QUANTILES,
        tolerance=1.0,
        **shared_inputs,
    )

    assert wr.conditional_shape != pytest.approx(qb.conditional_shape)


@pytest.mark.parametrize(
    ("source", "position"),
    [("unknown", "WR"), ("fixture-vendor", "TE")],
)
def test_fitter_refuses_unconfigured_source_or_position(source: str, position: str) -> None:
    with pytest.raises(DistributionConfigurationError, match="no floor/ceiling quantiles"):
        fit_player_distribution(
            source=source,
            position=position,
            mean=15.0,
            floor=7.0,
            ceiling=27.0,
            p_active=1.0,
            p_full_role_given_active=1.0,
            quantile_configuration=FIXTURE_QUANTILES,
        )


@pytest.mark.parametrize(
    ("mean", "floor", "ceiling"),
    [
        (10.0, 11.0, 20.0),
        (10.0, 5.0, 9.0),
        (10.0, 10.0, 20.0),
        (10.0, 0.0, 20.0),
        (math.inf, 5.0, 20.0),
    ],
)
def test_fitter_refuses_invalid_ordering_and_nonfinite_values(
    mean: float,
    floor: float,
    ceiling: float,
) -> None:
    with pytest.raises(DistributionError):
        fit_player_distribution(
            source="fixture-vendor",
            position="WR",
            mean=mean,
            floor=floor,
            ceiling=ceiling,
            p_active=1.0,
            p_full_role_given_active=1.0,
            quantile_configuration=FIXTURE_QUANTILES,
        )


def test_fitter_refuses_family_incompatibility_outside_tolerance() -> None:
    with pytest.raises(DistributionFitError, match="did not converge within tolerance"):
        fit_player_distribution(
            source="fixture-vendor",
            position="WR",
            mean=10.0,
            floor=1.0,
            ceiling=100.0,
            p_active=1.0,
            p_full_role_given_active=1.0,
            quantile_configuration=FIXTURE_QUANTILES,
            tolerance=1e-8,
        )


def test_quantile_interpretation_refuses_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="configured quantiles"):
        QuantileInterpretation(0.9, 0.1)


def test_cdf_and_quantile_are_inverse_above_the_inactive_atom() -> None:
    distribution = _distribution(p_active=0.7)
    quantiles = (0.31, 0.4, 0.55, 0.75, 0.95, 0.999)
    outcomes = tuple(distribution.quantile(q) for q in quantiles)

    assert outcomes == tuple(sorted(outcomes))
    for q, outcome in zip(quantiles, outcomes, strict=True):
        assert distribution.cdf(outcome) == pytest.approx(q, abs=1e-12)


@settings(max_examples=10, deadline=None)
@given(
    p_active=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    scale=st.floats(min_value=2.0, max_value=20.0, allow_nan=False),
    shape=st.floats(min_value=0.15, max_value=0.75, allow_nan=False),
    realized=st.floats(min_value=-5.0, max_value=45.0, allow_nan=False),
)
def test_crps_matches_monte_carlo_definition(
    p_active: float,
    scale: float,
    shape: float,
    realized: float,
) -> None:
    distribution = _distribution(p_active=p_active, scale=scale, shape=shape)
    sample_size = 30_000
    rng = random.Random(20260901)
    log_scale = math.log(scale)

    def independent_draw() -> float:
        if rng.random() >= p_active:
            return 0.0
        return rng.lognormvariate(log_scale, shape)

    first = tuple(independent_draw() for _ in range(sample_size))
    second = tuple(independent_draw() for _ in range(sample_size))
    terms = [
        abs(value - realized) - 0.5 * abs(value - paired)
        for value, paired in zip(first, second, strict=True)
    ]
    estimate = fmean(terms)
    standard_error = stdev(terms) / math.sqrt(sample_size)

    assert abs(crps(distribution, realized) - estimate) <= max(
        0.01, 6.0 * standard_error
    )


def test_crps_degenerate_and_log_score_atom_rules() -> None:
    inactive = _distribution(p_active=0.0)
    distribution = _distribution(p_active=0.75)

    assert crps(inactive, -3.0) == 3.0
    assert log_score(distribution, 0.0) == pytest.approx(-math.log(0.25))
    positive = distribution.conditional_quantile(0.6)
    median = distribution.conditional_scale
    median_log_density = (
        -math.log(median)
        - math.log(distribution.conditional_shape)
        - 0.5 * math.log(2.0 * math.pi)
    )
    assert log_score(distribution, median) == pytest.approx(
        -(math.log(0.75) + median_log_density)
    )
    assert math.isinf(log_score(inactive, positive))
    assert math.isinf(log_score(distribution, -0.5))
    assert math.isfinite(log_score(distribution, 1e100))


def test_crps_handles_a_finite_extreme_mean_without_intermediate_overflow() -> None:
    distribution = _distribution(
        p_active=1.0,
        scale=1e308,
        shape=1.0,
    )

    assert math.isfinite(distribution.conditional_mean)
    assert math.isfinite(crps(distribution, 0.0))
    with pytest.raises(DistributionError, match="floating-point range"):
        distribution.sample(1, random.Random(0))


def test_randomized_pit_spreads_the_zero_atom() -> None:
    distribution = _distribution(p_active=0.65)
    values = [randomized_pit(distribution, 0.0, random.Random(seed)) for seed in range(50)]
    assert min(values) >= 0.0
    assert max(values) <= 0.35
    assert len(set(values)) == len(values)


def test_pit_histogram_distinguishes_calibrated_and_miscalibrated_samples() -> None:
    truth = _distribution(p_active=0.65, scale=10.0, shape=0.5)
    sample_size = 20_000
    outcome_rng = random.Random(731)
    outcomes = tuple(
        0.0
        if outcome_rng.random() >= truth.p_active
        else outcome_rng.lognormvariate(math.log(10.0), 0.5)
        for _ in range(sample_size)
    )
    well = pit_histogram(
        (truth,) * sample_size,
        outcomes,
        bins=10,
        rng=random.Random(991),
    )
    wrong = _distribution(p_active=0.98, scale=6.0, shape=0.25)
    miscalibrated = pit_histogram(
        (wrong,) * sample_size,
        outcomes,
        bins=10,
        rng=random.Random(991),
    )

    assert sum(well.bin_counts) == sample_size
    assert well.bin_edges == tuple(index / 10 for index in range(11))
    assert well.pearson_chi_square < 30.0
    assert miscalibrated.pearson_chi_square > max(
        100.0, 5.0 * well.pearson_chi_square
    )

    final_bin = pit_histogram(
        (_distribution(p_active=0.0),),
        (1.0,),
        bins=10,
        rng=random.Random(1),
    )
    assert final_bin.bin_counts[-1] == 1
