-- Slice 29: bounded ownership fits, generalized evaluations, and immutable scenarios.

DROP TRIGGER validate_model_eval_insert;
DROP TRIGGER model_evals_immutable_update;
DROP TRIGGER model_evals_no_delete;
DROP INDEX idx_model_evals_prompt_model;
DROP INDEX idx_model_evals_label_set;

ALTER TABLE model_evals RENAME TO model_evals_stage1_old;

CREATE TABLE model_evals (
    model_eval_id TEXT PRIMARY KEY,
    evaluation_kind TEXT NOT NULL DEFAULT 'stage1'
        CHECK(evaluation_kind IN ('stage1', 'ownership')),
    prompt_version_id TEXT REFERENCES prompt_versions(prompt_version_id),
    model_id TEXT NOT NULL CHECK(length(trim(model_id)) > 0),
    label_set_sha256 TEXT NOT NULL CHECK(
        length(label_set_sha256) = 64 AND label_set_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    item_count INTEGER NOT NULL CHECK(item_count > 0),
    label_row_count INTEGER NOT NULL CHECK(label_row_count >= item_count),
    metrics_json TEXT NOT NULL CHECK(
        json_valid(metrics_json) AND json_type(metrics_json) = 'object'
    ),
    ownership_archetype TEXT CHECK(
        ownership_archetype IS NULL OR ownership_archetype IN (
            'cash', 'single_entry', '3max', '20max', 'mass_multi_entry', 'showdown'
        )
    ),
    ownership_site TEXT CHECK(
        ownership_site IS NULL OR ownership_site IN ('draftkings', 'fanduel')
    ),
    feature_version TEXT REFERENCES narrative_feature_versions(feature_version),
    config_sha256 TEXT CHECK(config_sha256 IS NULL OR length(config_sha256) = 64),
    report_path TEXT,
    beat_baseline INTEGER CHECK(beat_baseline IS NULL OR beat_baseline IN (0, 1)),
    source TEXT NOT NULL CHECK(length(trim(source)) > 0),
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT NOT NULL REFERENCES model_runs(run_id),
    CHECK(valid_to IS NULL OR valid_to > valid_from),
    CHECK(
        (evaluation_kind = 'stage1' AND prompt_version_id IS NOT NULL
         AND ownership_archetype IS NULL AND ownership_site IS NULL
         AND feature_version IS NULL AND config_sha256 IS NULL
         AND report_path IS NULL AND beat_baseline IS NULL)
        OR
        (evaluation_kind = 'ownership' AND prompt_version_id IS NULL
         AND ownership_archetype IS NOT NULL AND ownership_site IS NOT NULL
         AND feature_version IS NOT NULL AND config_sha256 IS NOT NULL
         AND report_path IS NOT NULL AND beat_baseline IS NOT NULL)
    )
) STRICT;

INSERT INTO model_evals(
    model_eval_id, evaluation_kind, prompt_version_id, model_id, label_set_sha256,
    item_count, label_row_count, metrics_json, ownership_archetype, ownership_site,
    feature_version, config_sha256, report_path, beat_baseline, source, published_at,
    observed_at, ingested_at, effective_at, valid_from, valid_to, source_version, run_id
)
SELECT model_eval_id, 'stage1', prompt_version_id, model_id, label_set_sha256,
       item_count, label_row_count, metrics_json, NULL, NULL, NULL, NULL, NULL, NULL,
       source, published_at, observed_at, ingested_at, effective_at, valid_from, valid_to,
       source_version, run_id
FROM model_evals_stage1_old;

DROP TABLE model_evals_stage1_old;

CREATE INDEX idx_model_evals_prompt_model
    ON model_evals(prompt_version_id, model_id, observed_at);
CREATE INDEX idx_model_evals_label_set
    ON model_evals(label_set_sha256, observed_at);
CREATE INDEX idx_model_evals_ownership
    ON model_evals(
        evaluation_kind, ownership_site, ownership_archetype, feature_version, observed_at
    );

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
            strftime('%Y-%m-%dT%H:%M:%S', stamp.value) IS NOT
                substr(stamp.value, 1, 19)
        )
    ) THEN RAISE(ABORT, 'model evaluation timestamps must be canonical UTC') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM model_runs AS run
        WHERE run.run_id = NEW.run_id
          AND run.run_type = CASE NEW.evaluation_kind
              WHEN 'stage1' THEN 'stage_1_eval' ELSE 'ownership_eval' END
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

