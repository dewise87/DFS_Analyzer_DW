"""Experimental simulation: marginals, dependence, field calibration, and payouts."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import narrative_alpha.simulation.cli as simulation_cli
import narrative_alpha.simulation.field as simulation_field
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
    SimulationRunError,
    draw_player_outcomes,
    evaluate_contest,
    generate_field,
    implied_pairwise_correlations,
    load_ownership_for_decision,
    load_simulation_config,
    split_tied_payouts,
)
from narrative_alpha.simulation.calibration import (
    _duplication_distribution,
    _resolve_standings_lineups,
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


def test_dependence_assumptions_pin_football_pairwise_correlations() -> None:
    config = load_simulation_config()
    implied = implied_pairwise_correlations(config.dependence)

    assert 0.42 <= implied["qb_wr_same_team"] <= 0.52
    assert 0.35 <= implied["wr_wr_same_team"] <= 0.46
    assert 0.10 <= implied["qb_qb_opposing"] <= 0.15
    assert implied["cross_game"] == 0
    assert set(implied_pairwise_correlations(config.dependence, independent=True).values()) == {0}


def test_copula_keeps_touch_competition_small_and_independent_turns_it_off() -> None:
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

    assert 0.05 < float(np.corrcoef(dependent.T)[0, 1]) < 0.18
    assert abs(float(np.corrcoef(independent.T)[0, 1])) < 0.02


def test_native_field_hits_calibrated_targets_and_stack_knob() -> None:
    request = _request()
    config = load_simulation_config()
    field_config = config.field.model_copy(
        update={"salary_use": 0.72, "salary_use_tolerance": 0.001}
    )
    ownership = {
        player.player_id: 0.3 if player.position == "WR" else 0.2
        for player in request.candidate_player_scenario.players
    }

    result = generate_field(
        request,
        ownership,
        lineup_count=1_000,
        rng=np.random.default_rng(1),
        config=field_config,
    )

    assert result.maximum_marginal_error <= field_config.ownership_tolerance
    assert sum(result.calibrated_targets.values()) == pytest.approx(9)
    assert result.stack_rate == pytest.approx(field_config.stack_rate, abs=0.05)
    assert result.salary_use == pytest.approx(field_config.salary_use, abs=0.001)


def test_stack_rate_knob_moves_the_achieved_field_rate() -> None:
    request = _request()
    config = load_simulation_config()
    ownership = {player.player_id: 0.25 for player in request.candidate_player_scenario.players}
    base = {
        "salary_use": 0.72,
        "salary_use_tolerance": 0.001,
        "ownership_tolerance": 0.04,
    }

    low = generate_field(
        request,
        ownership,
        lineup_count=500,
        rng=np.random.default_rng(101),
        config=config.field.model_copy(update=base | {"stack_rate": 0.1}),
    )
    high = generate_field(
        request,
        ownership,
        lineup_count=500,
        rng=np.random.default_rng(102),
        config=config.field.model_copy(update=base | {"stack_rate": 0.9}),
    )

    assert low.stack_rate == pytest.approx(0.1, abs=0.05)
    assert high.stack_rate == pytest.approx(0.9, abs=0.05)
    assert high.stack_rate - low.stack_rate > 0.7


def test_a_carried_calibration_reaches_tolerance_in_fewer_populations() -> None:
    """Replicates after the first start from the first's correction vector.

    Every replicate calibrates the same pool to the same targets, so rediscovering that
    vector eight times is the whole reason a large contest missed its Sunday budget. The
    carried field must still be a calibrated field, not merely a faster one.
    """

    request = _large_request(salary_shift=200)
    config = load_simulation_config()
    desired_salary = {"QB": 6_500, "RB": 5_500, "WR": 5_200, "TE": 4_500, "DST": 5_200}
    ownership = {
        player.player_id: 0.01
        + 0.35 * math.exp(-abs(player.salary - desired_salary[player.position]) / 800)
        for player in request.candidate_player_scenario.players
    }
    populations: list[int] = []
    original = simulation_field._generate_population

    def counted(*arguments: object, **keywords: object) -> object:
        populations.append(1)
        return original(*arguments, **keywords)  # type: ignore[arg-type]

    def build(seed: int, biases: Mapping[int, float] | None) -> tuple[object, int]:
        populations.clear()
        result = generate_field(
            request,
            ownership,
            lineup_count=800,
            rng=np.random.default_rng(seed),
            config=config.field,
            biases=biases,
        )
        return result, len(populations)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(simulation_field, "_generate_population", counted)
        first, cold_populations = build(4501, None)
        cold_second, uncarried_populations = build(4502, None)
        carried_second, carried_populations = build(4502, first.calibration_biases)

    assert cold_populations > 1
    assert carried_populations < uncarried_populations
    assert carried_second.maximum_marginal_error <= config.field.ownership_tolerance
    assert carried_second.salary_use == pytest.approx(
        config.field.salary_use, abs=config.field.salary_use_tolerance
    )
    # Same seed, different starting vector: the carry is a speed-up, not a copy.
    assert carried_second.lineups != cold_second.lineups


def test_a_carried_calibration_that_does_not_cover_the_pool_is_refused() -> None:
    request = _request()
    config = load_simulation_config()
    ownership = {player.player_id: 0.25 for player in request.candidate_player_scenario.players}
    partial = {player.player_id: 0.0 for player in request.candidate_player_scenario.players[:-1]}

    with pytest.raises(FieldGenerationError, match="does not cover this pool"):
        generate_field(
            request,
            ownership,
            lineup_count=10,
            rng=np.random.default_rng(3),
            config=config.field.model_copy(
                update={"salary_use": 0.72, "salary_use_tolerance": 0.001}
            ),
            biases=partial,
        )


def test_native_field_fails_loudly_when_no_salary_legal_lineup_exists() -> None:
    request = _request().model_copy(update={"salary_cap": 1})
    config = load_simulation_config()
    fast_failure = config.field.model_copy(
        update={"calibration_iterations": 1, "lineup_attempts": 3}
    )
    ownership = {player.player_id: 0.25 for player in request.candidate_player_scenario.players}

    with pytest.raises(FieldGenerationError, match=r"salary calibration.*achieved="):
        generate_field(
            request,
            ownership,
            lineup_count=10,
            rng=np.random.default_rng(2),
            config=fast_failure,
        )


def test_native_field_reports_achieved_values_when_ownership_cannot_calibrate() -> None:
    request = _request()
    config = load_simulation_config()
    impossible_precision = config.field.model_copy(
        update={
            "salary_use": 0.72,
            "salary_use_tolerance": 0.001,
            "ownership_tolerance": 0.001,
            "calibration_iterations": 2,
        }
    )
    ownership = {player.player_id: 0.25 for player in request.candidate_player_scenario.players}

    with pytest.raises(FieldGenerationError) as raised:
        generate_field(
            request,
            ownership,
            lineup_count=1,
            rng=np.random.default_rng(2026),
            config=impossible_precision,
        )

    assert "field ownership calibration failed" in str(raised.value)
    assert "target=" in str(raised.value)
    assert "achieved=" in str(raised.value)
    assert "salary_use target=" in str(raised.value)


def test_tied_payouts_split_the_prizes_across_occupied_ranks() -> None:
    payouts = (
        SimpleNamespace(rank_from=1, rank_to=1, prize_cents=10_000),
        SimpleNamespace(rank_from=2, rank_to=2, prize_cents=0),
    )
    prizes, ranks = split_tied_payouts((100.0, 100.0), payouts)  # type: ignore[arg-type]

    assert prizes == (5_000.0, 5_000.0)
    assert ranks == (1, 1)


def test_tie_splitting_includes_second_prize_and_a_multi_rank_band() -> None:
    payouts = (
        SimpleNamespace(rank_from=1, rank_to=1, prize_cents=10_000),
        SimpleNamespace(rank_from=2, rank_to=2, prize_cents=6_000),
        SimpleNamespace(rank_from=3, rank_to=5, prize_cents=3_000),
    )

    prizes, ranks = split_tied_payouts((100, 100, 90, 90, 90), payouts)  # type: ignore[arg-type]

    assert prizes == (8_000, 8_000, 3_000, 3_000, 3_000)
    assert ranks == (1, 1, 3, 3, 3)


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


def test_metrics_and_duplication_cover_every_field_replicate_and_draw() -> None:
    request = _request()
    lineup = _lineup(request)
    player_ids = tuple(player.player_id for player in request.candidate_player_scenario.players)
    outcomes = np.arange(3 * len(player_ids), dtype=np.float64).reshape(3, -1)
    portfolio_key = tuple(player.player_id for player in lineup.players)
    different_key = tuple(player_id for player_id in player_ids if player_id not in portfolio_key)[
        :9
    ]
    payouts = (
        SimpleNamespace(rank_from=1, rank_to=1, prize_cents=10_000),
        SimpleNamespace(rank_from=2, rank_to=2, prize_cents=2_000),
    )

    result = evaluate_contest(
        outcomes,
        player_ids=player_ids,
        portfolio_lineups=(lineup,),
        field_lineups=(portfolio_key,),
        field_replicates=((portfolio_key,), (different_key,)),
        payout_bands=payouts,  # type: ignore[arg-type]
        entry_fee_cents=1_000,
        score_sample_limit=100,
    )

    assert result.lineup_results[0].duplication_distribution == ((0, 0.5), (1, 0.5))
    assert result.portfolio_result.duplication_distribution == ((0, 0.5), (1, 0.5))


def test_field_score_quantiles_use_a_seeded_sample_instead_of_the_field_head() -> None:
    request = _request()
    lineup = _lineup(request)
    player_ids = tuple(range(1, 101))
    outcomes = np.asarray([player_ids], dtype=np.float64)
    field = tuple((player_id,) * 9 for player_id in player_ids)
    payouts = (SimpleNamespace(rank_from=1, rank_to=101, prize_cents=100),)

    result = evaluate_contest(
        outcomes,
        player_ids=player_ids,
        portfolio_lineups=(lineup,),
        field_lineups=field,
        payout_bands=payouts,  # type: ignore[arg-type]
        entry_fee_cents=100,
        score_quantiles=(0.5,),
        score_sample_limit=10,
        score_sample_seed=7,
    )

    # A head-of-list sample would have median 45; the seeded population sample does not.
    assert result.score_quantiles[0][1] > 45


def test_calibration_resolves_lineup_names_to_player_id_sets_before_duplication() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE players(
            player_id INTEGER PRIMARY KEY, canonical_name TEXT NOT NULL, position TEXT
        );
        CREATE TABLE salaries(
            salary_id INTEGER PRIMARY KEY, slate_id INTEGER NOT NULL,
            player_id INTEGER NOT NULL, observed_at TEXT NOT NULL,
            valid_from TEXT NOT NULL, valid_to TEXT
        );
        CREATE TABLE player_aliases(
            alias_id INTEGER PRIMARY KEY, player_id INTEGER NOT NULL,
            source TEXT NOT NULL, alias TEXT NOT NULL, manual_override INTEGER NOT NULL,
            valid_from TEXT NOT NULL, valid_to TEXT
        );
        INSERT INTO players VALUES
            (1, 'Alpha Quarterback', 'QB'),
            (2, 'Beta Runner', 'RB'),
            (3, 'Gamma Receiver', 'WR');
        INSERT INTO salaries VALUES
            (1, 7, 1, '2026-09-01T00:00:00.000000Z', '2026-09-01T00:00:00.000000Z', NULL),
            (2, 7, 2, '2026-09-01T00:00:00.000000Z', '2026-09-01T00:00:00.000000Z', NULL),
            (3, 7, 3, '2026-09-01T00:00:00.000000Z', '2026-09-01T00:00:00.000000Z', NULL);
        INSERT INTO player_aliases VALUES
            (1, 2, 'draftkings-contest-standings', 'Runner Alias', 1,
             '2026-09-05T00:00:00.000000Z', NULL);
        """
    )
    resolved = _resolve_standings_lineups(
        connection,
        (
            "QB Alpha Quarterback RB Runner Alias WR Gamma Receiver",
            "WR Gamma Receiver QB Alpha Quarterback RB Beta Runner",
        ),
        slate_id=7,
        source="draftkings-contest-standings",
        observed_at=datetime(2026, 9, 4, tzinfo=UTC),
    )

    assert resolved == ((1, 2, 3), (1, 2, 3))
    assert _duplication_distribution(resolved) == ((1, 1.0),)


