-- Stokastic component projections. These are raw expected stat components, not site points.

CREATE TABLE projected_stats (
    projected_stat_id INTEGER PRIMARY KEY,
    source TEXT NOT NULL CHECK(length(trim(source)) > 0),
    season INTEGER NOT NULL CHECK(season >= 1),
    week INTEGER NOT NULL CHECK(week >= 1 AND week <= 99),
    player_id INTEGER NOT NULL REFERENCES players(player_id),
    stat TEXT NOT NULL CHECK(stat IN (
        'pass_att', 'pass_cmp', 'pass_yds', 'pass_td', 'pass_int',
        'rush_att', 'rush_yds', 'rush_td',
        'targets', 'target_share', 'receptions', 'rec_yds', 'rec_td',
        'pass_fumbles', 'rush_fumbles', 'rec_fumbles'
    )),
    value REAL NOT NULL,
    file_sha256 TEXT NOT NULL CHECK(
        length(file_sha256) = 64 AND file_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT NOT NULL CHECK(length(trim(source_version)) > 0),
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(source, season, week, player_id, stat, observed_at),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE INDEX idx_projected_stats_week_player
    ON projected_stats(source, season, week, player_id, observed_at DESC, stat);

-- Enforce the same canonical UTC-Z representation used by every point-in-time writer.
-- This deliberately uses only built-in SQLite expressions so bare sqlite3 connections work.
CREATE TRIGGER validate_projected_stats_timestamps_insert
BEFORE INSERT ON projected_stats
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
    ) THEN RAISE(ABORT, 'projected stat timestamps must be canonical UTC') END;
    SELECT CASE WHEN NEW.valid_to IS NOT NULL AND NEW.valid_to <= NEW.valid_from
        THEN RAISE(ABORT, 'projected stat valid_to must be later than valid_from') END;
END;

CREATE TRIGGER projected_stats_immutable_update
BEFORE UPDATE ON projected_stats
BEGIN
    SELECT RAISE(ABORT, 'projected_stats is append-only');
END;

CREATE TRIGGER projected_stats_no_delete
BEFORE DELETE ON projected_stats
BEGIN
    SELECT RAISE(ABORT, 'projected_stats is append-only');
END;
