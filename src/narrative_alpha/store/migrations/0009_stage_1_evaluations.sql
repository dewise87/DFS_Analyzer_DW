-- Stage 1 labeled evaluations and the Slice 17 operational follow-ups.

CREATE TABLE model_evals (
    model_eval_id TEXT PRIMARY KEY,
    prompt_version_id TEXT NOT NULL REFERENCES prompt_versions(prompt_version_id),
    model_id TEXT NOT NULL CHECK(length(trim(model_id)) > 0),
    label_set_sha256 TEXT NOT NULL CHECK(
        length(label_set_sha256) = 64 AND label_set_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    item_count INTEGER NOT NULL CHECK(item_count > 0),
    label_row_count INTEGER NOT NULL CHECK(label_row_count >= item_count),
    metrics_json TEXT NOT NULL CHECK(
        json_valid(metrics_json) AND json_type(metrics_json) = 'object'
    ),
    source TEXT NOT NULL CHECK(length(trim(source)) > 0),
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT NOT NULL REFERENCES model_runs(run_id),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE INDEX idx_model_evals_prompt_model
    ON model_evals(prompt_version_id, model_id, observed_at);
CREATE INDEX idx_model_evals_label_set
    ON model_evals(label_set_sha256, observed_at);

CREATE TRIGGER validate_model_eval_insert
BEFORE INSERT ON model_evals
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
    ) THEN RAISE(ABORT, 'model evaluation timestamps must be canonical UTC') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM model_runs AS run
        WHERE run.run_id = NEW.run_id
          AND run.run_type = 'stage_1_eval'
          AND run.status = 'running'
          AND run.started_at = NEW.observed_at
    ) THEN RAISE(ABORT, 'model evaluation must belong to its running evaluation run') END;
END;

CREATE TRIGGER model_evals_immutable_update BEFORE UPDATE ON model_evals
BEGIN
    SELECT RAISE(ABORT, 'model evaluations are immutable');
END;

CREATE TRIGGER model_evals_no_delete BEFORE DELETE ON model_evals
BEGIN
    SELECT RAISE(ABORT, 'model evaluations cannot be deleted');
END;

-- The initial recovery-lineage table omitted when an edge was observed. Backfill old rows
-- from their child run, then require canonical timestamps on every future edge.
DROP TRIGGER model_run_parents_immutable_update;

ALTER TABLE model_run_parents ADD COLUMN observed_at TEXT;
ALTER TABLE model_run_parents ADD COLUMN ingested_at TEXT;

UPDATE model_run_parents
SET observed_at = (
        SELECT started_at FROM model_runs WHERE run_id = model_run_parents.child_run_id
    ),
    ingested_at = (
        SELECT created_at FROM model_runs WHERE run_id = model_run_parents.child_run_id
    );

CREATE TRIGGER model_run_parents_immutable_update BEFORE UPDATE ON model_run_parents
BEGIN
    SELECT RAISE(ABORT, 'model run parent lineage is immutable');
END;

CREATE TRIGGER validate_model_run_parent_insert
BEFORE INSERT ON model_run_parents
BEGIN
    SELECT CASE WHEN NEW.observed_at IS NULL OR NEW.ingested_at IS NULL OR EXISTS (
        SELECT 1 FROM json_each(json_array(NEW.observed_at, NEW.ingested_at)) AS stamp
        WHERE typeof(stamp.value) <> 'text' OR length(stamp.value) <> 27 OR
              substr(stamp.value, 1, 4) = '0000' OR
              substr(stamp.value, 12, 2) NOT BETWEEN '00' AND '23' OR
              stamp.value NOT GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' OR
              strftime('%Y-%m-%dT%H:%M:%S', stamp.value) IS NOT substr(stamp.value, 1, 19)
    ) THEN RAISE(ABORT, 'model run parent timestamps must be canonical UTC') END;
END;

-- These are the high-volume reads in backlog planning and capped retry checks.
CREATE INDEX idx_source_items_stage1_window
    ON source_items(observed_at, source_item_id);
CREATE INDEX idx_source_item_extractions_failed_attempts
    ON source_item_extractions(source_item_id, prompt_version_id, model_id)
    WHERE status = 'failed';
CREATE INDEX idx_players_stage1_name
    ON players(canonical_name COLLATE NOCASE, observed_at, valid_from, valid_to);
CREATE INDEX idx_players_stage1_normalized_name
    ON players(
        lower(
            replace(
                replace(
                    replace(replace(canonical_name, '.', ''), char(39), ''),
                    char(8217), ''
                ),
                '-', ' '
            )
        ),
        player_id
    );

-- Policy/source fences must bind through the item's source identity. The `source` column on
-- an extraction is provenance (currently the extractor name), not a source-key foreign key.
DROP TRIGGER fence_source_policy_insert_during_stage1_submission;
DROP TRIGGER fence_source_policy_update_during_stage1_submission;
DROP TRIGGER fence_source_policy_delete_during_stage1_submission;
DROP TRIGGER fence_source_insert_during_stage1_submission;
DROP TRIGGER fence_source_update_during_stage1_submission;
DROP TRIGGER fence_source_delete_during_stage1_submission;

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
