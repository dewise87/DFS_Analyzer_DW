-- rebuild_with_foreign_keys_off
-- Retain failed provider responses under the same tombstone rules as successful output.
-- The runner disables FK enforcement before BEGIN, checks every FK before COMMIT, and
-- restores enforcement in finally. All dependent triggers are restored in this transaction.


DROP TRIGGER fence_source_delete_during_stage1_submission;

DROP TRIGGER fence_source_insert_during_stage1_submission;

DROP TRIGGER fence_source_item_delete_during_stage1_submission;

DROP TRIGGER fence_source_item_update_during_stage1_submission;

DROP TRIGGER fence_source_policy_delete_during_stage1_submission;

DROP TRIGGER fence_source_policy_insert_during_stage1_submission;

DROP TRIGGER fence_source_policy_update_during_stage1_submission;

DROP TRIGGER fence_source_update_during_stage1_submission;

DROP TRIGGER fence_tombstone_insert_during_stage1_submission;

DROP TRIGGER redact_stage1_content_after_tombstone;

DROP TRIGGER source_item_extraction_lineage_immutable;

DROP TRIGGER source_item_extraction_state_machine;

DROP TRIGGER source_item_extraction_transition_timestamp;

DROP TRIGGER source_item_extractions_no_delete;

DROP TRIGGER validate_claim_evidence_ref_insert;

DROP TRIGGER validate_claim_extraction_lineage_insert;

DROP TRIGGER validate_claim_player_ref_insert;

DROP TRIGGER validate_episode_claim_insert;

DROP TRIGGER validate_narrative_episode_insert;

DROP TRIGGER validate_source_item_extraction_insert;

DROP TRIGGER validate_source_item_extraction_update;

DROP TRIGGER validate_source_item_review_flag_insert;

CREATE TABLE source_item_extractions_rebuilt (
    extraction_id TEXT PRIMARY KEY,
    source_item_id INTEGER NOT NULL REFERENCES source_items(source_item_id),
    source_policy_id INTEGER NOT NULL REFERENCES source_policies(source_policy_id),
    source_family TEXT NOT NULL CHECK(length(trim(source_family)) > 0),
    source_content_sha256 TEXT NOT NULL CHECK(length(source_content_sha256) = 64),
    prompt_version_id TEXT NOT NULL REFERENCES prompt_versions(prompt_version_id),
    model_id TEXT NOT NULL CHECK(length(trim(model_id)) > 0),
    max_output_tokens INTEGER NOT NULL CHECK(max_output_tokens > 0),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64),
    provider_request_id TEXT CHECK(
        provider_request_id IS NULL OR length(trim(provider_request_id)) > 0
    ),
    batch_submission_request_id TEXT CHECK(
        batch_submission_request_id IS NULL OR length(trim(batch_submission_request_id)) > 0
    ),
    provider_batch_id TEXT CHECK(
        provider_batch_id IS NULL OR length(trim(provider_batch_id)) > 0
    ),
    provider_custom_id TEXT CHECK(
        provider_custom_id IS NULL OR length(trim(provider_custom_id)) > 0
    ),
    provider_message_id TEXT CHECK(
        provider_message_id IS NULL OR length(trim(provider_message_id)) > 0
    ),
    status TEXT NOT NULL CHECK(
        status IN ('creating', 'submitted', 'settling', 'succeeded', 'flagged', 'failed')
    ),
    output_json TEXT CHECK(
        output_json IS NULL OR
        (json_valid(output_json) AND json_type(output_json) = 'object')
    ),
    output_sha256 TEXT CHECK(output_sha256 IS NULL OR length(output_sha256) = 64),
    output_redacted_at TEXT,
    input_tokens INTEGER CHECK(input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK(output_tokens IS NULL OR output_tokens >= 0),
    cost_nanos_usd INTEGER CHECK(cost_nanos_usd IS NULL OR cost_nanos_usd >= 0),
    pricing_version TEXT NOT NULL CHECK(length(trim(pricing_version)) > 0),
    pricing_effective_at TEXT NOT NULL,
    pricing_source_url TEXT NOT NULL CHECK(length(trim(pricing_source_url)) > 0),
    input_nanos_per_token INTEGER NOT NULL CHECK(input_nanos_per_token >= 0),
    output_nanos_per_token INTEGER NOT NULL CHECK(output_nanos_per_token >= 0),
    latency_ms INTEGER CHECK(latency_ms IS NULL OR latency_ms >= 0),
    error_code TEXT CHECK(error_code IS NULL OR length(trim(error_code)) > 0),
    error_message TEXT,
    error_detail_json TEXT CHECK(
        error_detail_json IS NULL OR
        (json_valid(error_detail_json) AND json_type(error_detail_json) = 'object')
    ),
    refusal_bucket TEXT CHECK(refusal_bucket IS NULL OR length(trim(refusal_bucket)) > 0),
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(extraction_id, source_item_id),
    CHECK(valid_to IS NULL OR valid_to > valid_from),
    CHECK(
        (
            status = 'creating' AND output_json IS NULL AND output_sha256 IS NULL AND
            output_redacted_at IS NULL AND provider_batch_id IS NULL AND
            provider_request_id IS NULL AND batch_submission_request_id IS NULL AND
            provider_custom_id IS NULL AND provider_message_id IS NULL AND
            input_tokens IS NULL AND
            output_tokens IS NULL AND cost_nanos_usd IS NULL
        ) OR
        (
            status = 'submitted' AND output_json IS NULL AND output_sha256 IS NULL AND
            output_redacted_at IS NULL AND provider_message_id IS NULL AND
            provider_batch_id IS NOT NULL AND provider_custom_id IS NOT NULL AND
            input_tokens IS NULL AND
            output_tokens IS NULL AND cost_nanos_usd IS NULL
        ) OR
        (
            status = 'settling' AND output_json IS NOT NULL AND
            output_sha256 IS NOT NULL AND output_redacted_at IS NULL AND
            error_code IS NULL AND provider_message_id IS NOT NULL AND
            input_tokens IS NOT NULL AND output_tokens IS NOT NULL AND
            cost_nanos_usd IS NOT NULL AND
            provider_request_id IS NULL AND
            provider_batch_id IS NOT NULL AND provider_custom_id IS NOT NULL
        ) OR
        (
            status = 'succeeded' AND output_sha256 IS NOT NULL AND error_code IS NULL AND
            provider_message_id IS NOT NULL AND
            (
                (output_json IS NOT NULL AND output_redacted_at IS NULL) OR
                (output_json IS NULL AND output_redacted_at IS NOT NULL)
            ) AND
            provider_request_id IS NULL AND
            provider_batch_id IS NOT NULL AND provider_custom_id IS NOT NULL
        ) OR
        (
            status = 'flagged' AND output_json IS NULL AND output_sha256 IS NULL AND
            output_redacted_at IS NULL AND error_code IS NOT NULL
        ) OR
        (
            status = 'failed' AND error_code IS NOT NULL AND (
                (output_json IS NULL AND output_sha256 IS NULL AND output_redacted_at IS NULL) OR
                (output_sha256 IS NOT NULL AND (
                    (output_json IS NOT NULL AND output_redacted_at IS NULL) OR
                    (output_json IS NULL AND output_redacted_at IS NOT NULL)
                ))
            )
        )
    )
) STRICT;

