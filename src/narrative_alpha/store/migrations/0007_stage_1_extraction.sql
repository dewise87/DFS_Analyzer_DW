-- Stage 1 structured extraction, replay metadata, and durable source-review flags.

-- Every point-in-time timestamp in this store is written through the canonical UTC-Z
-- chokepoint (27 characters, microsecond precision, trailing Z), and the insert triggers
-- below make the database enforce that for the narrative tables. Under that invariant
-- lexical comparison is exact, so no registered SQL function is needed anywhere and
-- ad-hoc sqlite3 CLI writes keep working.

CREATE TABLE prompt_versions (
    prompt_version_id TEXT PRIMARY KEY,
    stage TEXT NOT NULL CHECK(stage = 'stage_1_extraction'),
    schema_version TEXT NOT NULL CHECK(length(trim(schema_version)) > 0),
    system_prompt TEXT NOT NULL CHECK(length(trim(system_prompt)) > 0),
    user_prompt_template TEXT NOT NULL CHECK(length(trim(user_prompt_template)) > 0),
    output_schema_json TEXT NOT NULL CHECK(
        json_valid(output_schema_json) AND json_type(output_schema_json) = 'object'
    ),
    prompt_sha256 TEXT NOT NULL UNIQUE CHECK(length(prompt_sha256) = 64),
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE TABLE source_item_extractions (
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
            status = 'failed' AND output_json IS NULL AND output_sha256 IS NULL AND
            output_redacted_at IS NULL AND error_code IS NOT NULL
        )
    )
) STRICT;

-- A completed extraction is replayed from storage. Failed transport/schema attempts remain
-- retryable and retain their own request provenance.
CREATE UNIQUE INDEX idx_source_item_extractions_terminal
    ON source_item_extractions(source_item_id, prompt_version_id, model_id)
    WHERE status IN ('succeeded', 'flagged');
CREATE UNIQUE INDEX idx_source_item_extractions_inflight
    ON source_item_extractions(source_item_id, prompt_version_id, model_id)
    WHERE status IN ('creating', 'submitted', 'settling');
CREATE INDEX idx_source_item_extractions_status
    ON source_item_extractions(status, source_item_id);

-- Operational leases serialize non-idempotent batch creation and durable result recovery.
-- They are deliberately mutable/deletable and contain no model or source content.
CREATE TABLE stage1_execution_leases (
    lease_key TEXT PRIMARY KEY CHECK(length(trim(lease_key)) > 0),
    operation_kind TEXT NOT NULL CHECK(operation_kind IN ('submission', 'batch_recovery')),
    owner_run_id TEXT NOT NULL REFERENCES model_runs(run_id),
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    CHECK(
        length(acquired_at) = 27 AND substr(acquired_at, 1, 4) <> '0000' AND
        substr(acquired_at, 12, 2) BETWEEN '00' AND '23' AND
        acquired_at GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND
        strftime('%Y-%m-%dT%H:%M:%S', acquired_at) IS substr(acquired_at, 1, 19)
    ),
    CHECK(
        length(expires_at) = 27 AND substr(expires_at, 1, 4) <> '0000' AND
        substr(expires_at, 12, 2) BETWEEN '00' AND '23' AND
        expires_at GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND
        strftime('%Y-%m-%dT%H:%M:%S', expires_at) IS substr(expires_at, 1, 19)
    ),
    CHECK(expires_at > acquired_at)
) STRICT;

CREATE INDEX idx_stage1_execution_leases_expiry
    ON stage1_execution_leases(expires_at, lease_key);

-- A model run can recover more than one prior batch/run. The legacy single parent_run_id remains
-- a convenience for the common one-parent case; this junction preserves the complete lineage.
CREATE TABLE model_run_parents (
    child_run_id TEXT NOT NULL REFERENCES model_runs(run_id),
    parent_run_id TEXT NOT NULL REFERENCES model_runs(run_id),
    relationship TEXT NOT NULL CHECK(
        relationship IN ('stage1_recovery', 'stage1_recovery_takeover')
    ),
    PRIMARY KEY(child_run_id, parent_run_id, relationship),
    CHECK(child_run_id <> parent_run_id)
) STRICT;

-- Batch create cannot be made idempotent at the provider. A short authorization transaction
-- installs scoped submission fences, then releases SQLite before the HTTP POST. Relevant policy,
-- source, content, and tombstone changes are ordered behind that fence; unrelated writers remain
-- free. Wall-clock expiry recovers from abrupt process death.
CREATE TRIGGER fence_source_policy_insert_during_stage1_submission
BEFORE INSERT ON source_policies
WHEN EXISTS (
    SELECT 1
    FROM stage1_execution_leases AS lease
    JOIN source_item_extractions AS extraction
      ON lease.lease_key = 'submission:' || extraction.extraction_id
    WHERE lease.operation_kind = 'submission'
      AND lease.expires_at > strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')
      AND extraction.source = NEW.source_id
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
    WHERE lease.operation_kind = 'submission'
      AND lease.expires_at > strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')
      AND extraction.source IN (OLD.source_id, NEW.source_id)
)
BEGIN
    SELECT RAISE(ABORT, 'source policy change blocked by active Stage 1 submission fence');
END;

