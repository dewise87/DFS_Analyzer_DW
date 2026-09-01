-- Phase 0/1 operational schema. Externally sourced tables are append-only versions carrying
-- the complete point-in-time provenance block from design doc section 3.2.

CREATE TABLE model_runs (
    run_id TEXT PRIMARY KEY,
    run_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running', 'succeeded', 'failed', 'degraded')),
    code_version TEXT NOT NULL,
    config_sha256 TEXT CHECK(config_sha256 IS NULL OR length(config_sha256) = 64),
    parent_run_id TEXT REFERENCES model_runs(run_id),
    error_message TEXT,
    created_at TEXT NOT NULL,
    CHECK(completed_at IS NULL OR completed_at >= started_at)
) STRICT;

CREATE TABLE teams (
    team_id INTEGER PRIMARY KEY,
    team_key TEXT NOT NULL,
    abbreviation TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    league TEXT NOT NULL DEFAULT 'NFL',
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(team_key, valid_from),
    UNIQUE(abbreviation, valid_from),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE TABLE players (
    player_id INTEGER PRIMARY KEY,
    player_key TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    position TEXT,
    birth_date TEXT,
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(player_key, valid_from),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE TABLE player_aliases (
    alias_id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    team_id INTEGER REFERENCES teams(team_id),
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    match_method TEXT NOT NULL,
    match_confidence REAL NOT NULL CHECK(match_confidence >= 0 AND match_confidence <= 1),
    manual_override INTEGER NOT NULL DEFAULT 0 CHECK(manual_override IN (0, 1)),
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(source, normalized_alias, team_id, valid_from),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE TABLE external_player_ids (
    external_player_id_record_id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    source TEXT NOT NULL,
    site TEXT,
    external_player_id TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(source, external_player_id, valid_from),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE TABLE games (
    game_id INTEGER PRIMARY KEY,
    external_game_id TEXT NOT NULL,
    season INTEGER NOT NULL CHECK(season >= 1),
    week INTEGER NOT NULL CHECK(week >= 1 AND week <= 99),
    kickoff_at TEXT NOT NULL,
    home_team_id INTEGER NOT NULL REFERENCES teams(team_id),
    away_team_id INTEGER NOT NULL REFERENCES teams(team_id),
    stadium_name TEXT,
    game_status TEXT,
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(source, external_game_id, observed_at),
    CHECK(home_team_id <> away_team_id),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE TABLE slates (
    slate_id INTEGER PRIMARY KEY,
    external_slate_id TEXT NOT NULL,
    site TEXT NOT NULL,
    slate_type TEXT NOT NULL CHECK(slate_type IN ('classic', 'showdown')),
    season INTEGER NOT NULL CHECK(season >= 1),
    week INTEGER NOT NULL CHECK(week >= 1 AND week <= 99),
    name TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    locks_at TEXT NOT NULL,
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(site, external_slate_id, observed_at),
    CHECK(locks_at >= starts_at),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE TABLE salaries (
    salary_id INTEGER PRIMARY KEY,
    slate_id INTEGER NOT NULL REFERENCES slates(slate_id),
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    game_id INTEGER REFERENCES games(game_id),
    team_id INTEGER NOT NULL REFERENCES teams(team_id),
    opponent_team_id INTEGER REFERENCES teams(team_id),
    site_player_id TEXT NOT NULL,
    roster_positions_json TEXT NOT NULL CHECK(json_valid(roster_positions_json)),
    salary INTEGER NOT NULL CHECK(salary >= 0),
    player_status TEXT,
    source_file_sha256 TEXT NOT NULL CHECK(length(source_file_sha256) = 64),
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(slate_id, player_id, observed_at),
    CHECK(opponent_team_id IS NULL OR opponent_team_id <> team_id),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE TABLE projection_snapshots (
    projection_snapshot_id INTEGER PRIMARY KEY,
    slate_id INTEGER NOT NULL REFERENCES slates(slate_id),
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    site TEXT NOT NULL,
    projection_mean REAL NOT NULL,
    projection_floor REAL,
    projection_ceiling REAL,
    ownership_projection REAL CHECK(
        ownership_projection IS NULL OR
        (ownership_projection >= 0 AND ownership_projection <= 1)
    ),
    source_file_sha256 TEXT NOT NULL CHECK(length(source_file_sha256) = 64),
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(source, site, slate_id, player_id, observed_at),
    CHECK(projection_floor IS NULL OR projection_floor <= projection_mean),
    CHECK(projection_ceiling IS NULL OR projection_ceiling >= projection_mean),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE TABLE ownership_baselines (
    ownership_baseline_id INTEGER PRIMARY KEY,
    slate_id INTEGER NOT NULL REFERENCES slates(slate_id),
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    site TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('classic', 'flex', 'captain')),
    ownership REAL NOT NULL CHECK(ownership >= 0 AND ownership <= 1),
    source_file_sha256 TEXT NOT NULL CHECK(length(source_file_sha256) = 64),
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(source, site, slate_id, player_id, role, observed_at),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE TABLE actual_ownership (
    actual_ownership_id INTEGER PRIMARY KEY,
    external_contest_id TEXT NOT NULL,
    site TEXT NOT NULL,
    slate_id INTEGER NOT NULL REFERENCES slates(slate_id),
    contest_archetype TEXT NOT NULL CHECK(
        contest_archetype IN (
            'cash', 'single_entry', '3max', '20max', 'mass_multi_entry', 'showdown'
        )
    ),
    field_size INTEGER NOT NULL CHECK(field_size > 0),
    entry_limit INTEGER NOT NULL CHECK(entry_limit > 0),
    entry_fee_cents INTEGER NOT NULL CHECK(entry_fee_cents >= 0),
    payout_curve_id TEXT,
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    role TEXT NOT NULL CHECK(role IN ('classic', 'flex', 'captain')),
    lineup_count INTEGER NOT NULL CHECK(lineup_count > 0),
    roster_count INTEGER NOT NULL CHECK(roster_count >= 0),
    actual_ownership REAL NOT NULL CHECK(actual_ownership >= 0 AND actual_ownership <= 1),
    source_file_sha256 TEXT NOT NULL CHECK(length(source_file_sha256) = 64),
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(external_contest_id, site, player_id, role, observed_at),
    CHECK(roster_count <= lineup_count),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE TABLE odds_snapshots (
    odds_snapshot_id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games(game_id),
    sportsbook TEXT,
    home_spread REAL,
    away_spread REAL,
    total REAL,
    home_spread_price INTEGER,
    away_spread_price INTEGER,
    over_price INTEGER,
    under_price INTEGER,
    response_file_sha256 TEXT NOT NULL CHECK(length(response_file_sha256) = 64),
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(source, game_id, sportsbook, observed_at),
    CHECK(home_spread IS NULL OR away_spread IS NULL OR home_spread = -away_spread),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE TABLE weather_snapshots (
    weather_snapshot_id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games(game_id),
    stadium_name TEXT NOT NULL,
    forecast_model TEXT NOT NULL,
    forecast_run_at TEXT NOT NULL,
    forecast_for_at TEXT NOT NULL,
    lead_time_seconds INTEGER NOT NULL CHECK(lead_time_seconds >= 0),
    temperature_c REAL,
    precipitation_probability REAL CHECK(
        precipitation_probability IS NULL OR
        (precipitation_probability >= 0 AND precipitation_probability <= 1)
    ),
    wind_speed_kph REAL CHECK(wind_speed_kph IS NULL OR wind_speed_kph >= 0),
    wind_gust_kph REAL CHECK(wind_gust_kph IS NULL OR wind_gust_kph >= 0),
    weather_code INTEGER,
    response_file_sha256 TEXT NOT NULL CHECK(length(response_file_sha256) = 64),
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(source, game_id, forecast_model, forecast_run_at, forecast_for_at, observed_at),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE TABLE results (
    result_id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games(game_id),
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    site TEXT NOT NULL,
    fantasy_points REAL NOT NULL,
    stat_line_json TEXT CHECK(stat_line_json IS NULL OR json_valid(stat_line_json)),
    source_file_sha256 TEXT NOT NULL CHECK(length(source_file_sha256) = 64),
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(source, site, game_id, player_id, observed_at),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE TABLE decision_snapshots (
    decision_snapshot_id TEXT PRIMARY KEY,
    slate_id INTEGER NOT NULL REFERENCES slates(slate_id),
    decision_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    manifest_schema_version TEXT NOT NULL,
    manifest_hashes_json TEXT NOT NULL CHECK(
        json_valid(manifest_hashes_json) AND json_type(manifest_hashes_json) = 'array'
    ),
    manifest_hash_set_sha256 TEXT NOT NULL CHECK(length(manifest_hash_set_sha256) = 64),
    run_id TEXT REFERENCES model_runs(run_id),
    note TEXT,
    UNIQUE(slate_id, decision_at, manifest_hash_set_sha256)
) STRICT;

CREATE INDEX idx_teams_as_of
    ON teams(team_key, observed_at, valid_from, valid_to);
CREATE INDEX idx_players_as_of
    ON players(player_key, observed_at, valid_from, valid_to);
CREATE INDEX idx_player_aliases_lookup
    ON player_aliases(source, normalized_alias, observed_at, valid_from, valid_to);
CREATE INDEX idx_external_player_ids_lookup
    ON external_player_ids(source, external_player_id, observed_at, valid_from, valid_to);
CREATE INDEX idx_games_week_as_of
    ON games(season, week, observed_at, valid_from, valid_to);
CREATE INDEX idx_slates_week_as_of
    ON slates(site, season, week, observed_at, valid_from, valid_to);
CREATE INDEX idx_salaries_slate_as_of
    ON salaries(slate_id, player_id, observed_at, valid_from, valid_to);
CREATE INDEX idx_projection_snapshots_slate_as_of
    ON projection_snapshots(slate_id, player_id, source, observed_at, valid_from, valid_to);
CREATE INDEX idx_ownership_baselines_slate_as_of
    ON ownership_baselines(slate_id, player_id, source, observed_at, valid_from, valid_to);
CREATE INDEX idx_actual_ownership_slate
    ON actual_ownership(slate_id, external_contest_id, player_id, role);
CREATE INDEX idx_odds_snapshots_game_as_of
    ON odds_snapshots(game_id, source, observed_at, valid_from, valid_to);
CREATE INDEX idx_weather_snapshots_game_as_of
    ON weather_snapshots(game_id, source, observed_at, valid_from, valid_to);
CREATE INDEX idx_results_game_player
    ON results(game_id, player_id, source);
CREATE INDEX idx_decision_snapshots_slate_cutoff
    ON decision_snapshots(slate_id, decision_at);
