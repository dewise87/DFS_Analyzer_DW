-- Reviewed source policies, feed configuration, prospective evidence, and durable tombstones.

CREATE TABLE sources (
    source_id TEXT PRIMARY KEY,
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
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE TABLE source_policies (
    source_policy_id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
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

CREATE INDEX idx_source_policies_current
    ON source_policies(source_id, observed_at DESC, valid_from, valid_to);

CREATE TABLE source_items (
    source_item_id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
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
    -- Deliberately source-scoped: identical text at different sources is distinct reach.
    UNIQUE(source_id, content_sha256),
    CHECK(valid_to IS NULL OR valid_to > valid_from),
    CHECK(
        (raw_content IS NULL AND cleaned_text IS NULL) OR
        (raw_content IS NOT NULL AND cleaned_text IS NOT NULL)
    )
) STRICT;

CREATE INDEX idx_source_items_retention
    ON source_items(source_id, observed_at)
    WHERE raw_content IS NOT NULL;
CREATE INDEX idx_source_items_content_hash ON source_items(content_sha256);

CREATE TABLE content_tombstones (
    content_tombstone_id INTEGER PRIMARY KEY,
    source_item_id INTEGER NOT NULL UNIQUE REFERENCES source_items(source_item_id),
    source_id TEXT NOT NULL REFERENCES sources(source_id),
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

