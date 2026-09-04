import csv
import hashlib
import io
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from narrative_alpha.build import canonical_json_bytes
from narrative_alpha.portfolio import (
    CandidatePlayer,
    CandidatePlayerScenario,
    ContestArchetype,
    DfsSite,
    ExposureLimit,
    Lineup,
    LineupPlayer,
    NumericRange,
    OptimizationRequest,
    OptimizerError,
    PlayerExposureRange,
    PydfsAdapter,
    SlateType,
    StackRule,
    UnsupportedOptimizationFeature,
    UploadEntry,
    lineup_sha256,
    validate_lineup,
    validate_portfolio,
)
from narrative_alpha.portfolio.pydfs_adapter import _build_game_infos

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "narrative_alpha"

TEAMS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH")


@st.composite
def _optimization_requests(draw: st.DrawFn) -> OptimizationRequest:
    """Draw structurally varied, always-feasible classic optimization requests.

    Salaries and projections are drawn independently per player so the argmax
    and the binding constraints genuinely change between examples. Position
    counts stay above roster demand and every salary stays at or below
    cap // 9, so a valid lineup always exists.
    """

    site = draw(st.sampled_from((DfsSite.DRAFTKINGS, DfsSite.FANDUEL)))
    defense = "DST" if site is DfsSite.DRAFTKINGS else "D"
    position_counts = (
        ("QB", draw(st.integers(min_value=2, max_value=4))),
        ("RB", draw(st.integers(min_value=4, max_value=8))),
        ("WR", draw(st.integers(min_value=5, max_value=10))),
        ("TE", draw(st.integers(min_value=3, max_value=5))),
        (defense, draw(st.integers(min_value=2, max_value=4))),
    )
    salary_cap = 50_000 if site is DfsSite.DRAFTKINGS else 60_000
    max_salary = salary_cap // 9
    players: list[CandidatePlayer] = []
    player_id = 1
    for position, count in position_counts:
        for index in range(count):
            team_index = (player_id - 1) % len(TEAMS)
            slots = (position, "FLEX") if position in {"RB", "WR", "TE"} else (position,)
            players.append(
                CandidatePlayer(
                    player_id=player_id,
                    site_player_id=str(10_000 + player_id),
                    name=f"{position} Player {index + 1}",
                    team=TEAMS[team_index],
                    opponent=TEAMS[team_index ^ 1],
                    position=position,
                    eligible_roster_slots=slots,
                    salary=draw(st.integers(min_value=3_000, max_value=max_salary)),
                    projection=draw(
                        st.floats(min_value=0, max_value=40, allow_nan=False, allow_infinity=False)
                    ),
                    projected_ownership=draw(
                        st.floats(
                            min_value=0.01, max_value=0.6, allow_nan=False, allow_infinity=False
                        )
                    ),
                    game_id=f"game-{team_index // 2 + 1}",
                    game_start=datetime(2026, 9, 13, 17 + team_index // 2, tzinfo=UTC),
                )
            )
            player_id += 1
    return OptimizationRequest(
        site=site,
        slate_id=1,
        slate_type=SlateType.CLASSIC,
        contest_archetype=ContestArchetype.CASH,
        salary_cap=salary_cap,
        candidate_player_scenario=CandidatePlayerScenario(
            scenario_id="drawn-scenario",
            players=tuple(players),
            projection_source_versions=("drawn-projection-v1",),
        ),
        lineup_uniqueness=1,
        number_of_lineups=draw(st.integers(min_value=1, max_value=3)),
    )


@given(optimization_request=_optimization_requests())
@settings(max_examples=25, deadline=None)
def test_generated_lineups_pass_independent_validation_across_randomized_pools(
    optimization_request: OptimizationRequest,
) -> None:
    lineups = PydfsAdapter().build_lineups(optimization_request)

    assert len(lineups) == optimization_request.number_of_lineups
    for lineup in lineups:
        assert validate_lineup(lineup, optimization_request).valid
    assert validate_portfolio(lineups, optimization_request).valid
    assert len({lineup.lineup_id for lineup in lineups}) == (optimization_request.number_of_lineups)


@pytest.mark.parametrize(
    ("site", "captain_slot", "roster_size", "salary_multiplier"),
    (
        (DfsSite.DRAFTKINGS, "CPT", 6, 1.5),
        (DfsSite.FANDUEL, "MVP", 5, 1.0),
    ),
)
def test_showdown_build_uses_native_captain_mode_and_site_multipliers(
    site: DfsSite,
    captain_slot: str,
    roster_size: int,
    salary_multiplier: float,
) -> None:
    request = _showdown_request(site)

    lineup = PydfsAdapter().build_lineups(request)[0]
    captain = next(player for player in lineup.players if player.slot == captain_slot)
    candidate = next(
        player
        for player in request.candidate_player_scenario.players
        if player.player_id == captain.player_id
    )
    upload = PydfsAdapter().export_upload_csv((lineup,), site).decode("utf-8")

    assert len(lineup.players) == roster_size
    assert captain.salary == round(candidate.salary * salary_multiplier)
    assert captain.projection == pytest.approx(candidate.projection * 1.5)
    assert upload.splitlines()[0].split(",")[0] == captain_slot
    assert validate_lineup(lineup, request).valid


def test_classic_request_bytes_do_not_gain_the_captain_ownership_field() -> None:
    content = canonical_json_bytes(_request(DfsSite.DRAFTKINGS))

    assert b"projected_ownership_captain" not in content
    assert hashlib.sha256(content).hexdigest() == (
        "35a33540c26574519b31f1331deea44034ca27eb4608daa8614d2892b7a35871"
    )


def test_showdown_pinned_lineup_is_preserved_byte_for_byte() -> None:
    adapter = PydfsAdapter()
    request = _showdown_request(DfsSite.DRAFTKINGS)
    lineup = adapter.build_lineups(request)[0]
    pinned_request = request.model_copy(update={"pinned_lineups": (lineup,)})

    rebuilt = adapter.build_lineups(pinned_request)

    assert rebuilt == (lineup,)
    assert adapter.export_upload_csv(rebuilt, request.site) == adapter.export_upload_csv(
        (lineup,), request.site
    )


def test_min_teams_request_builds_after_pool_load() -> None:
    request = _request(DfsSite.DRAFTKINGS).model_copy(update={"min_teams": 4})

    lineups = PydfsAdapter().build_lineups(request)

    assert len(lineups) == 1
    assert len({player.team for player in lineups[0].players}) >= 4


def test_setter_raised_pydfs_errors_surface_as_adapter_error() -> None:
    request = _request(DfsSite.DRAFTKINGS).model_copy(update={"min_teams": 9})

    with pytest.raises(OptimizerError, match="pydfs"):
        PydfsAdapter().build_lineups(request)


def test_ownership_sum_range_bounds_summed_lineup_ownership() -> None:
    unconstrained = _request(DfsSite.DRAFTKINGS)
    adapter = PydfsAdapter()
    baseline = adapter.build_lineups(unconstrained)[0]
    baseline_sum = sum(player.projected_ownership or 0 for player in baseline.players)

    request = unconstrained.model_copy(
        update={"ownership_sum_range": NumericRange(minimum=0.9, maximum=1.4)}
    )
    lineups = adapter.build_lineups(request)

    assert baseline_sum < 0.9  # the requested range genuinely constrains the solve
    for lineup in lineups:
        total = sum(player.projected_ownership or 0 for player in lineup.players)
        assert 0.9 <= total <= 1.4


def test_degenerate_ownership_sum_range_is_rejected_with_adapter_error() -> None:
    request = _request(DfsSite.DRAFTKINGS).model_copy(
        update={"ownership_sum_range": NumericRange(minimum=1.0, maximum=1.0)}
    )

    with pytest.raises(OptimizerError, match="ownership_sum_range"):
        PydfsAdapter().build_lineups(request)


def test_ownership_sum_range_outside_unit_average_is_rejected() -> None:
    request = _request(DfsSite.DRAFTKINGS).model_copy(
        update={"ownership_sum_range": NumericRange(minimum=0.0, maximum=1.4)}
    )

    with pytest.raises(OptimizerError, match="ownership_sum_range"):
        PydfsAdapter().build_lineups(request)


def test_player_exposure_ranges_are_rejected_explicitly() -> None:
    request = _request(DfsSite.DRAFTKINGS, number_of_lineups=2).model_copy(
        update={
            "player_exposure_ranges": (PlayerExposureRange(player_id=1, minimum=0.5, maximum=1.0),)
        }
    )

    with pytest.raises(UnsupportedOptimizationFeature) as raised:
        PydfsAdapter().build_lineups(request)

    assert "player exposure ranges" in raised.value.features


def test_pool_with_no_flex_rb_is_rejected_explicitly() -> None:
    base = _request(DfsSite.DRAFTKINGS)
    players = list(base.candidate_player_scenario.players)
    index = next(i for i, player in enumerate(players) if player.position == "RB")
    players[index] = players[index].model_copy(update={"eligible_roster_slots": ("RB",)})
    scenario = base.candidate_player_scenario.model_copy(update={"players": tuple(players)})
    request = base.model_copy(update={"candidate_player_scenario": scenario})

    with pytest.raises(UnsupportedOptimizationFeature) as raised:
        PydfsAdapter().build_lineups(request)

    assert any(
        players[index].name in feature and "FLEX" in feature for feature in raised.value.features
    )


def test_game_info_teams_are_deterministic_regardless_of_candidate_order() -> None:
    players = _player_pool(DfsSite.DRAFTKINGS, 0, 0)

    forward = _build_game_infos(players)
    reverse = _build_game_infos(tuple(reversed(players)))

    assert forward.keys() == reverse.keys()
    for game_id, game in forward.items():
        assert (game.home_team, game.away_team) == (
            reverse[game_id].home_team,
            reverse[game_id].away_team,
        )
        assert [game.home_team, game.away_team] == sorted([game.home_team, game.away_team])
        assert game.home_team != game.away_team


def test_importing_portfolio_without_adapter_leaves_pydfs_unimported() -> None:
    code = "\n".join(
        (
            "import sys",
            "from narrative_alpha.portfolio import validate_lineup",
            "assert 'pydfs_lineup_optimizer' not in sys.modules, 'pydfs imported eagerly'",
            "from narrative_alpha.portfolio import PydfsAdapter",
            "assert PydfsAdapter.__name__ == 'PydfsAdapter'",
            "assert 'pydfs_lineup_optimizer' in sys.modules",
        )
    )

    subprocess.run((sys.executable, "-c", code), check=True)


def test_lineup_rejects_partial_rosters() -> None:
    request = _request(DfsSite.DRAFTKINGS)
    lineup = PydfsAdapter().build_lineups(request)[0]
    short = lineup.players[:3]

    with pytest.raises(ValidationError, match="exactly 9 players"):
        Lineup(
            lineup_id=lineup_sha256(lineup.site, lineup.slate_id, short),
            site=lineup.site,
            slate_id=lineup.slate_id,
            players=short,
            total_salary=sum(player.salary for player in short),
            total_projection=round(sum(player.projection for player in short), 6),
        )


def test_stack_rule_and_exposure_limit_reject_blank_strings() -> None:
    with pytest.raises(ValidationError, match="stack positions"):
        StackRule(positions=("QB", "   "), count=2)
    with pytest.raises(ValidationError, match="exposure key"):
        ExposureLimit(key="   ", maximum=0.5)


def test_phase_zero_adapter_rejects_advanced_controls_explicitly() -> None:
    request = _request(DfsSite.DRAFTKINGS).model_copy(
        update={
            "contest_archetype": ContestArchetype.SINGLE_ENTRY,
            "stack_rules": (StackRule(positions=("QB", "WR"), count=2),),
            "duplication_penalty": 0.5,
            "time_limit_seconds": 10.0,
        }
    )

    with pytest.raises(UnsupportedOptimizationFeature) as raised:
        PydfsAdapter().build_lineups(request)

    assert raised.value.features == (
        "stack rules",
        "duplication penalty",
        "solver time limit",
    )


def test_independent_validation_catches_solver_output_tampering() -> None:
    request = _request(DfsSite.FANDUEL)
    lineup = PydfsAdapter().build_lineups(request)[0]
    changed_players = (lineup.players[0].model_copy(update={"slot": "WR"}), *lineup.players[1:])
    changed_tuple = tuple(changed_players)
    tampered = Lineup(
        lineup_id=lineup_sha256(lineup.site, lineup.slate_id, changed_tuple),
        site=lineup.site,
        slate_id=lineup.slate_id,
        players=changed_tuple,
        total_salary=lineup.total_salary,
        total_projection=lineup.total_projection,
    )

    result = validate_lineup(tampered, request)

    assert result.valid is False
    assert {issue.code for issue in result.errors} >= {"roster_slots", "position"}


@pytest.mark.parametrize("site", (DfsSite.DRAFTKINGS, DfsSite.FANDUEL))
def test_upload_csv_contains_site_template_metadata_and_roster_ids(site: DfsSite) -> None:
    request = _request(site)
    adapter = PydfsAdapter()
    lineups = adapter.build_lineups(request)
    entries = (
        UploadEntry(
            entry_id="entry-1",
            contest_id="contest-1",
            contest_name="Fixture Contest",
            entry_fee="$1.00",
        ),
    )

    content = adapter.export_upload_csv(lineups, site, entries)
    rows = list(csv.reader(io.StringIO(content.decode("utf-8"))))

    assert len(rows) == 2
    if site is DfsSite.DRAFTKINGS:
        assert rows[0][:4] == ["Entry ID", "Contest Name", "Contest ID", "Entry Fee"]
        assert rows[1][:4] == ["entry-1", "Fixture Contest", "contest-1", "$1.00"]
        assert all(" (" in cell and cell.endswith(")") for cell in rows[1][4:])
    else:
        assert rows[0][:3] == ["entry_id", "contest_id", "contest_name"]
        assert rows[1][:3] == ["entry-1", "contest-1", "Fixture Contest"]
        assert all(cell.isdigit() for cell in rows[1][3:])


@pytest.mark.parametrize(
    ("site", "golden_filename"),
    (
        (DfsSite.DRAFTKINGS, "draftkings_upload.csv"),
        (DfsSite.FANDUEL, "fanduel_upload.csv"),
    ),
)
def test_upload_csv_is_byte_stable_against_golden_file(site: DfsSite, golden_filename: str) -> None:
    candidates = {player.player_id: player for player in _player_pool(site, 0, 0)}
    player_slots = zip(
        (1, 5, 6, 13, 14, 15, 23, 7, 27),
        ("QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"),
        strict=True,
    )
    players = tuple(
        LineupPlayer(
            slot=("DEF" if site is DfsSite.FANDUEL and slot == "DST" else slot),
            player_id=player_id,
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
        for player_id, slot in player_slots
    )
    lineup = Lineup(
        lineup_id=lineup_sha256(site, 1, players),
        site=site,
        slate_id=1,
        players=players,
        total_salary=sum(player.salary for player in players),
        total_projection=sum(player.projection for player in players),
    )
    entry = UploadEntry(
        entry_id="entry-1",
        contest_id="contest-1",
        contest_name="Fixture Contest",
        entry_fee="$1.00",
    )

    rendered = PydfsAdapter().export_upload_csv((lineup,), site, (entry,))

    assert rendered == (Path(__file__).parent / "golden" / golden_filename).read_bytes()


def test_only_pydfs_adapter_imports_legacy_optimizer_package() -> None:
    importing_files = []
    for path in SOURCE_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "pydfs_lineup_optimizer" in text:
            importing_files.append(path.relative_to(SOURCE_ROOT).as_posix())

    assert importing_files == ["portfolio/pydfs_adapter.py"]


def _request(
    site: DfsSite,
    *,
    salary_shift: int = 0,
    projection_shift: float = 0,
    number_of_lineups: int = 1,
) -> OptimizationRequest:
    players = _player_pool(site, salary_shift, projection_shift)
    return OptimizationRequest(
        site=site,
        slate_id=1,
        slate_type=SlateType.CLASSIC,
        contest_archetype=ContestArchetype.CASH,
        salary_cap=50_000 if site is DfsSite.DRAFTKINGS else 60_000,
        candidate_player_scenario=CandidatePlayerScenario(
            scenario_id="fixture-scenario",
            players=players,
            projection_source_versions=("fixture-projection-v1",),
        ),
        lineup_uniqueness=1,
        number_of_lineups=number_of_lineups,
        upload_entries=(
            (
                UploadEntry(
                    entry_id="entry-1",
                    contest_id="contest-1",
                    contest_name="Fixture Contest",
                    entry_fee="$1.00",
                ),
            )
            if number_of_lineups == 1
            else ()
        ),
    )


def _showdown_request(site: DfsSite) -> OptimizationRequest:
    positions = ("QB", "RB", "WR", "TE", "K", "DST")
    players = tuple(
        CandidatePlayer(
            player_id=index,
            site_player_id=str(20_000 + index),
            name=f"Showdown Player {index}",
            team="AAA" if index % 2 else "BBB",
            opponent="BBB" if index % 2 else "AAA",
            position=position,
            eligible_roster_slots=("CPT", "FLEX")
            if site is DfsSite.DRAFTKINGS
            else ("MVP", "FLEX"),
            salary=(6_200 if site is DfsSite.DRAFTKINGS else 9_000) + index * 100,
            projection=round(24.0 - index, 4),
            projected_ownership=5 / 6,
            projected_ownership_captain=1 / 6,
            game_id="game-1",
            game_start=datetime(2026, 9, 13, 17, tzinfo=UTC),
        )
        for index, position in enumerate(positions, start=1)
    )
    return OptimizationRequest(
        site=site,
        slate_id=2,
        slate_type=SlateType.SHOWDOWN,
        contest_archetype=ContestArchetype.SHOWDOWN,
        salary_cap=50_000 if site is DfsSite.DRAFTKINGS else 60_000,
        candidate_player_scenario=CandidatePlayerScenario(
            scenario_id="showdown-fixture-scenario",
            players=players,
            projection_source_versions=("fixture-projection-v1",),
        ),
        lineup_uniqueness=1,
        number_of_lineups=1,
    )


def _player_pool(
    site: DfsSite, salary_shift: int, projection_shift: float
) -> tuple[CandidatePlayer, ...]:
    positions = (("QB", 4), ("RB", 8), ("WR", 10), ("TE", 4), ("D", 4))
    if site is DfsSite.DRAFTKINGS:
        positions = (*positions[:-1], ("DST", 4))
    base_salaries = {"QB": 6200, "RB": 4700, "WR": 4300, "TE": 3500, "D": 2800, "DST": 2800}
    players: list[CandidatePlayer] = []
    player_id = 1
    for position, count in positions:
        for index in range(count):
            team_index = (player_id - 1) % len(TEAMS)
            team = TEAMS[team_index]
            opponent = TEAMS[team_index ^ 1]
            slots = (position, "FLEX") if position in {"RB", "WR", "TE"} else (position,)
            players.append(
                CandidatePlayer(
                    player_id=player_id,
                    site_player_id=str(10_000 + player_id),
                    name=f"{position} Player {index + 1}",
                    team=team,
                    opponent=opponent,
                    position=position,
                    eligible_roster_slots=slots,
                    salary=base_salaries[position] + salary_shift + index * 25,
                    projection=round(30 - player_id * 0.3 + projection_shift, 4),
                    projected_ownership=0.05 + (index % 4) * 0.02,
                    game_id=f"game-{team_index // 2 + 1}",
                    game_start=datetime(2026, 9, 13, 17 + team_index // 2, tzinfo=UTC),
                )
            )
            player_id += 1
    return tuple(players)
