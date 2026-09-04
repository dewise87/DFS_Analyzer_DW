"""A native stochastic field generator; it never crosses the pydfs adapter boundary."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from narrative_alpha.portfolio import CLASSIC_SITE_RULES, CandidatePlayer, OptimizationRequest
from narrative_alpha.simulation.config import FieldConfig


class FieldGenerationError(ValueError):
    """Raised when legal lineups cannot reproduce the ownership target."""


@dataclass(frozen=True)
class FieldGenerationResult:
    lineups: tuple[tuple[int, ...], ...]
    source_targets: Mapping[int, float]
    calibrated_targets: Mapping[int, float]
    achieved_marginals: Mapping[int, float]
    stack_rate: float
    maximum_marginal_error: float


def calibrate_ownership_targets(
    players: Sequence[CandidatePlayer],
    ownership: Mapping[int, float],
    request: OptimizationRequest,
) -> dict[int, float]:
    """Apply position-logit offsets so ownership sums to the site's roster totals.

    Fixed slots receive their exact totals. The FLEX total is distributed across its
    eligible positions in proportion to the source marginal mass above the fixed-slot
    floor, then one offset per position solves the §12.2.6 equation.
    """

    candidates = tuple(players)
    _validate_targets(candidates, ownership)
    slots = CLASSIC_SITE_RULES[request.site].slots
    position_players: dict[str, list[CandidatePlayer]] = {}
    for player in candidates:
        position_players.setdefault(_position(player.position), []).append(player)

    fixed = Counter(_position(slot) for slot in slots if slot != "FLEX")
    flex_slots = sum(slot == "FLEX" for slot in slots)
    flex_positions = tuple(
        sorted(
            position
            for position, values in position_players.items()
            if any("FLEX" in player.eligible_roster_slots for player in values)
        )
    )
    target_position_totals = {position: float(count) for position, count in fixed.items()}
    if flex_slots:
        if not flex_positions:
            raise FieldGenerationError("the roster has FLEX slots but no FLEX-eligible players")
        raw_totals = {
            position: math.fsum(
                ownership[player.player_id] for player in position_players[position]
            )
            for position in flex_positions
        }
        capacities = {
            position: len(position_players[position]) - target_position_totals.get(position, 0.0)
            for position in flex_positions
        }
        fixed_flex_total = math.fsum(
            target_position_totals.get(position, 0.0) for position in flex_positions
        )
        raw_matches_roster = math.isclose(
            math.fsum(raw_totals.values()),
            fixed_flex_total + flex_slots,
            abs_tol=1e-8,
        ) and all(
            raw_totals[position] >= target_position_totals.get(position, 0.0) - 1e-8
            for position in flex_positions
        )
        extras = (
            {
                position: raw_totals[position] - target_position_totals.get(position, 0.0)
                for position in flex_positions
            }
            if raw_matches_roster
            else _allocate_flex_totals(raw_totals, capacities, float(flex_slots))
        )
        for position, extra in extras.items():
            target_position_totals[position] = target_position_totals.get(position, 0.0) + extra

    unknown_positions = set(position_players) - set(target_position_totals)
    if unknown_positions:
        listed = ", ".join(sorted(unknown_positions))
        raise FieldGenerationError(f"candidate positions have no site roster slot: {listed}")

    calibrated: dict[int, float] = {}
    for position in sorted(position_players):
        group = position_players[position]
        position_total = target_position_totals[position]
        if position_total < -1e-12 or position_total > len(group) + 1e-12:
            raise FieldGenerationError(
                f"position {position} cannot supply calibrated roster total {position_total:.6f}"
            )
        offset = _logit_offset(
            tuple(ownership[player.player_id] for player in group), position_total
        )
        for player in group:
            calibrated[player.player_id] = _shift_probability(ownership[player.player_id], offset)
    expected = float(len(slots))
    actual = math.fsum(calibrated.values())
    if not math.isclose(actual, expected, abs_tol=1e-8):
        raise FieldGenerationError(
            f"roster-total calibration produced {actual:.9f}, expected {expected:.9f}"
        )
    return calibrated


def generate_field(
    request: OptimizationRequest,
    ownership: Mapping[int, float],
    *,
    lineup_count: int,
    rng: np.random.Generator,
    config: FieldConfig,
) -> FieldGenerationResult:
    """Generate legal field lineups and fail unless every marginal meets tolerance."""

    if lineup_count < 1:
        raise FieldGenerationError("field lineup_count must be positive")
    players = request.candidate_player_scenario.players
    source_targets = {player.player_id: float(ownership[player.player_id]) for player in players}
    calibrated = calibrate_ownership_targets(players, source_targets, request)
    biases = {player.player_id: 0.0 for player in players}
    best: tuple[tuple[tuple[int, ...], ...], dict[int, float], float, float] | None = None

    for _ in range(config.calibration_iterations):
        lineups, stack_rate = _generate_population(
            request,
            calibrated,
            lineup_count=lineup_count,
            rng=rng,
            config=config,
            biases=biases,
        )
        counts = Counter(player_id for lineup in lineups for player_id in lineup)
        achieved = {player.player_id: counts[player.player_id] / lineup_count for player in players}
        maximum_error = max(
            abs(achieved[player_id] - target) for player_id, target in calibrated.items()
        )
        if best is None or maximum_error < best[2]:
            best = (lineups, achieved, maximum_error, stack_rate)
        if maximum_error <= config.ownership_tolerance + 1e-12:
            return FieldGenerationResult(
                lineups=lineups,
                source_targets=source_targets,
                calibrated_targets=calibrated,
                achieved_marginals=achieved,
                stack_rate=stack_rate,
                maximum_marginal_error=maximum_error,
            )
        for player_id, target in calibrated.items():
            difference = target - achieved[player_id]
            scale = max(target * (1.0 - target), 0.04)
            biases[player_id] = max(-12.0, min(12.0, biases[player_id] + 0.75 * difference / scale))

    assert best is not None
    worst = sorted(
        (
            (abs(best[1][player_id] - target), player_id, target, best[1][player_id])
            for player_id, target in calibrated.items()
        ),
        reverse=True,
    )[:5]
    detail = "; ".join(
        f"player {player_id}: target={target:.6f} achieved={achieved:.6f} error={error:.6f}"
        for error, player_id, target, achieved in worst
    )
    raise FieldGenerationError(
        f"field ownership calibration failed: maximum error {best[2]:.6f} exceeds "
        f"tolerance {config.ownership_tolerance:.6f}; {detail}"
    )


def lineup_is_legal(lineup: Sequence[int], request: OptimizationRequest) -> bool:
    """Validate the site roster, salary, team, and game rules without pydfs."""

    players_by_id = {
        player.player_id: player for player in request.candidate_player_scenario.players
    }
    if len(lineup) != len(CLASSIC_SITE_RULES[request.site].slots) or len(set(lineup)) != len(
        lineup
    ):
        return False
    try:
        players = tuple(players_by_id[player_id] for player_id in lineup)
    except KeyError:
        return False
    if sum(player.salary for player in players) > request.salary_cap:
        return False
    team_counts = Counter(player.team for player in players)
    if (
        request.max_players_per_team is not None
        and max(team_counts.values()) > request.max_players_per_team
    ):
        return False
    if request.min_teams is not None and len(team_counts) < request.min_teams:
        return False
    if (
        request.min_games is not None
        and len({player.game_id for player in players}) < request.min_games
    ):
        return False
    return _can_assign_slots(players, CLASSIC_SITE_RULES[request.site].slots)


def _generate_population(
    request: OptimizationRequest,
    targets: Mapping[int, float],
    *,
    lineup_count: int,
    rng: np.random.Generator,
    config: FieldConfig,
    biases: Mapping[int, float],
) -> tuple[tuple[tuple[int, ...], ...], float]:
    players = tuple(
        sorted(request.candidate_player_scenario.players, key=lambda item: item.player_id)
    )
    counts: Counter[int] = Counter()
    lineups: list[tuple[int, ...]] = []
    stacked = 0
    for lineup_index in range(lineup_count):
        require_stack = bool(rng.random() < config.stack_rate)
        lineup = _draw_lineup(
            request,
            players,
            targets,
            counts,
            lineup_index=lineup_index,
            lineup_count=lineup_count,
            require_stack=require_stack,
            rng=rng,
            config=config,
            biases=biases,
        )
        counts.update(lineup)
        lineups.append(tuple(sorted(lineup)))
        if _is_stack(lineup, players):
            stacked += 1
    return tuple(lineups), stacked / lineup_count


def _draw_lineup(
    request: OptimizationRequest,
    players: tuple[CandidatePlayer, ...],
    targets: Mapping[int, float],
    counts: Counter[int],
    *,
    lineup_index: int,
    lineup_count: int,
    require_stack: bool,
    rng: np.random.Generator,
    config: FieldConfig,
    biases: Mapping[int, float],
) -> tuple[int, ...]:
    slots = CLASSIC_SITE_RULES[request.site].slots
    remaining_lineups = lineup_count - lineup_index
    for _ in range(config.lineup_attempts):
        selected: list[int] = []
        team_counts: Counter[str] = Counter()
        qb_team: str | None = None
        failed = False
        for slot in slots:
            eligible = [
                player
                for player in players
                if player.player_id not in selected
                and _eligible(player, slot)
                and (
                    request.max_players_per_team is None
                    or team_counts[player.team] < request.max_players_per_team
                )
            ]
            if not eligible:
                failed = True
                break
            weights: list[float] = []
            for player in eligible:
                target = targets[player.player_id]
                needed = target * lineup_count - counts[player.player_id]
                desired_now = max(0.01, min(1.0, needed / max(1, remaining_lineups)))
                odds = desired_now / max(1e-9, 1.0 - desired_now)
                weight = odds * math.exp(biases[player.player_id])
                if (
                    require_stack
                    and qb_team is not None
                    and player.team == qb_team
                    and _position(player.position) in {"RB", "WR", "TE"}
                ):
                    weight *= config.stack_weight
                weights.append(max(weight, 1e-12))
            chosen = eligible[_weighted_index(weights, rng)]
            selected.append(chosen.player_id)
            team_counts[chosen.team] += 1
            if slot == "QB":
                qb_team = chosen.team
        if failed:
            continue
        lineup = tuple(selected)
        if _is_stack(lineup, players) != require_stack:
            continue
        if lineup_is_legal(lineup, request):
            return lineup
    label = " with the configured QB stack" if require_stack else ""
    raise FieldGenerationError(
        f"could not generate a legal field lineup{label} after {config.lineup_attempts} attempts"
    )


def _weighted_index(weights: Sequence[float], rng: np.random.Generator) -> int:
    total = math.fsum(weights)
    if not math.isfinite(total) or total <= 0:
        raise FieldGenerationError("field candidate weights are not finite and positive")
    threshold = float(rng.random()) * total
    running = 0.0
    for index, weight in enumerate(weights):
        running += weight
        if running >= threshold:
            return index
    return len(weights) - 1


def _is_stack(lineup: Sequence[int], players: Sequence[CandidatePlayer]) -> bool:
    by_id = {player.player_id: player for player in players}
    selected = tuple(by_id[player_id] for player_id in lineup)
    quarterback = next((player for player in selected if _position(player.position) == "QB"), None)
    return quarterback is not None and any(
        player.player_id != quarterback.player_id
        and player.team == quarterback.team
        and _position(player.position) in {"RB", "WR", "TE"}
        for player in selected
    )


def _can_assign_slots(players: Sequence[CandidatePlayer], slots: Sequence[str]) -> bool:
    ordered_slots = tuple(sorted(slots, key=lambda slot: sum(_eligible(p, slot) for p in players)))

    def assign(index: int, used: set[int]) -> bool:
        if index == len(ordered_slots):
            return True
        slot = ordered_slots[index]
        return any(
            assign(index + 1, used | {player.player_id})
            for player in players
            if player.player_id not in used and _eligible(player, slot)
        )

    return assign(0, set())


def _eligible(player: CandidatePlayer, slot: str) -> bool:
    normalized = "DST" if slot == "DEF" else slot
    eligible = {"DST" if value == "DEF" else value for value in player.eligible_roster_slots}
    return normalized in eligible


def _position(value: str) -> str:
    normalized = value.strip().upper()
    return "DST" if normalized in {"D", "DEF"} else normalized


def _validate_targets(players: Sequence[CandidatePlayer], ownership: Mapping[int, float]) -> None:
    missing = sorted(player.player_id for player in players if player.player_id not in ownership)
    if missing:
        raise FieldGenerationError(f"ownership target missing for player IDs: {missing}")
    for player in players:
        value = ownership[player.player_id]
        if isinstance(value, bool) or not math.isfinite(float(value)) or not 0 <= value <= 1:
            raise FieldGenerationError(
                f"ownership target for player {player.player_id} must be in [0, 1]"
            )


def _allocate_flex_totals(
    raw_totals: Mapping[str, float], capacities: Mapping[str, float], flex_total: float
) -> dict[str, float]:
    extras = {position: 0.0 for position in raw_totals}
    remaining = flex_total
    active = {position for position, capacity in capacities.items() if capacity > 0}
    while remaining > 1e-12:
        if not active:
            raise FieldGenerationError("candidate pool cannot fill the FLEX roster total")
        weights = {position: max(raw_totals[position], 1e-9) for position in active}
        total_weight = math.fsum(weights.values())
        allocated = 0.0
        for position in sorted(active):
            share = remaining * weights[position] / total_weight
            room = capacities[position] - extras[position]
            amount = min(share, room)
            extras[position] += amount
            allocated += amount
        remaining -= allocated
        active = {
            position for position in active if capacities[position] - extras[position] > 1e-12
        }
        if allocated <= 1e-15:
            raise FieldGenerationError("candidate pool cannot allocate the FLEX roster total")
    return extras


def _logit_offset(probabilities: tuple[float, ...], target_total: float) -> float:
    if not probabilities:
        raise FieldGenerationError("cannot calibrate an empty position")
    if target_total <= 0:
        return -50.0
    if target_total >= len(probabilities):
        return 50.0
    low, high = -50.0, 50.0
    for _ in range(100):
        middle = (low + high) / 2.0
        total = math.fsum(_shift_probability(value, middle) for value in probabilities)
        if total < target_total:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def _shift_probability(probability: float, offset: float) -> float:
    clipped = min(max(float(probability), 1e-12), 1.0 - 1e-12)
    logit = math.log(clipped / (1.0 - clipped)) + offset
    if logit >= 0:
        inverse = math.exp(-logit)
        return 1.0 / (1.0 + inverse)
    exponent = math.exp(logit)
    return exponent / (1.0 + exponent)
