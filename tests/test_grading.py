from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from narrative_alpha.grading import (
    build_source_credibility_report,
    grade_availability_claim,
    grade_ownership_claim,
    grade_usage_claim,
    grade_week,
    load_grading_config,
    render_source_credibility_report,
)
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.report_cli import main as report_main
from narrative_alpha.store import apply_migrations, connect_database

LOCK = datetime(2026, 9, 13, 17, tzinfo=UTC)
GRADED = datetime(2026, 9, 15, 12, tzinfo=UTC)
CONFIG = Path("config/claim_grading.toml")


@pytest.mark.parametrize(
    ("direction", "status", "stat_line", "points", "expected"),
    (
        ("increase", "available", {"played": True}, 10.0, "correct"),
        ("decrease", "unavailable", {"dnp": True}, 0.0, "correct"),
        ("increase", "unavailable", {"dnp": True}, 0.0, "incorrect"),
        ("decrease", "available", {"played": True}, 10.0, "incorrect"),
        ("increase", None, {}, 0.0, "indeterminate"),
    ),
)
def test_availability_rule_both_directions_and_indeterminate(
    direction: str,
    status: str | None,
    stat_line: object,
    points: float,
    expected: str,
) -> None:
    verdict = grade_availability_claim(  # type: ignore[arg-type]
        direction,
        availability_status=status,
        stat_line=stat_line,
        fantasy_points=points,
    )
    assert verdict.verdict == expected


@pytest.mark.parametrize(
    ("direction", "stat_line", "expected"),
    (
        ("increase", {"snap_share": 0.80, "snap_share_baseline": 0.50}, "correct"),
        ("decrease", {"snap_share": 0.80, "snap_share_baseline": 0.50}, "incorrect"),
        ("decrease", {"snap_share": 0.20, "snap_share_baseline": 0.50}, "correct"),
        ("increase", {"snap_share": 0.20, "snap_share_baseline": 0.50}, "incorrect"),
        ("increase", {"snap_share": 0.51, "snap_share_baseline": 0.50}, "incorrect"),
        ("increase", {"snap_share": 0.80}, "ungradable"),
        ("increase", {}, "ungradable"),
        ("increase", {"contest_id": "c", "roster_position": "WR"}, "ungradable"),
    ),
)
def test_usage_rule_both_directions_and_indeterminate(
    direction: str, stat_line: object, expected: str
) -> None:
    rule = load_grading_config(CONFIG).config.rules.usage
    verdict = grade_usage_claim(  # type: ignore[arg-type]
        direction,
        "snap_share",
        stat_line=stat_line,
        rule=rule,
    )
    assert verdict.verdict == expected


@pytest.mark.parametrize(
    ("direction", "actual", "baseline", "expected"),
    (
        ("increase", 0.20, 0.10, "correct"),
        ("decrease", 0.20, 0.10, "incorrect"),
        ("decrease", 0.10, 0.20, "correct"),
        ("increase", 0.10, 0.20, "incorrect"),
        ("increase", None, 0.20, "indeterminate"),
    ),
)
def test_ownership_rule_both_directions_and_indeterminate(
    direction: str,
    actual: float | None,
    baseline: float | None,
    expected: str,
) -> None:
    verdict = grade_ownership_claim(  # type: ignore[arg-type]
        direction,
        actual_ownership=actual,
        baseline_ownership=baseline,
        neutral_threshold=0.01,
    )
    assert verdict.verdict == expected


