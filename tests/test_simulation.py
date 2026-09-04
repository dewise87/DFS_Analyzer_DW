"""Experimental simulation: marginals, dependence, field calibration, and payouts."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from narrative_alpha.portfolio import (
    CandidatePlayer,
    CandidatePlayerScenario,
    ContestArchetype,
    DfsSite,
    Lineup,
    LineupPlayer,
    OptimizationRequest,
    SlateType,
    lineup_sha256,
)
from narrative_alpha.quant import PlayerOutcomeDistribution
from narrative_alpha.simulation import (
    FieldGenerationError,
    PlayerSimulationInput,
    draw_player_outcomes,
    evaluate_contest,
    generate_field,
    load_simulation_config,
    split_tied_payouts,
)


def test_outcome_draws_reproduce_stored_marginal_quantiles() -> None:
    config = load_simulation_config()
    distribution = PlayerOutcomeDistribution(
        p_active=0.82,
        p_full_role_given_active=0.9,
        conditional_location=0,
        conditional_scale=14,
        conditional_shape=0.45,
    )
    player = _candidate(1, "RB", team="AAA", game="AAA-BBB")
    outcomes = draw_player_outcomes(
        (PlayerSimulationInput(player, distribution),),
        draws=100_000,
        rng=np.random.default_rng(77),
        dependence=config.dependence,
        independent=False,
    )[:, 0]

    for quantile in (0.25, 0.5, 0.9):
        empirical = float(np.quantile(outcomes, quantile, method="inverted_cdf"))
        expected = distribution.quantile(quantile)
        assert empirical == pytest.approx(expected, rel=0.025, abs=0.1)


def test_copula_makes_same_position_teammates_negative_and_independent_turns_it_off() -> None:
    config = load_simulation_config()
    distribution = PlayerOutcomeDistribution(
        p_active=1,
        p_full_role_given_active=1,
        conditional_location=0,
        conditional_scale=12,
        conditional_shape=0.4,
    )
    players = (
        PlayerSimulationInput(_candidate(1, "RB", team="AAA", game="AAA-BBB"), distribution),
        PlayerSimulationInput(_candidate(2, "RB", team="AAA", game="AAA-BBB"), distribution),
    )
    dependent = draw_player_outcomes(
        players,
        draws=50_000,
        rng=np.random.default_rng(8),
        dependence=config.dependence,
    )
    independent = draw_player_outcomes(
        players,
        draws=50_000,
        rng=np.random.default_rng(8),
        dependence=config.dependence,
        independent=True,
    )

    assert float(np.corrcoef(dependent.T)[0, 1]) < -0.25
    assert abs(float(np.corrcoef(independent.T)[0, 1])) < 0.02


def test_native_field_hits_calibrated_targets_and_stack_knob() -> None:
    request = _request()
    config = load_simulation_config()
    ownership = {
        player.player_id: 0.3 if player.position == "WR" else 0.2
        for player in request.candidate_player_scenario.players
    }

    result = generate_field(
        request,
        ownership,
        lineup_count=1_000,
        rng=np.random.default_rng(1),
        config=config.field,
    )

    assert result.maximum_marginal_error <= config.field.ownership_tolerance
    assert sum(result.calibrated_targets.values()) == pytest.approx(9)
    assert result.stack_rate == pytest.approx(config.field.stack_rate, abs=0.05)


def test_native_field_fails_loudly_when_no_salary_legal_lineup_exists() -> None:
    request = _request().model_copy(update={"salary_cap": 1})
    config = load_simulation_config()
    fast_failure = config.field.model_copy(
        update={"calibration_iterations": 1, "lineup_attempts": 3}
    )
    ownership = {player.player_id: 0.25 for player in request.candidate_player_scenario.players}

    with pytest.raises(FieldGenerationError, match="could not generate a legal field lineup"):
        generate_field(
            request,
            ownership,
            lineup_count=10,
            rng=np.random.default_rng(2),
            config=fast_failure,
        )


def test_tied_payouts_split_the_prizes_across_occupied_ranks() -> None:
    payouts = (
        SimpleNamespace(rank_from=1, rank_to=1, prize_cents=10_000),
        SimpleNamespace(rank_from=2, rank_to=2, prize_cents=0),
    )
    prizes, ranks = split_tied_payouts((100.0, 100.0), payouts)  # type: ignore[arg-type]

    assert prizes == (5_000.0, 5_000.0)
    assert ranks == (1, 1)


def test_one_exact_field_duplicate_halves_our_first_place_payout() -> None:
    request = _request()
    lineup = _lineup(request)
    player_ids = tuple(player.player_id for player in lineup.players)
    outcomes = np.full((20, len(player_ids)), 10.0)
    payouts = (
        SimpleNamespace(rank_from=1, rank_to=1, prize_cents=10_000),
        SimpleNamespace(rank_from=2, rank_to=2, prize_cents=0),
    )

    result = evaluate_contest(
        outcomes,
        player_ids=player_ids,
        portfolio_lineups=(lineup,),
        field_lineups=(player_ids,),
        payout_bands=payouts,  # type: ignore[arg-type]
        entry_fee_cents=1_000,
    )

    assert result.lineup_results[0].expected_payout_cents == 5_000
    assert result.lineup_results[0].duplication_distribution == ((1, 1.0),)


def test_same_seed_produces_identical_field_and_outcome_bytes() -> None:
    request = _request()
    config = load_simulation_config()
    ownership = {player.player_id: 0.25 for player in request.candidate_player_scenario.players}
    distribution = PlayerOutcomeDistribution(
        p_active=1,
        p_full_role_given_active=1,
        conditional_location=0,
        conditional_scale=10,
        conditional_shape=0.3,
    )
    simulation_players = tuple(
        PlayerSimulationInput(player, distribution)
        for player in request.candidate_player_scenario.players
    )

    first_field = generate_field(
        request,
        ownership,
        lineup_count=500,
        rng=np.random.default_rng(91),
        config=config.field,
    )
    second_field = generate_field(
        request,
        ownership,
        lineup_count=500,
        rng=np.random.default_rng(91),
        config=config.field,
    )
    first_outcomes = draw_player_outcomes(
        simulation_players,
        draws=100,
        rng=np.random.default_rng(92),
        dependence=config.dependence,
    )
    second_outcomes = draw_player_outcomes(
        simulation_players,
        draws=100,
        rng=np.random.default_rng(92),
        dependence=config.dependence,
    )

    assert repr(first_field).encode() == repr(second_field).encode()
    assert first_outcomes.tobytes() == second_outcomes.tobytes()


def _candidate(
    player_id: int,
    position: str,
    *,
    team: str,
    game: str,
) -> CandidatePlayer:
    slots = (position,) if position in {"QB", "DST"} else (position, "FLEX")
    return CandidatePlayer(
        player_id=player_id,
        site_player_id=str(player_id),
        name=f"{position} {player_id}",
        team=team,
        opponent=f"O{team}",
        position=position,
        eligible_roster_slots=slots,
        salary=4_000,
        projection=10,
        projected_ownership=0.2,
        game_id=game,
    )


def _request() -> OptimizationRequest:
    players: list[CandidatePlayer] = []
    player_id = 0
    for position, count in {"QB": 4, "RB": 8, "WR": 10, "TE": 5, "DST": 4}.items():
        for index in range(count):
            player_id += 1
            players.append(
                _candidate(
                    player_id,
                    position,
                    team=f"T{index % 4}",
                    game=f"G{index % 2}",
                )
            )
    scenario = CandidatePlayerScenario(
        scenario_id="simulation-fixture",
        players=tuple(players),
        projection_source_versions=("fixture-v1",),
    )
    return OptimizationRequest(
        site=DfsSite.DRAFTKINGS,
        slate_id=1,
        slate_type=SlateType.CLASSIC,
        contest_archetype=ContestArchetype.CASH,
        salary_cap=50_000,
        candidate_player_scenario=scenario,
        min_games=2,
    )


def _lineup(request: OptimizationRequest) -> Lineup:
    slots = ("QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST")
    wanted = (1, 5, 6, 13, 14, 15, 23, 7, 28)
    candidates = {player.player_id: player for player in request.candidate_player_scenario.players}
    players = tuple(
        LineupPlayer(
            slot=slot,
            player_id=candidates[player_id].player_id,
            site_player_id=candidates[player_id].site_player_id,
            name=candidates[player_id].name,
            team=candidates[player_id].team,
            opponent=candidates[player_id].opponent,
            position=candidates[player_id].position,
            salary=candidates[player_id].salary,
            projection=candidates[player_id].projection,
            projected_ownership=candidates[player_id].projected_ownership,
            game_id=candidates[player_id].game_id,
        )
        for slot, player_id in zip(slots, wanted, strict=True)
    )
    return Lineup(
        lineup_id=lineup_sha256(request.site, request.slate_id, players),
        site=request.site,
        slate_id=request.slate_id,
        players=players,
        total_salary=sum(player.salary for player in players),
        total_projection=sum(player.projection for player in players),
    )
