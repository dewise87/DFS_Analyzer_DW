-- Operator console history: one append-only row per `na-ops batch` step attempt.
--
-- This table is operational metadata, not evidence, so it carries no point-in-time
-- provenance columns. It does obey the same canonical UTC-Z invariant as the narrative
-- tables (27 characters, microsecond precision, trailing Z), enforced at insert, so
-- `started_at`/`finished_at` comparisons stay exact under plain lexical ordering.

CREATE TABLE ops_runs (
    ops_run_id INTEGER PRIMARY KEY,
    batch_run_id TEXT NOT NULL CHECK(length(trim(batch_run_id)) > 0),
    step TEXT NOT NULL CHECK(
        step IN ('collect', 'purge', 'extract', 'nflverse_refresh')
    ),
    status TEXT NOT NULL CHECK(status IN ('succeeded', 'failed', 'skipped')),
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    summary_json TEXT NOT NULL CHECK(
        json_valid(summary_json) AND json_type(summary_json) = 'object'
    ),
    code_version TEXT NOT NULL CHECK(length(trim(code_version)) > 0),
    error_text TEXT,
    CHECK(finished_at >= started_at),
    -- A success explains itself through its summary; anything else must say why in words
    -- the operator can act on without opening the database.
    CHECK(
        (status = 'succeeded' AND error_text IS NULL) OR
        (status IN ('failed', 'skipped') AND length(trim(error_text)) > 0)
    )
) STRICT;

CREATE INDEX idx_ops_runs_step_status
    ON ops_runs(step, status, started_at DESC, ops_run_id DESC);

CREATE TRIGGER validate_ops_run_timestamps_insert
BEFORE INSERT ON ops_runs
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM json_each(json_array(NEW.started_at, NEW.finished_at)) AS stamp
        WHERE stamp.value IS NOT NULL AND (
            typeof(stamp.value) <> 'text' OR length(stamp.value) <> 27 OR
            substr(stamp.value, 1, 4) = '0000' OR
            substr(stamp.value, 12, 2) NOT BETWEEN '00' AND '23' OR
            stamp.value NOT GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' OR
            strftime('%Y-%m-%dT%H:%M:%S', stamp.value) IS NOT substr(stamp.value, 1, 19)
        )
    ) THEN RAISE(ABORT, 'ops run timestamps must be canonical UTC') END;
END;

CREATE TRIGGER ops_runs_immutable_update
BEFORE UPDATE ON ops_runs
BEGIN
    SELECT RAISE(ABORT, 'ops_runs is append-only; record a new attempt instead');
END;

CREATE TRIGGER ops_runs_no_delete
BEFORE DELETE ON ops_runs
BEGIN
    SELECT RAISE(ABORT, 'ops_runs is append-only; history may not be deleted');
END;
