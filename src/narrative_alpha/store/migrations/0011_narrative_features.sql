-- Deterministic Stage 3 narrative heat and Appendix B feature snapshots.

CREATE TABLE narrative_feature_versions (
    feature_version TEXT PRIMARY KEY CHECK(length(trim(feature_version)) > 0),
    formula_version TEXT NOT NULL CHECK(length(trim(formula_version)) > 0),
    config_sha256 TEXT NOT NULL CHECK(length(config_sha256) = 64),
    config_json TEXT NOT NULL CHECK(
        json_valid(config_json) AND json_type(config_json) = 'object'
    ),
    registered_at TEXT NOT NULL,
    source TEXT NOT NULL CHECK(length(trim(source)) > 0)
) STRICT;

CREATE TABLE narrative_features (
    feature_id TEXT PRIMARY KEY CHECK(length(trim(feature_id)) > 0),
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    slate_id INTEGER NOT NULL REFERENCES slates(slate_id),
    contest_archetype TEXT CHECK(
        contest_archetype IS NULL OR contest_archetype IN (
            'cash', 'single_entry', '3max', '20max', 'mass_multi_entry', 'showdown'
        )
    ),
    site TEXT NOT NULL CHECK(site IN ('draftkings', 'fanduel')),
    role TEXT NOT NULL CHECK(role IN ('classic', 'flex', 'captain')),
    as_of TEXT NOT NULL,

    baseline_ownership REAL CHECK(
        baseline_ownership IS NULL OR
        (baseline_ownership >= 0 AND baseline_ownership <= 1)
    ),
    baseline_ownership_change_6h REAL,
    projection_change_6h REAL,
    salary INTEGER NOT NULL CHECK(salary >= 0),
    value_rank REAL,
    position_scarcity REAL,
    alternative_quality_index REAL,

    h_signed REAL NOT NULL,
    h_absolute REAL NOT NULL CHECK(h_absolute >= 0),
    h_mainstream REAL NOT NULL,
    h_dfs REAL NOT NULL,
    h_team_fan REAL NOT NULL,
    h_velocity_6h REAL NOT NULL,
    h_acceleration REAL NOT NULL,
    h_consensus REAL NOT NULL CHECK(h_consensus >= 0 AND h_consensus <= 1),
    h_source_entropy REAL NOT NULL CHECK(
        h_source_entropy >= 0 AND h_source_entropy <= 1
    ),
    h_novelty_share REAL NOT NULL CHECK(
        h_novelty_share >= 0 AND h_novelty_share <= 1
    ),

    h_signed_z REAL NOT NULL CHECK(h_signed_z >= -4 AND h_signed_z <= 4),
    h_absolute_z REAL NOT NULL CHECK(h_absolute_z >= -4 AND h_absolute_z <= 4),
    h_mainstream_z REAL NOT NULL CHECK(h_mainstream_z >= -4 AND h_mainstream_z <= 4),
    h_dfs_z REAL NOT NULL CHECK(h_dfs_z >= -4 AND h_dfs_z <= 4),
    h_team_fan_z REAL NOT NULL CHECK(h_team_fan_z >= -4 AND h_team_fan_z <= 4),
    h_velocity_6h_z REAL NOT NULL CHECK(
        h_velocity_6h_z >= -4 AND h_velocity_6h_z <= 4
    ),
    h_acceleration_z REAL NOT NULL CHECK(
        h_acceleration_z >= -4 AND h_acceleration_z <= 4
    ),
    h_consensus_z REAL NOT NULL CHECK(h_consensus_z >= -4 AND h_consensus_z <= 4),
    h_source_entropy_z REAL NOT NULL CHECK(
        h_source_entropy_z >= -4 AND h_source_entropy_z <= 4
    ),
    h_novelty_share_z REAL NOT NULL CHECK(
        h_novelty_share_z >= -4 AND h_novelty_share_z <= 4
    ),

    unique_episode_count INTEGER NOT NULL CHECK(unique_episode_count >= 0),
    unique_source_count INTEGER NOT NULL CHECK(unique_source_count >= 0),
    unique_author_count INTEGER CHECK(unique_author_count IS NULL OR unique_author_count >= 0),
    source_overlap_index REAL NOT NULL CHECK(
        source_overlap_index >= 0 AND source_overlap_index <= 1
    ),
    unique_episode_count_z REAL NOT NULL CHECK(
        unique_episode_count_z >= -4 AND unique_episode_count_z <= 4
    ),
    unique_source_count_z REAL NOT NULL CHECK(
        unique_source_count_z >= -4 AND unique_source_count_z <= 4
    ),
    unique_author_count_z REAL CHECK(
        unique_author_count_z IS NULL OR
        (unique_author_count_z >= -4 AND unique_author_count_z <= 4)
    ),
    source_overlap_index_z REAL NOT NULL CHECK(
        source_overlap_index_z >= -4 AND source_overlap_index_z <= 4
    ),

    model_version TEXT,
    feature_version TEXT NOT NULL REFERENCES narrative_feature_versions(feature_version),
    formula_version TEXT NOT NULL,
    feature_config_sha256 TEXT NOT NULL CHECK(length(feature_config_sha256) = 64),
    episode_method_version TEXT NOT NULL CHECK(length(trim(episode_method_version)) > 0),
    episode_ids_json TEXT NOT NULL CHECK(
        json_valid(episode_ids_json) AND json_type(episode_ids_json) = 'array'
    ),
    ownership_baseline_ids_json TEXT NOT NULL CHECK(
        json_valid(ownership_baseline_ids_json) AND
        json_type(ownership_baseline_ids_json) = 'array'
    ),
    baseline_ownership_snapshot_id INTEGER REFERENCES ownership_baselines(ownership_baseline_id),
    baseline_previous_snapshot_id INTEGER REFERENCES ownership_baselines(ownership_baseline_id),
    projection_snapshot_id INTEGER REFERENCES projection_snapshots(projection_snapshot_id),
    projection_previous_snapshot_id INTEGER REFERENCES projection_snapshots(projection_snapshot_id),
    salary_id INTEGER NOT NULL REFERENCES salaries(salary_id),
    input_sha256 TEXT NOT NULL CHECK(length(input_sha256) = 64),

    source TEXT NOT NULL CHECK(length(trim(source)) > 0),
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT NOT NULL REFERENCES model_runs(run_id),

    UNIQUE(player_id, slate_id, site, as_of, feature_version),
    CHECK(valid_to IS NULL OR valid_to > valid_from),
    CHECK(h_absolute + 0.000000000001 >= abs(h_signed)),
    CHECK((baseline_ownership IS NULL) = (baseline_ownership_snapshot_id IS NULL)),
    CHECK(
        (baseline_ownership_change_6h IS NULL) =
        (baseline_previous_snapshot_id IS NULL)
    ),
    CHECK(
        (projection_change_6h IS NULL) =
        (projection_previous_snapshot_id IS NULL)
    ),
    CHECK(
        (projection_snapshot_id IS NULL) =
        (projection_previous_snapshot_id IS NULL)
    ),
    CHECK((unique_author_count IS NULL) = (unique_author_count_z IS NULL)),
    CHECK(model_version IS NULL),
    CHECK(effective_at = as_of),
    CHECK(source_version = feature_version)
) STRICT;

