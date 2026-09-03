-- Slice 34: immutable nearest-target claim grades and multidimensional source ledger.

CREATE TABLE claim_grades (
    claim_grade_id TEXT PRIMARY KEY,
    grading_run_id TEXT NOT NULL CHECK(length(trim(grading_run_id)) > 0),
    season INTEGER NOT NULL CHECK(season >= 1),
    week INTEGER NOT NULL CHECK(week >= 1 AND week <= 99),
    site TEXT NOT NULL CHECK(site IN ('draftkings', 'fanduel')),
    slate_id INTEGER NOT NULL REFERENCES slates(slate_id),
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    team TEXT NOT NULL CHECK(length(trim(team)) > 0),
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    source_id TEXT NOT NULL REFERENCES source_keys(source_id),
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
    claim_falsifiable INTEGER NOT NULL CHECK(claim_falsifiable IN (0, 1)),
    grade_target_key TEXT NOT NULL CHECK(length(trim(grade_target_key)) > 0),
    rule_id TEXT,
    rule_sha256 TEXT CHECK(
        rule_sha256 IS NULL OR
        (length(rule_sha256) = 64 AND rule_sha256 NOT GLOB '*[^0-9a-f]*')
    ),
    grading_config_version TEXT NOT NULL CHECK(length(trim(grading_config_version)) > 0),
    grading_config_sha256 TEXT NOT NULL CHECK(
        length(grading_config_sha256) = 64 AND
        grading_config_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    result_id INTEGER REFERENCES results(result_id),
    availability_id TEXT REFERENCES player_availability(availability_id),
    actual_ownership_id INTEGER REFERENCES actual_ownership(actual_ownership_id),
    ownership_baseline_id INTEGER REFERENCES ownership_baselines(ownership_baseline_id),
    outcome_json TEXT NOT NULL CHECK(
        json_valid(outcome_json) AND json_type(outcome_json) = 'object'
    ),
    verdict TEXT NOT NULL CHECK(
        verdict IN ('correct', 'incorrect', 'ungradable', 'indeterminate')
    ),
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    claim_observed_at TEXT NOT NULL,
    slate_lock_at TEXT NOT NULL,
    lead_time_minutes REAL NOT NULL CHECK(lead_time_minutes >= 0),
    graded_at TEXT NOT NULL,
    UNIQUE(grading_run_id, claim_id, grade_target_key),
    CHECK(
        (rule_id IS NULL AND rule_sha256 IS NULL AND verdict = 'ungradable') OR
        (rule_id IS NOT NULL AND length(trim(rule_id)) > 0 AND rule_sha256 IS NOT NULL
         AND verdict <> 'ungradable')
    ),
    CHECK(slate_lock_at >= claim_observed_at),
    CHECK(graded_at >= slate_lock_at),
    CHECK(
        abs(
            lead_time_minutes -
            ((julianday(slate_lock_at) - julianday(claim_observed_at)) * 1440.0)
        ) < 0.001
    )
) STRICT;

CREATE INDEX idx_claim_grades_week
    ON claim_grades(season, week, site, graded_at, claim_grade_id);
CREATE INDEX idx_claim_grades_source_cell
    ON claim_grades(source_id, team, claim_type, claim_dimension, graded_at);
CREATE INDEX idx_claim_grades_latest_target
    ON claim_grades(claim_id, grade_target_key, graded_at DESC, claim_grade_id DESC);

CREATE TRIGGER validate_claim_grade_timestamps_insert
BEFORE INSERT ON claim_grades
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM json_each(json_array(
            NEW.claim_observed_at, NEW.slate_lock_at, NEW.graded_at
        )) AS stamp
        WHERE typeof(stamp.value) <> 'text' OR length(stamp.value) <> 27 OR
              substr(stamp.value, 1, 4) = '0000' OR
              substr(stamp.value, 12, 2) NOT BETWEEN '00' AND '23' OR
              stamp.value NOT GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' OR
              strftime('%Y-%m-%dT%H:%M:%S', stamp.value) IS NOT substr(stamp.value, 1, 19)
    ) THEN RAISE(ABORT, 'claim grade timestamps must be canonical UTC') END;
