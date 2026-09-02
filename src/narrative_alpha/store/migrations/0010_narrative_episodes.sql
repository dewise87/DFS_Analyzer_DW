-- Deterministic Stage 2 narrative episodes and their auditable propagation graph.

CREATE TABLE narrative_episodes (
    episode_id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK(
        subject_type IN ('player', 'team', 'unclustered')
    ),
    subject_player_id INTEGER REFERENCES players(player_id),
    subject_team_code TEXT,
    unclustered_key TEXT,
    claim_dimension TEXT NOT NULL CHECK(
        claim_dimension IN (
            'active_status', 'snap_share', 'route_share', 'touch_share',
            'target_share', 'role', 'health', 'efficiency', 'mean', 'tail',
            'dependence', 'ownership', 'none'
        )
    ),
    opened_at TEXT NOT NULL,
    last_item_at TEXT NOT NULL,
    origin_claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    method_version TEXT NOT NULL CHECK(length(trim(method_version)) > 0),
    -- The Stage 1 prompt whose claims this snapshot clustered; a re-extraction under a
    -- new prompt is a different snapshot, never a conflict with this one.
    prompt_version_id TEXT NOT NULL REFERENCES prompt_versions(prompt_version_id),
    as_of TEXT NOT NULL,
    window_hours REAL NOT NULL CHECK(window_hours > 0),
    unique_source_count INTEGER NOT NULL CHECK(unique_source_count > 0),
    unique_source_family_count INTEGER NOT NULL CHECK(
        unique_source_family_count > 0 AND
        unique_source_family_count <= unique_source_count
    ),
    source_entropy REAL NOT NULL CHECK(source_entropy >= 0),
    reach_proxy INTEGER NOT NULL CHECK(reach_proxy = unique_source_count),
    velocity_per_6h REAL NOT NULL CHECK(velocity_per_6h >= 0),
    recency_hours REAL NOT NULL CHECK(recency_hours >= 0),
    n_events INTEGER NOT NULL CHECK(n_events > 0),
    item_count INTEGER NOT NULL CHECK(
        item_count > 0 AND n_events <= item_count AND unique_source_count <= item_count
    ),
    source TEXT NOT NULL CHECK(length(trim(source)) > 0),
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT NOT NULL REFERENCES model_runs(run_id),
    CHECK(opened_at <= last_item_at AND last_item_at <= as_of),
    CHECK(valid_to IS NULL OR valid_to > valid_from),
    CHECK(
        (subject_type = 'player' AND subject_player_id IS NOT NULL AND
         subject_team_code IS NULL AND unclustered_key IS NULL) OR
        (subject_type = 'team' AND subject_player_id IS NULL AND
         subject_team_code IN (
             'ARI', 'ATL', 'BAL', 'BUF', 'CAR', 'CHI', 'CIN', 'CLE',
             'DAL', 'DEN', 'DET', 'GB', 'HOU', 'IND', 'JAX', 'KC',
             'LAC', 'LAR', 'LV', 'MIA', 'MIN', 'NE', 'NO', 'NYG',
             'NYJ', 'PHI', 'PIT', 'SEA', 'SF', 'TB', 'TEN', 'WAS'
         ) AND unclustered_key IS NULL) OR
        (subject_type = 'unclustered' AND subject_player_id IS NULL AND
         subject_team_code IS NULL AND length(trim(unclustered_key)) > 0)
    )
) STRICT;

CREATE INDEX idx_narrative_episodes_snapshot
    ON narrative_episodes(method_version, as_of, episode_id);
CREATE INDEX idx_narrative_episodes_prompt
    ON narrative_episodes(prompt_version_id, as_of, episode_id);
CREATE INDEX idx_narrative_episodes_player
    ON narrative_episodes(subject_player_id, as_of, episode_id)
    WHERE subject_type = 'player';
CREATE INDEX idx_narrative_episodes_team
    ON narrative_episodes(subject_team_code, as_of, episode_id)
    WHERE subject_type = 'team';

