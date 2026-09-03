"""The sole boundary around the legacy pydfs-lineup-optimizer dependency."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any, cast

from pydfs_lineup_optimizer import (  # type: ignore[import-untyped]
    LineupOptimizerException,
    Player,
    Site,
    Sport,
    get_optimizer,
)
from pydfs_lineup_optimizer.player import GameInfo  # type: ignore[import-untyped]

from narrative_alpha.portfolio.adapter import (
    OptimizerError,
    UnsupportedOptimizationFeature,
)
from narrative_alpha.portfolio.export import export_upload_csv
from narrative_alpha.portfolio.models import (
    CLASSIC_SITE_RULES,
    CandidatePlayer,
    ContestArchetype,
    DfsSite,
    Lineup,
    LineupPlayer,
    OptimizationRequest,
    SlateType,
    UploadEntry,
    ValidationResult,
    lineup_sha256,
)
from narrative_alpha.portfolio.validation import (
    eligible_for_slot,
    validate_lineup,
    validate_portfolio,
)


class PydfsAdapter:
    """Projection-maximizing Phase 0 adapter with explicit capability checks."""

    def build_lineups(self, request: OptimizationRequest) -> tuple[Lineup, ...]:
        unsupported = _unsupported_features(request)
        if unsupported:
            raise UnsupportedOptimizationFeature(unsupported)

        roster_size = len(CLASSIC_SITE_RULES[request.site].slots)
        ownership_bounds = _ownership_average_bounds(request, roster_size)
        ownership_pool_range = _ownership_pool_range(request)
        _require_ownership_band_intersection(request, ownership_pool_range)
        exposure_maxima = _player_exposure_maxima(request)
        candidates = {
            player.site_player_id: player for player in request.candidate_player_scenario.players
        }

        # Every pydfs interaction stays inside this boundary so callers only ever
        # see the adapter's own error type, never a raw LineupOptimizerException.
        try:
            optimizer = get_optimizer(_pydfs_site(request.site), Sport.FOOTBALL)
            optimizer.settings.budget = request.salary_cap
            if request.max_players_per_team is not None:
                optimizer.settings.max_from_one_team = request.max_players_per_team
            if request.min_games is not None:
                optimizer.settings.min_games = request.min_games

            games = _build_game_infos(request.candidate_player_scenario.players)
            pydfs_players = []
            pydfs_by_canonical_id: dict[int, Player] = {}
            maximum_counts = {
                player_id: math.floor(maximum * request.number_of_lineups + 1e-9)
                for player_id, maximum in exposure_maxima.items()
            }
            pinned_counts = Counter(
                player.player_id for lineup in request.pinned_lineups for player in lineup.players
            )
            over_exposed = sorted(
                player_id
                for player_id, count in pinned_counts.items()
                if count > maximum_counts.get(player_id, request.number_of_lineups)
            )
            if over_exposed:
                raise OptimizerError(
                    "pinned lineups already exceed player exposure policy for IDs: "
                    + ", ".join(str(player_id) for player_id in over_exposed)
                )
            for candidate in request.candidate_player_scenario.players:
                game = games[candidate.game_id]
                first_name, last_name = _split_name(candidate.name)
                pydfs_player = Player(
                    player_id=candidate.site_player_id,
                    first_name=first_name,
                    last_name=last_name,
                    positions=[_pydfs_position(candidate, request.site)],
                    team=candidate.team,
                    salary=candidate.salary,
                    fppg=candidate.projection,
                    is_injured=candidate.is_injured,
                    projected_ownership=candidate.projected_ownership,
                    game_info=game,
                    max_exposure=_pydfs_max_exposure(
                        maximum_counts.get(candidate.player_id),
                        request.number_of_lineups,
                    ),
                )
                pydfs_players.append(pydfs_player)
                pydfs_by_canonical_id[candidate.player_id] = pydfs_player
            optimizer.player_pool.load_players(pydfs_players)

            # pydfs validates min_teams against the loaded pool, so this must
            # run after load_players (an empty pool always has zero teams).
            if request.min_teams is not None:
                optimizer.set_total_teams(min_teams=request.min_teams)
            if request.number_of_lineups > 1:
                optimizer.set_max_repeating_players(roster_size - request.lineup_uniqueness)
            if ownership_bounds is not None:
                optimizer.set_projected_ownership(*ownership_bounds)

            # pydfs' public exclude_lineups contract iterates each supplied lineup as a
            # collection of its own Player objects.  Keeping canonical IDs in our request
            # makes that exclusion deterministic and replayable without leaking the
            # dependency outside this adapter.
            excluded_lineups: list[Any] = [
                tuple(pydfs_by_canonical_id[player_id] for player_id in lineup)
                for lineup in request.excluded_lineup_player_ids
            ]
            # A pinned lineup is returned verbatim and never regenerated: the optimizer
            # is asked only for the remainder, with the pinned rows excluded as well so a
            # duplicate of one cannot come back as "new".
            pinned = request.pinned_lineups
            excluded_lineups.extend(
                tuple(pydfs_by_canonical_id[player.player_id] for player in lineup.players)
                for lineup in pinned
            )
            remaining = request.number_of_lineups - len(pinned)
            solved: tuple[Any, ...] = (
                ()
                if remaining == 0
                else tuple(
                    optimizer.optimize(
                        remaining,
                        exclude_lineups=excluded_lineups,
                        exposure_strategy=_portfolio_exposure_strategy(
                            optimizer,
                            list(optimizer.player_pool.filtered_players),
                            tuple(
                                candidates[str(player.id)].player_id
                                for player in optimizer.player_pool.filtered_players
                            ),
                            pinned_counts,
                            request.number_of_lineups,
                        ),
                    )
                )
            )
            lineups = (
                *pinned,
                *(_convert_lineup(pydfs_lineup, request, candidates) for pydfs_lineup in solved),
            )
        except LineupOptimizerException as error:
            raise OptimizerError(f"pydfs could not generate requested lineups: {error}") from error
        validation = validate_portfolio(lineups, request)
        if not validation.valid:
            reasons = "; ".join(issue.message for issue in validation.errors)
            raise OptimizerError(f"independent lineup validation failed: {reasons}")
        if len(lineups) != request.number_of_lineups:
            raise OptimizerError(
                f"pydfs returned {len(lineups)} of {request.number_of_lineups} requested lineups"
            )
        return lineups

    def validate_lineup(self, lineup: Lineup, request: OptimizationRequest) -> ValidationResult:
        return validate_lineup(lineup, request)

    def export_upload_csv(
        self,
        lineups: tuple[Lineup, ...],
        site: DfsSite,
        entries: tuple[UploadEntry, ...] = (),
    ) -> bytes:
        return export_upload_csv(lineups, site, entries)


def _unsupported_features(request: OptimizationRequest) -> tuple[str, ...]:
    features: list[str] = []
    if request.slate_type is not SlateType.CLASSIC:
        features.append("showdown slate rules")
    if request.contest_archetype is ContestArchetype.SHOWDOWN:
        features.append(f"contest objective {request.contest_archetype.value}")
    if request.objective != "projection":
        features.append(f"objective {request.objective}")
    if request.stack_rules:
        features.append("stack rules")
    if request.bring_back_rules:
        features.append("bring-back rules")
    if request.team_exposure_limits:
        features.append("team exposure limits")
    if request.game_exposure_limits:
        features.append("game exposure limits")
    if request.player_exposure_ranges and not _player_exposures_supported(request):
        features.append("player exposure ranges")
    if request.duplication_penalty:
        features.append("duplication penalty")
    if request.late_game_optionality_value:
        features.append("late-game optionality value")
    if request.portfolio_covariance_penalty:
        features.append("portfolio covariance penalty")
    if request.time_limit_seconds is not None:
        features.append("solver time limit")
    if request.number_of_lineups > 1 and request.lineup_uniqueness == 9:
        features.append("nine-player lineup uniqueness")
    for candidate in request.candidate_player_scenario.players:
        # pydfs grants every roster slot its position allows on the site; a
        # candidate declaring a narrower eligible_roster_slots set (for
        # example an RB without FLEX) cannot be honored faithfully.
        narrowed = tuple(
            slot
            for slot in _pydfs_granted_slots(candidate, request.site)
            if not eligible_for_slot(candidate, slot, request.site)
        )
        if narrowed:
            features.append(
                f"eligible_roster_slots for {candidate.name} narrower than pydfs "
                f"grants for {candidate.position} (missing {', '.join(narrowed)})"
            )
    return tuple(features)


def _player_exposures_supported(request: OptimizationRequest) -> bool:
    """pydfs can faithfully enforce a complete set of maximum-only ranges."""

    ranges = request.player_exposure_ranges
    candidate_ids = {player.player_id for player in request.candidate_player_scenario.players}
    return {exposure.player_id for exposure in ranges} == candidate_ids and all(
        exposure.minimum == 0 for exposure in ranges
    )


def _player_exposure_maxima(request: OptimizationRequest) -> dict[int, float]:
    if not request.player_exposure_ranges:
        return {}
    return {exposure.player_id: exposure.maximum for exposure in request.player_exposure_ranges}


def _pydfs_max_exposure(maximum_count: int | None, total_lineups: int) -> float | None:
    if maximum_count is None:
        return None
    # TotalExposureStrategy treats zero as "no limit". A negative sentinel is truthy
    # and is already reached before iteration one, faithfully representing zero slots.
    return -1.0 if maximum_count == 0 else maximum_count / total_lineups


def _portfolio_exposure_strategy(
    optimizer: Any,
    players: list[Player],
    canonical_player_ids: tuple[int, ...],
    pinned_counts: Counter[int],
    total_lineups: int,
) -> type[Any]:
    """Seed pydfs exposure accounting with pinned rows and the whole portfolio size."""

    initial: dict[str, int] = {}
    for index, (player, canonical_id) in enumerate(zip(players, canonical_player_ids, strict=True)):
        variable_name = optimizer._solver_class.build_player_var_name(player, str(index))
        count = pinned_counts.get(canonical_id, 0)
        if count:
            initial[variable_name] = count

    class PortfolioExposureStrategy:
        def __init__(self, exposures: dict[str, float], _remaining_lineups: int) -> None:
            self.exposures = exposures
            self.total_lineups = total_lineups
            self.used_vars = defaultdict(int, initial)

        def set_used(self, variables: list[str]) -> None:
            for variable in variables:
                if variable in self.exposures:
                    self.used_vars[variable] += 1

        def is_reached_exposure(self, variable: str) -> bool:
            maximum = self.exposures.get(variable)
            if not maximum:
                return False
            return maximum <= self.used_vars.get(variable, 0) / self.total_lineups

    return PortfolioExposureStrategy


def _build_game_infos(players: Iterable[CandidatePlayer]) -> dict[str, GameInfo]:
    """Build one GameInfo per game with deterministic team ordering.

    Salary data does not identify which side is home, so both slots are
    populated deterministically by sorted team code regardless of which
    candidate is seen first. Phase 0 rules never consume home/away
    orientation.
    """

    games: dict[str, GameInfo] = {}
    for candidate in players:
        home_team, away_team = sorted((candidate.team, candidate.opponent))
        games.setdefault(
            candidate.game_id,
            GameInfo(
                home_team=home_team,
                away_team=away_team,
                starts_at=candidate.game_start,
            ),
        )
    return games


def _pydfs_granted_slots(player: CandidatePlayer, site: DfsSite) -> tuple[str, ...]:
    """The roster slots pydfs 3.6.1 grants this player's position on this site."""

    position = player.position
    if site is DfsSite.FANDUEL and position in {"D", "DEF", "DST"}:
        return ("DEF",)
    if position in {"RB", "WR", "TE"}:
        return (position, "FLEX")
    return (position,)


