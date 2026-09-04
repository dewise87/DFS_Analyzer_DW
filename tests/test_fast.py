from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from test_build import (
    DATA_AT,
    DECISION_AT,
    _players,
    _seed_database,
    _seed_showdown_database,
    _showdown_players,
)
from test_extraction import (
    PRICING_PATH,
    RUN_TIME,
    FakeProvider,
    _claim_payload,
    _seed_player,
    _seed_source_item,
)

from narrative_alpha.build import build_decision
from narrative_alpha.fast import (
    FastInactivesError,
    FastItemError,
    FastLaneCapError,
    FastLaneRuleError,
    extract_fast_item,
    load_fast_lane_rules,
    process_official_inactives,
)
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.portfolio import PydfsAdapter
from narrative_alpha.replay import read_frozen_decision, replay_decision
from narrative_alpha.store import apply_migrations, connect_database


def test_rules_refuse_expired_unsigned_and_unknown_fields(tmp_path: Path) -> None:
    expired = tmp_path / "expired.yaml"
    expired.write_text(_rules_yaml(expires_at="2026-09-12T00:00:00Z"), encoding="utf-8")
    with pytest.raises(FastLaneRuleError, match="expired"):
        load_fast_lane_rules(expired, at=DECISION_AT)

    unsigned = tmp_path / "unsigned.yaml"
    unsigned.write_text(_rules_yaml().replace('approved_by: "Fixture"\n', ""), encoding="utf-8")
    with pytest.raises(FastLaneRuleError, match="approved_by"):
        load_fast_lane_rules(unsigned, at=DECISION_AT)

    unknown = tmp_path / "unknown.yaml"
    unknown.write_text(_rules_yaml() + "surprise: true\n", encoding="utf-8")
    with pytest.raises(FastLaneRuleError, match="surprise"):
        load_fast_lane_rules(unknown, at=DECISION_AT)


def test_inactives_refreeze_only_affected_lineups_and_replay_byte_identically(
    tmp_path: Path,
) -> None:
    database = tmp_path / "fast.sqlite3"
    artifacts = tmp_path / "decisions"
    _seed_fast_build(database)
    base = build_decision(
        database,
        slate_id=1,
        site="draftkings",
        decision_at=DECISION_AT,
        artifact_directory=artifacts,
        number_of_lineups=3,
    )
    inactive = base.lineups[0].players[0]
    affected = sum(
        inactive.player_id in {player.player_id for player in lineup.players}
        for lineup in base.lineups
    )

    report = process_official_inactives(
        database,
        season=2026,
        week=1,
        site="dk",
        snapshot_root=tmp_path / "snapshots",
        text=f"{inactive.team}: {inactive.name}\n",
        artifact_directory=artifacts,
        now=DECISION_AT + timedelta(minutes=5),
    )

    with connect_database(database) as connection:
        availability = connection.execute("SELECT * FROM player_availability").fetchone()
        projection_count = connection.execute(
            "SELECT count(*) FROM projection_snapshots"
        ).fetchone()[0]
        snapshot = connection.execute(
            "SELECT decision_at FROM decision_snapshots WHERE decision_snapshot_id = ?",
            (report.decision_snapshot_id,),
        ).fetchone()
        replayed = replay_decision(
            connection,
            decision_snapshot_id=report.decision_snapshot_id,
            decision_at=DECISION_AT + timedelta(minutes=5),
            artifact_root=artifacts,
            adapter=PydfsAdapter(),
        )

    assert report.affected_lineups == affected
    assert report.portfolio_lineups == len(base.lineups)
    assert replayed.report.lineup_count == len(base.lineups)
    with connect_database(database) as connection:
        frozen = read_frozen_decision(
            connection,
            decision_snapshot_id=report.decision_snapshot_id,
            decision_at=DECISION_AT + timedelta(minutes=5),
            artifact_root=artifacts,
        )
    untouched = [lineup for lineup in base.lineups if lineup not in frozen.lineups]
    assert len(untouched) == affected, "every unaffected lineup is pinned verbatim"
    assert len({lineup.lineup_id for lineup in frozen.lineups}) == len(base.lineups)
    assert all(
        inactive.player_id not in {p.player_id for p in lineup.players} for lineup in frozen.lineups
    )
    captured = list((tmp_path / "snapshots").glob("2026/week_01/*/inactives/*"))
    assert len(captured) == 1
    assert inactive.name in {name for diff in report.diffs for name in diff.out}
    assert report.upload_csv_path.read_bytes() == replayed.output_bytes
    assert replayed.report.output_matches
    assert frozen.contest_policy.sha256 == base.contest_policy.sha256
    assert frozen.request.objective == base.request.objective
    assert frozen.request.ownership_sum_range == base.request.ownership_sum_range
    assert frozen.request.lineup_uniqueness == base.request.lineup_uniqueness
    # The cash policy caps exposure at 1.0, which constrains nothing and so puts nothing
    # in the request — the re-freeze carries that emptiness exactly as the base did.
    assert frozen.request.player_exposure_ranges == ()
    assert snapshot is not None
    assert availability["player_id"] == inactive.player_id
    assert availability["availability_status"] == "unavailable"
    assert availability["effective_at"] == availability["observed_at"]
    assert availability["rule_id"] == "official-inactives-v1"
    assert projection_count == len(_players())


