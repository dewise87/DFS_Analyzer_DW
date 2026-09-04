-- Experimental contest simulation outputs and the optional slate_simulate lane step.

CREATE TABLE simulation_runs (
    simulation_run_id INTEGER PRIMARY KEY,
    decision_snapshot_id TEXT NOT NULL REFERENCES decision_snapshots(decision_snapshot_id),
    contest_id INTEGER NOT NULL REFERENCES contests(contest_id),
    created_at TEXT NOT NULL,
    report_path TEXT NOT NULL CHECK(length(trim(report_path)) > 0),
    report_sha256 TEXT NOT NULL CHECK(
        length(report_sha256) = 64 AND report_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    config_version TEXT NOT NULL CHECK(length(trim(config_version)) > 0),
    config_sha256 TEXT NOT NULL CHECK(
        length(config_sha256) = 64 AND config_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    draw_count INTEGER NOT NULL CHECK(draw_count > 0),
    seed INTEGER NOT NULL CHECK(seed >= 0),
    independent INTEGER NOT NULL CHECK(independent IN (0, 1)),
    ownership_source TEXT NOT NULL CHECK(
        ownership_source IN ('scenario_model', 'vendor_baseline')
    ),
    ownership_scenario_run_id TEXT REFERENCES model_runs(run_id),
    metrics_json TEXT NOT NULL CHECK(
        json_valid(metrics_json) AND json_type(metrics_json) = 'object'
    ),
    CHECK(
        (ownership_source = 'scenario_model' AND ownership_scenario_run_id IS NOT NULL)
        OR
        (ownership_source = 'vendor_baseline' AND ownership_scenario_run_id IS NULL)
    ),
    CHECK(length(created_at) = 27 AND substr(created_at, -1) = 'Z')
) STRICT;

CREATE INDEX idx_simulation_runs_decision_contest
    ON simulation_runs(decision_snapshot_id, contest_id, simulation_run_id);

CREATE TRIGGER simulation_runs_immutable_update
BEFORE UPDATE ON simulation_runs
BEGIN
    SELECT RAISE(ABORT, 'simulation_runs is append-only');
END;

CREATE TRIGGER simulation_runs_no_delete
BEFORE DELETE ON simulation_runs
BEGIN
    SELECT RAISE(ABORT, 'simulation_runs is append-only');
END;

-- SQLite cannot widen a CHECK constraint, so preserve ops history while adding the
-- optional post-memo simulation step.
DROP TRIGGER validate_ops_run_timestamps_insert;
DROP TRIGGER ops_runs_immutable_update;
DROP TRIGGER ops_runs_no_delete;
DROP INDEX idx_ops_runs_step_status;

CREATE TABLE ops_runs_rebuilt (
    ops_run_id INTEGER PRIMARY KEY,
    batch_run_id TEXT NOT NULL CHECK(length(trim(batch_run_id)) > 0),
    step TEXT NOT NULL CHECK(
        step IN (
            'collect', 'purge', 'extract', 'nflverse_refresh', 'episodes',
            'slate_salaries', 'slate_projections', 'slate_episodes',
            'slate_features', 'slate_build', 'slate_memo', 'slate_simulate',
            'results_capture', 'results_ingest', 'results_replay',
            'results_report', 'results_labels', 'results_stats', 'results_grade'
        )
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
    CHECK(
        (status = 'succeeded' AND error_text IS NULL) OR
        (status IN ('failed', 'skipped') AND length(trim(error_text)) > 0)
    )
) STRICT;

INSERT INTO ops_runs_rebuilt (
    ops_run_id, batch_run_id, step, status, started_at, finished_at, summary_json,
    code_version, error_text
)
SELECT
    ops_run_id, batch_run_id, step, status, started_at, finished_at, summary_json,
    code_version, error_text
FROM ops_runs
ORDER BY ops_run_id;

DROP TABLE ops_runs;
ALTER TABLE ops_runs_rebuilt RENAME TO ops_runs;

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