INSERT INTO source_item_extractions_rebuilt (extraction_id, source_item_id, source_policy_id, source_family, source_content_sha256, prompt_version_id, model_id, max_output_tokens, request_sha256, provider_request_id, batch_submission_request_id, provider_batch_id, provider_custom_id, provider_message_id, status, output_json, output_sha256, output_redacted_at, input_tokens, output_tokens, cost_nanos_usd, pricing_version, pricing_effective_at, pricing_source_url, input_nanos_per_token, output_nanos_per_token, latency_ms, error_code, error_message, source, published_at, observed_at, ingested_at, effective_at, valid_from, valid_to, source_version, run_id)
SELECT extraction_id, source_item_id, source_policy_id, source_family, source_content_sha256, prompt_version_id, model_id, max_output_tokens, request_sha256, provider_request_id, batch_submission_request_id, provider_batch_id, provider_custom_id, provider_message_id, status, output_json, output_sha256, output_redacted_at, input_tokens, output_tokens, cost_nanos_usd, pricing_version, pricing_effective_at, pricing_source_url, input_nanos_per_token, output_nanos_per_token, latency_ms, error_code, error_message, source, published_at, observed_at, ingested_at, effective_at, valid_from, valid_to, source_version, run_id FROM source_item_extractions;

DROP TABLE source_item_extractions;

ALTER TABLE source_item_extractions_rebuilt RENAME TO source_item_extractions;

CREATE INDEX idx_source_item_extractions_failed_attempts
    ON source_item_extractions(source_item_id, prompt_version_id, model_id)
    WHERE status = 'failed';

CREATE UNIQUE INDEX idx_source_item_extractions_inflight
    ON source_item_extractions(source_item_id, prompt_version_id, model_id)
    WHERE status IN ('creating', 'submitted', 'settling');

CREATE INDEX idx_source_item_extractions_status
    ON source_item_extractions(status, source_item_id);

CREATE UNIQUE INDEX idx_source_item_extractions_terminal
    ON source_item_extractions(source_item_id, prompt_version_id, model_id)
    WHERE status IN ('succeeded', 'flagged');

CREATE TRIGGER fence_source_delete_during_stage1_submission
BEFORE DELETE ON sources
WHEN EXISTS (
    SELECT 1
    FROM stage1_execution_leases AS lease
    JOIN source_item_extractions AS extraction
      ON lease.lease_key = 'submission:' || extraction.extraction_id
    JOIN source_items AS item ON item.source_item_id = extraction.source_item_id
    WHERE lease.operation_kind = 'submission'
      AND lease.expires_at > strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')
      AND item.source_id = OLD.source_id
)
BEGIN
    SELECT RAISE(ABORT, 'source change blocked by active Stage 1 submission fence');
END;

CREATE TRIGGER fence_source_insert_during_stage1_submission
BEFORE INSERT ON sources
WHEN EXISTS (
    SELECT 1
    FROM stage1_execution_leases AS lease
    JOIN source_item_extractions AS extraction
      ON lease.lease_key = 'submission:' || extraction.extraction_id
    JOIN source_items AS item ON item.source_item_id = extraction.source_item_id
    WHERE lease.operation_kind = 'submission'
      AND lease.expires_at > strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')
      AND item.source_id = NEW.source_id
)
BEGIN
    SELECT RAISE(ABORT, 'source change blocked by active Stage 1 submission fence');
END;