END;

CREATE TRIGGER validate_claim_grade_lineage_insert
BEFORE INSERT ON claim_grades
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM claims AS claim
        JOIN claim_player_refs AS ref ON ref.claim_id = claim.claim_id
        WHERE claim.claim_id = NEW.claim_id
          AND ref.player_id = NEW.player_id
          AND claim.source = NEW.source_id
          AND claim.claim_type = NEW.claim_type
          AND claim.claim_dimension = NEW.claim_dimension
          AND claim.falsifiable = NEW.claim_falsifiable
          AND claim.observed_at = NEW.claim_observed_at
    ) THEN RAISE(ABORT, 'claim grade does not match its immutable claim/player lineage') END;
    SELECT CASE WHEN NEW.result_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM results AS result
        JOIN salaries AS salary
          ON salary.player_id = result.player_id AND salary.game_id = result.game_id
        JOIN teams AS team ON team.team_id = salary.team_id
        WHERE result.result_id = NEW.result_id
          AND result.player_id = NEW.player_id AND result.site = NEW.site
          AND salary.slate_id = NEW.slate_id AND team.abbreviation = NEW.team
    ) THEN RAISE(ABORT, 'claim grade result does not match its slate/player/team target') END;
    SELECT CASE WHEN NEW.availability_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM player_availability AS availability
        WHERE availability.availability_id = NEW.availability_id
          AND availability.slate_id = NEW.slate_id
          AND availability.player_id = NEW.player_id
          AND availability.site = NEW.site
          AND availability.season = NEW.season AND availability.week = NEW.week
    ) THEN RAISE(ABORT, 'claim grade availability does not match its target') END;
    SELECT CASE WHEN NEW.actual_ownership_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM actual_ownership AS actual
        WHERE actual.actual_ownership_id = NEW.actual_ownership_id
          AND actual.slate_id = NEW.slate_id AND actual.player_id = NEW.player_id
          AND actual.site = NEW.site
    ) THEN RAISE(ABORT, 'claim grade actual ownership does not match its target') END;
    SELECT CASE WHEN NEW.ownership_baseline_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM ownership_baselines AS baseline
        WHERE baseline.ownership_baseline_id = NEW.ownership_baseline_id
          AND baseline.slate_id = NEW.slate_id AND baseline.player_id = NEW.player_id
          AND baseline.site = NEW.site
    ) THEN RAISE(ABORT, 'claim grade ownership baseline does not match its target') END;
END;

CREATE TRIGGER claim_grades_no_update
BEFORE UPDATE ON claim_grades
BEGIN
    SELECT RAISE(ABORT, 'claim_grades is append-only');
END;

CREATE TRIGGER claim_grades_no_delete
BEFORE DELETE ON claim_grades
BEGIN
    SELECT RAISE(ABORT, 'claim_grades is append-only');
END;

