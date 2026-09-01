-- Reproducible point-in-time player outcome marginal fits.

CREATE TABLE player_distributions (
    player_distribution_id INTEGER PRIMARY KEY,
    slate_id INTEGER NOT NULL REFERENCES slates(slate_id),
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    position TEXT NOT NULL CHECK(length(trim(position)) > 0),
    source_set_json TEXT NOT NULL CHECK(
        json_valid(source_set_json) AND
        json_type(source_set_json) = 'array' AND
        json_array_length(source_set_json) > 0
    ),
    source_set_sha256 TEXT NOT NULL CHECK(length(source_set_sha256) = 64),
    as_of_at TEXT NOT NULL,
    distribution_family TEXT NOT NULL CHECK(
        distribution_family = 'lognormal'
    ),
    p_active REAL NOT NULL CHECK(p_active >= 0 AND p_active <= 1),
    p_full_role_given_active REAL NOT NULL CHECK(
        p_full_role_given_active >= 0 AND p_full_role_given_active <= 1
    ),
    conditional_location REAL NOT NULL CHECK(conditional_location = 0),
    conditional_scale REAL NOT NULL CHECK(conditional_scale > 0),
    conditional_shape REAL NOT NULL CHECK(conditional_shape > 0),
    input_mean REAL NOT NULL CHECK(input_mean > 0),
    input_floor REAL NOT NULL CHECK(input_floor > 0),
    input_ceiling REAL NOT NULL CHECK(input_ceiling > 0),
    floor_quantile REAL NOT NULL CHECK(floor_quantile > 0 AND floor_quantile < 1),
    ceiling_quantile REAL NOT NULL CHECK(
        ceiling_quantile > 0 AND ceiling_quantile < 1
    ),
    fit_tolerance REAL NOT NULL CHECK(fit_tolerance > 0),
    fit_max_relative_error REAL NOT NULL CHECK(
        fit_max_relative_error >= 0 AND fit_max_relative_error <= fit_tolerance
    ),
    fit_config_sha256 TEXT NOT NULL CHECK(length(fit_config_sha256) = 64),
    fitter_version TEXT NOT NULL CHECK(length(trim(fitter_version)) > 0),
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(slate_id, player_id, source_set_sha256, as_of_at),
    CHECK(input_floor < input_mean AND input_mean < input_ceiling),
    CHECK(floor_quantile < ceiling_quantile),
    CHECK(as_of_at <= ingested_at),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE INDEX idx_player_distributions_slate_as_of
    ON player_distributions(
        slate_id, player_id, source_set_sha256, as_of_at,
        observed_at, valid_from, valid_to
    );