CREATE TRIGGER fence_source_item_delete_during_stage1_submission
BEFORE DELETE ON source_items
WHEN EXISTS (
    SELECT 1
    FROM stage1_execution_leases AS lease
    JOIN source_item_extractions AS extraction
      ON lease.lease_key = 'submission:' || extraction.extraction_id
    WHERE lease.operation_kind = 'submission'
      AND lease.expires_at > strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')
      AND extraction.source_item_id = OLD.source_item_id
)
BEGIN
    SELECT RAISE(ABORT, 'source item change blocked by active Stage 1 submission fence');
END;

CREATE TRIGGER fence_source_item_update_during_stage1_submission
BEFORE UPDATE ON source_items
WHEN EXISTS (
    SELECT 1
    FROM stage1_execution_leases AS lease
    JOIN source_item_extractions AS extraction
      ON lease.lease_key = 'submission:' || extraction.extraction_id
    WHERE lease.operation_kind = 'submission'
      AND lease.expires_at > strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')
      AND extraction.source_item_id IN (OLD.source_item_id, NEW.source_item_id)
)
BEGIN
    SELECT RAISE(ABORT, 'source item change blocked by active Stage 1 submission fence');
END;

CREATE TRIGGER fence_source_policy_delete_during_stage1_submission
BEFORE DELETE ON source_policies
WHEN EXISTS (
    SELECT 1
    FROM stage1_execution_leases AS lease
    JOIN source_item_extractions AS extraction
      ON lease.lease_key = 'submission:' || extraction.extraction_id
    JOIN source_items AS item ON item.source_item_id = extraction.source_item_id
    WHERE lease.operation_kind = 'submission'
      AND lease.expires_at > strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')
      AND item.source_id = OLD.source_id
)
BEGIN
    SELECT RAISE(ABORT, 'source policy change blocked by active Stage 1 submission fence');
END;

CREATE TRIGGER fence_source_policy_insert_during_stage1_submission
BEFORE INSERT ON source_policies
WHEN EXISTS (
    SELECT 1
    FROM stage1_execution_leases AS lease
    JOIN source_item_extractions AS extraction
      ON lease.lease_key = 'submission:' || extraction.extraction_id
    JOIN source_items AS item ON item.source_item_id = extraction.source_item_id
    WHERE lease.operation_kind = 'submission'
      AND lease.expires_at > strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')
      AND item.source_id = NEW.source_id
)
BEGIN
    SELECT RAISE(ABORT, 'source policy change blocked by active Stage 1 submission fence');
END;

CREATE TRIGGER fence_source_policy_update_during_stage1_submission
BEFORE UPDATE ON source_policies
WHEN EXISTS (
    SELECT 1
    FROM stage1_execution_leases AS lease
    JOIN source_item_extractions AS extraction
      ON lease.lease_key = 'submission:' || extraction.extraction_id
    JOIN source_items AS item ON item.source_item_id = extraction.source_item_id
    WHERE lease.operation_kind = 'submission'
      AND lease.expires_at > strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')
      AND item.source_id IN (OLD.source_id, NEW.source_id)
)
BEGIN
    SELECT RAISE(ABORT, 'source policy change blocked by active Stage 1 submission fence');
END;

CREATE TRIGGER fence_source_update_during_stage1_submission
BEFORE UPDATE ON sources
WHEN EXISTS (
    SELECT 1
    FROM stage1_execution_leases AS lease
    JOIN source_item_extractions AS extraction
      ON lease.lease_key = 'submission:' || extraction.extraction_id
    JOIN source_items AS item ON item.source_item_id = extraction.source_item_id
    WHERE lease.operation_kind = 'submission'
      AND lease.expires_at > strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')
      AND item.source_id IN (OLD.source_id, NEW.source_id)
)
BEGIN
    SELECT RAISE(ABORT, 'source change blocked by active Stage 1 submission fence');
END;

CREATE TRIGGER fence_tombstone_insert_during_stage1_submission
BEFORE INSERT ON content_tombstones
WHEN EXISTS (
    SELECT 1
    FROM stage1_execution_leases AS lease
    JOIN source_item_extractions AS extraction
      ON lease.lease_key = 'submission:' || extraction.extraction_id
    WHERE lease.operation_kind = 'submission'
      AND lease.expires_at > strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')
      AND extraction.source_item_id = NEW.source_item_id
)
BEGIN
    SELECT RAISE(ABORT, 'tombstone blocked by active Stage 1 submission fence');
END;

CREATE TRIGGER redact_stage1_content_after_tombstone
AFTER INSERT ON content_tombstones
BEGIN
    UPDATE source_items
       SET title = NULL, raw_content = NULL, cleaned_text = NULL
     WHERE source_item_id = NEW.source_item_id;
    UPDATE claim_evidence_refs
       SET verbatim_extract = NULL, redacted_at = NEW.tombstoned_at
     WHERE source_item_id = NEW.source_item_id AND verbatim_extract IS NOT NULL;
    UPDATE claims
       SET disconfirming_context = NULL, context_redacted_at = NEW.tombstoned_at
     WHERE source_item_id = NEW.source_item_id AND disconfirming_context IS NOT NULL;
    UPDATE source_item_extractions
       SET output_json = NULL, output_redacted_at = NEW.tombstoned_at
     WHERE source_item_id = NEW.source_item_id
       AND status = 'succeeded' AND output_json IS NOT NULL;
    UPDATE source_item_extractions
       SET output_json = NULL,
           output_redacted_at = CASE WHEN output_sha256 IS NULL THEN NULL
                                     ELSE NEW.tombstoned_at END,
           error_message = NULL, error_detail_json = NULL
     WHERE source_item_id = NEW.source_item_id AND status = 'failed'
       AND (output_json IS NOT NULL OR error_message IS NOT NULL OR error_detail_json IS NOT NULL);
