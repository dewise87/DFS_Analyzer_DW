"""A native stochastic field generator; it never crosses the pydfs adapter boundary."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache

import numpy as np
from numpy.typing import NDArray

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
    salary_use: float
    #: The per-player logit corrections that produced these lineups. They describe the
    #: gap between the calibrated target and what the sampler draws on this pool, which
    #: is a property of the pool and not of the seed, so a later replicate can start from
    #: them instead of rediscovering them.
    calibration_biases: Mapping[int, float]


@dataclass(frozen=True)
class _SlotCandidates:
    """Per-slot views that hold still while one population is drawn.

    Gathering a slot's salaries, teams, and targets once per population instead of once
    per slot per lineup is what lets a 100,000-entry field build in tens of seconds. Every
    element is the same double the per-lineup gather produced, so a seeded field is
    unchanged by the batching.
    """

    indices: NDArray[np.int64]
    salaries: NDArray[np.int64]
    teams: NDArray[np.int64]
    touch: NDArray[np.bool_]
    targets: NDArray[np.float64]
    target_counts: NDArray[np.float64]
    exponential_biases: NDArray[np.float64]


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
    biases: Mapping[int, float] | None = None,
) -> FieldGenerationResult:
    """Generate legal field lineups and fail unless every marginal meets tolerance.

    ``biases`` seeds the ownership-correction loop with a vector a previous field on this
    same pool and target converged to; every replicate after the first passes one, which
    is what keeps a 100,000-entry contest inside a Sunday budget. It is a starting point
    only: the loop still measures what it drew and still refuses outside tolerance, so a
    stale or wrong vector costs iterations rather than producing an uncalibrated field.
    """

    if lineup_count < 1:
        raise FieldGenerationError("field lineup_count must be positive")
    players = request.candidate_player_scenario.players
    _validate_targets(players, ownership)
    source_targets = {player.player_id: float(ownership[player.player_id]) for player in players}
    calibrated = calibrate_ownership_targets(players, source_targets, request)
    biases = _starting_biases(players, biases)
    best: tuple[tuple[tuple[int, ...], ...], dict[int, float], float, float, float] | None = None

    for _ in range(config.calibration_iterations):
        lineups, stack_rate, salary_use = _generate_population(
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
        salary_error = abs(salary_use - config.salary_use)
        if best is None or (maximum_error, salary_error) < (
            best[2],
            abs(best[4] - config.salary_use),
        ):
            best = (lineups, achieved, maximum_error, stack_rate, salary_use)
        if (
            maximum_error <= config.ownership_tolerance + 1e-12
            and salary_error <= config.salary_use_tolerance + 1e-12
        ):
            return FieldGenerationResult(
                lineups=lineups,
                source_targets=source_targets,
                calibrated_targets=calibrated,
                achieved_marginals=achieved,
                stack_rate=stack_rate,
                maximum_marginal_error=maximum_error,
                salary_use=salary_use,
                calibration_biases=dict(biases),
            )
        for player_id, target in calibrated.items():
            smoothing = 0.5 / lineup_count
            correction = 0.8 * math.log((target + smoothing) / (achieved[player_id] + smoothing))
            biases[player_id] = max(-12.0, min(12.0, biases[player_id] + correction))

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
        f"tolerance {config.ownership_tolerance:.6f}; salary_use target="
        f"{config.salary_use:.6f} achieved={best[4]:.6f} tolerance="
        f"{config.salary_use_tolerance:.6f}; {detail}"
    )


def lineup_is_legal(
    lineup: Sequence[int],
    request: OptimizationRequest,
    *,
    _players_by_id: Mapping[int, CandidatePlayer] | None = None,
) -> bool:
    """Validate the site roster, salary, team, and game rules without pydfs."""

    players_by_id = _players_by_id or {
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
) -> tuple[tuple[tuple[int, ...], ...], float, float]:
    players = tuple(
        sorted(request.candidate_player_scenario.players, key=lambda item: item.player_id)
    )
    player_ids = np.asarray([player.player_id for player in players], dtype=np.int64)
    salaries = np.asarray([player.salary for player in players], dtype=np.int64)
    team_names = tuple(sorted({player.team for player in players}))
    team_lookup = {team: index for index, team in enumerate(team_names)}
    team_indices = np.asarray([team_lookup[player.team] for player in players], dtype=np.int64)
    touch_players = np.asarray(
        [_position(player.position) in {"RB", "WR", "TE"} for player in players],
        dtype=np.bool_,
    )
    counts = np.zeros(len(players), dtype=np.int64)
    target_values = np.asarray([targets[int(player_id)] for player_id in player_ids])
    bias_values = np.asarray([biases[int(player_id)] for player_id in player_ids])
    exponential_biases = np.exp(bias_values)
    lineups: list[tuple[int, ...]] = []
    stacked = 0
    salary_total = 0
    by_id = {player.player_id: player for player in players}
    slots = CLASSIC_SITE_RULES[request.site].slots
    slot_candidates = {
        slot: _slot_candidates(
            np.asarray(
                [index for index, player in enumerate(players) if _eligible(player, slot)],
                dtype=np.int64,
            ),
            salaries=salaries,
            team_indices=team_indices,
            touch_players=touch_players,
            targets=target_values,
            exponential_biases=exponential_biases,
            lineup_count=lineup_count,
        )
        for slot in set(slots)
    }
    salary_bounds = _precomputed_salary_bounds(players, slots)
    salary_floor, salary_ceiling = _salary_band(request, config)
    if salary_bounds[0][0] > salary_ceiling or salary_bounds[0][1] < salary_floor:
        nearest_salary = (
            salary_bounds[0][0] if salary_bounds[0][0] > salary_ceiling else salary_bounds[0][1]
        )
        raise FieldGenerationError(
            "field salary calibration is infeasible from the candidate pool: "
            f"salary_use target={config.salary_use:.6f} tolerance="
            f"{config.salary_use_tolerance:.6f}, achievable coarse range="
            f"[{salary_bounds[0][0] / request.salary_cap:.6f}, "
            f"{salary_bounds[0][1] / request.salary_cap:.6f}], achieved="
            f"{nearest_salary / request.salary_cap:.6f}"
        )
    team_slots = int(team_indices.max()) + 1
    for lineup_index in range(lineup_count):
        require_stack = bool(rng.random() < config.stack_rate)
        lineup, lineup_indices = _draw_lineup(
            request,
            player_ids=player_ids,
            counts=counts,
            lineup_index=lineup_index,
            lineup_count=lineup_count,
            require_stack=require_stack,
            rng=rng,
            config=config,
            salary_bounds=salary_bounds,
            salary_floor=salary_floor,
            salary_ceiling=salary_ceiling,
            slot_candidates=slot_candidates,
            player_count=len(players),
            team_slots=team_slots,
            players_by_id=by_id,
        )
        counts[lineup_indices] += 1
        lineups.append(tuple(sorted(lineup)))
        salary_total += int(salaries[lineup_indices].sum())
        if _is_stack(lineup, by_id):
            stacked += 1
    return (
        tuple(lineups),
        stacked / lineup_count,
        salary_total / (lineup_count * request.salary_cap),
    )


def _draw_lineup(
    request: OptimizationRequest,
    *,
    player_ids: NDArray[np.int64],
    counts: NDArray[np.int64],
    lineup_index: int,
    lineup_count: int,
    require_stack: bool,
    rng: np.random.Generator,
    config: FieldConfig,
    salary_bounds: tuple[tuple[int, int], ...],
    salary_floor: int,
    salary_ceiling: int,
    slot_candidates: Mapping[str, _SlotCandidates],
    player_count: int,
    team_slots: int,
    players_by_id: Mapping[int, CandidatePlayer],
) -> tuple[tuple[int, ...], NDArray[np.int64]]:
    slots = CLASSIC_SITE_RULES[request.site].slots
    remaining_lineups = lineup_count - lineup_index
    closest_salary = 0
    for _ in range(config.lineup_attempts):
        selected: list[int] = []
        selected_mask = np.zeros(player_count, dtype=np.bool_)
        team_counts = np.zeros(team_slots, dtype=np.int64)
        qb_team: int | None = None
        remaining_salary = salary_ceiling
        failed = False
        for slot_index, slot in enumerate(slots):
            remaining_slots = len(slots) - slot_index - 1
            minimum_completion, maximum_completion = salary_bounds[slot_index + 1]
            spent = salary_ceiling - remaining_salary
            available = slot_candidates[slot]
            candidate_salaries = available.salaries
            # The minimum-completion term already implies salaries <= remaining_salary.
            mask = (
                ~selected_mask[available.indices]
                & (candidate_salaries + minimum_completion <= remaining_salary)
                & (spent + candidate_salaries + maximum_completion >= salary_floor)
            )
            if request.max_players_per_team is not None:
                mask &= team_counts[available.teams] < request.max_players_per_team
            if not require_stack and qb_team is not None:
                mask &= ~((available.teams == qb_team) & available.touch)
            eligible = np.flatnonzero(mask)
            if eligible.size == 0:
                failed = True
                break
            desired_salary = (request.salary_cap * config.salary_use - spent) / (
                remaining_slots + 1
            )
            eligible_salaries = candidate_salaries[eligible]
            salary_scale = max(1.0, float(eligible_salaries.max() - eligible_salaries.min()))
            catch_up = (
                available.target_counts[eligible] - counts[available.indices[eligible]]
            ) / max(1, remaining_lineups)
            desired_now = np.clip(0.65 * available.targets[eligible] + 0.35 * catch_up, 0.01, 0.99)
            weights = desired_now / np.maximum(1e-9, 1.0 - desired_now)
            weights *= available.exponential_biases[eligible]
            salary_pull = (eligible_salaries - desired_salary) / salary_scale
            weights *= np.exp(np.clip(salary_pull, -1.0, 1.0))
            if require_stack and qb_team is not None:
                weights *= np.where(
                    (available.teams[eligible] == qb_team) & available.touch[eligible],
                    config.stack_weight,
                    1.0,
                )
            position = int(eligible[_weighted_array_index(weights, rng)])
            chosen = int(available.indices[position])
            chosen_team = int(available.teams[position])
            selected.append(chosen)
            selected_mask[chosen] = True
            remaining_salary -= int(candidate_salaries[position])
            team_counts[chosen_team] += 1
            if slot == "QB":
                qb_team = chosen_team
        if failed:
            continue
        indices = np.asarray(selected, dtype=np.int64)
        lineup = tuple(int(player_ids[index]) for index in indices)
        salary = salary_ceiling - remaining_salary
        if abs(salary - request.salary_cap * config.salary_use) < abs(
            closest_salary - request.salary_cap * config.salary_use
        ):
            closest_salary = salary
        if not salary_floor <= salary <= salary_ceiling:
            continue
        if _is_stack(lineup, players_by_id) != require_stack:
            continue
        if lineup_is_legal(lineup, request, _players_by_id=players_by_id):
            return lineup, indices
    label = " with the configured QB stack" if require_stack else ""
    raise FieldGenerationError(
        f"could not generate a legal field lineup{label} after {config.lineup_attempts} attempts; "
        f"salary_use target={config.salary_use:.6f} achieved="
        f"{closest_salary / request.salary_cap:.6f} tolerance="
        f"{config.salary_use_tolerance:.6f}"
    )


def _salary_band(request: OptimizationRequest, config: FieldConfig) -> tuple[int, int]:
    """The inclusive spent-salary window the configured salary-use band allows."""

    floor = math.ceil(request.salary_cap * (config.salary_use - config.salary_use_tolerance) - 1e-9)
    ceiling = min(
        request.salary_cap,
        math.floor(request.salary_cap * (config.salary_use + config.salary_use_tolerance) + 1e-9),
    )
    return floor, ceiling


def _slot_candidates(
    indices: NDArray[np.int64],
    *,
    salaries: NDArray[np.int64],
    team_indices: NDArray[np.int64],
    touch_players: NDArray[np.bool_],
    targets: NDArray[np.float64],
    exponential_biases: NDArray[np.float64],
    lineup_count: int,
) -> _SlotCandidates:
    slot_targets = targets[indices]
    return _SlotCandidates(
        indices=indices,
        salaries=salaries[indices],
        teams=team_indices[indices],
        touch=touch_players[indices],
        targets=slot_targets,
        target_counts=slot_targets * lineup_count,
        exponential_biases=exponential_biases[indices],
    )


def _precomputed_salary_bounds(
    players: Sequence[CandidatePlayer], slots: Sequence[str]
) -> tuple[tuple[int, int], ...]:
    """Conservative min/max completion salary for every remaining slot suffix."""

    per_slot: list[tuple[int, int]] = []
    for slot in slots:
        salaries = [player.salary for player in players if _eligible(player, slot)]
        if not salaries:
            raise FieldGenerationError(f"candidate pool has no player eligible for {slot}")
        per_slot.append((min(salaries), max(salaries)))
    suffix: list[tuple[int, int]] = [(0, 0)] * (len(slots) + 1)
    for index in range(len(slots) - 1, -1, -1):
        next_minimum, next_maximum = suffix[index + 1]
        suffix[index] = (
            per_slot[index][0] + next_minimum,
            per_slot[index][1] + next_maximum,
        )
    return tuple(suffix)


def _weighted_array_index(weights: NDArray[np.float64], rng: np.random.Generator) -> int:
    cumulative = np.cumsum(weights)
    total = float(cumulative[-1])
    if not math.isfinite(total) or total <= 0:
        raise FieldGenerationError("field candidate weights are not finite and positive")
    return min(len(weights) - 1, int(np.searchsorted(cumulative, rng.random() * total)))


def _is_stack(lineup: Sequence[int], players_by_id: Mapping[int, CandidatePlayer]) -> bool:
    selected = tuple(players_by_id[player_id] for player_id in lineup)
    quarterback = next((player for player in selected if _position(player.position) == "QB"), None)
    return quarterback is not None and any(
        player.player_id != quarterback.player_id
        and player.team == quarterback.team
        and _position(player.position) in {"RB", "WR", "TE"}
        for player in selected
    )


def _can_assign_slots(players: Sequence[CandidatePlayer], slots: Sequence[str]) -> bool:
    # One eligibility pass feeds both the most-constrained-first ordering and the search,
    # so the backstop costs one pass over the roster rather than one per branch.
    eligible_by_slot = tuple(
        tuple(index for index, player in enumerate(players) if _eligible(player, slot))
        for slot in slots
    )
    ordered_slots = tuple(sorted(eligible_by_slot, key=len))

    def assign(index: int, used: frozenset[int]) -> bool:
        if index == len(ordered_slots):
            return True
        return any(
            assign(index + 1, used | {candidate})
            for candidate in ordered_slots[index]
            if candidate not in used
        )

    return assign(0, frozenset())


def _eligible(player: CandidatePlayer, slot: str) -> bool:
    normalized = "DST" if slot == "DEF" else slot
    return normalized in _normalized_slots(player.eligible_roster_slots)


@cache
def _normalized_slots(slots: tuple[str, ...]) -> frozenset[str]:
    return frozenset("DST" if value == "DEF" else value for value in slots)


def _position(value: str) -> str:
    normalized = value.strip().upper()
    return "DST" if normalized in {"D", "DEF"} else normalized


def _starting_biases(
    players: Sequence[CandidatePlayer], carried: Mapping[int, float] | None
) -> dict[int, float]:
    if carried is None:
        return {player.player_id: 0.0 for player in players}
    expected = {player.player_id for player in players}
    if set(carried) != expected:
        missing = sorted(expected - set(carried))
        extra = sorted(set(carried) - expected)
        raise FieldGenerationError(
            f"carried field calibration does not cover this pool; missing={missing}, extra={extra}"
        )
    values = {int(player_id): float(value) for player_id, value in carried.items()}
    if any(not math.isfinite(value) for value in values.values()):
        raise FieldGenerationError("carried field calibration contains a non-finite bias")
    return values


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
