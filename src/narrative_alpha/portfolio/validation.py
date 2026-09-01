"""Site-rule validation independent of every optimization solver."""

from __future__ import annotations

from collections import Counter

from narrative_alpha.portfolio.models import (
    CLASSIC_SITE_RULES,
    CandidatePlayer,
    DfsSite,
    Lineup,
    OptimizationRequest,
    ValidationIssue,
    ValidationResult,
)


def validate_lineup(lineup: Lineup, request: OptimizationRequest) -> ValidationResult:
    """Double-check roster, position, salary, team, game, and site rules."""

    errors: list[ValidationIssue] = []
    rules = CLASSIC_SITE_RULES[request.site]
    if lineup.site is not request.site or lineup.slate_id != request.slate_id:
        errors.append(_issue("wrong_slate", "lineup site/slate does not match request"))

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
        if not eligible_for_slot(candidate, lineup_player.slot, request.site):
            errors.append(
                _issue(
                    "position",
                    f"{candidate.name} ({candidate.position}) is ineligible for "
                    f"{lineup_player.slot}",
                )
            )

    if lineup.total_salary > request.salary_cap:
        errors.append(
            _issue(
                "salary_cap",
                f"salary {lineup.total_salary} exceeds cap {request.salary_cap}",
            )
        )

    team_counts = Counter(player.team for player in lineup.players)
    max_team = request.max_players_per_team or rules.default_max_players_per_team
    if max_team is not None and team_counts and max(team_counts.values()) > max_team:
        errors.append(
            _issue("max_team", f"lineup exceeds maximum {max_team} players from one team")
        )
    min_teams = request.min_teams or rules.default_min_teams
    if min_teams is not None and len(team_counts) < min_teams:
        errors.append(_issue("min_teams", f"lineup uses fewer than {min_teams} teams"))
    min_games = request.min_games or rules.default_min_games
    if min_games is not None and len({player.game_id for player in lineup.players}) < min_games:
        errors.append(_issue("min_games", f"lineup uses fewer than {min_games} games"))

    if request.ownership_sum_range is not None:
        ownership = [player.projected_ownership for player in lineup.players]
        if any(value is None for value in ownership):
            errors.append(_issue("ownership_missing", "ownership bound requires every player"))
        else:
            total = sum(value for value in ownership if value is not None)
            if not (
                request.ownership_sum_range.minimum <= total <= request.ownership_sum_range.maximum
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
    for index, lineup in enumerate(lineups, start=1):
        result = validate_lineup(lineup, request)
        errors.extend(
            _issue(f"lineup_{index}_{issue.code}", issue.message) for issue in result.errors
        )

    max_overlap = len(CLASSIC_SITE_RULES[request.site].slots) - request.lineup_uniqueness
    for first_index, first in enumerate(lineups):
        first_ids = {player.player_id for player in first.players}
        for second_index, second in enumerate(lineups[first_index + 1 :], start=first_index + 1):
            overlap = len(first_ids & {player.player_id for player in second.players})
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


def eligible_for_slot(player: CandidatePlayer, slot: str, site: DfsSite) -> bool:
    """Report whether the independent site rules allow ``player`` in ``slot``."""

    slot = slot.upper()
    position = player.position.upper()
    if slot == "FLEX":
        return position in {"RB", "WR", "TE"} and "FLEX" in player.eligible_roster_slots
    if site is DfsSite.FANDUEL and slot == "DEF":
        return position in {"D", "DEF", "DST"}
    return position == slot and slot in player.eligible_roster_slots


def _issue(code: str, message: str) -> ValidationIssue:
    return ValidationIssue(code=code, message=message)