END;

CREATE TRIGGER source_item_extraction_lineage_immutable
BEFORE UPDATE ON source_item_extractions
WHEN NOT (
    NEW.extraction_id IS OLD.extraction_id AND
    NEW.source_item_id IS OLD.source_item_id AND
    NEW.source_policy_id IS OLD.source_policy_id AND
    NEW.source_family IS OLD.source_family AND
    NEW.source_content_sha256 IS OLD.source_content_sha256 AND
    NEW.prompt_version_id IS OLD.prompt_version_id AND NEW.model_id IS OLD.model_id AND
    NEW.max_output_tokens IS OLD.max_output_tokens AND
    NEW.request_sha256 IS OLD.request_sha256 AND
    NEW.pricing_version IS OLD.pricing_version AND
    NEW.pricing_effective_at IS OLD.pricing_effective_at AND
    NEW.pricing_source_url IS OLD.pricing_source_url AND
    NEW.input_nanos_per_token IS OLD.input_nanos_per_token AND
    NEW.output_nanos_per_token IS OLD.output_nanos_per_token AND
    NEW.source IS OLD.source AND NEW.published_at IS OLD.published_at AND
    NEW.observed_at IS OLD.observed_at AND NEW.effective_at IS OLD.effective_at AND
    NEW.valid_to IS OLD.valid_to AND NEW.source_version IS OLD.source_version AND
    NEW.run_id IS OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'extraction request, policy, pricing, and provenance are immutable');
END;

