-- Point-in-time official availability facts used by the deterministic Sunday fast lane.

CREATE TABLE player_availability (
    availability_id TEXT PRIMARY KEY,
    slate_id INTEGER NOT NULL REFERENCES slates(slate_id),
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    season INTEGER NOT NULL CHECK(season >= 1),
    week INTEGER NOT NULL CHECK(week >= 1 AND week <= 99),
    site TEXT NOT NULL CHECK(site IN ('draftkings', 'fanduel')),
    availability_status TEXT NOT NULL CHECK(availability_status IN ('available', 'unavailable')),
    rule_id TEXT NOT NULL CHECK(length(trim(rule_id)) > 0),
    rules_version TEXT NOT NULL CHECK(length(trim(rules_version)) > 0),
    source_file_sha256 TEXT NOT NULL CHECK(length(source_file_sha256) = 64),
    source TEXT NOT NULL CHECK(length(trim(source)) > 0),
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(slate_id, player_id, observed_at, source_file_sha256),
    CHECK(valid_to IS NULL OR valid_to > valid_from),
    CHECK(
        published_at IS NULL OR (
            length(published_at) = 27 AND substr(published_at, -1) = 'Z'
        )
    ),
    CHECK(length(observed_at) = 27 AND substr(observed_at, -1) = 'Z'),
    CHECK(length(ingested_at) = 27 AND substr(ingested_at, -1) = 'Z'),
    CHECK(
        effective_at IS NULL OR (
            length(effective_at) = 27 AND substr(effective_at, -1) = 'Z'
        )
    ),
    CHECK(length(valid_from) = 27 AND substr(valid_from, -1) = 'Z'),
    CHECK(
        valid_to IS NULL OR (
            length(valid_to) = 27 AND substr(valid_to, -1) = 'Z'
        )
    )
) STRICT;

CREATE INDEX idx_player_availability_slate_as_of
    ON player_availability(slate_id, site, player_id, observed_at, valid_from, valid_to);

CREATE TRIGGER player_availability_no_update
BEFORE UPDATE ON player_availability
BEGIN
    SELECT RAISE(ABORT, 'player_availability is append-only');
END;

CREATE TRIGGER player_availability_no_delete
BEFORE DELETE ON player_availability
BEGIN
    SELECT RAISE(ABORT, 'player_availability is append-only');
END;