def test_unfalsifiable_never_scores_posterior_shrinks_and_ledger_appends(
    tmp_path: Path,
) -> None:
    database = tmp_path / "grading.sqlite3"
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed_world(connection)
        _seed_claim(connection, "usage-up", "usage", "snap_share", "increase", True)
        _seed_claim(connection, "usage-up-2", "usage", "snap_share", "increase", True)
        _seed_claim(connection, "vague", "narrative", "mean", "increase", False)
        _seed_claim(connection, "no-rule", "health", "health", "increase", True)
        _seed_claim(
            connection, "available", "availability", "active_status", "increase", True
        )
        _seed_claim(
            connection,
            "ownership-up",
            "field_propagation",
            "ownership",
            "increase",
            True,
        )
        connection.commit()

        first = grade_week(
            connection,
            season=2026,
            week=1,
            site="draftkings",
            grading_run_id="grade-run-1",
            graded_at=GRADED,
        )
        later = utc_timestamp(GRADED)
        connection.execute(
            """
            INSERT INTO results(
                result_id, game_id, player_id, site, fantasy_points, stat_line_json,
                source_file_sha256, source, published_at, observed_at, ingested_at,
                effective_at, valid_from, valid_to, source_version, run_id
            ) VALUES (2, 1, 1, 'draftkings', 99.0, ?, ?, 'later-results', NULL, ?, ?,
                      NULL, ?, NULL, 'results-v2', NULL)
            """,
            (
                json.dumps({"active": True, "snap_share": 0.80, "snap_share_baseline": 0.50}),
                "9" * 64,
                later,
                later,
                later,
            ),
        )
        second = grade_week(
            connection,
            season=2026,
            week=1,
            site="draftkings",
            grading_run_id="grade-run-2",
            graded_at=GRADED,
        )

        vague = connection.execute(
            """
            SELECT verdict, rule_id, result_id, reason
            FROM claim_grades
            WHERE claim_id = 'vague'
            ORDER BY rowid
            """
        ).fetchall()
        no_rule = connection.execute(
            "SELECT verdict, rule_id, reason FROM claim_grades WHERE claim_id = 'no-rule'"
        ).fetchall()
        availability = connection.execute(
            """
            SELECT verdict, result_id, availability_id
            FROM claim_grades WHERE claim_id = 'available' ORDER BY rowid
            """
        ).fetchall()
        ownership = connection.execute(
            """
            SELECT verdict, actual_ownership_id, ownership_baseline_id, result_id
            FROM claim_grades WHERE claim_id = 'ownership-up' ORDER BY rowid
            """
        ).fetchall()
        ledger_count = int(
            connection.execute(
                """
                SELECT count(*) FROM source_credibility
                WHERE claim_type = 'usage' AND claim_dimension = 'snap_share'
                """
            ).fetchone()[0]
        )
        report = build_source_credibility_report(connection, season=2026, week=1)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE claim_grades SET verdict = 'incorrect'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE source_credibility SET n_graded = 99")

    # The second run sees a new result row, so five grades change; the ownership grade
    # is identical (same actual row, same verdict) and is recognised as a repeat.
    assert first.grades_inserted == 6
    assert second.grades_inserted == 5
    assert [row["verdict"] for row in vague] == ["ungradable", "ungradable"]
    assert all(row["rule_id"] is None for row in vague)
    assert [row["result_id"] for row in vague] == [1, 2]
    assert all("performance is ignored" in row["reason"] for row in vague)
    assert [row["verdict"] for row in no_rule] == ["ungradable", "ungradable"]
    assert all(row["rule_id"] is None for row in no_rule)
    assert all("no configured rule" in row["reason"] for row in no_rule)
    assert [tuple(row) for row in availability] == [
        ("correct", 1, None),
        ("correct", 2, None),
    ]
    assert [tuple(row) for row in ownership] == [("correct", 1, 1, None)]
    assert ledger_count == 2
    assert report.grading_run_id == "grade-run-2"
    usage = next(row for row in report.rows if row.claim_type == "usage")
    assert usage.n_graded == 2
    assert usage.precision == 1.0
    # Two correct grades observed two hours before lock and graded two days after it:
    # (1 + 2w) / (2 + 2w) with w = 0.5 ** (age_days / 42).
    age_days = (GRADED - (LOCK - timedelta(hours=2))).total_seconds() / 86400.0
    assert usage.posterior_mean == pytest.approx(0.75, abs=0.01)
    assert usage.weighted_correct == pytest.approx(2 * 0.5 ** (age_days / 42), abs=1e-6)
    assert usage.interval_low < 0.4
    assert usage.interval_high > 0.95
    rendered = render_source_credibility_report(report)
    assert "accuracy posterior 0.74" in rendered and "n=2  90% interval" in rendered
    assert "SOURCE x CLAIM TYPE" in rendered and "fixture-source | usage" in rendered
    assert "raw accuracy 1.000 (unshrunk, no interval" in rendered