def test_showdown_inactives_pin_unaffected_lineups_and_replace_the_rest(
    tmp_path: Path,
) -> None:
    database = tmp_path / "fast-showdown.sqlite3"
    artifacts = tmp_path / "decisions"
    _seed_fast_showdown_build(database)
    base = build_decision(
        database,
        slate_id=1,
        site="draftkings",
        decision_at=DECISION_AT,
        artifact_directory=artifacts,
        number_of_lineups=3,
        contest_archetype="showdown",
    )
    inactive = min(base.lineups[0].players, key=lambda player: player.projection)
    unaffected = tuple(
        lineup
        for lineup in base.lineups
        if inactive.player_id not in {player.player_id for player in lineup.players}
    )

    report = process_official_inactives(
        database,
        season=2026,
        week=1,
        site="dk",
        snapshot_root=tmp_path / "snapshots",
        text=f"{inactive.team}: {inactive.name}\n",
        artifact_directory=artifacts,
        now=DECISION_AT + timedelta(minutes=5),
    )

    with connect_database(database) as connection:
        frozen = read_frozen_decision(
            connection,
            decision_snapshot_id=report.decision_snapshot_id,
            decision_at=DECISION_AT + timedelta(minutes=5),
            artifact_root=artifacts,
        )
        replayed = replay_decision(
            connection,
            decision_snapshot_id=report.decision_snapshot_id,
            decision_at=DECISION_AT + timedelta(minutes=5),
            artifact_root=artifacts,
            adapter=PydfsAdapter(),
        )

    assert frozen.request.contest_archetype.value == "showdown"
    assert frozen.lineups[: len(unaffected)] == unaffected
    assert report.affected_lineups == len(base.lineups) - len(unaffected)
    assert all(
        inactive.player_id not in {player.player_id for player in lineup.players}
        for lineup in frozen.lineups
    )
    assert replayed.report.output_matches
    assert report.upload_csv_path.read_bytes() == replayed.output_bytes
    assert any(
        entry.startswith(("CPT ", "FLEX "))
        for diff in report.diffs
        for entry in (*diff.out, *diff.in_)
    )


def test_unresolved_inactive_refuses_whole_command_and_names_queue(tmp_path: Path) -> None:
    database = tmp_path / "fast.sqlite3"
    artifacts = tmp_path / "decisions"
    _seed_fast_build(database)
    build_decision(
        database,
        slate_id=1,
        site="draftkings",
        decision_at=DECISION_AT,
        artifact_directory=artifacts,
    )

    with pytest.raises(FastInactivesError, match=r"(?s)unresolved queue.*na-crosswalk resolve"):
        process_official_inactives(
            database,
            season=2026,
            week=1,
            site="dk",
            snapshot_root=tmp_path / "snapshots",
            text="AAA: Mystery Player\n",
            artifact_directory=artifacts,
            now=DECISION_AT + timedelta(minutes=5),
        )

    with connect_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM player_availability").fetchone()[0] == 0
        queued = connection.execute(
            "SELECT name_raw, status FROM unresolved_player_matches"
        ).fetchone()
    assert queued is not None and queued["status"] == "pending"