def test_ownership_loader_uses_both_recorded_sources_without_recomputing_baselines() -> None:
    request = _request()
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE decision_ownership_routing(
            decision_snapshot_id TEXT PRIMARY KEY, applied INTEGER NOT NULL,
            scenario_run_id TEXT
        );
        CREATE TABLE ownership_scenarios(
            run_id TEXT NOT NULL, player_id INTEGER NOT NULL, role TEXT NOT NULL,
            applied_ownership REAL NOT NULL, observed_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO decision_ownership_routing VALUES ('baseline', 0, NULL);
        INSERT INTO decision_ownership_routing VALUES ('routed', 1, 'run-1');
        """
    )
    stamp = "2026-09-04T00:00:00.000000Z"
    connection.executemany(
        "INSERT INTO ownership_scenarios VALUES ('run-1', ?, 'classic', ?, ?, ?)",
        (
            (player.player_id, 0.123, stamp, stamp)
            for player in request.candidate_player_scenario.players
        ),
    )
    as_of = datetime(2026, 9, 4, tzinfo=UTC)

    source, run_id, baseline = load_ownership_for_decision(
        connection,
        decision_snapshot_id="baseline",
        request=request,
        as_of=as_of,
    )
    routed_source, routed_run_id, routed = load_ownership_for_decision(
        connection,
        decision_snapshot_id="routed",
        request=request,
        as_of=as_of,
    )

    assert (source, run_id) == ("vendor_baseline", None)
    assert set(baseline.values()) == {0.2}
    assert (routed_source, routed_run_id) == ("scenario_model", "run-1")
    assert set(routed.values()) == {0.123}


def test_unrouted_ownership_refuses_a_null_in_the_frozen_scenario() -> None:
    request = _request()
    players = list(request.candidate_player_scenario.players)
    players[0] = players[0].model_copy(update={"projected_ownership": None})
    request = request.model_copy(
        update={
            "candidate_player_scenario": request.candidate_player_scenario.model_copy(
                update={"players": tuple(players)}
            )
        }
    )
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE decision_ownership_routing("
        "decision_snapshot_id TEXT PRIMARY KEY, applied INTEGER, scenario_run_id TEXT)"
    )
    connection.execute("INSERT INTO decision_ownership_routing VALUES ('baseline', 0, NULL)")

    with pytest.raises(SimulationRunError, match=r"frozen candidate scenario.*null"):
        load_ownership_for_decision(
            connection,
            decision_snapshot_id="baseline",
            request=request,
            as_of=datetime(2026, 9, 4, tzinfo=UTC),
        )


def test_calibration_cli_routes_the_requested_week_and_prints_each_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "store.sqlite3"
    snapshot_root = tmp_path / "snapshots"
    connection = object()
    seen: dict[str, object] = {}

    monkeypatch.setattr(simulation_cli, "connect_database", lambda path: nullcontext(connection))
    monkeypatch.setattr(simulation_cli, "apply_migrations", lambda received: None)

    def calibration(received: object, **kwargs: object) -> tuple[SimpleNamespace, ...]:
        seen["connection"] = received
        seen.update(kwargs)
        return (SimpleNamespace(comparison_path=tmp_path / "comparison.txt"),)

    monkeypatch.setattr(simulation_cli, "calibrate_week", calibration)

    exit_code = simulation_cli.main(
        [
            "calibrate",
            "--database",
            str(database),
            "--season",
            "2026",
            "--week",
            "4",
            "--snapshot-root",
            str(snapshot_root),
        ]
    )

    assert exit_code == 0
    assert seen == {
        "connection": connection,
        "season": 2026,
        "week": 4,
        "snapshot_root": snapshot_root,
    }
    assert capsys.readouterr().out == f"{tmp_path / 'comparison.txt'}\n"


def test_same_seed_produces_identical_field_and_outcome_bytes() -> None:
    request = _request()
    config = load_simulation_config()
    field_config = config.field.model_copy(
        update={"salary_use": 0.72, "salary_use_tolerance": 0.001}
    )
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
        config=field_config,
    )
    second_field = generate_field(
        request,
        ownership,
        lineup_count=500,
        rng=np.random.default_rng(91),
        config=field_config,
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


@pytest.mark.parametrize("salary_shift", (200, 500))
def test_salary_aware_field_converges_on_a_220_player_draftkings_pool(
    salary_shift: int,
) -> None:
    request = _large_request(salary_shift=salary_shift)
    config = load_simulation_config()
    desired_salary = {"QB": 6_500, "RB": 5_500, "WR": 5_200, "TE": 4_500, "DST": 5_200}
    ownership = {
        player.player_id: 0.01
        + 0.35 * math.exp(-abs(player.salary - desired_salary[player.position]) / 800)
        for player in request.candidate_player_scenario.players
    }

    result = generate_field(
        request,
        ownership,
        lineup_count=800,
        rng=np.random.default_rng(4300 + salary_shift),
        config=config.field,
    )

    assert len(request.candidate_player_scenario.players) == 220
    assert (
        np.mean([player.salary for player in request.candidate_player_scenario.players])
        >= request.salary_cap / 9
    )
    assert result.maximum_marginal_error <= config.field.ownership_tolerance
    assert result.salary_use == pytest.approx(
        config.field.salary_use, abs=config.field.salary_use_tolerance
    )


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


def _large_request(*, salary_shift: int) -> OptimizationRequest:
    counts = {"QB": 30, "RB": 50, "WR": 80, "TE": 35, "DST": 25}
    bases = {"QB": 6_500, "RB": 5_900, "WR": 5_700, "TE": 5_300, "DST": 4_800}
    players: list[CandidatePlayer] = []
    player_id = 0
    for position, count in counts.items():
        for index in range(count):
            player_id += 1
            player = _candidate(
                player_id,
                position,
                team=f"T{index % 16}",
                game=f"G{index % 8}",
            )
            players.append(
                player.model_copy(
                    update={"salary": bases[position] + salary_shift + ((index % 21) - 10) * 250}
                )
            )
    scenario = CandidatePlayerScenario(
        scenario_id=f"large-simulation-fixture-{salary_shift}",
        players=tuple(players),
        projection_source_versions=("fixture-v1",),
    )
    return OptimizationRequest(
        site=DfsSite.DRAFTKINGS,
        slate_id=1,
        slate_type=SlateType.CLASSIC,
        contest_archetype=ContestArchetype.MASS_MULTI_ENTRY,
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