CREATE TABLE episode_claims (
    episode_id TEXT NOT NULL REFERENCES narrative_episodes(episode_id),
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    source_item_id INTEGER NOT NULL REFERENCES source_items(source_item_id),
    source_id TEXT NOT NULL REFERENCES source_keys(source_id),
    source_family TEXT NOT NULL CHECK(length(trim(source_family)) > 0),
    relation TEXT NOT NULL CHECK(
        relation IN (
            'origin', 'independent', 'corroborating', 'derivative', 'contradicting'
        )
    ),
    similarity_score REAL NOT NULL CHECK(
        similarity_score >= 0 AND similarity_score <= 1
    ),
    linked_claim_id TEXT REFERENCES claims(claim_id),
    method TEXT NOT NULL CHECK(length(trim(method)) > 0),
    method_version TEXT NOT NULL CHECK(length(trim(method_version)) > 0),
    as_of TEXT NOT NULL,
    source TEXT NOT NULL CHECK(length(trim(source)) > 0),
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT NOT NULL REFERENCES model_runs(run_id),
    PRIMARY KEY(episode_id, claim_id),
    CHECK(valid_to IS NULL OR valid_to > valid_from),
    CHECK(
        (relation IN ('origin', 'independent') AND linked_claim_id IS NULL) OR
        (relation IN ('corroborating', 'derivative', 'contradicting') AND
         linked_claim_id IS NOT NULL)
    )
) STRICT;

CREATE UNIQUE INDEX idx_episode_claims_one_origin
    ON episode_claims(episode_id) WHERE relation = 'origin';
CREATE INDEX idx_episode_claims_claim
    ON episode_claims(claim_id, as_of, episode_id);
CREATE INDEX idx_episode_claims_item
    ON episode_claims(source_item_id, as_of, episode_id);

CREATE TRIGGER validate_narrative_episode_insert
BEFORE INSERT ON narrative_episodes
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM json_each(json_array(
            NEW.opened_at, NEW.last_item_at, NEW.as_of, NEW.published_at,
            NEW.observed_at, NEW.ingested_at, NEW.effective_at, NEW.valid_from,
            NEW.valid_to
        )) AS stamp
        WHERE stamp.value IS NOT NULL AND (
            typeof(stamp.value) <> 'text' OR length(stamp.value) <> 27 OR
            substr(stamp.value, 1, 4) = '0000' OR
            substr(stamp.value, 12, 2) NOT BETWEEN '00' AND '23' OR
            stamp.value NOT GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' OR
            strftime('%Y-%m-%dT%H:%M:%S', stamp.value) IS NOT substr(stamp.value, 1, 19)
        )
    ) THEN RAISE(ABORT, 'narrative episode timestamps must be canonical UTC') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM model_runs AS run
        WHERE run.run_id = NEW.run_id
          AND run.run_type = 'stage_2_episodes'
          AND run.status = 'running'
          AND run.started_at = NEW.observed_at
    ) THEN RAISE(ABORT, 'narrative episode must belong to its running Stage 2 run') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM claims AS claim
        WHERE claim.claim_id = NEW.origin_claim_id
          AND claim.claim_dimension = NEW.claim_dimension
          AND claim.observed_at <= NEW.as_of
          AND claim.ingested_at <= NEW.as_of
          AND claim.valid_from <= NEW.as_of
          AND (claim.valid_to IS NULL OR NEW.as_of < claim.valid_to)
    ) THEN RAISE(ABORT, 'narrative episode origin is not eligible at its as-of') END;
    SELECT CASE WHEN NEW.subject_type = 'player' AND NOT EXISTS (
        SELECT 1 FROM claim_player_refs AS ref
        WHERE ref.claim_id = NEW.origin_claim_id
          AND ref.player_id = NEW.subject_player_id
          AND ref.observed_at <= NEW.as_of AND ref.ingested_at <= NEW.as_of
          AND ref.valid_from <= NEW.as_of
          AND (ref.valid_to IS NULL OR NEW.as_of < ref.valid_to)
    ) THEN RAISE(ABORT, 'player episode origin does not reference its subject') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM claims AS claim
        JOIN source_item_extractions AS extraction
          ON extraction.extraction_id = claim.extraction_id
        WHERE claim.claim_id = NEW.origin_claim_id
          AND extraction.prompt_version_id = NEW.prompt_version_id
    ) THEN RAISE(ABORT, 'narrative episode origin was not extracted under its prompt version') END;