CREATE TRIGGER fence_source_policy_delete_during_stage1_submission
BEFORE DELETE ON source_policies
WHEN EXISTS (
    SELECT 1
    FROM stage1_execution_leases AS lease
    JOIN source_item_extractions AS extraction
      ON lease.lease_key = 'submission:' || extraction.extraction_id
    WHERE lease.operation_kind = 'submission'
      AND lease.expires_at > strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')
      AND extraction.source = OLD.source_id
)
BEGIN
    SELECT RAISE(ABORT, 'source policy change blocked by active Stage 1 submission fence');
END;

CREATE TRIGGER fence_source_insert_during_stage1_submission
BEFORE INSERT ON sources
WHEN EXISTS (
    SELECT 1
    FROM stage1_execution_leases AS lease
    JOIN source_item_extractions AS extraction
      ON lease.lease_key = 'submission:' || extraction.extraction_id
    WHERE lease.operation_kind = 'submission'
      AND lease.expires_at > strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')
      AND extraction.source = NEW.source_id
)
BEGIN
    SELECT RAISE(ABORT, 'source change blocked by active Stage 1 submission fence');
END;

CREATE TRIGGER fence_source_update_during_stage1_submission
BEFORE UPDATE ON sources
WHEN EXISTS (
    SELECT 1
    FROM stage1_execution_leases AS lease
    JOIN source_item_extractions AS extraction
      ON lease.lease_key = 'submission:' || extraction.extraction_id
    WHERE lease.operation_kind = 'submission'
      AND lease.expires_at > strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')
      AND extraction.source IN (OLD.source_id, NEW.source_id)
)
BEGIN
    SELECT RAISE(ABORT, 'source change blocked by active Stage 1 submission fence');
END;

CREATE TRIGGER fence_source_delete_during_stage1_submission
BEFORE DELETE ON sources
WHEN EXISTS (
    SELECT 1
    FROM stage1_execution_leases AS lease
    JOIN source_item_extractions AS extraction
      ON lease.lease_key = 'submission:' || extraction.extraction_id
    WHERE lease.operation_kind = 'submission'
      AND lease.expires_at > strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')
      AND extraction.source = OLD.source_id
)
BEGIN
    SELECT RAISE(ABORT, 'source change blocked by active Stage 1 submission fence');
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

CREATE TABLE source_item_review_flags (
    source_item_review_flag_id TEXT PRIMARY KEY,
    source_item_id INTEGER NOT NULL REFERENCES source_items(source_item_id),
    source_id TEXT NOT NULL REFERENCES source_keys(source_id),
    source_policy_id INTEGER NOT NULL REFERENCES source_policies(source_policy_id),
    flag_type TEXT NOT NULL CHECK(
        flag_type IN (
            'prompt_injection_input',
            'prompt_injection_output',
            'prohibited_output',
            'provider_trace_missing',
            'policy_blocked_output'
        )
    ),
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    prompt_version_id TEXT NOT NULL REFERENCES prompt_versions(prompt_version_id),
    model_id TEXT NOT NULL CHECK(length(trim(model_id)) > 0),
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
    review_status TEXT NOT NULL DEFAULT 'pending' CHECK(
        review_status IN ('pending', 'confirmed', 'dismissed')
    ),
    reviewed_at TEXT,
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(source_item_id, prompt_version_id, model_id, flag_type),
    CHECK(valid_to IS NULL OR valid_to > valid_from),
    CHECK(
        (review_status = 'pending' AND reviewed_at IS NULL) OR
        (review_status IN ('confirmed', 'dismissed') AND reviewed_at IS NOT NULL)
    )
) STRICT;

CREATE INDEX idx_source_item_review_flags_pending
    ON source_item_review_flags(review_status, source_id, source_item_id);