CREATE TABLE ownership_model_fits (
    run_id TEXT PRIMARY KEY REFERENCES model_runs(run_id),
    model_version TEXT NOT NULL CHECK(length(trim(model_version)) > 0),
    config_version TEXT NOT NULL CHECK(length(trim(config_version)) > 0),
    config_sha256 TEXT NOT NULL CHECK(length(config_sha256) = 64),
    feature_version TEXT NOT NULL REFERENCES narrative_feature_versions(feature_version),
    site TEXT NOT NULL CHECK(site IN ('draftkings', 'fanduel')),
    contest_archetype TEXT NOT NULL CHECK(
        contest_archetype IN (
            'cash', 'single_entry', '3max', '20max', 'mass_multi_entry', 'showdown'
        )
    ),
    amplitude REAL NOT NULL CHECK(amplitude > 0),
    parameter_names_json TEXT NOT NULL CHECK(
        json_valid(parameter_names_json) AND json_type(parameter_names_json) = 'array'
    ),
    map_parameters_json TEXT NOT NULL CHECK(
        json_valid(map_parameters_json) AND json_type(map_parameters_json) = 'array'
    ),
    covariance_json TEXT NOT NULL CHECK(
        json_valid(covariance_json) AND json_type(covariance_json) = 'array'
    ),
    training_rows INTEGER NOT NULL CHECK(training_rows > 0),
    training_weeks INTEGER NOT NULL CHECK(training_weeks >= 3),
    missing_feature_rows INTEGER NOT NULL CHECK(missing_feature_rows >= 0),
    missing_baseline_rows INTEGER NOT NULL CHECK(missing_baseline_rows >= 0),
    training_start TEXT NOT NULL,
    training_end TEXT NOT NULL,
    input_sha256 TEXT NOT NULL CHECK(length(input_sha256) = 64),
    created_at TEXT NOT NULL,
    source TEXT NOT NULL CHECK(length(trim(source)) > 0),
    -- Quasi-binomial inflation of the Laplace covariance (Pearson chi-square per degree
    -- of freedom, floored at 1). Recorded so a scenario's intervals can be traced.
    dispersion REAL NOT NULL DEFAULT 1.0 CHECK(dispersion >= 1.0),
    CHECK(training_end >= training_start)
) STRICT;

CREATE INDEX idx_ownership_model_fits_selection
    ON ownership_model_fits(site, contest_archetype, created_at, run_id);

CREATE TRIGGER validate_ownership_model_fit_insert
BEFORE INSERT ON ownership_model_fits
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM model_runs AS run
        WHERE run.run_id = NEW.run_id
          AND run.run_type = 'ownership_fit'
          AND run.status = 'running'
          AND run.config_sha256 = NEW.config_sha256
          AND run.started_at = NEW.created_at
    ) THEN RAISE(ABORT, 'ownership fit must belong to its running configured model run') END;
END;

CREATE TRIGGER ownership_model_fits_immutable_update BEFORE UPDATE ON ownership_model_fits
BEGIN
    SELECT RAISE(ABORT, 'ownership model fits are immutable');
END;

CREATE TRIGGER ownership_model_fits_no_delete BEFORE DELETE ON ownership_model_fits
BEGIN
    SELECT RAISE(ABORT, 'ownership model fits cannot be deleted');
END;