def _ownership_average_bounds(
    request: OptimizationRequest, roster_size: int
) -> tuple[float, float] | None:
    """Convert a summed-ownership range into pydfs per-player average bounds.

    pydfs' set_projected_ownership constrains the average ownership of the
    selected players (and silently divides bounds above 1 by 100), so the
    requested sum bounds are divided by the fixed roster size before hand-off.
    """

    sum_range = request.ownership_sum_range
    if sum_range is None:
        return None
    if any(
        player.projected_ownership is None for player in request.candidate_player_scenario.players
    ):
        raise OptimizerError("ownership_sum_range requires ownership for every player")
    if sum_range.minimum == sum_range.maximum:
        raise OptimizerError(
            "ownership_sum_range with minimum equal to maximum is not supported by "
            "the pydfs adapter"
        )
    minimum = sum_range.minimum / roster_size
    maximum = sum_range.maximum / roster_size
    if not (0 < minimum <= 1 and 0 < maximum <= 1):
        raise OptimizerError(
            "ownership_sum_range must convert to per-player average bounds in (0, 1]; "
            f"got [{minimum:.6f}, {maximum:.6f}] for roster size {roster_size}"
        )
    return minimum, maximum


def _ownership_pool_range(request: OptimizationRequest) -> tuple[float, float] | None:
    """Return min/max ownership sums among site-valid candidate-pool lineups."""

    if request.ownership_sum_range is None:
        return None
    players = request.candidate_player_scenario.players
    if any(player.projected_ownership is None for player in players):
        return None  # _ownership_average_bounds gives the stable missing-data refusal.
    minimum = _extreme_ownership_sum(request, maximize=False)
    maximum = _extreme_ownership_sum(request, maximize=True)
    return minimum, maximum