CREATE TABLE claims (
    claim_id TEXT PRIMARY KEY,
    extraction_id TEXT NOT NULL,
    source_item_id INTEGER NOT NULL,
    source_policy_id INTEGER NOT NULL REFERENCES source_policies(source_policy_id),
    prompt_version_id TEXT NOT NULL REFERENCES prompt_versions(prompt_version_id),
    model_id TEXT NOT NULL CHECK(length(trim(model_id)) > 0),
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
    claim_type TEXT NOT NULL CHECK(
        claim_type IN (
            'availability', 'usage', 'health', 'performance_observation',
            'narrative', 'life_event', 'environment', 'team_context',
            'field_propagation', 'none'
        )
    ),
    claim_dimension TEXT NOT NULL CHECK(
        claim_dimension IN (
            'active_status', 'snap_share', 'route_share', 'touch_share',
            'target_share', 'role', 'health', 'efficiency', 'mean', 'tail',
            'dependence', 'ownership', 'none'
        )
    ),
    outcome_direction TEXT NOT NULL CHECK(
        outcome_direction IN ('decrease', 'neutral', 'increase', 'unknown')
    ),
    roster_behavior_direction TEXT NOT NULL CHECK(
        roster_behavior_direction IN ('decrease', 'neutral', 'increase', 'unknown')
    ),
    evidence_class TEXT NOT NULL CHECK(evidence_class IN ('A', 'B', 'C')),
    evidence_basis TEXT NOT NULL CHECK(
        evidence_basis IN (
            'official', 'direct_quote', 'beat_report', 'film_claim',
            'play_by_play', 'statistics', 'community_observation',
            'generic_sentiment', 'joke', 'unknown'
        )
    ),
    falsifiable INTEGER NOT NULL CHECK(falsifiable IN (0, 1)),
    specificity REAL NOT NULL CHECK(specificity >= 0 AND specificity <= 1),
    actionability REAL NOT NULL CHECK(actionability >= 0 AND actionability <= 1),
    novelty TEXT NOT NULL CHECK(
        novelty IN ('new', 'corroborating', 'contradicting', 'derivative', 'stale')
    ),
    model_confidence TEXT NOT NULL CHECK(
        model_confidence IN ('low', 'medium', 'high', 'unknown')
    ),
    team_refs_json TEXT NOT NULL CHECK(
        json_valid(team_refs_json) AND json_type(team_refs_json) = 'array'
    ),
    uncertainty_flags_json TEXT NOT NULL CHECK(
        json_valid(uncertainty_flags_json) AND json_type(uncertainty_flags_json) = 'array'
    ),
    ambiguity_flags_json TEXT NOT NULL CHECK(
        json_valid(ambiguity_flags_json) AND json_type(ambiguity_flags_json) = 'array'
    ),
    suggested_channels_json TEXT NOT NULL CHECK(
        json_valid(suggested_channels_json) AND json_type(suggested_channels_json) = 'array'
    ),
    disconfirming_context TEXT,
    disconfirming_context_sha256 TEXT CHECK(
        disconfirming_context_sha256 IS NULL OR length(disconfirming_context_sha256) = 64
    ),
    context_redacted_at TEXT,
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    FOREIGN KEY(extraction_id, source_item_id)
        REFERENCES source_item_extractions(extraction_id, source_item_id),
    UNIQUE(extraction_id, claim_id),
    CHECK(
        provider_message_id IS NOT NULL AND provider_request_id IS NULL AND
        batch_submission_request_id IS NOT NULL AND
        provider_batch_id IS NOT NULL AND provider_custom_id IS NOT NULL
    ),
    CHECK(
        (
            disconfirming_context IS NULL AND disconfirming_context_sha256 IS NULL AND
            context_redacted_at IS NULL
        ) OR
        (
            disconfirming_context IS NOT NULL AND
            disconfirming_context_sha256 IS NOT NULL AND context_redacted_at IS NULL
        ) OR
        (
            disconfirming_context IS NULL AND
            disconfirming_context_sha256 IS NOT NULL AND context_redacted_at IS NOT NULL
        )
    ),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE INDEX idx_claims_source_item ON claims(source_item_id, claim_id);
CREATE INDEX idx_claims_prompt_model ON claims(prompt_version_id, model_id, claim_id);

CREATE TABLE claim_player_refs (
    claim_player_ref_id INTEGER PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    name_raw TEXT NOT NULL CHECK(length(trim(name_raw)) > 0),
    player_id INTEGER REFERENCES players(player_id),
    unresolved_id INTEGER REFERENCES unresolved_player_matches(unresolved_id),
    resolution_method TEXT,
    resolution_confidence REAL CHECK(
        resolution_confidence IS NULL OR
        (resolution_confidence >= 0 AND resolution_confidence <= 1)
    ),
    manual_override INTEGER NOT NULL CHECK(manual_override IN (0, 1)),
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(claim_id, ordinal),
    CHECK(
        (
            player_id IS NOT NULL AND unresolved_id IS NULL AND
            resolution_method IS NOT NULL AND resolution_confidence IS NOT NULL
        ) OR
        (
            player_id IS NULL AND unresolved_id IS NOT NULL AND
            resolution_method IS NULL AND resolution_confidence IS NULL AND
            manual_override = 0
        )
    ),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE INDEX idx_claim_player_refs_player ON claim_player_refs(player_id, claim_id);
CREATE INDEX idx_claim_player_refs_unresolved ON claim_player_refs(unresolved_id, claim_id);

-- Offsets index the canonical Stage 1 source string, not `source_items.cleaned_text`
-- directly: `normalize_item_text(title, cleaned_text)` (NFKC, whitespace-collapsed,
-- title + "\n" + summary), whose SHA-256 is `source_items.content_sha256` and is copied
-- here as `source_text_sha256` so drift is detectable. Reconstruct the extract by
-- re-deriving that string from the stored title and cleaned text.
CREATE TABLE claim_evidence_refs (
    claim_evidence_ref_id INTEGER PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    source_item_id INTEGER NOT NULL REFERENCES source_items(source_item_id),
    source_text_sha256 TEXT NOT NULL CHECK(length(source_text_sha256) = 64),
    extract_start INTEGER NOT NULL CHECK(extract_start >= 0),
    extract_end INTEGER NOT NULL CHECK(extract_end > extract_start),
    verbatim_extract TEXT CHECK(
        verbatim_extract IS NULL OR
        (length(verbatim_extract) > 0 AND length(verbatim_extract) <= 512)
    ),
    extract_sha256 TEXT NOT NULL CHECK(length(extract_sha256) = 64),
    redacted_at TEXT,
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(claim_id, ordinal),
    CHECK(
        (verbatim_extract IS NOT NULL AND redacted_at IS NULL) OR
        (verbatim_extract IS NULL AND redacted_at IS NOT NULL)
    ),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE INDEX idx_claim_evidence_refs_item
    ON claim_evidence_refs(source_item_id, claim_id);

-- Exact Unicode span equality is checked atomically by the writer against the canonical NFKC
-- source string. SQLite cannot perform NFKC itself, so this trigger enforces that the cited item
-- and exact canonical-text hash are still retained when the already-validated span is inserted.
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
    )
)
BEGIN
    SELECT RAISE(ABORT, 'invalid or immutable extraction state transition');
END;

-- Terminal model artifacts and claims are immutable. The only supported mutation is compliance
-- redaction after a source tombstone, performed by the trigger below while hashes and offsets stay.
CREATE TRIGGER claims_immutable_update
BEFORE UPDATE ON claims
WHEN NOT (
    OLD.disconfirming_context IS NOT NULL AND
    NEW.disconfirming_context IS NULL AND
    NEW.disconfirming_context_sha256 IS OLD.disconfirming_context_sha256 AND
    OLD.context_redacted_at IS NULL AND NEW.context_redacted_at IS NOT NULL AND
    EXISTS (
        SELECT 1 FROM content_tombstones AS tombstone
        WHERE tombstone.source_item_id = OLD.source_item_id
          AND tombstone.tombstoned_at IS NEW.context_redacted_at
    ) AND
    NEW.claim_id IS OLD.claim_id AND NEW.extraction_id IS OLD.extraction_id AND
    NEW.source_item_id IS OLD.source_item_id AND
    NEW.source_policy_id IS OLD.source_policy_id AND
    NEW.prompt_version_id IS OLD.prompt_version_id AND NEW.model_id IS OLD.model_id AND
    NEW.provider_request_id IS OLD.provider_request_id AND
    NEW.batch_submission_request_id IS OLD.batch_submission_request_id AND
    NEW.provider_batch_id IS OLD.provider_batch_id AND
    NEW.provider_custom_id IS OLD.provider_custom_id AND
    NEW.provider_message_id IS OLD.provider_message_id AND
    NEW.claim_type IS OLD.claim_type AND NEW.claim_dimension IS OLD.claim_dimension AND
    NEW.outcome_direction IS OLD.outcome_direction AND
    NEW.roster_behavior_direction IS OLD.roster_behavior_direction AND
    NEW.evidence_class IS OLD.evidence_class AND NEW.evidence_basis IS OLD.evidence_basis AND
    NEW.falsifiable IS OLD.falsifiable AND NEW.specificity IS OLD.specificity AND
    NEW.actionability IS OLD.actionability AND NEW.novelty IS OLD.novelty AND
    NEW.model_confidence IS OLD.model_confidence AND
    NEW.team_refs_json IS OLD.team_refs_json AND
    NEW.uncertainty_flags_json IS OLD.uncertainty_flags_json AND
    NEW.ambiguity_flags_json IS OLD.ambiguity_flags_json AND
    NEW.suggested_channels_json IS OLD.suggested_channels_json AND
    NEW.source IS OLD.source AND NEW.published_at IS OLD.published_at AND
    NEW.observed_at IS OLD.observed_at AND NEW.ingested_at IS OLD.ingested_at AND
    NEW.effective_at IS OLD.effective_at AND NEW.valid_from IS OLD.valid_from AND
    NEW.valid_to IS OLD.valid_to AND NEW.source_version IS OLD.source_version AND
    NEW.run_id IS OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'claims are immutable except for compliance redaction');
END;

CREATE TRIGGER claim_player_refs_immutable_update
BEFORE UPDATE ON claim_player_refs
BEGIN
    SELECT RAISE(ABORT, 'claim player references are immutable');
END;

CREATE TRIGGER claim_evidence_refs_immutable_update
BEFORE UPDATE ON claim_evidence_refs
WHEN NOT (
    OLD.verbatim_extract IS NOT NULL AND NEW.verbatim_extract IS NULL AND
    OLD.redacted_at IS NULL AND NEW.redacted_at IS NOT NULL AND
    EXISTS (
        SELECT 1 FROM content_tombstones AS tombstone
        WHERE tombstone.source_item_id = OLD.source_item_id
          AND tombstone.tombstoned_at IS NEW.redacted_at
    ) AND
    NEW.claim_evidence_ref_id IS OLD.claim_evidence_ref_id AND
    NEW.claim_id IS OLD.claim_id AND NEW.ordinal IS OLD.ordinal AND
    NEW.source_item_id IS OLD.source_item_id AND
    NEW.source_text_sha256 IS OLD.source_text_sha256 AND
    NEW.extract_start IS OLD.extract_start AND NEW.extract_end IS OLD.extract_end AND
    NEW.extract_sha256 IS OLD.extract_sha256 AND NEW.source IS OLD.source AND
    NEW.published_at IS OLD.published_at AND NEW.observed_at IS OLD.observed_at AND
    NEW.ingested_at IS OLD.ingested_at AND NEW.effective_at IS OLD.effective_at AND
    NEW.valid_from IS OLD.valid_from AND NEW.valid_to IS OLD.valid_to AND
    NEW.source_version IS OLD.source_version AND NEW.run_id IS OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'claim evidence references are immutable except for compliance redaction');
END;

CREATE TRIGGER prompt_versions_immutable_update
BEFORE UPDATE ON prompt_versions
BEGIN
    SELECT RAISE(ABORT, 'prompt versions are immutable');
END;

CREATE TRIGGER source_item_review_flags_controlled_update
BEFORE UPDATE ON source_item_review_flags
WHEN NOT (
    OLD.review_status = 'pending' AND
    NEW.review_status IN ('confirmed', 'dismissed') AND
    OLD.reviewed_at IS NULL AND NEW.reviewed_at IS NOT NULL AND
    length(NEW.reviewed_at) = 27 AND substr(NEW.reviewed_at, 1, 4) <> '0000' AND
    substr(NEW.reviewed_at, 12, 2) BETWEEN '00' AND '23' AND
    NEW.reviewed_at GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND
    strftime('%Y-%m-%dT%H:%M:%S', NEW.reviewed_at) IS substr(NEW.reviewed_at, 1, 19) AND
    NEW.reviewed_at >= OLD.observed_at AND NEW.reviewed_at >= OLD.ingested_at AND
    NEW.reviewed_at >= OLD.valid_from AND
    NEW.source_item_review_flag_id IS OLD.source_item_review_flag_id AND
    NEW.source_item_id IS OLD.source_item_id AND NEW.source_id IS OLD.source_id AND
    NEW.source_policy_id IS OLD.source_policy_id AND NEW.flag_type IS OLD.flag_type AND
    NEW.reason IS OLD.reason AND NEW.prompt_version_id IS OLD.prompt_version_id AND
    NEW.model_id IS OLD.model_id AND
    NEW.provider_request_id IS OLD.provider_request_id AND
    NEW.batch_submission_request_id IS OLD.batch_submission_request_id AND
    NEW.provider_batch_id IS OLD.provider_batch_id AND
    NEW.provider_custom_id IS OLD.provider_custom_id AND NEW.source IS OLD.source AND
    NEW.published_at IS OLD.published_at AND NEW.observed_at IS OLD.observed_at AND
    NEW.ingested_at IS OLD.ingested_at AND NEW.effective_at IS OLD.effective_at AND
    NEW.valid_from IS OLD.valid_from AND NEW.valid_to IS OLD.valid_to AND
    NEW.source_version IS OLD.source_version AND NEW.run_id IS OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'review flag provenance is immutable');
END;

CREATE TRIGGER prompt_versions_no_delete BEFORE DELETE ON prompt_versions
BEGIN
    SELECT RAISE(ABORT, 'prompt versions cannot be deleted');
END;

CREATE TRIGGER source_item_extractions_no_delete BEFORE DELETE ON source_item_extractions
BEGIN
    SELECT RAISE(ABORT, 'extraction attempts cannot be deleted');
END;

CREATE TRIGGER source_item_review_flags_no_delete BEFORE DELETE ON source_item_review_flags
BEGIN
    SELECT RAISE(ABORT, 'source review flags cannot be deleted');
END;

CREATE TRIGGER claims_no_delete BEFORE DELETE ON claims
BEGIN
    SELECT RAISE(ABORT, 'claims cannot be deleted');
END;

CREATE TRIGGER claim_player_refs_no_delete BEFORE DELETE ON claim_player_refs
BEGIN
    SELECT RAISE(ABORT, 'claim player references cannot be deleted');
END;

CREATE TRIGGER claim_evidence_refs_no_delete BEFORE DELETE ON claim_evidence_refs
BEGIN
    SELECT RAISE(ABORT, 'claim evidence references cannot be deleted');
END;

CREATE TRIGGER model_run_parents_immutable_update BEFORE UPDATE ON model_run_parents
BEGIN
    SELECT RAISE(ABORT, 'model run parent lineage is immutable');
END;

CREATE TRIGGER model_run_parents_no_delete BEFORE DELETE ON model_run_parents
BEGIN
    SELECT RAISE(ABORT, 'model run parent lineage cannot be deleted');
END;

CREATE TRIGGER validate_content_tombstone_insert
BEFORE INSERT ON content_tombstones
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM source_items AS item
        WHERE item.source_item_id = NEW.source_item_id
          AND item.source_id = NEW.source_id
          AND item.content_sha256 = NEW.content_sha256
    ) THEN RAISE(ABORT, 'content tombstone does not match its source item') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM json_each(json_array(
            NEW.tombstoned_at, NEW.published_at, NEW.observed_at, NEW.ingested_at,
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
    ) THEN RAISE(ABORT, 'content tombstone timestamps must be canonical UTC') END;
    SELECT CASE WHEN NEW.valid_to IS NOT NULL
                     AND NEW.valid_to <= NEW.valid_from
                THEN RAISE(ABORT, 'content tombstone valid_to must be later than valid_from') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM source_items AS item
        WHERE item.source_item_id = NEW.source_item_id
          AND item.observed_at > NEW.tombstoned_at
    ) THEN RAISE(ABORT, 'content tombstone cannot predate its source item') END;