CREATE TRIGGER source_item_extraction_state_machine
BEFORE UPDATE ON source_item_extractions
WHEN NOT (
    (
        OLD.status = 'creating' AND NEW.status = 'creating' AND
        NEW.provider_request_id IS OLD.provider_request_id AND
        NEW.batch_submission_request_id IS OLD.batch_submission_request_id AND
        NEW.provider_batch_id IS OLD.provider_batch_id AND
        NEW.provider_custom_id IS OLD.provider_custom_id AND
        NEW.provider_message_id IS OLD.provider_message_id AND
        NEW.output_json IS OLD.output_json AND NEW.output_sha256 IS OLD.output_sha256 AND
        NEW.output_redacted_at IS OLD.output_redacted_at AND
        NEW.input_tokens IS OLD.input_tokens AND NEW.output_tokens IS OLD.output_tokens AND
        NEW.cost_nanos_usd IS OLD.cost_nanos_usd AND NEW.latency_ms IS OLD.latency_ms AND
        NEW.ingested_at IS OLD.ingested_at AND NEW.valid_from IS OLD.valid_from
    ) OR
    (
        OLD.status = 'creating' AND NEW.status = 'submitted' AND
        NEW.provider_request_id IS OLD.provider_request_id AND
        NEW.provider_message_id IS OLD.provider_message_id AND
        NEW.output_json IS OLD.output_json AND NEW.output_sha256 IS OLD.output_sha256 AND
        NEW.output_redacted_at IS OLD.output_redacted_at AND
        NEW.input_tokens IS OLD.input_tokens AND NEW.output_tokens IS OLD.output_tokens AND
        NEW.cost_nanos_usd IS OLD.cost_nanos_usd AND NEW.latency_ms IS OLD.latency_ms AND
        NEW.ingested_at IS OLD.ingested_at AND NEW.valid_from IS OLD.valid_from AND
        NEW.error_code IS NULL AND NEW.error_message IS NULL
    ) OR
    (
        OLD.status = 'creating' AND NEW.status = 'flagged' AND
        NEW.provider_request_id IS OLD.provider_request_id AND
        NEW.batch_submission_request_id IS OLD.batch_submission_request_id AND
        NEW.provider_batch_id IS OLD.provider_batch_id AND
        NEW.provider_custom_id IS OLD.provider_custom_id AND
        NEW.provider_message_id IS OLD.provider_message_id AND
        NEW.output_json IS OLD.output_json AND NEW.output_sha256 IS OLD.output_sha256 AND
        NEW.output_redacted_at IS OLD.output_redacted_at AND
        NEW.input_tokens IS OLD.input_tokens AND NEW.output_tokens IS OLD.output_tokens AND
        NEW.cost_nanos_usd IS OLD.cost_nanos_usd AND NEW.latency_ms IS OLD.latency_ms AND
        NEW.error_code = 'prompt_injection_input' AND
        NEW.ingested_at IS OLD.ingested_at AND NEW.valid_from IS OLD.valid_from
    ) OR
    (
        OLD.status = 'creating' AND NEW.status = 'failed' AND
        NEW.provider_request_id IS OLD.provider_request_id AND
        NEW.batch_submission_request_id IS OLD.batch_submission_request_id AND
        NEW.provider_batch_id IS OLD.provider_batch_id AND
        NEW.provider_custom_id IS OLD.provider_custom_id AND
        NEW.provider_message_id IS OLD.provider_message_id AND
        NEW.output_json IS OLD.output_json AND NEW.output_sha256 IS OLD.output_sha256 AND
        NEW.output_redacted_at IS OLD.output_redacted_at AND
        NEW.input_tokens IS OLD.input_tokens AND NEW.output_tokens IS OLD.output_tokens AND
        NEW.cost_nanos_usd IS OLD.cost_nanos_usd AND NEW.latency_ms IS OLD.latency_ms
    ) OR
    (
        OLD.status = 'submitted' AND NEW.status = 'submitted' AND
        NEW.provider_request_id IS OLD.provider_request_id AND
        NEW.batch_submission_request_id IS OLD.batch_submission_request_id AND
        NEW.provider_batch_id IS OLD.provider_batch_id AND
        NEW.provider_custom_id IS OLD.provider_custom_id AND
        NEW.provider_message_id IS OLD.provider_message_id AND
        NEW.output_json IS OLD.output_json AND NEW.output_sha256 IS OLD.output_sha256 AND
        NEW.output_redacted_at IS OLD.output_redacted_at AND
        NEW.input_tokens IS OLD.input_tokens AND NEW.output_tokens IS OLD.output_tokens AND
        NEW.cost_nanos_usd IS OLD.cost_nanos_usd AND NEW.latency_ms IS OLD.latency_ms AND
        NEW.ingested_at IS OLD.ingested_at AND NEW.valid_from IS OLD.valid_from
    ) OR
    (
        OLD.status = 'submitted' AND NEW.status IN ('settling', 'flagged', 'failed') AND
        NEW.batch_submission_request_id IS OLD.batch_submission_request_id AND
        NEW.provider_batch_id IS OLD.provider_batch_id AND
        NEW.provider_custom_id IS OLD.provider_custom_id AND
        NEW.output_redacted_at IS OLD.output_redacted_at
    ) OR
    (
        OLD.status = 'settling' AND NEW.status = 'succeeded' AND
        NEW.provider_request_id IS OLD.provider_request_id AND
        NEW.batch_submission_request_id IS OLD.batch_submission_request_id AND
        NEW.provider_batch_id IS OLD.provider_batch_id AND
        NEW.provider_custom_id IS OLD.provider_custom_id AND
        NEW.provider_message_id IS OLD.provider_message_id AND
        NEW.output_json IS OLD.output_json AND NEW.output_sha256 IS OLD.output_sha256 AND
        NEW.output_redacted_at IS OLD.output_redacted_at AND
        NEW.input_tokens IS OLD.input_tokens AND NEW.output_tokens IS OLD.output_tokens AND
        NEW.cost_nanos_usd IS OLD.cost_nanos_usd AND NEW.latency_ms IS OLD.latency_ms AND
        NEW.error_code IS OLD.error_code AND NEW.error_message IS OLD.error_message AND
        NEW.ingested_at IS OLD.ingested_at AND NEW.valid_from IS OLD.valid_from
    ) OR
    (
        OLD.status = 'succeeded' AND NEW.status = 'succeeded' AND
        OLD.output_json IS NOT NULL AND NEW.output_json IS NULL AND
        OLD.output_redacted_at IS NULL AND NEW.output_redacted_at IS NOT NULL AND
        EXISTS (
            SELECT 1 FROM content_tombstones AS tombstone
            WHERE tombstone.source_item_id = OLD.source_item_id
              AND tombstone.tombstoned_at IS NEW.output_redacted_at
        ) AND
        NEW.provider_request_id IS OLD.provider_request_id AND
        NEW.batch_submission_request_id IS OLD.batch_submission_request_id AND
        NEW.provider_batch_id IS OLD.provider_batch_id AND
        NEW.provider_custom_id IS OLD.provider_custom_id AND
        NEW.provider_message_id IS OLD.provider_message_id AND
        NEW.output_sha256 IS OLD.output_sha256 AND NEW.input_tokens IS OLD.input_tokens AND
        NEW.output_tokens IS OLD.output_tokens AND
        NEW.cost_nanos_usd IS OLD.cost_nanos_usd AND
        NEW.latency_ms IS OLD.latency_ms AND NEW.error_code IS OLD.error_code AND
        NEW.error_message IS OLD.error_message AND
        NEW.ingested_at IS OLD.ingested_at AND NEW.valid_from IS OLD.valid_from
    ) OR
    (
        OLD.status = 'failed' AND NEW.status = 'failed' AND
        (OLD.output_json IS NOT NULL OR OLD.error_message IS NOT NULL OR
         OLD.error_detail_json IS NOT NULL) AND NEW.output_json IS NULL AND
        OLD.output_redacted_at IS NULL AND
        NEW.output_redacted_at IS CASE WHEN OLD.output_sha256 IS NULL THEN NULL
                                     ELSE (SELECT tombstoned_at FROM content_tombstones
                                           WHERE source_item_id = OLD.source_item_id) END AND
        EXISTS (
            SELECT 1 FROM content_tombstones AS tombstone
            WHERE tombstone.source_item_id = OLD.source_item_id
              
        ) AND
        NEW.provider_request_id IS OLD.provider_request_id AND
        NEW.batch_submission_request_id IS OLD.batch_submission_request_id AND
        NEW.provider_batch_id IS OLD.provider_batch_id AND
        NEW.provider_custom_id IS OLD.provider_custom_id AND
        NEW.provider_message_id IS OLD.provider_message_id AND
        NEW.output_sha256 IS OLD.output_sha256 AND NEW.input_tokens IS OLD.input_tokens AND
        NEW.output_tokens IS OLD.output_tokens AND
        NEW.cost_nanos_usd IS OLD.cost_nanos_usd AND
        NEW.latency_ms IS OLD.latency_ms AND NEW.error_code IS OLD.error_code AND
        NEW.error_message IS NULL AND NEW.error_detail_json IS NULL AND
        NEW.ingested_at IS OLD.ingested_at AND NEW.valid_from IS OLD.valid_from
    )
)
BEGIN
    SELECT RAISE(ABORT, 'invalid or immutable extraction state transition');