def _extreme_ownership_sum(request: OptimizationRequest, *, maximize: bool) -> float:
    optimizer, candidates = _ownership_optimizer(request, maximize=maximize)
    try:
        lineup = next(optimizer.optimize(1))
    except (LineupOptimizerException, StopIteration) as error:
        raise OptimizerError(
            "candidate pool has no lineup satisfying the site, salary, team, and game rules"
        ) from error
    return sum(cast(float, candidates[str(item.id)].projected_ownership) for item in lineup)


def _ownership_band_is_feasible(request: OptimizationRequest) -> bool:
    roster_size = len(CLASSIC_SITE_RULES[request.site].slots)
    bounds = _ownership_average_bounds(request, roster_size)
    optimizer, _ = _ownership_optimizer(request, maximize=True)
    if bounds is not None:
        optimizer.set_projected_ownership(*bounds)
    try:
        next(optimizer.optimize(1))
    except (LineupOptimizerException, StopIteration):
        return False
    return True


def _ownership_optimizer(
    request: OptimizationRequest,
    *,
    maximize: bool,
) -> tuple[Any, dict[str, CandidatePlayer]]:
    """Build the one-lineup solver used only to characterize ownership feasibility."""

    optimizer = get_optimizer(_pydfs_site(request.site), Sport.FOOTBALL)
    optimizer.settings.budget = request.salary_cap
    if request.max_players_per_team is not None:
        optimizer.settings.max_from_one_team = request.max_players_per_team
    if request.min_games is not None:
        optimizer.settings.min_games = request.min_games
    candidates = {
        player.site_player_id: player for player in request.candidate_player_scenario.players
    }
    games = _build_game_infos(request.candidate_player_scenario.players)
    pydfs_players = []
    direction = 1.0 if maximize else -1.0
    for candidate in request.candidate_player_scenario.players:
        first_name, last_name = _split_name(candidate.name)
        pydfs_players.append(
            Player(
                player_id=candidate.site_player_id,
                first_name=first_name,
                last_name=last_name,
                positions=[_pydfs_position(candidate, request.site)],
                team=candidate.team,
                salary=candidate.salary,
                fppg=direction * cast(float, candidate.projected_ownership),
                is_injured=candidate.is_injured,
                projected_ownership=candidate.projected_ownership,
                game_info=games[candidate.game_id],
            )
        )
    optimizer.player_pool.load_players(pydfs_players)
    if request.min_teams is not None:
        optimizer.set_total_teams(min_teams=request.min_teams)
    return optimizer, candidates