END;

CREATE TRIGGER content_tombstones_immutable_update BEFORE UPDATE ON content_tombstones
BEGIN
    SELECT RAISE(ABORT, 'content tombstones are immutable');
END;

CREATE TRIGGER content_tombstones_no_delete BEFORE DELETE ON content_tombstones
BEGIN
    SELECT RAISE(ABORT, 'content tombstones cannot be deleted');
END;

-- Version rows are append-only. The sole supported mutation closes the current interval;
-- the exact policy/configuration bytes that authorized a provider request never change.
CREATE TRIGGER source_policies_controlled_update
BEFORE UPDATE ON source_policies
WHEN NOT (
    OLD.valid_to IS NULL AND NEW.valid_to IS NOT NULL AND
    length(NEW.valid_to) = 27 AND substr(NEW.valid_to, 1, 4) <> '0000' AND
    substr(NEW.valid_to, 12, 2) BETWEEN '00' AND '23' AND
    NEW.valid_to GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND
    strftime('%Y-%m-%dT%H:%M:%S', NEW.valid_to) IS substr(NEW.valid_to, 1, 19) AND
    NEW.valid_to > OLD.valid_from AND
    NEW.source_policy_id IS OLD.source_policy_id AND
    NEW.source_id IS OLD.source_id AND NEW.permitted_use IS OLD.permitted_use AND
    NEW.raw_retention_days IS OLD.raw_retention_days AND
    NEW.personal_data_fields_allowed IS OLD.personal_data_fields_allowed AND
    NEW.must_honor_deletions IS OLD.must_honor_deletions AND
    NEW.redistribution_allowed IS OLD.redistribution_allowed AND
    NEW.third_party_processing_allowed IS OLD.third_party_processing_allowed AND
    NEW.commercial_use_status IS OLD.commercial_use_status AND
    NEW.terms_reviewed_at IS OLD.terms_reviewed_at AND NEW.source IS OLD.source AND
    NEW.published_at IS OLD.published_at AND NEW.observed_at IS OLD.observed_at AND
    NEW.ingested_at IS OLD.ingested_at AND NEW.effective_at IS OLD.effective_at AND
    NEW.valid_from IS OLD.valid_from AND NEW.source_version IS OLD.source_version AND
    NEW.run_id IS OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'source policy versions are immutable except for interval closure');