END;

CREATE TRIGGER source_item_extraction_transition_timestamp
BEFORE UPDATE ON source_item_extractions
WHEN (NEW.ingested_at IS NOT OLD.ingested_at OR NEW.valid_from IS NOT OLD.valid_from)
  AND NOT (
      NEW.ingested_at IS NEW.valid_from AND
      length(NEW.ingested_at) = 27 AND substr(NEW.ingested_at, 1, 4) <> '0000' AND
      substr(NEW.ingested_at, 12, 2) BETWEEN '00' AND '23' AND
      NEW.ingested_at GLOB
          '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND
      strftime('%Y-%m-%dT%H:%M:%S', NEW.ingested_at) IS substr(NEW.ingested_at, 1, 19) AND
      NEW.ingested_at >= OLD.observed_at AND NEW.ingested_at >= OLD.ingested_at AND
      NEW.ingested_at >= OLD.valid_from
  )
BEGIN
    SELECT RAISE(ABORT, 'extraction transition timestamp is invalid or backdated');
END;

CREATE TRIGGER source_item_extractions_no_delete BEFORE DELETE ON source_item_extractions
BEGIN
    SELECT RAISE(ABORT, 'extraction attempts cannot be deleted');
END;

CREATE TRIGGER validate_claim_evidence_ref_insert
BEFORE INSERT ON claim_evidence_refs
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM claims AS claim
        JOIN source_item_extractions AS extraction
          ON extraction.extraction_id = claim.extraction_id
        JOIN source_items AS item
          ON item.source_item_id = NEW.source_item_id
        WHERE claim.claim_id = NEW.claim_id
          AND extraction.status = 'settling'
          AND claim.source_item_id = NEW.source_item_id
          AND item.cleaned_text IS NOT NULL
          AND item.content_sha256 = NEW.source_text_sha256
          AND NOT EXISTS (
              SELECT 1 FROM content_tombstones AS tombstone
              WHERE tombstone.source_item_id = NEW.source_item_id
          )
    ) THEN RAISE(ABORT, 'claim evidence is not bound to retained canonical source text') END;
END;

CREATE TRIGGER validate_claim_extraction_lineage_insert
BEFORE INSERT ON claims
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM source_item_extractions AS extraction
        WHERE extraction.extraction_id = NEW.extraction_id
          AND extraction.status = 'settling'
          AND extraction.source_item_id = NEW.source_item_id
          AND extraction.source_policy_id = NEW.source_policy_id
          AND extraction.prompt_version_id = NEW.prompt_version_id
          AND extraction.model_id = NEW.model_id
          AND extraction.provider_request_id IS NEW.provider_request_id
          AND extraction.batch_submission_request_id IS NEW.batch_submission_request_id
          AND extraction.provider_batch_id IS NEW.provider_batch_id
          AND extraction.provider_custom_id IS NEW.provider_custom_id
          AND extraction.provider_message_id IS NEW.provider_message_id
    ) THEN RAISE(ABORT, 'claim does not match its settling extraction') END;
END;

CREATE TRIGGER validate_claim_player_ref_insert
BEFORE INSERT ON claim_player_refs
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM claims AS claim
        JOIN source_item_extractions AS extraction
          ON extraction.extraction_id = claim.extraction_id
        WHERE claim.claim_id = NEW.claim_id
          AND extraction.status = 'settling'
    ) THEN RAISE(ABORT, 'claim player reference is outside extraction settlement') END;
END;

