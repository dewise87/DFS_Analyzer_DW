"""Contest ranking, tie splitting, duplication, and portfolio metrics."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy.sparse import csr_matrix  # type: ignore[import-untyped]

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
    values = np.asarray(scores, dtype=np.float64)
    prize_by_rank = _prize_by_rank(len(scores), payout_bands)
    order = np.argsort(-values, kind="stable")
    sorted_scores = values[order]
    _, first, inverse, counts = np.unique(
        -sorted_scores, return_index=True, return_inverse=True, return_counts=True
    )
    prize_totals = np.add.reduceat(prize_by_rank, first)
    group_prizes = prize_totals / counts
    sorted_payouts = group_prizes[inverse]
    sorted_ranks = first[inverse] + 1
    payouts = np.empty_like(sorted_payouts)
    ranks = np.empty_like(sorted_ranks)
    payouts[order] = sorted_payouts
    ranks[order] = sorted_ranks
    return tuple(float(value) for value in payouts), tuple(int(value) for value in ranks)


def evaluate_contest(
    outcomes: NDArray[np.float64],
    *,
    player_ids: Sequence[int],
    portfolio_lineups: Sequence[Lineup],
    field_lineups: Sequence[Sequence[int]],
    field_replicates: Sequence[Sequence[Sequence[int]]] | None = None,
    payout_bands: Sequence[ContestPayoutRow],
    entry_fee_cents: int,
    score_quantiles: Sequence[float] = (0.05, 0.25, 0.5, 0.75, 0.95),
    score_sample_limit: int = 100000,
    score_sample_seed: int = 0,
    draw_block_size: int = 8,
) -> ContestEvaluation:
    """Score field realizations in bounded-memory draw blocks.

    Every payout metric is computed over ``field replicate x outcome draw``. The legacy
    ``field_lineups`` argument remains the single-replicate shorthand used by callers that
    do not need field uncertainty.
    """

    if outcomes.ndim != 2 or outcomes.shape[0] < 1:
        raise ContestSimulationError("outcomes must be a non-empty draws-by-players matrix")
    if not np.isfinite(outcomes).all():
        raise ContestSimulationError("contest outcomes must be finite")
    if outcomes.shape[1] != len(player_ids) or len(set(player_ids)) != len(player_ids):
        raise ContestSimulationError("outcome columns must match unique player_ids")
    if not portfolio_lineups:
        raise ContestSimulationError("the decision portfolio contains no lineups")
    if entry_fee_cents < 0:
        raise ContestSimulationError("entry_fee_cents cannot be negative")
    if not payout_bands:
        raise ContestSimulationError("contest has no payout bands")
    if score_sample_limit < 1:
        raise ContestSimulationError("score_sample_limit must be positive")
    if draw_block_size < 1:
        raise ContestSimulationError("draw_block_size must be positive")

    index = {player_id: column for column, player_id in enumerate(player_ids)}
    portfolio_keys = tuple(
        tuple(sorted(player.player_id for player in lineup.players)) for lineup in portfolio_lineups
    )
    raw_replicates = (field_lineups,) if field_replicates is None else field_replicates
    replicate_keys = tuple(
        tuple(tuple(sorted(int(player_id) for player_id in lineup)) for lineup in replicate)
        for replicate in raw_replicates
    )
    if not replicate_keys or not replicate_keys[0]:
        raise ContestSimulationError("contest scoring requires at least one field lineup")
    field_count = len(replicate_keys[0])
    if any(len(replicate) != field_count for replicate in replicate_keys):
        raise ContestSimulationError("all field replicates must have the same entry count")
    roster_size = len(portfolio_keys[0])
    if any(
        len(lineup) != roster_size
        for replicate in replicate_keys
        for lineup in (*portfolio_keys, *replicate)
    ):
        raise ContestSimulationError("all contest lineups must have the same roster size")

    draws = outcomes.shape[0]
    portfolio_count = len(portfolio_lineups)
    replicate_count = len(replicate_keys)
    sample_count = replicate_count * draws
    payout_samples = np.zeros((sample_count, portfolio_count), dtype=np.float64)
    score_samples = np.zeros((sample_count, portfolio_count), dtype=np.float64)
    ranks = np.zeros((sample_count, portfolio_count), dtype=np.int64)
    prize_by_rank = _prize_by_rank(portfolio_count + field_count, payout_bands)
    sampled_indices = _score_sample_indices(
        replicate_count * draws * field_count,
        score_sample_limit,
        seed=score_sample_seed,
    )
    sampled_field_scores: list[float] = []
    duplicate_samples: list[tuple[int, ...]] = []

    for replicate_index, field_keys in enumerate(replicate_keys):
        all_keys = portfolio_keys + field_keys
        lineup_matrix = _lineup_matrix(all_keys, index=index, player_count=len(player_ids))
        field_counts = Counter(field_keys)
        duplicate_samples.append(tuple(field_counts[key] for key in portfolio_keys))
        for block_start in range(0, draws, draw_block_size):
            block_end = min(draws, block_start + draw_block_size)
            scores = np.asarray(
                outcomes[block_start:block_end] @ lineup_matrix.T,
                dtype=np.float64,
            )
            orders = np.argsort(-scores, axis=1, kind="stable")
            for offset, order in enumerate(orders):
                draw = block_start + offset
                sample_row = replicate_index * draws + draw
                sorted_scores = scores[offset, order]
                _, first, inverse, counts = np.unique(
                    -sorted_scores,
                    return_index=True,
                    return_inverse=True,
                    return_counts=True,
                )
                group_prizes = np.add.reduceat(prize_by_rank, first) / counts
                positions = np.empty_like(order)
                positions[order] = np.arange(len(order))
                portfolio_positions = positions[:portfolio_count]
                groups = inverse[portfolio_positions]
                payout_samples[sample_row] = group_prizes[groups]
                ranks[sample_row] = first[groups] + 1
                score_samples[sample_row] = scores[offset, :portfolio_count]
            _collect_score_sample(
                sampled_field_scores,
                sampled_indices,
                scores[:, portfolio_count:],
                global_start=(replicate_index * draws + block_start) * field_count,
            )

    duplicate_columns = tuple(
        tuple(sample[column] for sample in duplicate_samples) for column in range(portfolio_count)
    )
    total_entries = portfolio_count + field_count
    top_cutoff = max(1, math.ceil(total_entries * 0.01))
    lineup_results = tuple(
        _metric(
            portfolio_lineups[column].lineup_id,
            payout_samples[:, column],
            ranks[:, column] <= top_cutoff,
            duplicate_columns[column],
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
        duplication_distribution=_distribution(tuple(sum(sample) for sample in duplicate_samples)),
        downside_p5_payout_cents=_lower_quantile(portfolio_payouts, 0.05),
        mean_pairwise_outcome_correlation=mean_correlation,
        outcome_correlation_matrix=correlation_matrix,
    )
    score_values = np.asarray(sampled_field_scores, dtype=np.float64)
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
    duplicate_counts: Sequence[int],
    entry_fee_cents: int,
) -> LineupSimulationResult:
    expected = float(payouts.mean())
    return LineupSimulationResult(
        lineup_id=lineup_id,
        expected_payout_cents=expected,
        expected_roi=_roi(expected, entry_fee_cents),
        cash_probability=float(np.mean(payouts > 0)),
        top_one_percent_probability=float(np.mean(top)),
        duplication_distribution=_distribution(duplicate_counts),
        downside_p5_payout_cents=_lower_quantile(payouts, 0.05),
    )


def _prize_by_rank(
    entry_count: int, payout_bands: Sequence[ContestPayoutRow]
) -> NDArray[np.float64]:
    prizes = np.zeros(entry_count, dtype=np.float64)
    assigned = np.zeros(entry_count, dtype=np.bool_)
    for payout in payout_bands:
        start = max(0, int(payout.rank_from) - 1)
        end = min(entry_count, int(payout.rank_to))
        if start >= end:
            continue
        available = ~assigned[start:end]
        prizes[start:end][available] = float(payout.prize_cents)
        assigned[start:end] = True
    return prizes


def _lineup_matrix(
    lineups: Sequence[Sequence[int]], *, index: Mapping[int, int], player_count: int
) -> csr_matrix:
    try:
        columns = np.fromiter(
            (index[player_id] for lineup in lineups for player_id in lineup),
            dtype=np.int64,
            count=sum(len(lineup) for lineup in lineups),
        )
    except KeyError as error:
        raise ContestSimulationError(
            f"lineup references player {int(error.args[0])} without an outcome distribution"
        ) from error
    rows = np.repeat(np.arange(len(lineups), dtype=np.int64), [len(lineup) for lineup in lineups])
    data = np.ones(len(columns), dtype=np.float64)
    return csr_matrix((data, (rows, columns)), shape=(len(lineups), player_count))


def _score_sample_indices(total: int, limit: int, *, seed: int) -> NDArray[np.int64]:
    if total <= limit:
        return np.arange(total, dtype=np.int64)
    rng = np.random.default_rng(seed)
    return np.sort(rng.choice(total, size=limit, replace=False).astype(np.int64))


def _collect_score_sample(
    into: list[float],
    sampled_indices: NDArray[np.int64],
    field_scores: NDArray[np.float64],
    *,
    global_start: int,
) -> None:
    end = global_start + field_scores.size
    left = int(np.searchsorted(sampled_indices, global_start, side="left"))
    right = int(np.searchsorted(sampled_indices, end, side="left"))
    if left == right:
        return
    local = sampled_indices[left:right] - global_start
    flat = field_scores.reshape(-1)
    into.extend(float(value) for value in flat[local])


def _distribution(values: Sequence[int]) -> tuple[tuple[int, float], ...]:
    if not values:
        raise ContestSimulationError("duplication distribution requires at least one replicate")
    counts = Counter(int(value) for value in values)
    return tuple((value, count / len(values)) for value, count in sorted(counts.items()))


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