END;

CREATE TRIGGER source_policies_no_delete BEFORE DELETE ON source_policies
BEGIN
    SELECT RAISE(ABORT, 'source policy versions cannot be deleted');
END;

CREATE TRIGGER sources_controlled_update
BEFORE UPDATE ON sources
WHEN NOT (
    OLD.valid_to IS NULL AND NEW.valid_to IS NOT NULL AND
    length(NEW.valid_to) = 27 AND substr(NEW.valid_to, 1, 4) <> '0000' AND
    substr(NEW.valid_to, 12, 2) BETWEEN '00' AND '23' AND
    NEW.valid_to GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND
    strftime('%Y-%m-%dT%H:%M:%S', NEW.valid_to) IS substr(NEW.valid_to, 1, 19) AND
    NEW.valid_to > OLD.valid_from AND
    NEW.source_record_id IS OLD.source_record_id AND NEW.source_id IS OLD.source_id AND
    NEW.display_name IS OLD.display_name AND NEW.source_family IS OLD.source_family AND
    NEW.collector_kind IS OLD.collector_kind AND NEW.feed_url IS OLD.feed_url AND
    NEW.enabled IS OLD.enabled AND NEW.source IS OLD.source AND
    NEW.published_at IS OLD.published_at AND NEW.observed_at IS OLD.observed_at AND
    NEW.ingested_at IS OLD.ingested_at AND NEW.effective_at IS OLD.effective_at AND
    NEW.valid_from IS OLD.valid_from AND NEW.source_version IS OLD.source_version AND
    NEW.run_id IS OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'source configuration versions are immutable except for interval closure');