CREATE TABLE ownership_scenarios (
    ownership_scenario_id TEXT PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    slate_id INTEGER NOT NULL REFERENCES slates(slate_id),
    site TEXT NOT NULL CHECK(site IN ('draftkings', 'fanduel')),
    contest_archetype TEXT NOT NULL CHECK(
        contest_archetype IN (
            'cash', 'single_entry', '3max', '20max', 'mass_multi_entry', 'showdown'
        )
    ),
    role TEXT NOT NULL CHECK(role IN ('classic', 'flex', 'captain')),
    position TEXT NOT NULL CHECK(length(trim(position)) > 0),
    decision_snapshot_id TEXT NOT NULL REFERENCES decision_snapshots(decision_snapshot_id),
    baseline_ownership REAL NOT NULL CHECK(baseline_ownership BETWEEN 0 AND 1),
    ownership_p10 REAL NOT NULL CHECK(ownership_p10 BETWEEN 0 AND 1),
    ownership_p50 REAL NOT NULL CHECK(ownership_p50 BETWEEN 0 AND 1),
    ownership_p90 REAL NOT NULL CHECK(ownership_p90 BETWEEN 0 AND 1),
    delta_p50 REAL NOT NULL CHECK(delta_p50 BETWEEN -1 AND 1),
    prob_delta_positive REAL NOT NULL CHECK(prob_delta_positive BETWEEN 0 AND 1),
    governance_status TEXT NOT NULL CHECK(
        governance_status IN ('UNVALIDATED', 'TESTING', 'PROVISIONAL', 'VALIDATED')
    ),
    status_multiplier REAL NOT NULL CHECK(status_multiplier BETWEEN 0 AND 1),
    applied_ownership REAL NOT NULL CHECK(applied_ownership BETWEEN 0 AND 1),
    calibrated_to_roster_totals INTEGER NOT NULL
        CHECK(calibrated_to_roster_totals IN (0, 1)),
    model_run_id TEXT NOT NULL REFERENCES ownership_model_fits(run_id),
    run_id TEXT NOT NULL REFERENCES model_runs(run_id),
    model_version TEXT NOT NULL CHECK(length(trim(model_version)) > 0),
    config_sha256 TEXT NOT NULL CHECK(length(config_sha256) = 64),
    feature_version TEXT NOT NULL REFERENCES narrative_feature_versions(feature_version),
    source TEXT NOT NULL CHECK(length(trim(source)) > 0),
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(ownership_p10 <= ownership_p50 AND ownership_p50 <= ownership_p90),
    UNIQUE(run_id, player_id, role)
) STRICT;

CREATE INDEX idx_ownership_scenarios_decision
    ON ownership_scenarios(decision_snapshot_id, contest_archetype, site, role, player_id);

CREATE TRIGGER validate_ownership_scenario_insert
BEFORE INSERT ON ownership_scenarios
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM model_runs AS run
        WHERE run.run_id = NEW.run_id
          AND run.run_type = 'ownership_scenarios'
          AND run.status = 'running'
          AND run.config_sha256 = NEW.config_sha256
          AND run.started_at = NEW.observed_at
    ) THEN RAISE(ABORT, 'ownership scenario must belong to its running configured run') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM ownership_model_fits AS fit
        WHERE fit.run_id = NEW.model_run_id
          AND fit.model_version = NEW.model_version
          AND fit.config_sha256 = NEW.config_sha256
          AND fit.feature_version = NEW.feature_version
          AND fit.site = NEW.site
          AND fit.contest_archetype = NEW.contest_archetype
    ) THEN RAISE(ABORT, 'ownership scenario provenance does not match its model fit') END;
END;

CREATE TRIGGER ownership_scenarios_immutable_update BEFORE UPDATE ON ownership_scenarios
BEGIN
    SELECT RAISE(ABORT, 'ownership scenarios are immutable');
END;

CREATE TRIGGER ownership_scenarios_no_delete BEFORE DELETE ON ownership_scenarios
BEGIN
    SELECT RAISE(ABORT, 'ownership scenarios cannot be deleted');
END;