def test_sources_cli_renders_an_empty_week(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "empty.sqlite3"
    with connect_database(database) as connection:
        apply_migrations(connection)

    exit_code = report_main(
        [
            "sources",
            "--database",
            str(database),
            "--season",
            "2026",
            "--week",
            "1",
        ]
    )

    assert exit_code == 0
    assert "no ledger snapshot exists for this week" in capsys.readouterr().out


def test_availability_is_graded_against_post_lock_facts_only() -> None:
    """A Saturday 'available' that becomes a Sunday scratch: the pre-lock row is not the
    outcome, and with no post-lock fact the grade is indeterminate, never 'correct'."""

    production_stat_line = {"contest_id": "contest-1", "roster_position": "WR"}
    no_post_lock = grade_availability_claim(
        "increase",
        availability_status=None,
        stat_line=production_stat_line,
        fantasy_points=0.0,
        pre_lock_status="available",
    )
    assert no_post_lock.verdict == "indeterminate"
    assert "pre-lock status is not an outcome" in no_post_lock.reason
    assert no_post_lock.outcome["pre_lock_official_availability"] == "available"

    scratched = grade_availability_claim(
        "decrease",
        availability_status="unavailable",
        stat_line=production_stat_line,
        fantasy_points=0.0,
        pre_lock_status="available",
    )
    assert scratched.verdict == "correct"
    wrong_call = grade_availability_claim(
        "increase",
        availability_status="unavailable",
        stat_line=None,
        fantasy_points=None,
        pre_lock_status="available",
    )
    assert wrong_call.verdict == "incorrect"


def test_a_claim_observed_after_lock_is_counted_and_not_graded(tmp_path: Path) -> None:
    database = tmp_path / "grading.sqlite3"
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed_world(connection)
        _seed_claim(
            connection, "before", "availability", "active_status", "increase", True
        )
        _seed_claim(
            connection,
            "after",
            "availability",
            "active_status",
            "increase",
            True,
            observed_at=LOCK + timedelta(minutes=10),
        )
        connection.commit()
        report = grade_week(
            connection,
            season=2026,
            week=1,
            site="draftkings",
            grading_run_id="grade-run-post-lock",
            graded_at=GRADED,
        )
        graded = connection.execute("SELECT DISTINCT claim_id FROM claim_grades").fetchall()

    assert report.claims_excluded_post_lock == 1
    assert [row["claim_id"] for row in graded] == ["before"]


def test_an_identical_regrade_inserts_nothing(tmp_path: Path) -> None:
    database = tmp_path / "grading.sqlite3"
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed_world(connection)
        _seed_claim(connection, "usage-up", "usage", "snap_share", "increase", True)
        connection.commit()
        first = grade_week(
            connection, season=2026, week=1, site="draftkings",
            grading_run_id="grade-run-a", graded_at=GRADED,
        )
        second = grade_week(
            connection, season=2026, week=1, site="draftkings",
            grading_run_id="grade-run-b", graded_at=GRADED + timedelta(hours=1),
        )
        ledger_rows = connection.execute("SELECT count(*) FROM source_credibility").fetchone()[0]

    assert first.grades_inserted == 1
    assert second.grades_inserted == 0
    assert ledger_rows == 2


def test_the_posterior_is_time_decayed() -> None:
    from narrative_alpha.grading.core import posterior_from_weights

    fresh_mean, _, _ = posterior_from_weights(1.0, 1.0, 1.0, 1.0, interval_mass=0.9)
    old_correct_mean, _, _ = posterior_from_weights(1.0, 1.0, 0.25, 1.0, interval_mass=0.9)
    old_incorrect_mean, _, _ = posterior_from_weights(1.0, 1.0, 1.0, 0.25, interval_mass=0.9)
    assert fresh_mean == pytest.approx(0.5)
    assert old_correct_mean < 0.5 < old_incorrect_mean


def _seed_world(connection: sqlite3.Connection) -> None:
    base = LOCK - timedelta(days=2)
    stamp = utc_timestamp(base)
    connection.execute("INSERT INTO source_keys(source_id) VALUES ('fixture-source')")
    connection.execute(
        """
        INSERT INTO sources(
            source_id, display_name, source_family, collector_kind, feed_url, enabled,
            source, published_at, observed_at, ingested_at, effective_at, valid_from,
            valid_to, source_version, run_id
        ) VALUES ('fixture-source', 'Fixture', 'national_media', 'rss_atom',
                  'https://example.test/feed.xml', 1, 'fixture', NULL, ?, ?, NULL, ?,
                  NULL, 'fixture-v1', NULL)
        """,
        (stamp, stamp, stamp),
    )
    policy = connection.execute(
        """
        INSERT INTO source_policies(
            source_id, permitted_use, raw_retention_days, personal_data_fields_allowed,
            must_honor_deletions, redistribution_allowed, third_party_processing_allowed,
            commercial_use_status, terms_reviewed_at, source, published_at, observed_at,
            ingested_at, effective_at, valid_from, valid_to, source_version, run_id
        ) VALUES ('fixture-source', 'internal', 30, '[]', 1, 0, 1, 'prohibited', ?,
                  'fixture', NULL, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (stamp, stamp, stamp, stamp),
    )
    assert policy.lastrowid is not None
    connection.execute(
        """
        INSERT INTO teams(
            team_id, team_key, abbreviation, canonical_name, league, source, published_at,
            observed_at, ingested_at, effective_at, valid_from, valid_to, source_version,
            run_id
        ) VALUES (1, 'ABC', 'ABC', 'Team ABC', 'NFL', 'fixture', NULL, ?, ?, NULL, ?,
                  NULL, 'fixture-v1', NULL),
                 (2, 'XYZ', 'XYZ', 'Team XYZ', 'NFL', 'fixture', NULL, ?, ?, NULL, ?,
                  NULL, 'fixture-v1', NULL)
        """,
        (stamp, stamp, stamp, stamp, stamp, stamp),
    )
    connection.execute(
        """
        INSERT INTO players(
            player_id, player_key, canonical_name, position, birth_date, source,
            published_at, observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES (1, 'player-1', 'Example Player', 'WR', NULL, 'fixture', NULL, ?, ?, NULL,
                  ?, NULL, 'fixture-v1', NULL)
        """,
        (stamp, stamp, stamp),
    )
    connection.execute(
        """
        INSERT INTO games(
            game_id, external_game_id, season, week, kickoff_at, home_team_id, away_team_id,
            stadium_name, game_status, source, published_at, observed_at, ingested_at,
            effective_at, valid_from, valid_to, source_version, run_id
        ) VALUES (1, 'game-1', 2026, 1, ?, 1, 2, NULL, 'final', 'fixture', NULL, ?, ?,
                  NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (utc_timestamp(LOCK), stamp, stamp, stamp),
    )
    connection.execute(
        """
        INSERT INTO slates(
            slate_id, external_slate_id, site, slate_type, season, week, name, starts_at,
            locks_at, source, published_at, observed_at, ingested_at, effective_at,
            valid_from, valid_to, source_version, run_id
        ) VALUES (1, 'slate-1', 'draftkings', 'classic', 2026, 1, 'Main', ?, ?,
                  'fixture', NULL, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (utc_timestamp(LOCK), utc_timestamp(LOCK), stamp, stamp, stamp),
    )
    connection.execute(
        """
        INSERT INTO salaries(
            slate_id, player_id, game_id, team_id, opponent_team_id, site_player_id,
            roster_positions_json, salary, player_status, source_file_sha256, source,
            published_at, observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES (1, 1, 1, 1, 2, 'p1', '["WR"]', 5000, NULL, ?, 'fixture', NULL,
                  ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        ("a" * 64, stamp, stamp, stamp),
    )
    connection.execute(
        """
        INSERT INTO decision_snapshots(
            decision_snapshot_id, slate_id, decision_at, created_at, manifest_schema_version,
            manifest_hashes_json, manifest_hash_set_sha256, run_id, note
        ) VALUES ('decision-1', 1, ?, ?, '1.0', '[]', ?, NULL, NULL)
        """,
        (utc_timestamp(LOCK - timedelta(minutes=5)),) * 2 + ("b" * 64,),
    )
    connection.execute(
        """
        INSERT INTO player_availability(
            availability_id, slate_id, player_id, season, week, site,
            availability_status, rule_id, rules_version, source_file_sha256, source,
            published_at, observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES ('availability-1', 1, 1, 2026, 1, 'draftkings', 'available',
                  'official-active', 'v1', ?, 'official', NULL, ?, ?, NULL, ?, NULL,
                  'official-v1', NULL)
        """,
        ("8" * 64, stamp, stamp, stamp),
    )
    connection.execute(
        """
        INSERT INTO ownership_baselines(
            ownership_baseline_id, slate_id, player_id, site, role, ownership,
            source_file_sha256, source, published_at, observed_at, ingested_at,
            effective_at, valid_from, valid_to, source_version, run_id
        ) VALUES (1, 1, 1, 'draftkings', 'classic', 0.10, ?, 'vendor', NULL, ?, ?,
                  NULL, ?, NULL, 'vendor-v1', NULL)
        """,
        ("7" * 64, stamp, stamp, stamp),
    )
    actual_at = utc_timestamp(GRADED - timedelta(minutes=30))
    connection.execute(
        """
        INSERT INTO actual_ownership(
            actual_ownership_id, external_contest_id, site, slate_id, contest_archetype,
            field_size, entry_limit, entry_fee_cents, payout_curve_id, player_id, role,
            lineup_count, roster_count, actual_ownership, source_file_sha256, source,
            published_at, observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES (1, 'contest-1', 'draftkings', 1, 'single_entry', 10, 1, 100, NULL,
                  1, 'classic', 10, 2, 0.20, ?, 'standings', NULL, ?, ?, NULL, ?, NULL,
                  'standings-v1', NULL)
        """,
        ("6" * 64, actual_at, actual_at, actual_at),
    )
    connection.execute(
        """
        INSERT INTO results(
            result_id, game_id, player_id, site, fantasy_points, stat_line_json,
            source_file_sha256, source, published_at, observed_at, ingested_at, effective_at,
            valid_from, valid_to, source_version, run_id
        ) VALUES (1, 1, 1, 'draftkings', 40.0, ?, ?, 'results', NULL, ?, ?, NULL, ?,
                  NULL, 'results-v1', NULL)
        """,
        (
            json.dumps({"active": True, "snap_share": 0.80, "snap_share_baseline": 0.50}),
            "c" * 64,
            utc_timestamp(GRADED - timedelta(hours=1)),
            utc_timestamp(GRADED - timedelta(hours=1)),
            utc_timestamp(GRADED - timedelta(hours=1)),
        ),
    )
    item = connection.execute(
        """
        INSERT INTO source_items(
            source_id, external_item_id, canonical_url, title, raw_content, cleaned_text,
            content_sha256, source, published_at, observed_at, ingested_at, effective_at,
            valid_from, valid_to, source_version, run_id
        ) VALUES ('fixture-source', 'item-1', NULL, 'Claims', X'636c61696d73', 'claims', ?,
                  'fixture-source', ?, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        ("d" * 64,) + (utc_timestamp(LOCK - timedelta(hours=2)),) * 4,
    )
    assert item.lastrowid is not None
    connection.execute(
        """
        INSERT INTO prompt_versions(
            prompt_version_id, stage, schema_version, system_prompt, user_prompt_template,
            output_schema_json, prompt_sha256, created_at, source, published_at, observed_at,
            ingested_at, effective_at, valid_from, valid_to, source_version, run_id
        ) VALUES ('prompt-1', 'stage_1_extraction', 'v1', 'system', 'user', '{}', ?, ?,
                  'fixture', NULL, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        ("e" * 64,) + (stamp,) * 4,
    )
    connection.execute(
        """
        INSERT INTO source_item_extractions(
            extraction_id, source_item_id, source_policy_id, source_family,
            source_content_sha256, prompt_version_id, model_id, max_output_tokens,
            request_sha256, provider_request_id, batch_submission_request_id,
            provider_batch_id, provider_custom_id, provider_message_id, status, output_json,
            output_sha256, output_redacted_at, input_tokens, output_tokens, cost_nanos_usd,
            pricing_version, pricing_effective_at, pricing_source_url, input_nanos_per_token,
            output_nanos_per_token, latency_ms, error_code, error_message, source,
            published_at, observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES ('extract-1', ?, ?, 'national_media', ?, 'prompt-1', 'fixture-model', 100,
                  ?, NULL, NULL, NULL, NULL, NULL, 'creating', NULL,
                  NULL, NULL, NULL, NULL, NULL, 'pricing-v1', '2026-01-01',
                  'https://example.test', 0, 0, NULL, NULL, NULL, 'fixture-source', NULL,
                  ?, ?, NULL, ?, NULL,
                  'fixture-v1', NULL)
        """,
        (
            int(item.lastrowid),
            int(policy.lastrowid),
            "d" * 64,
            "f" * 64,
            utc_timestamp(LOCK - timedelta(hours=2)),
            utc_timestamp(LOCK - timedelta(hours=2)),
            utc_timestamp(LOCK - timedelta(hours=2)),
        ),
    )
    connection.execute(
        """
        UPDATE source_item_extractions
        SET status = 'submitted', batch_submission_request_id = 'submit-1',
            provider_batch_id = 'batch-1', provider_custom_id = 'custom-1'
        WHERE extraction_id = 'extract-1'
        """
    )
    settled = utc_timestamp(LOCK - timedelta(hours=1))
    connection.execute(
        """
        UPDATE source_item_extractions
        SET status = 'settling', provider_message_id = 'message-1', output_json = '{}',
            output_sha256 = ?, input_tokens = 1, output_tokens = 1, cost_nanos_usd = 0,
            latency_ms = 1, ingested_at = ?, valid_from = ?
        WHERE extraction_id = 'extract-1'
        """,
        ("0" * 64, settled, settled),
    )


def _seed_claim(
    connection: sqlite3.Connection,
    claim_id: str,
    claim_type: str,
    dimension: str,
    direction: str,
    falsifiable: bool,
    observed_at: datetime | None = None,
) -> None:
    observed_instant = LOCK - timedelta(hours=2) if observed_at is None else observed_at
    observed = utc_timestamp(observed_instant)
    ingested = utc_timestamp(observed_instant + timedelta(hours=1))
    item_id = int(connection.execute("SELECT source_item_id FROM source_items").fetchone()[0])
    policy_id = int(
        connection.execute("SELECT source_policy_id FROM source_policies").fetchone()[0]
    )
    connection.execute(
        """
        INSERT INTO claims(
            claim_id, extraction_id, source_item_id, source_policy_id, prompt_version_id,
            model_id, provider_request_id, batch_submission_request_id, provider_batch_id,
            provider_custom_id, provider_message_id, claim_type, claim_dimension,
            outcome_direction, roster_behavior_direction, evidence_class, evidence_basis,
            falsifiable, specificity, actionability, novelty, model_confidence, team_refs_json,
            uncertainty_flags_json, ambiguity_flags_json, suggested_channels_json,
            disconfirming_context, disconfirming_context_sha256, context_redacted_at, source,
            published_at, observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES (?, 'extract-1', ?, ?, 'prompt-1', 'fixture-model', NULL, 'submit-1',
                  'batch-1', 'custom-1', 'message-1', ?, ?, ?, ?, 'B', 'beat_report', ?,
                  0.8, 0.8, 'new', 'high', '["ABC"]', '["none"]', '["none"]', '["mean"]',
                  NULL, NULL, NULL, 'fixture-source', NULL, ?, ?, NULL, ?, NULL,
                  'fixture-v1', NULL)
        """,
        (
            claim_id,
            item_id,
            policy_id,
            claim_type,
            dimension,
            direction,
            direction,
            int(falsifiable),
            observed,
            ingested,
            ingested,
        ),
    )
    connection.execute(
        """
        INSERT INTO claim_player_refs(
            claim_id, ordinal, name_raw, player_id, unresolved_id, resolution_method,
            resolution_confidence, manual_override, source, published_at, observed_at,
            ingested_at, effective_at, valid_from, valid_to, source_version, run_id
        ) VALUES (?, 0, 'Example Player', 1, NULL, 'exact', 1.0, 0, 'fixture-source', NULL,
                  ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (claim_id, observed, ingested, ingested),
    )


def test_one_game_outcome_is_graded_once_across_two_slates_of_that_game(
    tmp_path: Path,
) -> None:
    """§12.4.1: a second slate of the same game repeats an observation, it does not add one."""

    database = tmp_path / "multi-slate.sqlite3"
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed_world(connection)
        stamp = utc_timestamp(LOCK - timedelta(days=2))
        # A showdown on the same game, ordered ahead of the main slate by the selector and
        # carrying no official availability, so the evidence preference is exercised too.
        connection.execute(
            """
            INSERT INTO slates(
                slate_id, external_slate_id, site, slate_type, season, week, name,
                starts_at, locks_at, source, published_at, observed_at, ingested_at,
                effective_at, valid_from, valid_to, source_version, run_id
            ) VALUES (0, 'slate-0', 'draftkings', 'showdown', 2026, 1, 'Showdown', ?, ?,
                      'fixture', NULL, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
            """,
            (utc_timestamp(LOCK), utc_timestamp(LOCK), stamp, stamp, stamp),
        )
        connection.execute(
            """
            INSERT INTO salaries(
                slate_id, player_id, game_id, team_id, opponent_team_id, site_player_id,
                roster_positions_json, salary, player_status, source_file_sha256, source,
                published_at, observed_at, ingested_at, effective_at, valid_from, valid_to,
                source_version, run_id
            ) VALUES (0, 1, 1, 1, 2, 'p1', '["CPT"]', 9000, NULL, ?, 'fixture', NULL,
                      ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
            """,
            ("a" * 64, stamp, stamp, stamp),
        )
        _seed_claim(connection, "usage-up", "usage", "snap_share", "increase", True)
        _seed_claim(connection, "available", "availability", "active_status", "increase", True)
        connection.commit()

        report = grade_week(
            connection,
            season=2026,
            week=1,
            site="draftkings",
            grading_run_id="grade-run-1",
            graded_at=GRADED,
        )
        graded = connection.execute(
            """
            SELECT claim_id, slate_id, availability_id, result_id, verdict
            FROM claim_grades ORDER BY claim_id
            """
        ).fetchall()
        ledger = build_source_credibility_report(connection, season=2026, week=1)

    # Both slates are seen; each claim is still graded exactly once against its one outcome.
    assert report.claim_targets_seen == 4
    assert report.grades_inserted == 2
    assert [tuple(row) for row in graded] == [
        ("available", 1, None, 1, "correct"),
        ("usage-up", 1, None, 1, "correct"),
    ]
    for row in ledger.rows:
        assert row.n_graded == 1
        assert row.correct_count == 1