CREATE TABLE source_credibility (
    source_credibility_id TEXT PRIMARY KEY,
    grading_run_id TEXT NOT NULL CHECK(length(trim(grading_run_id)) > 0),
    season INTEGER NOT NULL CHECK(season >= 1),
    week INTEGER NOT NULL CHECK(week >= 1 AND week <= 99),
    source_id TEXT NOT NULL REFERENCES source_keys(source_id),
    team TEXT NOT NULL CHECK(length(trim(team)) > 0),
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
    as_of_at TEXT NOT NULL,
    n_graded INTEGER NOT NULL CHECK(n_graded >= 0),
    correct_count INTEGER NOT NULL CHECK(correct_count >= 0),
    incorrect_count INTEGER NOT NULL CHECK(incorrect_count >= 0),
    indeterminate_count INTEGER NOT NULL CHECK(indeterminate_count >= 0),
    ungradable_count INTEGER NOT NULL CHECK(ungradable_count >= 0),
    beta_prior_alpha REAL NOT NULL CHECK(beta_prior_alpha > 0),
    beta_prior_beta REAL NOT NULL CHECK(beta_prior_beta > 0),
    accuracy_posterior_mean REAL NOT NULL CHECK(accuracy_posterior_mean BETWEEN 0 AND 1),
    accuracy_interval_low REAL NOT NULL CHECK(accuracy_interval_low BETWEEN 0 AND 1),
    accuracy_interval_high REAL NOT NULL CHECK(accuracy_interval_high BETWEEN 0 AND 1),
    posterior_interval_mass REAL NOT NULL CHECK(posterior_interval_mass > 0 AND posterior_interval_mass < 1),
    precision REAL CHECK(precision IS NULL OR precision BETWEEN 0 AND 1),
    coverage REAL NOT NULL CHECK(coverage BETWEEN 0 AND 1),
    average_lead_time_minutes REAL NOT NULL CHECK(average_lead_time_minutes >= 0),
    correction_rate REAL NOT NULL CHECK(correction_rate BETWEEN 0 AND 1),
    last_claim_at TEXT NOT NULL,
    decay_weight REAL NOT NULL CHECK(decay_weight >= 0),
    decay_half_life_days REAL NOT NULL CHECK(decay_half_life_days > 0),
    -- Decay-weighted counts that form the posterior; n_graded stays the raw count.
    weighted_correct REAL NOT NULL DEFAULT 0 CHECK(weighted_correct >= 0),
    weighted_incorrect REAL NOT NULL DEFAULT 0 CHECK(weighted_incorrect >= 0),
    grading_config_version TEXT NOT NULL CHECK(length(trim(grading_config_version)) > 0),
    grading_config_sha256 TEXT NOT NULL CHECK(
        length(grading_config_sha256) = 64 AND
        grading_config_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    UNIQUE(grading_run_id, source_id, team, claim_type, claim_dimension),
    CHECK(n_graded = correct_count + incorrect_count),
    CHECK(accuracy_interval_low <= accuracy_posterior_mean),
    CHECK(accuracy_posterior_mean <= accuracy_interval_high)
) STRICT;

CREATE INDEX idx_source_credibility_report
    ON source_credibility(season, week, as_of_at DESC, grading_run_id, source_id, team);

CREATE TRIGGER validate_source_credibility_timestamps_insert
BEFORE INSERT ON source_credibility
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM json_each(json_array(NEW.as_of_at, NEW.last_claim_at)) AS stamp
        WHERE typeof(stamp.value) <> 'text' OR length(stamp.value) <> 27 OR
              substr(stamp.value, 1, 4) = '0000' OR
              substr(stamp.value, 12, 2) NOT BETWEEN '00' AND '23' OR
              stamp.value NOT GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' OR
              strftime('%Y-%m-%dT%H:%M:%S', stamp.value) IS NOT substr(stamp.value, 1, 19)
    ) THEN RAISE(ABORT, 'source credibility timestamps must be canonical UTC') END;
END;

CREATE TRIGGER source_credibility_no_update
BEFORE UPDATE ON source_credibility
BEGIN
    SELECT RAISE(ABORT, 'source_credibility is append-only');
END;

CREATE TRIGGER source_credibility_no_delete
BEFORE DELETE ON source_credibility
BEGIN
    SELECT RAISE(ABORT, 'source_credibility is append-only');
END;

-- SQLite cannot alter a CHECK constraint. Preserve the complete lane history while adding
-- the results_grade step after results_labels, following migrations 0013 and 0014.
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
            'slate_features', 'slate_build', 'slate_memo',
            'results_capture', 'results_ingest', 'results_replay',
            'results_report', 'results_labels', 'results_grade'
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