CREATE TRIGGER validate_episode_claim_insert
BEFORE INSERT ON episode_claims
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM json_each(json_array(
            NEW.as_of, NEW.published_at, NEW.observed_at, NEW.ingested_at,
            NEW.effective_at, NEW.valid_from, NEW.valid_to
        )) AS stamp
        WHERE stamp.value IS NOT NULL AND (
            typeof(stamp.value) <> 'text' OR length(stamp.value) <> 27 OR
            substr(stamp.value, 1, 4) = '0000' OR
            substr(stamp.value, 12, 2) NOT BETWEEN '00' AND '23' OR
            stamp.value NOT GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' OR
            strftime('%Y-%m-%dT%H:%M:%S', stamp.value) IS NOT substr(stamp.value, 1, 19)
        )
    ) THEN RAISE(ABORT, 'episode claim timestamps must be canonical UTC') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM narrative_episodes AS episode
        JOIN claims AS claim ON claim.claim_id = NEW.claim_id
        JOIN source_items AS item ON item.source_item_id = NEW.source_item_id
        JOIN source_item_extractions AS extraction
          ON extraction.extraction_id = claim.extraction_id
        WHERE episode.episode_id = NEW.episode_id
          AND episode.method_version = NEW.method_version
          AND episode.as_of = NEW.as_of
          AND episode.run_id = NEW.run_id
          AND claim.source_item_id = NEW.source_item_id
          AND claim.claim_dimension = episode.claim_dimension
          AND item.source_id = NEW.source_id
          AND extraction.source_family = NEW.source_family
          AND extraction.status = 'succeeded'
          AND claim.observed_at <= NEW.as_of
          AND claim.ingested_at <= NEW.as_of
          AND claim.valid_from <= NEW.as_of
          AND (claim.valid_to IS NULL OR NEW.as_of < claim.valid_to)
          AND item.observed_at <= NEW.as_of
          AND item.ingested_at <= NEW.as_of
          AND item.valid_from <= NEW.as_of
          AND (item.valid_to IS NULL OR NEW.as_of < item.valid_to)
    ) THEN RAISE(ABORT, 'episode claim does not match its episode or point-in-time input') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM narrative_episodes AS episode
        WHERE episode.episode_id = NEW.episode_id
          AND episode.subject_type = 'player'
    ) AND NOT EXISTS (
        SELECT 1
        FROM narrative_episodes AS episode
        JOIN claim_player_refs AS ref
          ON ref.claim_id = NEW.claim_id
         AND ref.player_id = episode.subject_player_id
         AND ref.observed_at <= NEW.as_of AND ref.ingested_at <= NEW.as_of
         AND ref.valid_from <= NEW.as_of
         AND (ref.valid_to IS NULL OR NEW.as_of < ref.valid_to)
        WHERE episode.episode_id = NEW.episode_id
    ) THEN RAISE(ABORT, 'episode claim does not reference its player subject') END;
    SELECT CASE WHEN NEW.relation = 'origin' AND NOT EXISTS (
        SELECT 1 FROM narrative_episodes AS episode
        WHERE episode.episode_id = NEW.episode_id
          AND episode.origin_claim_id = NEW.claim_id
    ) THEN RAISE(ABORT, 'episode origin relation does not match origin claim') END;
    SELECT CASE WHEN NEW.relation <> 'origin' AND EXISTS (
        SELECT 1 FROM narrative_episodes AS episode
        WHERE episode.episode_id = NEW.episode_id
          AND episode.origin_claim_id = NEW.claim_id
    ) THEN RAISE(ABORT, 'episode origin claim must carry the origin relation') END;
    SELECT CASE WHEN NEW.linked_claim_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM episode_claims AS linked
        WHERE linked.episode_id = NEW.episode_id
          AND linked.claim_id = NEW.linked_claim_id
    ) THEN RAISE(ABORT, 'episode claim link must point to an earlier episode member') END;
END;

CREATE TRIGGER validate_narrative_episode_insert
BEFORE INSERT ON narrative_episodes
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM json_each(json_array(
            NEW.opened_at, NEW.last_item_at, NEW.as_of, NEW.published_at,
            NEW.observed_at, NEW.ingested_at, NEW.effective_at, NEW.valid_from,
            NEW.valid_to
        )) AS stamp
        WHERE stamp.value IS NOT NULL AND (
            typeof(stamp.value) <> 'text' OR length(stamp.value) <> 27 OR
            substr(stamp.value, 1, 4) = '0000' OR
            substr(stamp.value, 12, 2) NOT BETWEEN '00' AND '23' OR
            stamp.value NOT GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' OR
            strftime('%Y-%m-%dT%H:%M:%S', stamp.value) IS NOT substr(stamp.value, 1, 19)
        )
    ) THEN RAISE(ABORT, 'narrative episode timestamps must be canonical UTC') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM model_runs AS run
        WHERE run.run_id = NEW.run_id
          AND run.run_type = 'stage_2_episodes'
          AND run.status = 'running'
          AND run.started_at = NEW.observed_at
    ) THEN RAISE(ABORT, 'narrative episode must belong to its running Stage 2 run') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM claims AS claim
        WHERE claim.claim_id = NEW.origin_claim_id
          AND claim.claim_dimension = NEW.claim_dimension
          AND claim.observed_at <= NEW.as_of
          AND claim.ingested_at <= NEW.as_of
          AND claim.valid_from <= NEW.as_of
          AND (claim.valid_to IS NULL OR NEW.as_of < claim.valid_to)
    ) THEN RAISE(ABORT, 'narrative episode origin is not eligible at its as-of') END;
    SELECT CASE WHEN NEW.subject_type = 'player' AND NOT EXISTS (
        SELECT 1 FROM claim_player_refs AS ref
        WHERE ref.claim_id = NEW.origin_claim_id
          AND ref.player_id = NEW.subject_player_id
          AND ref.observed_at <= NEW.as_of AND ref.ingested_at <= NEW.as_of
          AND ref.valid_from <= NEW.as_of
          AND (ref.valid_to IS NULL OR NEW.as_of < ref.valid_to)
    ) THEN RAISE(ABORT, 'player episode origin does not reference its subject') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM claims AS claim
        JOIN source_item_extractions AS extraction
          ON extraction.extraction_id = claim.extraction_id
        WHERE claim.claim_id = NEW.origin_claim_id
          AND extraction.prompt_version_id = NEW.prompt_version_id
    ) THEN RAISE(ABORT, 'narrative episode origin was not extracted under its prompt version') END;
END;