END;

CREATE TRIGGER sources_no_delete BEFORE DELETE ON sources
BEGIN
    SELECT RAISE(ABORT, 'source configuration versions cannot be deleted');
END;

-- These version tables remain insertable after migration 0007. Every timestamp must be the
-- canonical UTC-Z form the store writes, so lexical ordering (UNIQUE, indexes, interval
-- checks, the lease fences above) is exact and no registered SQL function is required.
CREATE TRIGGER validate_source_timestamps_insert
BEFORE INSERT ON sources
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM json_each(json_array(
            NEW.published_at, NEW.observed_at, NEW.ingested_at, NEW.effective_at,
            NEW.valid_from, NEW.valid_to
        )) AS stamp
        WHERE stamp.value IS NOT NULL AND (
            typeof(stamp.value) <> 'text' OR length(stamp.value) <> 27 OR
            substr(stamp.value, 1, 4) = '0000' OR
            substr(stamp.value, 12, 2) NOT BETWEEN '00' AND '23' OR
            stamp.value NOT GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' OR
            strftime('%Y-%m-%dT%H:%M:%S', stamp.value) IS NOT substr(stamp.value, 1, 19)
        )
    ) THEN RAISE(ABORT, 'source configuration timestamps must be canonical UTC') END;
    SELECT CASE WHEN NEW.valid_to IS NOT NULL
                     AND NEW.valid_to <= NEW.valid_from
                THEN RAISE(ABORT, 'source configuration valid_to must be later than valid_from') END;
END;

