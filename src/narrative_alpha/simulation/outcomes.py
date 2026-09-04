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


def implied_pairwise_correlations(
    dependence: DependenceConfig, *, independent: bool = False
) -> dict[str, float]:
    """Return the four first-season latent correlations exposed in every report.

    The same-position value uses the two-player contrast, the strongest negative
    within-position case. Larger position groups have a smaller negative covariance.
    """

    if independent:
        return {
            "qb_wr_same_team": 0.0,
            "wr_wr_same_team": 0.0,
            "qb_qb_opposing": 0.0,
            "cross_game": 0.0,
        }
    game = dependence.game_loading
    team = dependence.team_loading_by_position
    passing = dependence.qb_pass_catcher_loading
    negative = dependence.within_position_negative_loading
    return {
        "qb_wr_same_team": game**2 + team["QB"] * team["WR"] + passing**2,
        "wr_wr_same_team": game**2 + team["WR"] ** 2 + passing**2 - negative**2,
        "qb_qb_opposing": game**2,
        "cross_game": 0.0,
    }


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
    passing_factors = rng.standard_normal((draws, len(teams)))
    latent = np.zeros((draws, count), dtype=np.float64)

    game_loading = config.game_loading
    negative_loading = config.within_position_negative_loading
    pass_catcher_positions = frozenset(config.pass_catcher_positions)

    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    touch_positions = frozenset(config.touch_positions)
    for index, item in enumerate(players):
        player = item.player
        position = _position(player.position)
        try:
            team_loading = config.team_loading_by_position[position]
        except KeyError as error:
            raise OutcomeSimulationError(
                f"dependence config has no team loading for position {position}"
            ) from error
        latent[:, index] = (
            game_loading * game_factors[:, game_index[player.game_id]]
            + team_loading * team_factors[:, team_index[player.team]]
        )
        if position == "QB" or position in pass_catcher_positions:
            latent[:, index] += (
                config.qb_pass_catcher_loading * passing_factors[:, team_index[player.team]]
            )
        if position in touch_positions:
            groups[(player.team, position)].append(index)

    grouped_indices: set[int] = set()
    for indices in groups.values():
        if len(indices) < 2:
            continue
        grouped_indices.update(indices)
        raw = rng.standard_normal((draws, len(indices)))
        # Var(E_i - mean(E)) = 1 - 1/n; scaling restores unit variance. Its
        # pairwise correlation is -1/(n-1), the explicit shared-touch relation.
        contrasts = (raw - raw.mean(axis=1, keepdims=True)) / math.sqrt(1.0 - 1.0 / len(indices))
        for contrast_column, index in enumerate(indices):
            residual_loading = math.sqrt(
                1.0 - _shared_variance(players[index], config) - negative_loading**2
            )
            latent[:, index] += negative_loading * contrasts[
                :, contrast_column
            ] + residual_loading * rng.standard_normal(draws)

    for index in range(count):
        if index not in grouped_indices:
            residual_loading = math.sqrt(1.0 - _shared_variance(players[index], config))
            latent[:, index] += residual_loading * rng.standard_normal(draws)
    return latent


def _shared_variance(item: PlayerSimulationInput, config: DependenceConfig) -> float:
    position = _position(item.player.position)
    variance = config.game_loading**2 + config.team_loading_by_position[position] ** 2
    if position == "QB" or position in config.pass_catcher_positions:
        variance += config.qb_pass_catcher_loading**2
    return variance


def _position(value: str) -> str:
    normalized = value.strip().upper()
    return "DST" if normalized in {"D", "DEF"} else normalized
