"""Slice 30 — Stage 4 routing, provenance, replay identity, and the Stage 5 memo block."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_build import (
    DATA_AT,
    _players,
    _seed_candidate_pool,
)
from test_features import FixtureProvider

from narrative_alpha.build import BuildRoutingError, build_decision
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.interface import build_slate_memo, render_slate_memo
from narrative_alpha.narrative import (
    build_episodes,
    build_features,
    load_batch_pricing,
    normalize_item_text,
    run_extraction_batch,
)
from narrative_alpha.ops.config import OpsConfig, load_ops_config
from narrative_alpha.ops.status import (
    collect_ops_status,
    render_status,
    status_payload,
)
from narrative_alpha.ownership import (
    OwnershipTrainingRow,
    TrainingData,
    fit_ownership_model,
    load_ownership_config,
    persist_fit,
)
from narrative_alpha.ownership_routing import (
    CLASSIC_CAPS,
    MATERIAL_DELTA,
    NO_PINNED_ROUTING,
    pinned_routing_from_manifest,
)
from narrative_alpha.portfolio import CandidatePlayer, PydfsAdapter
from narrative_alpha.replay import replay_decision
from narrative_alpha.report_cli import load_build_result
from narrative_alpha.store import apply_migrations, connect_database

OWNERSHIP_CONFIG_PATH = Path("config/ownership_model.toml")
PRICING_PATH = Path("config/model_pricing.toml")
HEAT_CONFIG_PATH = Path("config/heat.toml")

ITEM_AT = DATA_AT + timedelta(hours=1)
EXTRACTED_AT = DATA_AT + timedelta(hours=2)
FIRST_DECISION_AT = datetime(2026, 9, 13, 16, 55, tzinfo=UTC)
SCENARIOS_AT = FIRST_DECISION_AT + timedelta(minutes=1)
SECOND_DECISION_AT = FIRST_DECISION_AT + timedelta(minutes=15)

# Stage 1 refuses a name carrying digits, so the candidate pool is renamed to plausible
# person names. The claim names one of them exactly, so the crosswalk resolves it and the
# build's fail-closed identity gate stays satisfied.
CANDIDATE_NAMES = (
    "Adrian Ashby", "Bennett Cole", "Curtis Dane", "Desmond Ellis",
    "Elliot Frost", "Franklin Gale", "Gordon Hale", "Hollis Irving",
    "Ivan Jarrett", "Jonah Keller", "Keegan Lowry", "Lucas Mercer",
    "Marcus Bell", "Nolan Ortiz", "Oscar Pike", "Preston Quinn",
    "Quentin Rowe", "Rowan Sayer", "Silas Thorne", "Trevor Upton",
    "Ulysses Vance", "Victor Whitlock", "Wesley Yates", "Xavier Zane",
)
NARRATIVE_PLAYER_NAME = CANDIDATE_NAMES[12]

# Stage 1 resolves a claim name through `player_team_history`, which needs canonical NFL
# codes; the build pool's placeholder codes map onto these one for one.
ROSTER_TEAMS = {
    "AAA": "BUF",
    "BBB": "MIA",
    "CCC": "NYJ",
    "DDD": "NE",
    "EEE": "KC",
    "FFF": "DEN",
    "GGG": "SF",
    "HHH": "SEA",
}


@dataclass(frozen=True)
class RoutingFixture:
    database: Path
    artifacts: Path
    slate_id: int
    player_ids: tuple[int, ...]
    narrative_player_id: int
    fit_run_id: str
    config_sha256: str
    feature_version: str
    model_version: str


def test_classic_caps_match_the_shipped_ownership_configuration() -> None:
    config = load_ownership_config(Path("config/ownership_model.toml"))
    for status, expected in CLASSIC_CAPS.items():
        assert config.cap("classic", status).maximum_delta == pytest.approx(expected), status


def test_material_delta_matches_the_shipped_ownership_configuration() -> None:
    """Routing sits below `narrative_alpha.ownership` in the import graph and cannot read
    the config; this pins its copy of the threshold to the file the model actually uses."""

    assert load_ownership_config(OWNERSHIP_CONFIG_PATH).evaluation.material_delta == MATERIAL_DELTA


def test_build_falls_back_to_the_vendor_baseline_without_a_winning_evaluation(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    with connect_database(fixture.database) as connection:
        _insert_scenario_set(
            connection,
            fixture,
            applied={player_id: 0.30 for player_id in fixture.player_ids},
            status="TESTING",
            at=SCENARIOS_AT,
        )
        connection.commit()

    unevaluated = build_decision(
        fixture.database,
        slate_id=fixture.slate_id,
        site="draftkings",
        decision_at=SECOND_DECISION_AT,
        artifact_directory=fixture.artifacts,
    )
    assert not unevaluated.ownership_routing.applied
    assert "no out-of-week ownership evaluation exists" in unevaluated.ownership_routing.reason

    with connect_database(fixture.database) as connection:
        _insert_evaluation(connection, fixture, beat_baseline=False, at=SCENARIOS_AT)
        connection.commit()
    lost = build_decision(
        fixture.database,
        slate_id=fixture.slate_id,
        site="draftkings",
        decision_at=SECOND_DECISION_AT + timedelta(minutes=1),
        artifact_directory=fixture.artifacts,
    )
    assert not lost.ownership_routing.applied
    assert "did not beat the untouched vendor baseline" in lost.ownership_routing.reason
    assert all(
        item.artifact_kind != "ownership_scenarios"
        for item in lost.snapshot.manifest_hashes_json
    )
    with connect_database(fixture.database) as connection:
        memo = build_slate_memo(lost, connection)
        reloaded = load_build_result(
            connection,
            decision_snapshot_id=lost.snapshot.decision_snapshot_id,
            decision_at=lost.snapshot.decision_at,
            artifact_root=fixture.artifacts,
        )
        reloaded_memo = build_slate_memo(reloaded, connection)
    assert memo.ownership_routing.applied is False
    assert "ownership_source=vendor_baseline" in render_slate_memo(memo)
    # The record keeps the real reason; a replay alone could only say "no set pinned".
    assert "did not beat the untouched vendor baseline" in reloaded.ownership_routing.reason
    assert "did not beat the untouched vendor baseline" in render_slate_memo(reloaded_memo)


def test_a_material_delta_with_no_episode_is_held_at_the_baseline(tmp_path: Path) -> None:
    """A move on a player the narrative never touched is intercept and calibration, not a
    claim: that player stays at the vendor baseline, the routing says so, and the rest of
    the set is still applied — the slate is not refused for a move the model did not mean."""

    fixture = _fixture(tmp_path)
    quiet = next(
        player_id for player_id in fixture.player_ids if player_id != fixture.narrative_player_id
    )
    with connect_database(fixture.database) as connection:
        baselines = _baselines(connection, fixture)
        applied = dict(baselines)
        applied[quiet] = baselines[quiet] + MATERIAL_DELTA + 0.01
        _insert_scenario_set(
            connection, fixture, applied=applied, status="TESTING", at=SCENARIOS_AT
        )
        _insert_evaluation(connection, fixture, beat_baseline=True, at=SCENARIOS_AT)
        connection.commit()

    built = build_decision(
        fixture.database,
        slate_id=fixture.slate_id,
        site="draftkings",
        decision_at=SECOND_DECISION_AT,
        artifact_directory=fixture.artifacts,
    )

    routing = built.ownership_routing
    assert routing.applied
    held = {delta.player_id: delta for delta in routing.held_deltas}
    assert set(held) == {quiet}
    assert held[quiet].applied_ownership == baselines[quiet]
    assert held[quiet].proposed_ownership == pytest.approx(applied[quiet])
    assert "held at the vendor baseline" in routing.reason
    candidate = next(
        player
        for player in built.request.candidate_player_scenario.players
        if player.player_id == quiet
    )
    assert candidate.projected_ownership == pytest.approx(baselines[quiet])
    with connect_database(fixture.database) as connection:
        stored = connection.execute(
            "SELECT applied, held_at_baseline, reason FROM decision_ownership_routing "
            "WHERE decision_snapshot_id = ?",
            (built.snapshot.decision_snapshot_id,),
        ).fetchone()
    assert stored is not None and tuple(stored)[:2] == (1, 1)
    assert stored["reason"] == routing.reason


def test_a_scenario_row_past_its_governance_cap_refuses_the_set(tmp_path: Path) -> None:
    """Stage 4 is the permission layer: it re-asserts the §12.2.5 cap on stored rows."""

    fixture = _fixture(tmp_path)
    with connect_database(fixture.database) as connection:
        baselines = _baselines(connection, fixture)
        applied = dict(baselines)
        applied[fixture.narrative_player_id] = min(
            1.0, baselines[fixture.narrative_player_id] + 0.06
        )
        _insert_scenario_set(
            connection, fixture, applied=applied, status="TESTING", at=SCENARIOS_AT
        )
        _insert_evaluation(connection, fixture, beat_baseline=True, at=SCENARIOS_AT)
        connection.commit()

    with pytest.raises(BuildRoutingError, match="past the TESTING cap"):
        build_decision(
            fixture.database,
            slate_id=fixture.slate_id,
            site="draftkings",
            decision_at=SECOND_DECISION_AT,
            artifact_directory=fixture.artifacts,
        )


def test_a_pinned_routing_ignores_a_set_that_landed_afterwards(tmp_path: Path) -> None:
    """The fast lane re-freezes with the base decision's routing: the same set, or the
    baseline, never one that landed in between."""

    fixture = _fixture(tmp_path)
    with connect_database(fixture.database) as connection:
        baselines = _baselines(connection, fixture)
        first_set = _insert_scenario_set(
            connection, fixture, applied=dict(baselines), status="TESTING", at=SCENARIOS_AT
        )
        _insert_evaluation(connection, fixture, beat_baseline=True, at=SCENARIOS_AT)
        connection.commit()
    routed = build_decision(
        fixture.database,
        slate_id=fixture.slate_id,
        site="draftkings",
        decision_at=SECOND_DECISION_AT,
        artifact_directory=fixture.artifacts,
    )
    assert routed.ownership_routing.scenario_run_id == first_set

    with connect_database(fixture.database) as connection:
        later = {player_id: value for player_id, value in baselines.items()}
        _insert_scenario_set(
            connection,
            fixture,
            applied=later,
            status="TESTING",
            at=SECOND_DECISION_AT + timedelta(minutes=1),
        )
        connection.commit()

    refrozen = build_decision(
        fixture.database,
        slate_id=fixture.slate_id,
        site="draftkings",
        decision_at=SECOND_DECISION_AT + timedelta(minutes=5),
        artifact_directory=fixture.artifacts,
        ownership_routing=pinned_routing_from_manifest(routed.snapshot.manifest_hashes_json),
    )
    assert refrozen.ownership_routing.applied
    assert refrozen.ownership_routing.scenario_run_id == first_set

    baseline_only = build_decision(
        fixture.database,
        slate_id=fixture.slate_id,
        site="draftkings",
        decision_at=SECOND_DECISION_AT + timedelta(minutes=6),
        artifact_directory=fixture.artifacts,
        ownership_routing=NO_PINNED_ROUTING,
    )
    assert not baseline_only.ownership_routing.applied


def test_a_scenario_set_missing_a_candidate_falls_back_for_the_whole_slate(
    tmp_path: Path,
) -> None:
    """Half a set would mix modeled and vendor ownership inside one roster calibration."""

    fixture = _fixture(tmp_path)
    with connect_database(fixture.database) as connection:
        baselines = _baselines(connection, fixture)
        partial = {
            player_id: value
            for player_id, value in baselines.items()
            if player_id != fixture.narrative_player_id
        }
        _insert_scenario_set(
            connection, fixture, applied=partial, status="TESTING", at=SCENARIOS_AT
        )
        _insert_evaluation(connection, fixture, beat_baseline=True, at=SCENARIOS_AT)
        connection.commit()

    built = build_decision(
        fixture.database,
        slate_id=fixture.slate_id,
        site="draftkings",
        decision_at=SECOND_DECISION_AT,
        artifact_directory=fixture.artifacts,
    )
    assert not built.ownership_routing.applied
    assert "candidate player(s)" in built.ownership_routing.reason
    assert str(fixture.narrative_player_id) in built.ownership_routing.reason


def test_ops_status_names_the_active_scenario_set_and_its_multiplier(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    with connect_database(fixture.database) as connection:
        baselines = _baselines(connection, fixture)
        applied = dict(baselines)
        applied[fixture.narrative_player_id] = (
            baselines[fixture.narrative_player_id] + MATERIAL_DELTA + 0.01
        )
        run_id = _insert_scenario_set(
            connection,
            fixture,
            applied=applied,
            status="TESTING",
            at=SCENARIOS_AT,
        )
        _insert_evaluation(
            connection,
            fixture,
            beat_baseline=True,
            at=SCENARIOS_AT,
        )
        connection.commit()
    build_decision(
        fixture.database,
        slate_id=fixture.slate_id,
        site="draftkings",
        decision_at=SECOND_DECISION_AT,
        artifact_directory=fixture.artifacts,
    )

    config = _ops_config(tmp_path)
    with connect_database(fixture.database) as connection:
        status = collect_ops_status(
            connection,
            config=config,
            database=fixture.database,
            now=SECOND_DECISION_AT + timedelta(minutes=5),
        )
    rendered = render_status(status)
    payload = status_payload(status)

    active = status.ownership_scenarios
    assert [row.scenario_run_id for row in active] == [run_id]
    assert active[0].governance_status == "TESTING"
    assert active[0].status_multiplier == pytest.approx(0.50)
    assert active[0].beat_baseline is True
    assert active[0].applied_in_newest_decision
    assert "OWNERSHIP ROUTING (Stage 4)" in rendered
    assert run_id in rendered
    assert "TESTING x0.50" in rendered
    assert "applied this set" in rendered
    assert payload["ownership_scenarios"][0]["scenario_run_id"] == run_id  # type: ignore[index]


def test_replay_is_byte_identical_with_scenarios_on_and_off(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    baseline_build = build_decision(
        fixture.database,
        slate_id=fixture.slate_id,
        site="draftkings",
        decision_at=SECOND_DECISION_AT,
        artifact_directory=fixture.artifacts,
        contest_archetype="3max",
        number_of_lineups=3,
    )
    assert not baseline_build.ownership_routing.applied

    with connect_database(fixture.database) as connection:
        tournament_fit_run_id = _persist_synthetic_fit(
            connection,
            load_ownership_config(OWNERSHIP_CONFIG_PATH),
            contest_archetype="3max",
        )
        baselines = _baselines(connection, fixture)
        applied = dict(baselines)
        applied[fixture.narrative_player_id] = (
            baselines[fixture.narrative_player_id] + MATERIAL_DELTA + 0.01
        )
        run_id = _insert_scenario_set(
            connection,
            fixture,
            applied=applied,
            status="TESTING",
            at=SCENARIOS_AT,
            contest_archetype="3max",
            model_run_id=tournament_fit_run_id,
        )
        _insert_evaluation(
            connection,
            fixture,
            beat_baseline=True,
            at=SCENARIOS_AT,
            contest_archetype="3max",
        )
        connection.commit()

    routed_build = build_decision(
        fixture.database,
        slate_id=fixture.slate_id,
        site="draftkings",
        decision_at=SECOND_DECISION_AT + timedelta(minutes=1),
        artifact_directory=fixture.artifacts,
        contest_archetype="3max",
        number_of_lineups=3,
    )
    routed = routed_build.ownership_routing
    assert routed.applied
    assert routed.scenario_run_id == run_id
    assert routed.governance_status == "TESTING"
    scenario_artifacts = tuple(
        item
        for item in routed_build.snapshot.manifest_hashes_json
        if item.artifact_kind == "ownership_scenarios"
    )
    assert len(scenario_artifacts) == 1
    assert scenario_artifacts[0].sha256 == routed.sha256
    assert scenario_artifacts[0].path.endswith(run_id)
    moved = next(
        player
        for player in routed_build.request.candidate_player_scenario.players
        if player.player_id == fixture.narrative_player_id
    )
    assert moved.projected_ownership == pytest.approx(applied[fixture.narrative_player_id])

    with connect_database(fixture.database) as connection:
        for built in (baseline_build, routed_build):
            replayed = replay_decision(
                connection,
                decision_snapshot_id=built.snapshot.decision_snapshot_id,
                decision_at=built.snapshot.decision_at,
                artifact_root=fixture.artifacts,
                adapter=PydfsAdapter(),
            )
            assert replayed.report.output_matches
            assert replayed.output_bytes == built.replay.output_bytes
            assert (
                replayed.ownership_routing.scenario_run_id
                == built.ownership_routing.scenario_run_id
            )


def test_red_team_section_answers_all_five_questions(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with connect_database(fixture.database) as connection:
        baselines = _baselines(connection, fixture)
        # A second vendor observation, so "did the baseline already move?" has an answer.
        _insert_baseline(
            connection,
            fixture.narrative_player_id,
            fixture.slate_id,
            ownership=baselines[fixture.narrative_player_id] + 0.03,
            observed_at=DATA_AT + timedelta(hours=3),
        )
        _insert_confounders(connection, fixture)
        connection.commit()

    with connect_database(fixture.database) as connection:
        refreshed = _baselines(connection, fixture)
        applied = dict(refreshed)
        applied[fixture.narrative_player_id] = (
            refreshed[fixture.narrative_player_id] + MATERIAL_DELTA + 0.01
        )
        _insert_scenario_set(
            connection, fixture, applied=applied, status="TESTING", at=SCENARIOS_AT
        )
        _insert_evaluation(connection, fixture, beat_baseline=True, at=SCENARIOS_AT)
        connection.commit()

    built = build_decision(
        fixture.database,
        slate_id=fixture.slate_id,
        site="draftkings",
        decision_at=SECOND_DECISION_AT,
        artifact_directory=fixture.artifacts,
    )
    with connect_database(fixture.database) as connection:
        memo = build_slate_memo(built, connection)
    rendered = render_slate_memo(memo)

    assert memo.ownership_routing.applied
    assert memo.ownership_routing.red_team
    answer = next(
        row
        for row in memo.ownership_routing.red_team
        if row.player_id == fixture.narrative_player_id
    )
    assert answer.episode_count >= 1
    assert answer.evidence_ref_count >= 1
    assert answer.contrary_claim_count == 0
    assert answer.baseline_observation_count == 2
    assert answer.baseline_move_points == pytest.approx(3.0)
    assert answer.episode_item_count >= 1
    assert answer.unique_source_count >= 1
    assert any(entry.startswith("availability:") for entry in answer.confounders)
    assert any(entry.startswith("odds:") for entry in answer.confounders)
    assert any(entry.startswith("weather:") for entry in answer.confounders)
    assert not answer.optimizer_reads_ownership
    assert "no roster would change" in answer.do_nothing_case

    assert "RED TEAM (Stage 5)" in rendered
    for label in (
        "contrary_evidence",
        "baseline_already_moved",
        "duplicate_sources",
        "confounders",
        "do_nothing",
    ):
        assert label in rendered
    assert "ownership_source=scenario_model" in rendered
    delta = next(
        row
        for row in memo.ownership_routing.applied_deltas
        if row.player_id == fixture.narrative_player_id
    )
    assert delta.episode_ids
    assert delta.evidence_refs


def _ops_config(tmp_path: Path) -> OpsConfig:
    """The smallest valid operator config, with the season/week the fixture slate uses."""

    snapshot_root = tmp_path / "snapshots"
    (snapshot_root / "2026" / "week_01").mkdir(parents=True)
    path = tmp_path / "ops.toml"
    path.write_text(
        f"""