def test_mean_cap_refuses_before_a_new_decision_is_frozen(tmp_path: Path) -> None:
    database = tmp_path / "fast.sqlite3"
    artifacts = tmp_path / "decisions"
    rules = tmp_path / "rules.yaml"
    rules.write_text(_rules_yaml(mean_cap=0.0), encoding="utf-8")
    _seed_fast_build(database)
    base = build_decision(
        database,
        slate_id=1,
        site="draftkings",
        decision_at=DECISION_AT,
        artifact_directory=artifacts,
    )
    inactive = base.lineups[0].players[0]

    with pytest.raises(FastLaneCapError, match=r"above rule.*human must confirm"):
        process_official_inactives(
            database,
            season=2026,
            week=1,
            site="dk",
            snapshot_root=tmp_path / "snapshots",
            text=inactive.name,
            artifact_directory=artifacts,
            rules_path=rules,
            now=DECISION_AT + timedelta(minutes=5),
        )

    with connect_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM decision_snapshots").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM player_availability").fetchone()[0] == 0
        run = connection.execute(
            "SELECT status, error_message FROM model_runs "
            "WHERE run_type = 'fast_official_inactives'"
        ).fetchone()
    assert run is not None and run["status"] == "failed"
    assert "human must confirm" in str(run["error_message"])


def test_fast_item_refuses_a_b_graded_source_without_calling_provider(tmp_path: Path) -> None:
    database = tmp_path / "item.sqlite3"
    catalog = tmp_path / "catalog.toml"
    catalog.write_text(_catalog("B"), encoding="utf-8")
    with connect_database(database) as connection:
        apply_migrations(connection)
        item_id = _seed_source_item(connection)

    with pytest.raises(FastItemError, match="graded B, not A"):
        extract_fast_item(
            database,
            source_item_id=item_id,
            provider=_NeverProvider(),
            catalog_path=catalog,
            pricing_path=PRICING_PATH,
            now=RUN_TIME,
        )


def test_fast_item_uses_usual_stage1_tables_and_is_tagged_fast(tmp_path: Path) -> None:
    database = tmp_path / "item.sqlite3"
    catalog = tmp_path / "catalog.toml"
    catalog.write_text(_catalog("A"), encoding="utf-8")
    title = "WAS role update"
    body = "Jordan Reed will start and see expanded routes for WAS."
    with connect_database(database) as connection:
        apply_migrations(connection)
        item_id = _seed_source_item(connection, title=title, body=body)
        _seed_player(connection, "Jordan Reed", "WAS", position="TE")
    source_text = f"{title}\n\n{body}"
    provider = FakeProvider(_claim_payload(item_id, source_text, name="Jordan Reed"))

    report = extract_fast_item(
        database,
        url="https://example.test/item",
        provider=provider,
        catalog_path=catalog,
        pricing_path=PRICING_PATH,
        now=RUN_TIME,
    )

    with connect_database(database) as connection:
        run = connection.execute(
            "SELECT run_type, status FROM model_runs WHERE run_id = ?", (report.run_id,)
        ).fetchone()
        claims = connection.execute("SELECT count(*) FROM claims").fetchone()[0]
    assert report.players == ("Jordan Reed",)
    assert report.claims[0].claim_type == "usage"
    assert run is not None and tuple(run) == ("stage_1_extraction_fast", "succeeded")
    assert claims == 1


