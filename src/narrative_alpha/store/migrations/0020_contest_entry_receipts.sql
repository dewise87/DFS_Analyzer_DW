-- Append-only entry-assignment ledger and settled entry receipts.

CREATE TABLE contest_entries (
    contest_entry_id INTEGER PRIMARY KEY,
    decision_snapshot_id TEXT NOT NULL REFERENCES decision_snapshots(decision_snapshot_id),
    contest_id INTEGER NOT NULL REFERENCES contests(contest_id),
    entry_id TEXT NOT NULL CHECK(length(trim(entry_id)) > 0),
    entry_fee_cents INTEGER NOT NULL CHECK(entry_fee_cents >= 0),
    lineup_id TEXT NOT NULL CHECK(length(lineup_id) = 64),
    recorded_at TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('slate_build', 'fast_refreeze')),
    UNIQUE(decision_snapshot_id, entry_id),
    CHECK(length(recorded_at) = 27 AND substr(recorded_at, -1) = 'Z')
) STRICT;

CREATE INDEX idx_contest_entries_contest_entry
    ON contest_entries(contest_id, entry_id, contest_entry_id);

CREATE TABLE contest_entry_results (
    contest_entry_result_id INTEGER PRIMARY KEY,
    contest_entry_id INTEGER NOT NULL REFERENCES contest_entries(contest_entry_id),
    settlement_status TEXT NOT NULL CHECK(settlement_status IN ('settled', 'unsettled')),
    rank INTEGER CHECK(rank IS NULL OR rank >= 1),
    points REAL,
    payout_cents INTEGER CHECK(payout_cents IS NULL OR payout_cents >= 0),
    unsettled_reason TEXT,
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
    UNIQUE(contest_entry_id, source_file_sha256),
    CHECK(valid_to IS NULL OR valid_to > valid_from),
    CHECK(
        (settlement_status = 'settled' AND rank IS NOT NULL AND points IS NOT NULL
         AND payout_cents IS NOT NULL AND unsettled_reason IS NULL)
        OR
        (settlement_status = 'unsettled' AND rank IS NULL AND points IS NULL
         AND payout_cents IS NULL AND length(trim(unsettled_reason)) > 0)
    ),
    CHECK(length(observed_at) = 27 AND substr(observed_at, -1) = 'Z'),
    CHECK(length(ingested_at) = 27 AND substr(ingested_at, -1) = 'Z'),
    CHECK(length(valid_from) = 27 AND substr(valid_from, -1) = 'Z')
) STRICT;

CREATE INDEX idx_contest_entry_results_entry_observed
    ON contest_entry_results(contest_entry_id, observed_at, contest_entry_result_id);

CREATE TRIGGER contest_entries_no_update
BEFORE UPDATE ON contest_entries
BEGIN
    SELECT RAISE(ABORT, 'contest_entries is append-only');
END;

CREATE TRIGGER contest_entries_no_delete
BEFORE DELETE ON contest_entries
BEGIN
    SELECT RAISE(ABORT, 'contest_entries is append-only');
END;

CREATE TRIGGER contest_entry_results_no_update
BEFORE UPDATE ON contest_entry_results
BEGIN
    SELECT RAISE(ABORT, 'contest_entry_results is append-only');
END;

CREATE TRIGGER contest_entry_results_no_delete
BEFORE DELETE ON contest_entry_results
BEGIN
    SELECT RAISE(ABORT, 'contest_entry_results is append-only');
END;