timezone = "America/New_York"
season = 2026
monthly_llm_budget_usd = "50.00"
keychain_service = "narrative-alpha-anthropic"

[batch]
weekdays = ["wed"]
local_time = "09:30"

[paths]
database = "{tmp_path / "store.sqlite3"}"
snapshot_root = "{snapshot_root}"
nflverse_archive = "{tmp_path / "archive"}"
log_directory = "{tmp_path / "logs"}"
""".lstrip(),
        encoding="utf-8",
    )
    return load_ops_config(path)


# --------------------------------------------------------------------------------------
# fixture construction
# --------------------------------------------------------------------------------------


def _fixture(tmp_path: Path) -> RoutingFixture:
    """Seed one real slate: candidates, a narrative episode, features, and a fitted model."""

    database = tmp_path / "routing.sqlite3"
    artifacts = tmp_path / "artifacts"
    config = load_ownership_config(OWNERSHIP_CONFIG_PATH)
    players = _named_players()
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed_candidate_pool(connection, players)
        _seed_roster_history(connection, players)
        for player in players:
            _insert_baseline(
                connection,
                player.player_id,
                1,
                ownership=0.10 + (player.player_id % 5) * 0.01,
                observed_at=DATA_AT,
            )
        _seed_narrative_claim(connection)
        connection.commit()
        build_episodes(connection, as_of=FIRST_DECISION_AT, built_at=FIRST_DECISION_AT)
        connection.commit()
        build_features(
            connection,
            slate_id=1,
            site="draftkings",
            as_of=FIRST_DECISION_AT,
            built_at=FIRST_DECISION_AT,
            config_path=HEAT_CONFIG_PATH,
        )
        connection.commit()

    build_decision(
        database,
        slate_id=1,
        site="draftkings",
        decision_at=FIRST_DECISION_AT,
        artifact_directory=artifacts,
    )

    with connect_database(database) as connection:
        fit_run_id = _persist_synthetic_fit(connection, config)
        connection.commit()
        narrative_player_id = int(
            connection.execute(
                "SELECT player_id FROM players WHERE canonical_name = ?",
                (NARRATIVE_PLAYER_NAME,),
            ).fetchone()[0]
        )
    return RoutingFixture(
        database=database,
        artifacts=artifacts,
        slate_id=1,
        player_ids=tuple(player.player_id for player in players),
        narrative_player_id=narrative_player_id,
        fit_run_id=fit_run_id,
        config_sha256=config.config_sha256,
        feature_version=config.feature_version,
        model_version=config.model_version,
    )


def _named_players() -> tuple[CandidatePlayer, ...]:
    """The shared build candidate pool with names Stage 1's schema accepts."""

    players = _players()
    assert len(players) == len(CANDIDATE_NAMES)
    return tuple(
        player.model_copy(update={"name": name})
        for player, name in zip(players, CANDIDATE_NAMES, strict=True)
    )