CREATE TRIGGER validate_source_policy_timestamps_insert
BEFORE INSERT ON source_policies
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM json_each(json_array(
            NEW.terms_reviewed_at, NEW.published_at, NEW.observed_at, NEW.ingested_at,
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
    ) THEN RAISE(ABORT, 'source policy timestamps must be canonical UTC') END;
    SELECT CASE WHEN NEW.valid_to IS NOT NULL
                     AND NEW.valid_to <= NEW.valid_from
                THEN RAISE(ABORT, 'source policy valid_to must be later than valid_from') END;
END;

CREATE TRIGGER validate_source_item_timestamps_insert
BEFORE INSERT ON source_items
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM json_each(json_array(
            NEW.published_at, NEW.observed_at, NEW.ingested_at, NEW.effective_at,
            NEW.valid_from, NEW.valid_to
        )) AS stamp
        WHERE stamp.value IS NOT NULL AND (
            typeof(stamp.value) <> 'text' OR length(stamp.value) <> 27 OR
            substr(stamp.value, 1, 4) = '0000' OR
            substr(stamp.value, 12, 2) NOT BETWEEN '00' AND '23' OR
            stamp.value NOT GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' OR
            strftime('%Y-%m-%dT%H:%M:%S', stamp.value) IS NOT substr(stamp.value, 1, 19)
        )
    ) THEN RAISE(ABORT, 'source item timestamps must be canonical UTC') END;
    SELECT CASE WHEN NEW.valid_to IS NOT NULL
                     AND NEW.valid_to <= NEW.valid_from
                THEN RAISE(ABORT, 'source item valid_to must be later than valid_from') END;
END;

-- Audit legacy tombstones before making them immutable or copying their redaction timestamp.
-- The temporary guard triggers provide actionable migration errors while keeping the whole
-- migration transactional.
CREATE TABLE stage1_migration_0007_guard (
    problem TEXT PRIMARY KEY
) STRICT;

CREATE TRIGGER reject_mismatched_legacy_tombstone_0007
BEFORE INSERT ON stage1_migration_0007_guard
WHEN NEW.problem = 'mismatched_tombstone'
BEGIN
    SELECT RAISE(ABORT, 'migration 0007 found a content tombstone that does not match its source item');
END;

CREATE TRIGGER reject_invalid_legacy_tombstone_timestamp_0007
BEFORE INSERT ON stage1_migration_0007_guard
WHEN NEW.problem = 'invalid_tombstone_timestamp'
BEGIN
    SELECT RAISE(ABORT, 'migration 0007 found a content tombstone with a non-canonical UTC timestamp');
END;

CREATE TRIGGER reject_invalid_legacy_source_timestamp_0007
BEFORE INSERT ON stage1_migration_0007_guard
WHEN NEW.problem = 'invalid_source_timestamp'
BEGIN
    SELECT RAISE(ABORT, 'migration 0007 found a source configuration with an invalid timestamp');
END;

CREATE TRIGGER reject_invalid_legacy_policy_timestamp_0007
BEFORE INSERT ON stage1_migration_0007_guard
WHEN NEW.problem = 'invalid_policy_timestamp'
BEGIN
    SELECT RAISE(ABORT, 'migration 0007 found a source policy with an invalid timestamp');
END;

CREATE TRIGGER reject_invalid_legacy_item_timestamp_0007
BEFORE INSERT ON stage1_migration_0007_guard
WHEN NEW.problem = 'invalid_item_timestamp'
BEGIN
    SELECT RAISE(ABORT, 'migration 0007 found a source item with an invalid timestamp');
END;

INSERT INTO stage1_migration_0007_guard(problem)
SELECT 'mismatched_tombstone'
WHERE EXISTS (
    SELECT 1
    FROM content_tombstones AS tombstone
    LEFT JOIN source_items AS item
      ON item.source_item_id = tombstone.source_item_id
     AND item.source_id = tombstone.source_id
     AND item.content_sha256 = tombstone.content_sha256
    WHERE item.source_item_id IS NULL
);

INSERT INTO stage1_migration_0007_guard(problem)
SELECT 'invalid_tombstone_timestamp'
WHERE EXISTS (
    SELECT 1
    FROM content_tombstones AS tombstone,
         json_each(json_array(
             tombstone.tombstoned_at, tombstone.published_at, tombstone.observed_at,
             tombstone.ingested_at, tombstone.effective_at, tombstone.valid_from,
             tombstone.valid_to
         )) AS stamp
    WHERE stamp.value IS NOT NULL AND (
        typeof(stamp.value) <> 'text' OR length(stamp.value) <> 27 OR
        substr(stamp.value, 1, 4) = '0000' OR
        substr(stamp.value, 12, 2) NOT BETWEEN '00' AND '23' OR
        stamp.value NOT GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' OR
        strftime('%Y-%m-%dT%H:%M:%S', stamp.value) IS NOT substr(stamp.value, 1, 19)
    )
)
OR EXISTS (
    SELECT 1 FROM content_tombstones AS tombstone
    WHERE tombstone.valid_to IS NOT NULL
      AND tombstone.valid_to <= tombstone.valid_from
);

