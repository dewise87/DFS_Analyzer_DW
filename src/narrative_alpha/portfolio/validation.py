"""Site-rule validation independent of every optimization solver."""

from __future__ import annotations

import math
from collections import Counter

from narrative_alpha.portfolio.models import (
    CandidatePlayer,
    DfsSite,
    Lineup,
    OptimizationRequest,
    ShowdownSiteRules,
    SlateType,
    ValidationIssue,
    ValidationResult,
    lineup_sha256,
    site_rules,
)


def validate_lineup(lineup: Lineup, request: OptimizationRequest) -> ValidationResult:
    """Double-check roster, position, salary, team, game, and site rules."""

    errors: list[ValidationIssue] = []
    rules = site_rules(request.site, request.slate_type)
    if lineup.site is not request.site or lineup.slate_id != request.slate_id:
        errors.append(_issue("wrong_slate", "lineup site/slate does not match request"))
    if lineup.lineup_id != lineup_sha256(lineup.site, lineup.slate_id, lineup.players):
        errors.append(_issue("lineup_identity", "lineup ID differs from its roster contents"))

    expected_slots = Counter(rules.slots)
    actual_slots = Counter(player.slot for player in lineup.players)
    if actual_slots != expected_slots:
        errors.append(
            _issue(
                "roster_slots",
                f"expected slots {dict(expected_slots)}, got {dict(actual_slots)}",
            )
        )

    player_ids = [player.player_id for player in lineup.players]
    site_ids = [player.site_player_id for player in lineup.players]
    if len(player_ids) != len(set(player_ids)) or len(site_ids) != len(set(site_ids)):
        errors.append(_issue("duplicate_player", "a player appears more than once"))

    candidates = {player.player_id: player for player in request.candidate_player_scenario.players}
    for lineup_player in lineup.players:
        candidate = candidates.get(lineup_player.player_id)
        if candidate is None:
            errors.append(
                _issue("unknown_player", f"player {lineup_player.player_id} is not in scenario")
            )
            continue
        if lineup_player.site_player_id != candidate.site_player_id:
            errors.append(
                _issue("site_player_id", f"site ID mismatch for player {candidate.player_id}")
            )
        if candidate.is_injured:
            errors.append(_issue("unavailable_player", f"{candidate.name} is unavailable"))
        for field in ("team", "opponent", "position", "game_id"):
            if getattr(lineup_player, field) != getattr(candidate, field):
                errors.append(
                    _issue("candidate_metadata", f"{candidate.name} {field} differs from scenario")
                )
        salary_multiplier = 1.0
        points_multiplier = 1.0
        if isinstance(rules, ShowdownSiteRules) and lineup_player.slot == rules.captain_slot:
            salary_multiplier = rules.captain_salary_multiplier
            points_multiplier = rules.captain_points_multiplier
        if lineup_player.salary != round(candidate.salary * salary_multiplier):
            errors.append(_issue("player_salary", f"{candidate.name} salary differs from scenario"))
        # The fast lane intentionally retains historical estimates on pinned rows.
        # New rows must use the current scenario's points and ownership verbatim.
        if lineup not in request.pinned_lineups:
            if not math.isclose(
                lineup_player.projection,
                round(candidate.projection * points_multiplier, 6),
                rel_tol=0,
                abs_tol=1e-6,
            ):
                errors.append(
                    _issue(
                        "player_projection", f"{candidate.name} projection differs from scenario"
                    )
                )
            if (
                lineup_player.projected_ownership != candidate.projected_ownership
                or lineup_player.projected_ownership_captain
                != candidate.projected_ownership_captain
            ):
                errors.append(
                    _issue("player_ownership", f"{candidate.name} ownership differs from scenario")
                )
        if not eligible_for_slot(candidate, lineup_player.slot, request.site, request.slate_type):
            errors.append(
                _issue(
                    "position",
                    f"{candidate.name} ({candidate.position}) is ineligible for "
                    f"{lineup_player.slot}",
                )
            )

    actual_salary = sum(player.salary for player in lineup.players)
    if lineup.total_salary != actual_salary:
        errors.append(_issue("salary_total", "lineup total differs from player salaries"))
    if not math.isclose(
        lineup.total_projection,
        round(sum(player.projection for player in lineup.players), 6),
        rel_tol=0,
        abs_tol=1e-6,
    ):
        errors.append(_issue("projection_total", "lineup total differs from player projections"))
    salary_cap = min(request.salary_cap, rules.default_salary_cap)
    if actual_salary > salary_cap:
        errors.append(
            _issue(
                "salary_cap",
                f"salary {actual_salary} exceeds cap {salary_cap}",
            )
        )

    team_counts = Counter(player.team for player in lineup.players)
    maxima = (request.max_players_per_team, rules.default_max_players_per_team)
    max_team = min((value for value in maxima if value is not None), default=None)
    if max_team is not None and team_counts and max(team_counts.values()) > max_team:
        errors.append(
            _issue("max_team", f"lineup exceeds maximum {max_team} players from one team")
        )
    min_teams = max(request.min_teams or 0, rules.default_min_teams or 0)
    if min_teams is not None and len(team_counts) < min_teams:
        errors.append(_issue("min_teams", f"lineup uses fewer than {min_teams} teams"))
    min_games = max(request.min_games or 0, rules.default_min_games or 0)
    if min_games is not None and len({player.game_id for player in lineup.players}) < min_games:
        errors.append(_issue("min_games", f"lineup uses fewer than {min_games} games"))

    if request.ownership_sum_range is not None:
        ownership = [
            player.projected_ownership_captain
            if request.slate_type is SlateType.SHOWDOWN and player.slot in {"CPT", "MVP"}
            else player.projected_ownership
            for player in lineup.players
        ]
        if any(value is None for value in ownership):
            errors.append(_issue("ownership_missing", "ownership bound requires every player"))
        else:
            total = sum(value for value in ownership if value is not None)
            if not math.isfinite(total) or not (
                request.ownership_sum_range.minimum - 1e-9
                <= total
                <= request.ownership_sum_range.maximum + 1e-9
            ):
                errors.append(
                    _issue(
                        "ownership_sum",
                        f"ownership sum {total:.6f} is outside requested range",
                    )
                )
    return ValidationResult(valid=not errors, errors=tuple(errors))