def _require_ownership_band_intersection(
    request: OptimizationRequest,
    pool_range: tuple[float, float] | None,
) -> None:
    band = request.ownership_sum_range
    if band is None or pool_range is None:
        return
    pool_minimum, pool_maximum = pool_range
    intersects = band.maximum >= pool_minimum - 1e-9 and band.minimum <= pool_maximum + 1e-9
    if not intersects or not _ownership_band_is_feasible(request):
        raise OptimizerError(_ownership_band_refusal(request, pool_range))


def _ownership_band_refusal(
    request: OptimizationRequest,
    pool_range: tuple[float, float],
) -> str:
    band = request.ownership_sum_range
    assert band is not None
    return (
        f"ownership-sum band [{band.minimum * 100:.2f}, {band.maximum * 100:.2f}] points "
        "cannot be satisfied by a valid lineup; the candidate pool's valid-lineup "
        f"range is [{pool_range[0] * 100:.2f}, {pool_range[1] * 100:.2f}] points"
    )


def _convert_lineup(
    pydfs_lineup: Iterable[Any],
    request: OptimizationRequest,
    candidates: dict[str, CandidatePlayer],
) -> Lineup:
    players: list[LineupPlayer] = []
    for item in pydfs_lineup:
        site_player_id = str(item.id)
        slot = str(item.lineup_position).upper()
        candidate = candidates[site_player_id]
        players.append(
            LineupPlayer(
                slot=slot,
                player_id=candidate.player_id,
                site_player_id=site_player_id,
                name=candidate.name,
                team=candidate.team,
                opponent=candidate.opponent,
                position=candidate.position,
                salary=candidate.salary,
                projection=candidate.projection,
                projected_ownership=candidate.projected_ownership,
                game_id=candidate.game_id,
            )
        )
    player_tuple = tuple(players)
    return Lineup(
        lineup_id=lineup_sha256(request.site, request.slate_id, player_tuple),
        site=request.site,
        slate_id=request.slate_id,
        players=player_tuple,
        total_salary=sum(player.salary for player in player_tuple),
        total_projection=round(sum(player.projection for player in player_tuple), 6),
    )


def _pydfs_site(site: DfsSite) -> str:
    value = Site.DRAFTKINGS if site is DfsSite.DRAFTKINGS else Site.FANDUEL
    return cast(str, value)


def _pydfs_position(player: CandidatePlayer, site: DfsSite) -> str:
    if site is DfsSite.FANDUEL and player.position in {"DEF", "DST"}:
        return "D"
    return player.position


def _split_name(name: str) -> tuple[str, str]:
    first, separator, last = name.rpartition(" ")
    return (first, last) if separator else (name, "")