CREATE TRIGGER validate_source_item_extraction_insert
BEFORE INSERT ON source_item_extractions
BEGIN
    SELECT CASE WHEN NEW.status <> 'creating'
        THEN RAISE(ABORT, 'extraction attempts must begin in creating state') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM source_items AS item
        JOIN source_policies AS policy ON policy.source_policy_id = NEW.source_policy_id
        WHERE item.source_item_id = NEW.source_item_id
          AND item.source_id = policy.source_id
          AND item.content_sha256 = NEW.source_content_sha256
    ) THEN RAISE(ABORT, 'extraction policy/content lineage mismatch') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM source_item_extractions AS prior
        WHERE prior.source_item_id = NEW.source_item_id
          AND prior.prompt_version_id = NEW.prompt_version_id
          AND prior.model_id = NEW.model_id
          AND (
              (prior.status IN ('creating', 'submitted', 'settling') AND
               NEW.status IN ('succeeded', 'flagged')) OR
              (prior.status IN ('succeeded', 'flagged') AND
               NEW.status IN ('creating', 'submitted', 'settling'))
          )
    ) THEN RAISE(ABORT, 'terminal and inflight extraction states cannot coexist') END;
END;

CREATE TRIGGER validate_source_item_extraction_update
BEFORE UPDATE ON source_item_extractions
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM source_items AS item
        JOIN source_policies AS policy ON policy.source_policy_id = NEW.source_policy_id
        WHERE item.source_item_id = NEW.source_item_id
          AND item.source_id = policy.source_id
          AND item.content_sha256 = NEW.source_content_sha256
    ) THEN RAISE(ABORT, 'extraction policy/content lineage mismatch') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM source_item_extractions AS prior
        WHERE prior.extraction_id <> OLD.extraction_id
          AND prior.source_item_id = NEW.source_item_id
          AND prior.prompt_version_id = NEW.prompt_version_id
          AND prior.model_id = NEW.model_id
          AND (
              (prior.status IN ('creating', 'submitted', 'settling') AND
               NEW.status IN ('succeeded', 'flagged')) OR
              (prior.status IN ('succeeded', 'flagged') AND
               NEW.status IN ('creating', 'submitted', 'settling'))
          )
    ) THEN RAISE(ABORT, 'terminal and inflight extraction states cannot coexist') END;
END;

CREATE TRIGGER validate_source_item_review_flag_insert
BEFORE INSERT ON source_item_review_flags
BEGIN
    SELECT CASE WHEN NEW.review_status <> 'pending' OR NEW.reviewed_at IS NOT NULL
        THEN RAISE(ABORT, 'review flags must begin pending and unreviewed') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM source_items AS item
        JOIN source_policies AS policy ON policy.source_policy_id = NEW.source_policy_id
        WHERE item.source_item_id = NEW.source_item_id
          AND item.source_id = NEW.source_id
          AND policy.source_id = NEW.source_id
    ) THEN RAISE(ABORT, 'review flag policy/source lineage mismatch') END;
    SELECT CASE WHEN NEW.flag_type = 'prompt_injection_input' AND (
        NEW.provider_request_id IS NOT NULL OR
        NEW.batch_submission_request_id IS NOT NULL OR
        NEW.provider_batch_id IS NOT NULL OR NEW.provider_custom_id IS NOT NULL
    ) THEN RAISE(ABORT, 'input injection flags cannot carry provider trace') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM source_item_extractions AS extraction
        WHERE extraction.source_item_id = NEW.source_item_id
          AND extraction.source_policy_id = NEW.source_policy_id
          AND extraction.prompt_version_id = NEW.prompt_version_id
          AND extraction.model_id = NEW.model_id
          AND extraction.status = 'flagged'
          AND extraction.error_code = NEW.flag_type
          AND extraction.provider_request_id IS NEW.provider_request_id
          AND extraction.batch_submission_request_id IS NEW.batch_submission_request_id
          AND extraction.provider_batch_id IS NEW.provider_batch_id
          AND extraction.provider_custom_id IS NEW.provider_custom_id
    ) THEN RAISE(ABORT, 'review flag does not match its terminal extraction trace') END;
END;

-- New diagnostic columns are written only at refusal and cleared only by tombstones.
CREATE TRIGGER source_item_extraction_diagnostics_insert
BEFORE INSERT ON source_item_extractions
WHEN NEW.error_detail_json IS NOT NULL OR NEW.refusal_bucket IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'extraction diagnostics require a failed transition');
END;

CREATE TRIGGER source_item_extraction_diagnostics_update
BEFORE UPDATE ON source_item_extractions
WHEN NOT (
    (NEW.error_detail_json IS OLD.error_detail_json AND NEW.refusal_bucket IS OLD.refusal_bucket) OR
    (OLD.status = 'submitted' AND NEW.status = 'failed') OR
    (OLD.status = 'failed' AND NEW.status = 'failed' AND NEW.error_detail_json IS NULL AND
     NEW.refusal_bucket IS OLD.refusal_bucket AND EXISTS (
         SELECT 1 FROM content_tombstones WHERE source_item_id = OLD.source_item_id
     ))
)
BEGIN
    SELECT RAISE(ABORT, 'extraction diagnostics are immutable except for redaction');
END;

CREATE TRIGGER source_item_extraction_no_tombstoned_output
BEFORE UPDATE ON source_item_extractions
WHEN NEW.status = 'failed' AND
     (NEW.output_json IS NOT NULL OR NEW.error_detail_json IS NOT NULL OR NEW.error_message IS NOT NULL)
     AND EXISTS (SELECT 1 FROM content_tombstones WHERE source_item_id = NEW.source_item_id)
BEGIN
    SELECT RAISE(ABORT, 'tombstoned extraction cannot retain failed output or diagnostics');
END;
