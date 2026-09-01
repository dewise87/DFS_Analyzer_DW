-- Separate stable source identity from append-only source configuration versions.
-- Migration 0005 made source_id the primary key, which cannot represent a changed feed URL
-- without overwriting the row. Policies and items reference the stable key instead.

CREATE TABLE source_keys (
    source_id TEXT PRIMARY KEY
) STRICT;

INSERT INTO source_keys(source_id)
SELECT source_id FROM sources;

CREATE TABLE sources_versioned (
    source_record_id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source_keys(source_id),
    display_name TEXT NOT NULL CHECK(length(trim(display_name)) > 0),
    source_family TEXT NOT NULL CHECK(length(trim(source_family)) > 0),
    collector_kind TEXT NOT NULL CHECK(
        collector_kind IN ('rss_atom', 'official_team_feed')
    ),
    feed_url TEXT NOT NULL CHECK(
        feed_url LIKE 'https://%' OR feed_url LIKE 'http://%'
    ),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(source_id, observed_at),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

INSERT INTO sources_versioned(
    source_id, display_name, source_family, collector_kind, feed_url, enabled,
    source, published_at, observed_at, ingested_at, effective_at, valid_from,
    valid_to, source_version, run_id
)
SELECT
    source_id, display_name, source_family, collector_kind, feed_url, enabled,
    source, published_at, observed_at, ingested_at, effective_at, valid_from,
    valid_to, source_version, run_id
FROM sources;

CREATE TABLE source_policies_versioned (
    source_policy_id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source_keys(source_id),
    permitted_use TEXT NOT NULL CHECK(length(trim(permitted_use)) > 0),
    raw_retention_days INTEGER NOT NULL CHECK(raw_retention_days >= 0),
    personal_data_fields_allowed TEXT NOT NULL CHECK(
        json_valid(personal_data_fields_allowed) AND
        json_type(personal_data_fields_allowed) = 'array'
    ),
    must_honor_deletions INTEGER NOT NULL CHECK(must_honor_deletions IN (0, 1)),
    redistribution_allowed INTEGER NOT NULL CHECK(redistribution_allowed IN (0, 1)),
    third_party_processing_allowed INTEGER NOT NULL CHECK(
        third_party_processing_allowed IN (0, 1)
    ),
    commercial_use_status TEXT NOT NULL CHECK(length(trim(commercial_use_status)) > 0),
    terms_reviewed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(source_id, observed_at),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

INSERT INTO source_policies_versioned
SELECT * FROM source_policies;

CREATE TABLE source_items_versioned (
    source_item_id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source_keys(source_id),
    external_item_id TEXT,
    canonical_url TEXT,
    title TEXT,
    raw_content BLOB,
    cleaned_text TEXT,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    -- Still deliberately source-scoped: cross-source copies remain separate reach.
    UNIQUE(source_id, content_sha256),
    CHECK(valid_to IS NULL OR valid_to > valid_from),
    CHECK(
        (raw_content IS NULL AND cleaned_text IS NULL) OR
        (raw_content IS NOT NULL AND cleaned_text IS NOT NULL)
    )
) STRICT;

INSERT INTO source_items_versioned
SELECT * FROM source_items;

CREATE TABLE content_tombstones_versioned (
    content_tombstone_id INTEGER PRIMARY KEY,
    source_item_id INTEGER NOT NULL UNIQUE REFERENCES source_items_versioned(source_item_id),
    source_id TEXT NOT NULL REFERENCES source_keys(source_id),
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    reason TEXT NOT NULL CHECK(reason IN ('retention_expired', 'platform_deleted')),
    tombstoned_at TEXT NOT NULL,
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

INSERT INTO content_tombstones_versioned
SELECT * FROM content_tombstones;

DROP TABLE content_tombstones;
DROP TABLE source_items;
DROP TABLE source_policies;
DROP TABLE sources;

ALTER TABLE sources_versioned RENAME TO sources;
ALTER TABLE source_policies_versioned RENAME TO source_policies;
ALTER TABLE source_items_versioned RENAME TO source_items;
ALTER TABLE content_tombstones_versioned RENAME TO content_tombstones;

CREATE INDEX idx_sources_current
    ON sources(source_id, observed_at DESC, valid_from, valid_to);
CREATE INDEX idx_source_policies_current
    ON source_policies(source_id, observed_at DESC, valid_from, valid_to);
CREATE INDEX idx_source_items_retention
    ON source_items(source_id, observed_at)
    WHERE raw_content IS NOT NULL;
CREATE INDEX idx_source_items_content_hash ON source_items(content_sha256);