END;

CREATE TRIGGER validate_episode_claim_insert
BEFORE INSERT ON episode_claims
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
    ) THEN RAISE(ABORT, 'episode claim timestamps must be canonical UTC') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM narrative_episodes AS episode
        JOIN claims AS claim ON claim.claim_id = NEW.claim_id
        JOIN source_items AS item ON item.source_item_id = NEW.source_item_id
        JOIN source_item_extractions AS extraction
          ON extraction.extraction_id = claim.extraction_id
        WHERE episode.episode_id = NEW.episode_id
          AND episode.method_version = NEW.method_version
          AND episode.as_of = NEW.as_of
          AND episode.run_id = NEW.run_id
          AND claim.source_item_id = NEW.source_item_id
          AND claim.claim_dimension = episode.claim_dimension
          AND item.source_id = NEW.source_id
          AND extraction.source_family = NEW.source_family
          AND extraction.status = 'succeeded'
          AND claim.observed_at <= NEW.as_of
          AND claim.ingested_at <= NEW.as_of
          AND claim.valid_from <= NEW.as_of
          AND (claim.valid_to IS NULL OR NEW.as_of < claim.valid_to)
          AND item.observed_at <= NEW.as_of
          AND item.ingested_at <= NEW.as_of
          AND item.valid_from <= NEW.as_of
          AND (item.valid_to IS NULL OR NEW.as_of < item.valid_to)
    ) THEN RAISE(ABORT, 'episode claim does not match its episode or point-in-time input') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM narrative_episodes AS episode
        WHERE episode.episode_id = NEW.episode_id
          AND episode.subject_type = 'player'
    ) AND NOT EXISTS (
        SELECT 1
        FROM narrative_episodes AS episode
        JOIN claim_player_refs AS ref
          ON ref.claim_id = NEW.claim_id
         AND ref.player_id = episode.subject_player_id
         AND ref.observed_at <= NEW.as_of AND ref.ingested_at <= NEW.as_of
         AND ref.valid_from <= NEW.as_of
         AND (ref.valid_to IS NULL OR NEW.as_of < ref.valid_to)
        WHERE episode.episode_id = NEW.episode_id
    ) THEN RAISE(ABORT, 'episode claim does not reference its player subject') END;
    SELECT CASE WHEN NEW.relation = 'origin' AND NOT EXISTS (
        SELECT 1 FROM narrative_episodes AS episode
        WHERE episode.episode_id = NEW.episode_id
          AND episode.origin_claim_id = NEW.claim_id
    ) THEN RAISE(ABORT, 'episode origin relation does not match origin claim') END;
    SELECT CASE WHEN NEW.relation <> 'origin' AND EXISTS (
        SELECT 1 FROM narrative_episodes AS episode
        WHERE episode.episode_id = NEW.episode_id
          AND episode.origin_claim_id = NEW.claim_id
    ) THEN RAISE(ABORT, 'episode origin claim must carry the origin relation') END;
    SELECT CASE WHEN NEW.linked_claim_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM episode_claims AS linked
        WHERE linked.episode_id = NEW.episode_id
          AND linked.claim_id = NEW.linked_claim_id
    ) THEN RAISE(ABORT, 'episode claim link must point to an earlier episode member') END;
END;

CREATE TRIGGER narrative_episodes_immutable_update
BEFORE UPDATE ON narrative_episodes
BEGIN
    SELECT RAISE(ABORT, 'narrative episodes are append-only');
END;

CREATE TRIGGER narrative_episodes_no_delete
BEFORE DELETE ON narrative_episodes
BEGIN
    SELECT RAISE(ABORT, 'narrative episodes are append-only');
END;

CREATE TRIGGER episode_claims_immutable_update
BEFORE UPDATE ON episode_claims
BEGIN
    SELECT RAISE(ABORT, 'episode claims are append-only');
END;

CREATE TRIGGER episode_claims_no_delete
BEFORE DELETE ON episode_claims
BEGIN
    SELECT RAISE(ABORT, 'episode claims are append-only');
END;
