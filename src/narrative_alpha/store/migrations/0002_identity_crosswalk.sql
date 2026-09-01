-- Durable identity matching metadata, temporal team membership, and manual-review queue.

ALTER TABLE external_player_ids
    ADD COLUMN match_method TEXT NOT NULL DEFAULT 'seed';
ALTER TABLE external_player_ids
    ADD COLUMN match_confidence REAL NOT NULL DEFAULT 1.0
        CHECK(match_confidence >= 0 AND match_confidence <= 1);
ALTER TABLE external_player_ids
    ADD COLUMN manual_override INTEGER NOT NULL DEFAULT 0
        CHECK(manual_override IN (0, 1));

-- Canonical team code captured on the alias row so alias identity can be
-- team-scoped without requiring a seeded teams table.
ALTER TABLE player_aliases
    ADD COLUMN team TEXT;

CREATE UNIQUE INDEX idx_external_player_ids_active
    ON external_player_ids(source, coalesce(site, ''), external_player_id)
    WHERE valid_to IS NULL;
-- An active alias identity (source, normalized_alias, team) may map to at most
-- one player; player_id is deliberately excluded from the key.
CREATE UNIQUE INDEX idx_player_aliases_active
    ON player_aliases(source, normalized_alias, coalesce(team, ''))
    WHERE valid_to IS NULL;

CREATE TABLE player_team_history (
    player_team_history_id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    team TEXT NOT NULL,
    position TEXT,
    roster_status TEXT,
    season INTEGER CHECK(season IS NULL OR season >= 1),
    week INTEGER CHECK(week IS NULL OR (week >= 1 AND week <= 99)),
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(player_id, team, valid_from),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE INDEX idx_player_team_history_match
    ON player_team_history(team, player_id, observed_at, valid_from, valid_to);

CREATE TABLE unresolved_player_matches (
    unresolved_id INTEGER PRIMARY KEY,
    identity_key TEXT NOT NULL UNIQUE CHECK(length(identity_key) = 64),
    source TEXT NOT NULL,
    site TEXT,
    external_player_id TEXT,
    name_raw TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    team TEXT NOT NULL,
    opponent TEXT,
    position TEXT,
    roster_status TEXT,
    birth_date TEXT,
    eligible_positions_json TEXT NOT NULL CHECK(json_valid(eligible_positions_json)),
    candidates_json TEXT NOT NULL CHECK(json_valid(candidates_json)),
    source_file_sha256 TEXT CHECK(
        source_file_sha256 IS NULL OR length(source_file_sha256) = 64
    ),
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1 CHECK(occurrences >= 1),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'resolved', 'ignored')),
    resolved_player_id INTEGER REFERENCES players(player_id),
    resolved_at TEXT,
    resolution_note TEXT,
    match_method TEXT,
    match_confidence REAL CHECK(
        match_confidence IS NULL OR
        (match_confidence >= 0 AND match_confidence <= 1)
    ),
    manual_override INTEGER NOT NULL DEFAULT 0 CHECK(manual_override IN (0, 1)),
    run_id TEXT REFERENCES model_runs(run_id),
    CHECK(last_observed_at >= first_observed_at),
    CHECK(
        (status = 'pending' AND resolved_player_id IS NULL AND resolved_at IS NULL) OR
        (status = 'ignored' AND resolved_player_id IS NULL AND resolved_at IS NOT NULL) OR
        (status = 'resolved' AND resolved_player_id IS NOT NULL AND resolved_at IS NOT NULL)
    )
) STRICT;

CREATE INDEX idx_unresolved_player_matches_status
    ON unresolved_player_matches(status, last_observed_at, unresolved_id);