def _seed_roster_history(
    connection: sqlite3.Connection, players: tuple[CandidatePlayer, ...]
) -> None:
    stamp = utc_timestamp(DATA_AT - timedelta(days=1))
    for player in players:
        connection.execute(
            """
            INSERT INTO player_team_history(
                player_id, team, position, roster_status, season, week, source,
                published_at, observed_at, ingested_at, effective_at, valid_from,
                valid_to, source_version, run_id
            ) VALUES (?, ?, ?, 'ACT', 2026, 1, 'fixture', NULL, ?, ?, NULL, ?, NULL,
                      'fixture-v1', NULL)
            """,
            (
                player.player_id,
                ROSTER_TEAMS[player.team],
                player.position,
                stamp,
                stamp,
                stamp,
            ),
        )


def _seed_narrative_claim(
    connection: sqlite3.Connection,
    *,
    item_key: str = "routing-item-1",
    observed_at: datetime = ITEM_AT,
    extracted_at: datetime = EXTRACTED_AT,
    source_id: str = "fixture-beat",
    seed_source: bool = True,
) -> None:
    """Run the real Stage 1 path so the episode, claim, and evidence rows are genuine."""

    stamp = utc_timestamp(DATA_AT - timedelta(days=1))
    if not seed_source:
        return _seed_narrative_item(
            connection,
            item_key=item_key,
            observed_at=observed_at,
            extracted_at=extracted_at,
            source_id=source_id,
        )
    connection.execute("INSERT INTO source_keys(source_id) VALUES (?)", (source_id,))
    connection.execute(
        """
        INSERT INTO sources(
            source_id, display_name, source_family, collector_kind, feed_url, enabled,
            source, published_at, observed_at, ingested_at, effective_at, valid_from,
            valid_to, source_version, run_id
        ) VALUES (?, ?, 'national_media', 'rss_atom', ?, 1, 'fixture', NULL, ?, ?, NULL,
                  ?, NULL, 'fixture-v1', NULL)
        """,
        (source_id, source_id, f"https://example.test/{source_id}.xml", stamp, stamp, stamp),
    )
    connection.execute(
        """
        INSERT INTO source_policies(
            source_id, permitted_use, raw_retention_days, personal_data_fields_allowed,
            must_honor_deletions, redistribution_allowed, third_party_processing_allowed,
            commercial_use_status, terms_reviewed_at, source, published_at, observed_at,
            ingested_at, effective_at, valid_from, valid_to, source_version, run_id
        ) VALUES (?, 'internal analysis', 30, '[]', 1, 0, 1, 'prohibited', ?,
                  'fixture', NULL, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (source_id, stamp, stamp, stamp, stamp),
    )
    _seed_narrative_item(
        connection,
        item_key=item_key,
        observed_at=observed_at,
        extracted_at=extracted_at,
        source_id=source_id,
    )


def _seed_narrative_item(
    connection: sqlite3.Connection,
    *,
    item_key: str,
    observed_at: datetime,
    extracted_at: datetime,
    source_id: str,
) -> None:
    """One more collected item about the same player, extracted through the real Stage 1."""

    title = f"{NARRATIVE_PLAYER_NAME} takes every first-team rep"
    # The item key rides in the body so a second collected item is a genuinely different
    # source item: `source_items` is unique on (source_id, content_sha256).
    body = (
        f"{NARRATIVE_PLAYER_NAME} worked as the clear top target in the session and the "
        f"staff said the role is his for the week. Filed as {item_key}."
    )
    canonical = normalize_item_text(title, body)
    item_stamp = utc_timestamp(observed_at)
    cursor = connection.execute(
        """
        INSERT INTO source_items(
            source_id, external_item_id, canonical_url, title, raw_content, cleaned_text,
            content_sha256, source, published_at, observed_at, ingested_at, effective_at,
            valid_from, valid_to, source_version, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL,
                  'fixture-v1', NULL)
        """,
        (
            source_id,
            item_key,
            f"https://example.test/{item_key}",
            title,
            body.encode("utf-8"),
            body,
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            source_id,
            item_stamp,
            item_stamp,
            item_stamp,
            item_stamp,
        ),
    )
    assert cursor.lastrowid is not None
    item_id = int(cursor.lastrowid)
    start = canonical.index(NARRATIVE_PLAYER_NAME)
    payload: dict[str, object] = {
        "schema_version": "stage1-extraction-v1",
        "prompt_injection_detected": False,
        "claims": [
            {
                "player_refs": [{"name_raw": NARRATIVE_PLAYER_NAME}],
                "team_refs": [],
                "claim_type": "usage",
                "claim_dimension": "role",
                "outcome_direction": "increase",
                "roster_behavior_direction": "increase",
                "evidence_class": "B",
                "evidence_basis": "beat_report",
                "falsifiable": True,
                "specificity": 0.8,
                "actionability": 0.8,
                "novelty": "new",
                "model_confidence": "high",
                "uncertainty_flags": ["none"],
                "ambiguity_flags": ["none"],
                "suggested_channels": ["mean", "ownership"],
                "disconfirming_context": None,
                "evidence_refs": [
                    {
                        "source_item_id": item_id,
                        "extract_start": start,
                        "extract_end": len(canonical),
                        "verbatim_extract": canonical[start:],
                    }
                ],
            }
        ],
    }
    report = run_extraction_batch(
        connection,
        window_start=observed_at - timedelta(seconds=1),
        window_end=observed_at + timedelta(seconds=1),
        provider=FixtureProvider(payload),
        pricing=load_batch_pricing(PRICING_PATH),
        run_at=extracted_at,
        clock=lambda: extracted_at,
    )
    assert report.claims_stored == 1


def _persist_synthetic_fit(
    connection: sqlite3.Connection,
    config: object,
    *,
    contest_archetype: str = "cash",
) -> str:
    """Store one real fit row so scenario provenance has a model to point at."""

    rows = tuple(
        OwnershipTrainingRow(
            player_id=index,
            season=2026,
            week=1 + index % 3,
            slate_id=1,
            decision_snapshot_id="decision-fixture",
            decision_at=utc_timestamp(DATA_AT),
            site="draftkings",
            contest_archetype=contest_archetype,
            role="classic",
            position="WR",
            baseline_ownership=0.10 + index * 0.005,
            h_signed_z=float(index % 5) - 2.0,
            h_dfs_z=0.0,
            h_velocity_z=0.0,
            actual_ownership=0.10 + index * 0.006,
            roster_count=100 + index,
            lineup_count=1_000,
            label_source="synthetic",
        )
        for index in range(1, 31)
    )
    model = fit_ownership_model(
        rows,
        config=config,  # type: ignore[arg-type]
        contest_archetype=contest_archetype,
        site="draftkings",
        allow_synthetic=True,
    )
    stored = persist_fit(
        connection,
        replace(model, training_weeks=((2026, 1), (2026, 2), (2026, 3))),
        TrainingData(rows=rows, missing=(), decision_snapshot_ids=()),
        config=config,  # type: ignore[arg-type]
        fitted_at=FIRST_DECISION_AT,
    )
    assert stored.run_id is not None
    return stored.run_id


def _insert_baseline(
    connection: sqlite3.Connection,
    player_id: int,
    slate_id: int,
    *,
    ownership: float,
    observed_at: datetime,
) -> None:
    stamp = utc_timestamp(observed_at)
    connection.execute(
        """
        INSERT INTO ownership_baselines(
            slate_id, player_id, site, role, ownership, source_file_sha256, source,
            published_at, observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES (?, ?, 'draftkings', 'classic', ?, ?, 'fixture-vendor', NULL, ?, ?,
                  NULL, ?, NULL, 'vendor-v1', NULL)
        """,
        (
            slate_id,
            player_id,
            ownership,
            hashlib.sha256(f"baseline-{player_id}-{stamp}".encode()).hexdigest(),
            stamp,
            stamp,
            stamp,
        ),
    )


def _baselines(
    connection: sqlite3.Connection, fixture: RoutingFixture
) -> dict[int, float]:
    """The vendor baseline each feature row cites, which a scenario set must start from."""

    rows = connection.execute(
        """
        SELECT nf.player_id, ob.ownership
        FROM narrative_features AS nf
        JOIN ownership_baselines AS ob
          ON ob.ownership_baseline_id = nf.baseline_ownership_snapshot_id
        WHERE nf.slate_id = ? AND nf.site = 'draftkings' AND nf.as_of = ?
          AND nf.feature_version = ?
        """,
        (fixture.slate_id, utc_timestamp(FIRST_DECISION_AT), fixture.feature_version),
    ).fetchall()
    return {int(row["player_id"]): float(row["ownership"]) for row in rows}


def _insert_scenario_set(
    connection: sqlite3.Connection,
    fixture: RoutingFixture,
    *,
    applied: dict[int, float],
    status: str,
    at: datetime,
    contest_archetype: str = "cash",
    model_run_id: str | None = None,
) -> str:
    """Write one governed scenario set with chosen applied values, through real triggers."""

    stamp = utc_timestamp(at)
    run_id = f"ownership-scenarios-{hashlib.sha256(stamp.encode()).hexdigest()[:16]}"
    decision_snapshot_id = str(
        connection.execute(
            """
            SELECT decision_snapshot_id FROM decision_snapshots
            WHERE slate_id = ? ORDER BY rtrim(decision_at, 'Z') LIMIT 1
            """,
            (fixture.slate_id,),
        ).fetchone()[0]
    )
    baselines = _baselines(connection, fixture)
    source_model_run_id = model_run_id or fixture.fit_run_id
    multiplier = 0.25 if status == "UNVALIDATED" else 0.50
    connection.execute(
        """
        INSERT INTO model_runs(
            run_id, run_type, started_at, completed_at, status, code_version,
            config_sha256, parent_run_id, error_message, created_at
        ) VALUES (?, 'ownership_scenarios', ?, NULL, 'running', 'test', ?, ?, NULL, ?)
        """,
        (run_id, stamp, fixture.config_sha256, source_model_run_id, stamp),
    )
    for player_id, value in sorted(applied.items()):
        baseline = baselines[player_id]
        connection.execute(
            """
            INSERT INTO ownership_scenarios(
                ownership_scenario_id, player_id, slate_id, site, contest_archetype,
                role, position, decision_snapshot_id, baseline_ownership,
                ownership_p10, ownership_p50, ownership_p90, delta_p50,
                prob_delta_positive, governance_status, status_multiplier,
                applied_ownership, calibrated_to_roster_totals, model_run_id, run_id,
                model_version, config_sha256, feature_version, source, observed_at,
                created_at
            ) VALUES (?, ?, ?, 'draftkings', ?, 'classic', 'WR', ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, 'ownership-map-laplace', ?, ?)
            """,
            (
                f"ownership-scenario-{run_id}-{player_id}",
                player_id,
                fixture.slate_id,
                contest_archetype,
                decision_snapshot_id,
                baseline,
                max(0.0, value - 0.02),
                value,
                min(1.0, value + 0.02),
                value - baseline,
                1.0 if value >= baseline else 0.0,
                status,
                multiplier,
                value,
                source_model_run_id,
                run_id,
                fixture.model_version,
                fixture.config_sha256,
                fixture.feature_version,
                stamp,
                stamp,
            ),
        )
    connection.execute(
        "UPDATE model_runs SET completed_at = ?, status = 'succeeded' WHERE run_id = ?",
        (stamp, run_id),
    )
    return run_id


def _insert_evaluation(
    connection: sqlite3.Connection,
    fixture: RoutingFixture,
    *,
    beat_baseline: bool,
    at: datetime,
    contest_archetype: str = "cash",
) -> str:
    stamp = utc_timestamp(at)
    digest = hashlib.sha256((stamp + str(beat_baseline)).encode()).hexdigest()[:16]
    run_id = f"ownership-eval-{digest}"
    model_eval_id = f"model-eval-{run_id}"
    connection.execute(
        """
        INSERT INTO model_runs(
            run_id, run_type, started_at, completed_at, status, code_version,
            config_sha256, parent_run_id, error_message, created_at
        ) VALUES (?, 'ownership_eval', ?, NULL, 'running', 'test', ?, NULL, NULL, ?)
        """,
        (run_id, stamp, fixture.config_sha256, stamp),
    )
    connection.execute(
        """
        INSERT INTO model_evals(
            model_eval_id, evaluation_kind, prompt_version_id, model_id, label_set_sha256,
            item_count, label_row_count, metrics_json, ownership_archetype, ownership_site,
            feature_version, config_sha256, report_path, beat_baseline, source,
            published_at, observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES (?, 'ownership', NULL, ?, ?, 30, 30, '{}', ?, 'draftkings', ?, ?,
                  ?, ?, 'ownership-forward-chain', NULL, ?, ?, ?, ?, NULL,
                  'ownership-eval-v1', ?)
        """,
        (
            model_eval_id,
            fixture.model_version,
            "c" * 64,
            contest_archetype,
            fixture.feature_version,
            fixture.config_sha256,
            "data/reports/ownership/cash-fixture.txt",
            int(beat_baseline),
            stamp,
            stamp,
            stamp,
            stamp,
            run_id,
        ),
    )
    connection.execute(
        "UPDATE model_runs SET completed_at = ?, status = 'succeeded' WHERE run_id = ?",
        (stamp, run_id),
    )
    return model_eval_id


def _insert_confounders(connection: sqlite3.Connection, fixture: RoutingFixture) -> None:
    """Odds, weather, and an availability row so all four confounder probes have answers."""

    game_id = int(
        connection.execute(
            "SELECT game_id FROM salaries WHERE player_id = ?",
            (fixture.narrative_player_id,),
        ).fetchone()[0]
    )
    for index, (total, spread) in enumerate(((44.5, -2.5), (47.5, -4.5))):
        stamp = utc_timestamp(DATA_AT + timedelta(hours=index))
        connection.execute(
            """
            INSERT INTO odds_snapshots(
                game_id, sportsbook, home_spread, away_spread, total,
                home_spread_price, away_spread_price, over_price, under_price,
                response_file_sha256, source, published_at, observed_at, ingested_at,
                effective_at, valid_from, valid_to, source_version, run_id
            ) VALUES (?, 'fixture-book', ?, ?, ?, -110, -110, -110, -110, ?, 'fixture',
                      NULL, ?, ?, NULL, ?, NULL, 'odds-v1', NULL)
            """,
            (
                game_id,
                spread,
                -spread,
                total,
                hashlib.sha256(f"odds-{index}".encode()).hexdigest(),
                stamp,
                stamp,
                stamp,
            ),
        )
    weather_at = utc_timestamp(DATA_AT + timedelta(hours=1))
    connection.execute(
        """
        INSERT INTO weather_snapshots(
            game_id, stadium_name, forecast_model, forecast_run_at, forecast_for_at,
            lead_time_seconds, temperature_c, precipitation_probability, wind_speed_kph,
            wind_gust_kph, weather_code, response_file_sha256, source, published_at,
            observed_at, ingested_at, effective_at, valid_from, valid_to, source_version,
            run_id
        ) VALUES (?, 'Fixture Stadium', 'fixture-model', ?, ?, 3600, 12.0, 0.4, 20.0,
                  35.0, 61, ?, 'fixture', NULL, ?, ?, NULL, ?, NULL, 'weather-v1', NULL)
        """,
        (
            game_id,
            weather_at,
            weather_at,
            hashlib.sha256(b"weather-1").hexdigest(),
            weather_at,
            weather_at,
            weather_at,
        ),
    )
    availability_at = utc_timestamp(DATA_AT + timedelta(hours=2))
    connection.execute(
        """
        INSERT INTO player_availability(
            availability_id, slate_id, player_id, season, week, site,
            availability_status, rule_id, rules_version, source_file_sha256, source,
            published_at, observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES ('availability-fixture-1', ?, ?, 2026, 1, 'draftkings', 'available',
                  'rule-official-inactives', 'fast-lane-rules-v1', ?, 'fixture', NULL,
                  ?, ?, NULL, ?, NULL, 'availability-v1', NULL)
        """,
        (
            fixture.slate_id,
            fixture.narrative_player_id,
            hashlib.sha256(b"availability-1").hexdigest(),
            availability_at,
            availability_at,
            availability_at,
        ),
    )