def test_a_second_wave_sees_the_whole_portfolio(tmp_path: Path) -> None:
    """The first re-freeze keeps every lineup, so a later inactive in an untouched one is found."""

    database = tmp_path / "fast.sqlite3"
    artifacts = tmp_path / "decisions"
    _seed_fast_build(database)
    base = build_decision(
        database,
        slate_id=1,
        site="draftkings",
        decision_at=DECISION_AT,
        artifact_directory=artifacts,
        number_of_lineups=3,
    )
    first = base.lineups[0].players[0]
    untouched = next(
        lineup
        for lineup in base.lineups
        if first.player_id not in {player.player_id for player in lineup.players}
    )
    second = next(
        player
        for player in untouched.players
        if player.player_id not in {p.player_id for p in base.lineups[0].players}
    )

    process_official_inactives(
        database,
        season=2026,
        week=1,
        site="dk",
        snapshot_root=tmp_path / "snapshots",
        text=f"{first.team}: {first.name}\n",
        artifact_directory=artifacts,
        now=DECISION_AT + timedelta(minutes=5),
    )
    report = process_official_inactives(
        database,
        season=2026,
        week=1,
        site="dk",
        snapshot_root=tmp_path / "snapshots",
        text=f"{second.position} {second.name} ({second.team}) \u2014 OUT (illness)\n",
        artifact_directory=artifacts,
        now=DECISION_AT + timedelta(minutes=10),
    )

    assert report.affected_lineups >= 1
    assert report.portfolio_lineups == 3
    assert second.name in {name for diff in report.diffs for name in diff.out}
    with connect_database(database) as connection:
        frozen = read_frozen_decision(
            connection,
            decision_snapshot_id=report.decision_snapshot_id,
            decision_at=DECISION_AT + timedelta(minutes=10),
            artifact_root=artifacts,
        )
    gone = {first.player_id, second.player_id}
    assert all(not gone & {p.player_id for p in lineup.players} for lineup in frozen.lineups)
    assert len({lineup.lineup_id for lineup in frozen.lineups}) == 3


def test_an_availability_row_does_not_change_an_earlier_decision(tmp_path: Path) -> None:
    """The Wed-Fri lane's byte-identity: a snapshot frozen before availability existed
    replays the same after an availability row observed before its cutoff appears."""

    database = tmp_path / "fast.sqlite3"
    artifacts = tmp_path / "decisions"
    _seed_fast_build(database)
    base = build_decision(
        database,
        slate_id=1,
        site="draftkings",
        decision_at=DECISION_AT,
        artifact_directory=artifacts,
        number_of_lineups=2,
    )
    player = base.lineups[0].players[0]
    stamp = utc_timestamp(DATA_AT)
    with connect_database(database) as connection:
        connection.execute(
            "INSERT INTO player_availability(availability_id, slate_id, player_id, season, "
            "week, site, availability_status, rule_id, rules_version, source_file_sha256, "
            "source, published_at, observed_at, ingested_at, effective_at, valid_from, "
            "valid_to, source_version, run_id) VALUES (?, 1, ?, 2026, 1, 'draftkings', "
            "'unavailable', 'official-inactives-v1', 'fixture-v1', ?, 'fixture', NULL, ?, ?, "
            "?, ?, NULL, NULL, NULL)",
            (
                "availability-" + "f" * 55,
                player.player_id,
                "e" * 64,
                stamp,
                stamp,
                stamp,
                stamp,
            ),
        )
        connection.commit()
        replayed = replay_decision(
            connection,
            decision_snapshot_id=base.snapshot.decision_snapshot_id,
            decision_at=DECISION_AT,
            artifact_root=artifacts,
            adapter=PydfsAdapter(),
        )

    assert replayed.report.output_matches
    assert replayed.lineups == base.lineups
    assert not any(
        item.artifact_kind == "availability" for item in base.snapshot.manifest_hashes_json
    )


def test_fast_item_refuses_when_the_month_is_over_budget(tmp_path: Path) -> None:
    database = tmp_path / "item.sqlite3"
    catalog = tmp_path / "catalog.toml"
    catalog.write_text(_catalog("A"), encoding="utf-8")
    rules = tmp_path / "rules.yaml"
    rules.write_text(_rules_yaml(expires_at="2027-01-01T00:00:00Z"), encoding="utf-8")
    with connect_database(database) as connection:
        apply_migrations(connection)
        item_id = _seed_source_item(connection)

    with pytest.raises(FastItemError, match="budget guard refused"):
        extract_fast_item(
            database,
            source_item_id=item_id,
            provider=_NeverProvider(),
            catalog_path=catalog,
            pricing_path=PRICING_PATH,
            rules_path=rules,
            monthly_budget_nanos=0,
            now=RUN_TIME,
        )


