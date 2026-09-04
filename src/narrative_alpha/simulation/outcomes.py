"""Marginal-preserving player draws with explicit football dependence assumptions."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray
from scipy.special import ndtr  # type: ignore[import-untyped]

from narrative_alpha.simulation.config import DependenceConfig
from narrative_alpha.simulation.models import PlayerSimulationInput


class OutcomeSimulationError(ValueError):
    """Raised when player marginals cannot support a simulation run."""


def draw_player_outcomes(
    players: Sequence[PlayerSimulationInput],
    *,
    draws: int,
    rng: np.random.Generator,
    dependence: DependenceConfig,
    independent: bool = False,
) -> NDArray[np.float64]:
    """Draw ``draws x players`` outcomes while preserving each stored marginal.

    The dependent path combines a game environment factor, a team offense/pace factor,
    and a centered within-team/position contrast. Centering makes players competing for
    the same positional touches move in opposite directions. The remaining variance is
    idiosyncratic, so every latent coordinate is still standard normal before the
    Gaussian CDF maps it through the stored generalized-inverse quantile.
    """

    if draws < 1:
        raise OutcomeSimulationError("draws must be positive")
    if not players:
        raise OutcomeSimulationError("at least one player distribution is required")

    count = len(players)
    if independent:
        uniforms = rng.random((draws, count))
    else:
        latent = _dependent_latent_normals(players, draws=draws, rng=rng, config=dependence)
        uniforms = ndtr(latent)

    outcomes = np.empty((draws, count), dtype=np.float64)
    for index, item in enumerate(players):
        outcomes[:, index] = np.fromiter(
            (item.distribution.quantile(float(value)) for value in uniforms[:, index]),
            dtype=np.float64,
            count=draws,
        )
    if not np.isfinite(outcomes).all():
        raise OutcomeSimulationError("a player outcome exceeded floating-point range")
    return outcomes


def _dependent_latent_normals(
    players: Sequence[PlayerSimulationInput],
    *,
    draws: int,
    rng: np.random.Generator,
    config: DependenceConfig,
) -> NDArray[np.float64]:
    count = len(players)
    game_ids = tuple(sorted({item.player.game_id for item in players}))
    teams = tuple(sorted({item.player.team for item in players}))
    game_index = {value: index for index, value in enumerate(game_ids)}
    team_index = {value: index for index, value in enumerate(teams)}
    game_factors = rng.standard_normal((draws, len(game_ids)))
    team_factors = rng.standard_normal((draws, len(teams)))
    latent = np.zeros((draws, count), dtype=np.float64)

    game_loading = config.game_loading
    team_loading = config.team_loading
    negative_loading = config.within_position_negative_loading
    base_variance = game_loading**2 + team_loading**2

    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    touch_positions = frozenset(config.touch_positions)
    for index, item in enumerate(players):
        player = item.player
        latent[:, index] = (
            game_loading * game_factors[:, game_index[player.game_id]]
            + team_loading * team_factors[:, team_index[player.team]]
        )
        if player.position.upper() in touch_positions:
            groups[(player.team, player.position.upper())].append(index)

    grouped_indices: set[int] = set()
    for indices in groups.values():
        if len(indices) < 2:
            continue
        grouped_indices.update(indices)
        raw = rng.standard_normal((draws, len(indices)))
        # Var(E_i - mean(E)) = 1 - 1/n; scaling restores unit variance. Its
        # pairwise correlation is -1/(n-1), the explicit shared-touch relation.
        contrasts = (raw - raw.mean(axis=1, keepdims=True)) / math.sqrt(1.0 - 1.0 / len(indices))
        residual_loading = math.sqrt(1.0 - base_variance - negative_loading**2)
        residual = rng.standard_normal((draws, len(indices)))
        latent[:, indices] += negative_loading * contrasts + residual_loading * residual

    ordinary_residual_loading = math.sqrt(1.0 - base_variance)
    for index in range(count):
        if index not in grouped_indices:
            latent[:, index] += ordinary_residual_loading * rng.standard_normal(draws)
    return latent