INSERT INTO stage1_migration_0007_guard(problem)
SELECT 'invalid_source_timestamp'
WHERE EXISTS (
    SELECT 1
    FROM sources AS source_row,
         json_each(json_array(
             source_row.published_at, source_row.observed_at, source_row.ingested_at,
             source_row.effective_at, source_row.valid_from, source_row.valid_to
         )) AS stamp
    WHERE stamp.value IS NOT NULL AND (
        typeof(stamp.value) <> 'text' OR length(stamp.value) <> 27 OR
        substr(stamp.value, 1, 4) = '0000' OR
        substr(stamp.value, 12, 2) NOT BETWEEN '00' AND '23' OR
        stamp.value NOT GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' OR
        strftime('%Y-%m-%dT%H:%M:%S', stamp.value) IS NOT substr(stamp.value, 1, 19)
    )
)
OR EXISTS (
    SELECT 1 FROM sources AS source_row
    WHERE source_row.valid_to IS NOT NULL
      AND source_row.valid_to <= source_row.valid_from
);

INSERT INTO stage1_migration_0007_guard(problem)
SELECT 'invalid_policy_timestamp'
WHERE EXISTS (
    SELECT 1
    FROM source_policies AS policy,
         json_each(json_array(
             policy.terms_reviewed_at, policy.published_at, policy.observed_at,
             policy.ingested_at, policy.effective_at, policy.valid_from, policy.valid_to
         )) AS stamp
    WHERE stamp.value IS NOT NULL AND (
        typeof(stamp.value) <> 'text' OR length(stamp.value) <> 27 OR
        substr(stamp.value, 1, 4) = '0000' OR
        substr(stamp.value, 12, 2) NOT BETWEEN '00' AND '23' OR
        stamp.value NOT GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' OR
        strftime('%Y-%m-%dT%H:%M:%S', stamp.value) IS NOT substr(stamp.value, 1, 19)
    )
)
OR EXISTS (
    SELECT 1 FROM source_policies AS policy
    WHERE policy.valid_to IS NOT NULL
      AND policy.valid_to <= policy.valid_from
);

INSERT INTO stage1_migration_0007_guard(problem)
SELECT 'invalid_item_timestamp'
WHERE EXISTS (
    SELECT 1
    FROM source_items AS item,
         json_each(json_array(
             item.published_at, item.observed_at, item.ingested_at, item.effective_at,
             item.valid_from, item.valid_to
         )) AS stamp
    WHERE stamp.value IS NOT NULL AND (
        typeof(stamp.value) <> 'text' OR length(stamp.value) <> 27 OR
        substr(stamp.value, 1, 4) = '0000' OR
        substr(stamp.value, 12, 2) NOT BETWEEN '00' AND '23' OR
        stamp.value NOT GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' OR
        strftime('%Y-%m-%dT%H:%M:%S', stamp.value) IS NOT substr(stamp.value, 1, 19)
    )
)
OR EXISTS (
    SELECT 1 FROM source_items AS item
    WHERE item.valid_to IS NOT NULL
      AND item.valid_to <= item.valid_from
);

DROP TRIGGER reject_mismatched_legacy_tombstone_0007;
DROP TRIGGER reject_invalid_legacy_tombstone_timestamp_0007;
DROP TRIGGER reject_invalid_legacy_source_timestamp_0007;
DROP TRIGGER reject_invalid_legacy_policy_timestamp_0007;
DROP TRIGGER reject_invalid_legacy_item_timestamp_0007;
DROP TABLE stage1_migration_0007_guard;

-- Backfill the deletion invariant for databases upgraded from migration 0006, where a
-- valid tombstone could exist without the collector having cleared the corresponding source row.
UPDATE source_items
SET title = NULL, raw_content = NULL, cleaned_text = NULL
WHERE EXISTS (
    SELECT 1 FROM content_tombstones AS tombstone
    WHERE tombstone.source_item_id = source_items.source_item_id
      AND tombstone.source_id = source_items.source_id
      AND tombstone.content_sha256 = source_items.content_sha256
);

-- Collected evidence is append-only audit input. Compliance may erase the reconstructive
-- text only after an exact item/source/hash tombstone exists; identity, hash, timestamps,
-- and provenance remain immutable so a successful extraction cannot be rebound or retained
-- longer by editing its source row.
CREATE TRIGGER source_items_controlled_update
BEFORE UPDATE ON source_items
WHEN NOT (
    NEW.title IS NULL AND NEW.raw_content IS NULL AND NEW.cleaned_text IS NULL AND
    EXISTS (
        SELECT 1 FROM content_tombstones AS tombstone
        WHERE tombstone.source_item_id = OLD.source_item_id
          AND tombstone.source_id = OLD.source_id
          AND tombstone.content_sha256 = OLD.content_sha256
    ) AND
    NEW.source_item_id IS OLD.source_item_id AND NEW.source_id IS OLD.source_id AND
    NEW.external_item_id IS OLD.external_item_id AND
    NEW.canonical_url IS OLD.canonical_url AND
    NEW.content_sha256 IS OLD.content_sha256 AND NEW.source IS OLD.source AND
    NEW.published_at IS OLD.published_at AND NEW.observed_at IS OLD.observed_at AND
    NEW.ingested_at IS OLD.ingested_at AND NEW.effective_at IS OLD.effective_at AND
    NEW.valid_from IS OLD.valid_from AND NEW.valid_to IS OLD.valid_to AND
    NEW.source_version IS OLD.source_version AND NEW.run_id IS OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'source items are immutable except for tombstone-backed redaction');
END;

CREATE TRIGGER source_items_no_delete BEFORE DELETE ON source_items
BEGIN
    SELECT RAISE(ABORT, 'source items cannot be deleted');
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
END;
