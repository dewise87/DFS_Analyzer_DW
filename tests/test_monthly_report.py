from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.monthly_report import build_monthly_report, monthly_window
from narrative_alpha.report_cli import main as report_main
from narrative_alpha.store import apply_migrations, connect_database

STAMP = utc_timestamp(datetime(2026, 9, 10, 12, tzinfo=UTC))


def test_monthly_report_empty_store_keeps_every_heading(tmp_path: Path) -> None:
    database = tmp_path / "store.sqlite3"
    with connect_database(database) as connection:
        apply_migrations(connection)
        rendered = build_monthly_report(
            connection,
            window=monthly_window("2026-09", timezone=ZoneInfo("UTC")),
            budget_nanos=5_000_000_000,
        )

    for heading in (
        "SOURCE YIELD",
        "STAGE 1 COST",
        "PROMPT VERSIONS AND STAGE 1 EVALUATIONS",
        "OWNERSHIP EVALUATIONS",
        "SIGNAL STATUSES",
        "LANE STEP FAILURES",
    ):
        assert heading in rendered
    assert rendered.count("query window") == 7
    assert rendered.count("none recorded") >= 6


def test_monthly_stage1_totals_reconcile_to_immutable_rows(tmp_path: Path) -> None:
    database = tmp_path / "store.sqlite3"
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed_stage1_attempt(connection)
        rendered = build_monthly_report(
            connection,
            window=monthly_window("2026-09", timezone=ZoneInfo("UTC")),
            budget_nanos=5_000_000_000,
        )

    assert "items collected=1  not yet purged=1  extracted=1  claims=1  grades=0" in rendered
    assert "spend         $0.12 of $5.00 budget" in rendered
    assert "tokens        input=100  output=20" in rendered
    assert "cost / retained item  $0.12" in rendered
    assert "cost / claim          $0.12" in rendered
    assert "p  model=m  attempts=1" in rendered


def test_monthly_cli_rejects_a_malformed_month(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as error:
        report_main(
            [
                "monthly",
                "--database",
                str(tmp_path / "store.sqlite3"),
                "--month",
                "2026-9",
            ]
        )
    assert error.value.code == 2


def _seed_stage1_attempt(connection) -> None:  # type: ignore[no-untyped-def]
    """One source/item/extraction/claim is enough to pin monthly count semantics."""

    content_hash, prompt_hash, request_hash, output_hash = "a" * 64, "b" * 64, "c" * 64, "d" * 64
    connection.execute("INSERT INTO source_keys(source_id) VALUES ('s')")
    connection.execute(
        """
        INSERT INTO sources(
            source_id, display_name, source_family, collector_kind, feed_url, enabled,
            source, published_at, observed_at, ingested_at, effective_at, valid_from,
            valid_to, source_version, run_id
        ) VALUES ('s', 'Source', 'fixture', 'rss_atom', 'https://example.test/feed', 1,
                  'fixture', NULL, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (STAMP, STAMP, STAMP),
    )
    connection.execute(
        """
        INSERT INTO source_policies(
            source_id, permitted_use, raw_retention_days, personal_data_fields_allowed,
            must_honor_deletions, redistribution_allowed, third_party_processing_allowed,
            commercial_use_status, terms_reviewed_at, source, published_at, observed_at,
            ingested_at, effective_at, valid_from, valid_to, source_version, run_id
        ) VALUES ('s', 'fixture', 30, '[]', 1, 0, 1, 'fixture', ?, 'fixture', NULL, ?, ?,
                  NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (STAMP, STAMP, STAMP, STAMP),
    )
    policy_id = int(
        connection.execute("SELECT source_policy_id FROM source_policies").fetchone()[0]
    )
    cursor = connection.execute(
        """
        INSERT INTO source_items(
            source_id, external_item_id, canonical_url, title, raw_content, cleaned_text,
            content_sha256, source, published_at, observed_at, ingested_at, effective_at,
            valid_from, valid_to, source_version, run_id
        ) VALUES ('s', 'item', 'https://example.test/item', 'Title', X'78', 'body', ?,
                  'fixture', NULL, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (content_hash, STAMP, STAMP, STAMP),
    )
    item_id = int(cursor.lastrowid)
    connection.execute(
        """
        INSERT INTO prompt_versions(
            prompt_version_id, stage, schema_version, system_prompt, user_prompt_template,
            output_schema_json, prompt_sha256, created_at, source, published_at, observed_at,
            ingested_at, effective_at, valid_from, valid_to, source_version, run_id
        ) VALUES ('p', 'stage_1_extraction', 'v1', 'system', 'user', '{}', ?, ?, 'fixture',
                  NULL, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (prompt_hash, STAMP, STAMP, STAMP, STAMP),
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
        ) VALUES ('e', ?, ?, 'fixture', ?, 'p', 'm', 100, ?, NULL, NULL, NULL, NULL, NULL,
                  'creating', NULL, NULL, NULL, NULL, NULL, NULL, 'pricing-v1', ?,
                  'https://example.test/pricing', 1, 1, NULL, NULL, NULL, 'fixture', NULL,
                  ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (item_id, policy_id, content_hash, request_hash, STAMP, STAMP, STAMP, STAMP),
    )
    connection.execute(
        """
        UPDATE source_item_extractions
        SET status = 'submitted', batch_submission_request_id = 'submit',
            provider_batch_id = 'batch', provider_custom_id = 'custom'
        WHERE extraction_id = 'e'
        """
    )
    connection.execute(
        """
        UPDATE source_item_extractions
        SET status = 'settling', provider_message_id = 'message', output_json = '{}',
            output_sha256 = ?, input_tokens = 100, output_tokens = 20,
            cost_nanos_usd = 123000000, latency_ms = 1
        WHERE extraction_id = 'e'
        """,
        (output_hash,),
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
        ) VALUES ('claim', 'e', ?, ?, 'p', 'm', NULL, 'submit', 'batch', 'custom', 'message',
                  'usage', 'role', 'increase', 'increase', 'B', 'beat_report', 1, .8, .8,
                  'new', 'high', '[]', '[]', '[]', '[]', NULL, NULL, NULL, 's', NULL, ?, ?,
                  NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (item_id, policy_id, STAMP, STAMP, STAMP),
    )
    connection.execute(
        "UPDATE source_item_extractions SET status = 'succeeded' WHERE extraction_id = 'e'"
    )
    connection.commit()