def test_fast_item_refuses_under_an_expired_rule_set(tmp_path: Path) -> None:
    database = tmp_path / "item.sqlite3"
    catalog = tmp_path / "catalog.toml"
    catalog.write_text(_catalog("A"), encoding="utf-8")
    rules = tmp_path / "rules.yaml"
    rules.write_text(_rules_yaml(expires_at="2026-09-01T12:00:00Z"), encoding="utf-8")
    with connect_database(database) as connection:
        apply_migrations(connection)
        item_id = _seed_source_item(connection)

    with pytest.raises(FastLaneRuleError, match="expired"):
        extract_fast_item(
            database,
            source_item_id=item_id,
            provider=_NeverProvider(),
            catalog_path=catalog,
            pricing_path=PRICING_PATH,
            rules_path=rules,
            now=RUN_TIME,
        )


def _seed_fast_build(database: Path) -> None:
    _seed_database(database)
    with connect_database(database) as connection:
        for player in _players():
            stamp = DATA_AT.isoformat(timespec="microseconds").replace("+00:00", "Z")
            connection.execute(
                "INSERT INTO player_team_history(player_id, team, position, roster_status, "
                "season, week, source, published_at, observed_at, ingested_at, effective_at, "
                "valid_from, valid_to, source_version, run_id) "
                "VALUES (?, ?, ?, 'ACT', 2026, 1, 'fixture', NULL, ?, ?, NULL, ?, NULL, "
                "'fixture-v1', NULL)",
                (player.player_id, player.team, player.position, stamp, stamp, stamp),
            )


def _seed_fast_showdown_build(database: Path) -> None:
    _seed_showdown_database(database, player_count=8)
    with connect_database(database) as connection:
        stamp = DATA_AT.isoformat(timespec="microseconds").replace("+00:00", "Z")
        for player in _showdown_players(player_count=8):
            connection.execute(
                "INSERT INTO player_team_history(player_id, team, position, roster_status, "
                "season, week, source, published_at, observed_at, ingested_at, effective_at, "
                "valid_from, valid_to, source_version, run_id) "
                "VALUES (?, ?, ?, 'ACT', 2026, 1, 'fixture', NULL, ?, ?, NULL, ?, NULL, "
                "'fixture-v1', NULL)",
                (player.player_id, player.team, player.position, stamp, stamp, stamp),
            )


class _NeverProvider:
    model_id = "claude-haiku-4-5-20251001"
    max_output_tokens = 4096

    def submit_batch(self, requests: object) -> object:
        raise AssertionError("B-graded source must be refused before provider use")

    def retrieve_batch(self, requests: object, submission: object) -> object:
        raise AssertionError("B-graded source must be refused before provider use")


def _rules_yaml(
    *,
    expires_at: str = "2026-09-30T00:00:00Z",
    mean_cap: float = 5.0,
) -> str:
    return f'''rules_version: "fixture-v1"
approved_at: "2026-09-01T00:00:00Z"
expires_at: "{expires_at}"
approved_by: "Fixture"
rules:
  - rule_id: "official-inactives-v1"
    trigger_source_class: "official_inactive_list"
    claim_type: "availability"
    max_automatic_adjustment:
      availability: 1.0
      mean: {mean_cap}
      shape: 0.0
      dependence: 0.0
      ownership: 0.0
    expires_at: "{expires_at}"
'''


def _catalog(grade: str) -> str:
    return f'''[policy_tiers.fixture]
permitted_use = "internal"
raw_retention_days = 30
personal_data_fields_allowed = []
must_honor_deletions = true
redistribution_allowed = false
third_party_processing_allowed = true
commercial_use_status = "prohibited"

[[sources]]
source_id = "source-a"
display_name = "Fixture"
source_family = "official_team"
collector_kind = "rss_atom"
feed_url = "https://example.test/feed.xml"
policy_tier = "fixture"
grade = "{grade}"
'''