def validate_portfolio(
    lineups: tuple[Lineup, ...], request: OptimizationRequest
) -> ValidationResult:
    errors: list[ValidationIssue] = []
    if len(lineups) != request.number_of_lineups:
        errors.append(
            _issue(
                "lineup_count", f"expected {request.number_of_lineups} lineups, got {len(lineups)}"
            )
        )
    if lineups[: len(request.pinned_lineups)] != request.pinned_lineups:
        errors.append(
            _issue("pinned_lineups", "pinned lineups must be returned verbatim and first")
        )
    excluded = {frozenset(ids) for ids in request.excluded_lineup_player_ids}
    for index, lineup in enumerate(lineups, start=1):
        if frozenset(player.player_id for player in lineup.players) in excluded:
            errors.append(_issue("excluded_lineup", f"lineup {index} was explicitly excluded"))
        result = validate_lineup(lineup, request)
        errors.extend(
            _issue(f"lineup_{index}_{issue.code}", issue.message) for issue in result.errors
        )

    max_overlap = (
        len(site_rules(request.site, request.slate_type).slots) - request.lineup_uniqueness
    )
    for first_index, first in enumerate(lineups):
        first_ids = _lineup_entries(first, request.slate_type)
        for second_index, second in enumerate(lineups[first_index + 1 :], start=first_index + 1):
            overlap = len(first_ids & _lineup_entries(second, request.slate_type))
            if overlap > max_overlap:
                errors.append(
                    _issue(
                        "lineup_uniqueness",
                        f"lineups {first_index + 1} and {second_index + 1} overlap by {overlap}",
                    )
                )

    counts = Counter(player.player_id for lineup in lineups for player in lineup.players)
    exposures = {item.player_id: item for item in request.player_exposure_ranges}
    if lineups:
        for player_id, exposure in exposures.items():
            actual = counts[player_id] / len(lineups)
            if not exposure.minimum <= actual <= exposure.maximum:
                errors.append(
                    _issue(
                        "player_exposure",
                        f"player {player_id} exposure {actual:.6f} is outside requested range",
                    )
                )
    return ValidationResult(valid=not errors, errors=tuple(errors))


def eligible_for_slot(
    player: CandidatePlayer,
    slot: str,
    site: DfsSite,
    slate_type: SlateType = SlateType.CLASSIC,
) -> bool:
    """Report whether the independent site rules allow ``player`` in ``slot``."""

    slot = slot.upper()
    position = player.position.upper()
    if slate_type is SlateType.SHOWDOWN:
        return slot in player.eligible_roster_slots
    if slot == "FLEX":
        return position in {"RB", "WR", "TE"} and "FLEX" in player.eligible_roster_slots
    if site is DfsSite.FANDUEL and slot == "DEF":
        return position in {"D", "DEF", "DST"}
    return position == slot and slot in player.eligible_roster_slots


def _lineup_entries(lineup: Lineup, slate_type: SlateType) -> set[object]:
    if slate_type is SlateType.CLASSIC:
        return {player.player_id for player in lineup.players}
    return {(player.player_id, player.slot) for player in lineup.players}


def _issue(code: str, message: str) -> ValidationIssue:
    return ValidationIssue(code=code, message=message)
