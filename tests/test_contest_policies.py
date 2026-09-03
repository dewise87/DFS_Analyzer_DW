from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from test_optimizer import _request

from narrative_alpha.portfolio import (
    ContestArchetype,
    ContestPolicyError,
    DfsSite,
    NumericRange,
    OptimizerError,
    PydfsAdapter,
    load_contest_policies,
    policy_request_fields,
)


def test_shipped_policy_is_byte_hashed_and_refuses_uncovered_archetypes() -> None:
    policy = load_contest_policies()

    assert policy.policy_version == "contest-policy-v1"
    assert len(policy.sha256) == 64
    assert policy.sha256 == hashlib.sha256(policy.raw_bytes).hexdigest()
    with pytest.raises(ContestPolicyError, match="showdown"):
        policy.for_archetype(ContestArchetype.SHOWDOWN)


def test_policy_loader_forbids_unknown_fields_and_archetypes(tmp_path: Path) -> None:
    original = Path("config/contest_policies.toml").read_text(encoding="utf-8")
    unknown_field = tmp_path / "unknown-field.toml"
    unknown_field.write_text(
        original.replace(
            'objective = "projection"',
            'surprise = true\nobjective = "projection"',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ContestPolicyError, match="surprise"):
        load_contest_policies(unknown_field)

    unknown_archetype = tmp_path / "unknown-archetype.toml"
    unknown_archetype.write_text(
        original
        + "\n[archetypes.lottery]\nlineup_uniqueness = 1\n"
        + 'max_player_exposure = 1.0\nobjective = "projection"\n',
        encoding="utf-8",
    )
    with pytest.raises(ContestPolicyError, match="unknown archetypes: lottery"):
        load_contest_policies(unknown_archetype)


@pytest.mark.parametrize(
    ("archetype", "lineup_count"),
    (
        (ContestArchetype.CASH, 1),
        (ContestArchetype.SINGLE_ENTRY, 1),
        (ContestArchetype.THREE_MAX, 3),
        (ContestArchetype.TWENTY_MAX, 20),
        (ContestArchetype.MASS_MULTI_ENTRY, 20),
    ),
)
def test_every_classic_archetype_builds_through_real_adapter(
    archetype: ContestArchetype,
    lineup_count: int,
) -> None:
    policies = load_contest_policies()
    base = _request(DfsSite.DRAFTKINGS, number_of_lineups=lineup_count)
    fields = policy_request_fields(policies, archetype, base.candidate_player_scenario)
    request = base.model_copy(
        update={"contest_archetype": archetype, **fields.as_update()}
    )

    lineups = PydfsAdapter().build_lineups(request)

    assert len(lineups) == lineup_count


def test_impossible_ownership_band_names_band_and_candidate_pool_range() -> None:
    request = _request(DfsSite.DRAFTKINGS).model_copy(
        update={"ownership_sum_range": NumericRange(minimum=2.0, maximum=2.5)}
    )

    with pytest.raises(OptimizerError) as raised:
        PydfsAdapter().build_lineups(request)

    message = str(raised.value)
    assert "ownership-sum band [200.00, 250.00] points" in message
    assert "candidate pool's valid-lineup range" in message


def test_a_full_exposure_policy_puts_no_exposure_ranges_in_the_request() -> None:
    """A maximum of 1.0 constrains nothing and must not bloat the cash request."""

    from test_build import _players

    from narrative_alpha.portfolio import CandidatePlayerScenario, ContestArchetype

    policies = load_contest_policies()
    scenario = CandidatePlayerScenario(
        scenario_id="scenario-fixture",
        players=_players(),
        projection_source_versions=("fixture",),
    )
    cash = policy_request_fields(policies, ContestArchetype.CASH, scenario)
    capped = policy_request_fields(policies, ContestArchetype.MASS_MULTI_ENTRY, scenario)

    assert cash.player_exposure_ranges == ()
    assert cash.minimum_lineups() == 1
    assert len(capped.player_exposure_ranges) == len(_players())
    assert capped.minimum_lineups() == 3


def test_too_few_lineups_for_the_exposure_cap_is_refused_before_building(
    tmp_path: Path,
) -> None:
    from test_build import DECISION_AT, _seed_database

    from narrative_alpha.build import BuildInputError, build_decision

    database = tmp_path / "store.sqlite3"
    _seed_database(database)
    with pytest.raises(BuildInputError, match="build at least 3 lineups"):
        build_decision(
            database,
            slate_id=1,
            site="draftkings",
            decision_at=DECISION_AT,
            artifact_directory=tmp_path / "decisions",
            number_of_lineups=1,
            contest_archetype="mass_multi_entry",
        )