CREATE INDEX idx_narrative_features_snapshot
    ON narrative_features(slate_id, site, as_of, feature_version, player_id);
CREATE INDEX idx_narrative_features_player
    ON narrative_features(player_id, as_of, feature_version, slate_id);

CREATE TRIGGER validate_narrative_feature_version_insert
BEFORE INSERT ON narrative_feature_versions
BEGIN
    SELECT CASE WHEN (
        typeof(NEW.registered_at) <> 'text' OR length(NEW.registered_at) <> 27 OR
        substr(NEW.registered_at, 1, 4) = '0000' OR
        substr(NEW.registered_at, 12, 2) NOT BETWEEN '00' AND '23' OR
        NEW.registered_at NOT GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' OR
        strftime('%Y-%m-%dT%H:%M:%S', NEW.registered_at) IS NOT
            substr(NEW.registered_at, 1, 19)
    ) THEN RAISE(ABORT, 'feature-version timestamp must be canonical UTC') END;
END;

CREATE TRIGGER validate_narrative_feature_insert
BEFORE INSERT ON narrative_features
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
    ) THEN RAISE(ABORT, 'narrative feature timestamps must be canonical UTC') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM narrative_feature_versions AS version
        WHERE version.feature_version = NEW.feature_version
          AND version.formula_version = NEW.formula_version
          AND version.config_sha256 = NEW.feature_config_sha256
    ) THEN RAISE(ABORT, 'narrative feature configuration does not match its version') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM model_runs AS run
        WHERE run.run_id = NEW.run_id
          AND run.run_type = 'stage_3_features'
          AND run.status = 'running'
          AND run.started_at = NEW.observed_at
    ) THEN RAISE(ABORT, 'narrative feature must belong to its running Stage 3 run') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM slates AS slate
        WHERE slate.slate_id = NEW.slate_id
          AND slate.site = NEW.site
          AND slate.observed_at <= NEW.as_of AND slate.ingested_at <= NEW.as_of
          AND slate.valid_from <= NEW.as_of
          AND (slate.valid_to IS NULL OR NEW.as_of < slate.valid_to)
    ) THEN RAISE(ABORT, 'narrative feature slate is not eligible at its as-of') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM salaries AS salary
        WHERE salary.salary_id = NEW.salary_id
          AND salary.slate_id = NEW.slate_id
          AND salary.player_id = NEW.player_id
          AND salary.salary = NEW.salary
          AND salary.observed_at <= NEW.as_of AND salary.ingested_at <= NEW.as_of
          AND salary.valid_from <= NEW.as_of
          AND (salary.valid_to IS NULL OR NEW.as_of < salary.valid_to)
    ) THEN RAISE(ABORT, 'narrative feature salary is not eligible at its as-of') END;
    SELECT CASE WHEN json_array_length(NEW.episode_ids_json) <> NEW.unique_episode_count
      OR (SELECT count(DISTINCT value) FROM json_each(NEW.episode_ids_json)) <>
         NEW.unique_episode_count
    THEN RAISE(ABORT, 'narrative feature episode ids must be unique and match the count') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM json_each(NEW.episode_ids_json) AS used
        WHERE NOT EXISTS (
            SELECT 1 FROM narrative_episodes AS episode
            WHERE episode.episode_id = used.value
              AND episode.subject_type = 'player'
              AND episode.subject_player_id = NEW.player_id
              AND episode.method_version = NEW.episode_method_version
              AND episode.as_of = NEW.as_of
        )
    ) THEN RAISE(ABORT, 'narrative feature cites an incompatible episode') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM json_each(NEW.ownership_baseline_ids_json) AS used
        WHERE NOT EXISTS (
            SELECT 1 FROM ownership_baselines AS baseline
            WHERE baseline.ownership_baseline_id = used.value
              AND baseline.slate_id = NEW.slate_id
              AND baseline.player_id = NEW.player_id
              AND baseline.site = NEW.site
              AND baseline.role = NEW.role
              AND baseline.observed_at <= NEW.as_of
              AND baseline.ingested_at <= NEW.as_of
              AND baseline.valid_from <= NEW.as_of
        )
    ) THEN RAISE(ABORT, 'narrative feature cites an incompatible ownership baseline') END;
    SELECT CASE WHEN NEW.baseline_ownership_snapshot_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM ownership_baselines AS baseline
        WHERE baseline.ownership_baseline_id = NEW.baseline_ownership_snapshot_id
          AND baseline.slate_id = NEW.slate_id
          AND baseline.player_id = NEW.player_id
          AND baseline.site = NEW.site AND baseline.role = NEW.role
          AND baseline.ownership = NEW.baseline_ownership
          AND baseline.observed_at <= NEW.as_of AND baseline.ingested_at <= NEW.as_of
          AND baseline.valid_from <= NEW.as_of
          AND (baseline.valid_to IS NULL OR NEW.as_of < baseline.valid_to)
          AND EXISTS (
              SELECT 1 FROM json_each(NEW.ownership_baseline_ids_json) AS used
              WHERE used.value = baseline.ownership_baseline_id
          )
    ) THEN RAISE(ABORT, 'narrative feature baseline value does not match its input') END;
    SELECT CASE WHEN NEW.baseline_previous_snapshot_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM ownership_baselines AS previous
        JOIN ownership_baselines AS current
          ON current.ownership_baseline_id = NEW.baseline_ownership_snapshot_id
        WHERE previous.ownership_baseline_id = NEW.baseline_previous_snapshot_id
          AND previous.slate_id = NEW.slate_id
          AND previous.player_id = NEW.player_id
          AND previous.site = NEW.site AND previous.role = NEW.role
          AND previous.source = current.source
          AND previous.observed_at <= NEW.as_of AND previous.ingested_at <= NEW.as_of
          AND previous.valid_from <= NEW.as_of
          AND EXISTS (
              SELECT 1 FROM json_each(NEW.ownership_baseline_ids_json) AS used
              WHERE used.value = previous.ownership_baseline_id
          )
    ) THEN RAISE(ABORT, 'narrative feature previous baseline is incompatible') END;
    SELECT CASE WHEN NEW.projection_snapshot_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM projection_snapshots AS current
        JOIN projection_snapshots AS previous
          ON previous.projection_snapshot_id = NEW.projection_previous_snapshot_id
        WHERE current.projection_snapshot_id = NEW.projection_snapshot_id
          AND current.slate_id = NEW.slate_id AND current.player_id = NEW.player_id
          AND current.site = NEW.site
          AND previous.slate_id = NEW.slate_id AND previous.player_id = NEW.player_id
          AND previous.site = NEW.site AND previous.source = current.source
          AND current.observed_at <= NEW.as_of AND current.ingested_at <= NEW.as_of
          AND previous.observed_at <= NEW.as_of AND previous.ingested_at <= NEW.as_of
          AND current.valid_from <= NEW.as_of
          AND (current.valid_to IS NULL OR NEW.as_of < current.valid_to)
          AND previous.valid_from <= NEW.as_of
    ) THEN RAISE(ABORT, 'narrative feature projection inputs are incompatible') END;
END;

CREATE TRIGGER narrative_feature_versions_immutable_update
BEFORE UPDATE ON narrative_feature_versions
BEGIN
    SELECT RAISE(ABORT, 'narrative feature versions are append-only');
END;

CREATE TRIGGER narrative_feature_versions_no_delete
BEFORE DELETE ON narrative_feature_versions
BEGIN
    SELECT RAISE(ABORT, 'narrative feature versions are append-only');
END;

CREATE TRIGGER narrative_features_immutable_update
BEFORE UPDATE ON narrative_features
BEGIN
    SELECT RAISE(ABORT, 'narrative features are append-only');
END;

CREATE TRIGGER narrative_features_no_delete
BEFORE DELETE ON narrative_features
BEGIN
    SELECT RAISE(ABORT, 'narrative features are append-only');
END;
