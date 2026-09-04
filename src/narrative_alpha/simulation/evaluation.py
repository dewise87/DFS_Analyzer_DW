"""Contest ranking, tie splitting, duplication, and portfolio metrics."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from narrative_alpha.portfolio import Lineup
from narrative_alpha.simulation.models import (
    LineupSimulationResult,
    PortfolioSimulationResult,
)
from narrative_alpha.store import ContestPayoutRow


class ContestSimulationError(ValueError):
    """Raised when a contest cannot be scored from its frozen inputs."""


@dataclass(frozen=True)
class ContestEvaluation:
    lineup_results: tuple[LineupSimulationResult, ...]
    portfolio_result: PortfolioSimulationResult
    score_quantiles: tuple[tuple[float, float], ...]


def split_tied_payouts(
    scores: Sequence[float], payout_bands: Sequence[ContestPayoutRow]
) -> tuple[tuple[float, ...], tuple[int, ...]]:
    """Rank descending and split all occupied-rank prizes equally across a tie."""

    if not scores:
        raise ContestSimulationError("contest scoring requires at least one entry")
    if any(not math.isfinite(float(score)) for score in scores):
        raise ContestSimulationError("contest scores must be finite")
    ordered = sorted(range(len(scores)), key=lambda index: (-float(scores[index]), index))
    payouts = [0.0] * len(scores)
    ranks = [0] * len(scores)
    cursor = 0
    while cursor < len(ordered):
        end = cursor + 1
        score = float(scores[ordered[cursor]])
        while end < len(ordered) and float(scores[ordered[end]]) == score:
            end += 1
        rank_from = cursor + 1
        rank_to = end
        shared = math.fsum(
            _prize_for_rank(rank, payout_bands) for rank in range(rank_from, rank_to + 1)
        ) / (end - cursor)
        for ordered_index in ordered[cursor:end]:
            payouts[ordered_index] = shared
            ranks[ordered_index] = rank_from
        cursor = end
    return tuple(payouts), tuple(ranks)


def evaluate_contest(
    outcomes: NDArray[np.float64],
    *,
    player_ids: Sequence[int],
    portfolio_lineups: Sequence[Lineup],
    field_lineups: Sequence[Sequence[int]],
    payout_bands: Sequence[ContestPayoutRow],
    entry_fee_cents: int,
    score_quantiles: Sequence[float] = (0.05, 0.25, 0.5, 0.75, 0.95),
    score_sample_limit: int = 100000,
) -> ContestEvaluation:
    """Score a fixed contest field under every player-outcome draw."""

    if outcomes.ndim != 2 or outcomes.shape[0] < 1:
        raise ContestSimulationError("outcomes must be a non-empty draws-by-players matrix")
    if outcomes.shape[1] != len(player_ids) or len(set(player_ids)) != len(player_ids):
        raise ContestSimulationError("outcome columns must match unique player_ids")
    if not portfolio_lineups:
        raise ContestSimulationError("the decision portfolio contains no lineups")
    if entry_fee_cents < 0:
        raise ContestSimulationError("entry_fee_cents cannot be negative")
    if not payout_bands:
        raise ContestSimulationError("contest has no payout bands")

    index = {player_id: column for column, player_id in enumerate(player_ids)}
    portfolio_keys = tuple(
        tuple(sorted(player.player_id for player in lineup.players)) for lineup in portfolio_lineups
    )
    field_keys = tuple(
        tuple(sorted(int(player_id) for player_id in lineup)) for lineup in field_lineups
    )
    all_keys = portfolio_keys + field_keys
    try:
        lineup_indices = np.asarray(
            [[index[player_id] for player_id in lineup] for lineup in all_keys],
            dtype=np.int64,
        )
    except KeyError as error:
        raise ContestSimulationError(
            f"lineup references player {int(error.args[0])} without an outcome distribution"
        ) from error
    if len({len(lineup) for lineup in all_keys}) != 1:
        raise ContestSimulationError("all contest lineups must have the same roster size")

    draws = outcomes.shape[0]
    portfolio_count = len(portfolio_lineups)
    payout_samples = np.zeros((draws, portfolio_count), dtype=np.float64)
    score_samples = np.zeros((draws, portfolio_count), dtype=np.float64)
    ranks = np.zeros((draws, portfolio_count), dtype=np.int64)
    sampled_field_scores: list[float] = []
    sample_per_draw = max(1, score_sample_limit // draws)

    for draw in range(draws):
        scores = outcomes[draw, lineup_indices].sum(axis=1)
        prizes, draw_ranks = split_tied_payouts(scores.tolist(), payout_bands)
        payout_samples[draw, :] = prizes[:portfolio_count]
        ranks[draw, :] = draw_ranks[:portfolio_count]
        score_samples[draw, :] = scores[:portfolio_count]
        if field_keys:
            sampled_field_scores.extend(
                float(value)
                for value in scores[portfolio_count : portfolio_count + sample_per_draw]
            )

    field_counts = Counter(field_keys)
    duplicate_counts = tuple(field_counts[key] for key in portfolio_keys)
    total_entries = len(all_keys)
    top_cutoff = max(1, math.ceil(total_entries * 0.01))
    lineup_results = tuple(
        _metric(
            portfolio_lineups[column].lineup_id,
            payout_samples[:, column],
            ranks[:, column] <= top_cutoff,
            duplicate_counts[column],
            entry_fee_cents,
        )
        for column in range(portfolio_count)
    )

    portfolio_payouts = payout_samples.sum(axis=1)
    portfolio_top = (ranks <= top_cutoff).any(axis=1)
    total_fee = entry_fee_cents * portfolio_count
    correlation_matrix, mean_correlation = _correlations(score_samples)
    portfolio_result = PortfolioSimulationResult(
        lineup_count=portfolio_count,
        expected_payout_cents=float(portfolio_payouts.mean()),
        expected_roi=_roi(float(portfolio_payouts.mean()), total_fee),
        cash_probability=float(np.mean(portfolio_payouts > 0)),
        top_one_percent_probability=float(np.mean(portfolio_top)),
        duplication_distribution=((sum(duplicate_counts), 1.0),),
        downside_p5_payout_cents=_lower_quantile(portfolio_payouts, 0.05),
        mean_pairwise_outcome_correlation=mean_correlation,
        outcome_correlation_matrix=correlation_matrix,
    )
    score_values = (
        np.asarray(sampled_field_scores, dtype=np.float64)
        if sampled_field_scores
        else score_samples.reshape(-1)
    )
    quantiles = tuple((float(q), _lower_quantile(score_values, float(q))) for q in score_quantiles)
    return ContestEvaluation(
        lineup_results=lineup_results,
        portfolio_result=portfolio_result,
        score_quantiles=quantiles,
    )


def _metric(
    lineup_id: str,
    payouts: NDArray[np.float64],
    top: NDArray[np.bool_],
    duplicate_count: int,
    entry_fee_cents: int,
) -> LineupSimulationResult:
    expected = float(payouts.mean())
    return LineupSimulationResult(
        lineup_id=lineup_id,
        expected_payout_cents=expected,
        expected_roi=_roi(expected, entry_fee_cents),
        cash_probability=float(np.mean(payouts > 0)),
        top_one_percent_probability=float(np.mean(top)),
        duplication_distribution=((duplicate_count, 1.0),),
        downside_p5_payout_cents=_lower_quantile(payouts, 0.05),
    )


def _prize_for_rank(rank: int, payout_bands: Sequence[ContestPayoutRow]) -> float:
    return float(
        next(
            (
                payout.prize_cents
                for payout in payout_bands
                if payout.rank_from <= rank <= payout.rank_to
            ),
            0,
        )
    )


def _roi(expected_payout: float, entry_fee: int) -> float | None:
    return None if entry_fee == 0 else (expected_payout - entry_fee) / entry_fee


def _lower_quantile(values: NDArray[np.float64], probability: float) -> float:
    if values.size == 0:
        raise ContestSimulationError("cannot take a quantile of an empty sample")
    ordered = np.sort(values)
    index = max(0, math.ceil(probability * len(ordered)) - 1)
    return float(ordered[index])


def _correlations(
    values: NDArray[np.float64],
) -> tuple[tuple[tuple[float, ...], ...], float | None]:
    columns = values.shape[1]
    matrix = np.eye(columns, dtype=np.float64)
    correlations: list[float] = []
    for first in range(columns):
        for second in range(first + 1, columns):
            left = values[:, first]
            right = values[:, second]
            if float(np.std(left)) == 0.0 or float(np.std(right)) == 0.0:
                correlation = 0.0
            else:
                correlation = float(np.corrcoef(left, right)[0, 1])
            matrix[first, second] = matrix[second, first] = correlation
            correlations.append(correlation)
    rows = tuple(tuple(float(value) for value in row) for row in matrix)
    return rows, (None if not correlations else math.fsum(correlations) / len(correlations))
