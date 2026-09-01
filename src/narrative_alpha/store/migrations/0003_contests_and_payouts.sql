-- Versioned contest metadata and manually captured payout curves.

CREATE TABLE contests (
    contest_id INTEGER PRIMARY KEY,
    external_contest_id TEXT NOT NULL,
    site TEXT NOT NULL,
    slate_id INTEGER NOT NULL REFERENCES slates(slate_id),
    archetype TEXT NOT NULL CHECK(
        archetype IN (
            'cash', 'single_entry', '3max', '20max', 'mass_multi_entry', 'showdown'
        )
    ),
    field_size INTEGER NOT NULL CHECK(field_size > 0),
    entry_limit INTEGER NOT NULL CHECK(entry_limit > 0),
    entry_fee_cents INTEGER NOT NULL CHECK(entry_fee_cents >= 0),
    total_prizes_cents INTEGER CHECK(
        total_prizes_cents IS NULL OR total_prizes_cents >= 0
    ),
    payout_curve_id TEXT,
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(site, external_contest_id, observed_at),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE TABLE contest_payouts (
    contest_payout_id INTEGER PRIMARY KEY,
    payout_curve_id TEXT NOT NULL,
    rank_from INTEGER NOT NULL CHECK(rank_from >= 1),
    rank_to INTEGER NOT NULL CHECK(rank_to >= 1),
    prize_cents INTEGER NOT NULL CHECK(prize_cents >= 0),
    source TEXT NOT NULL,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    effective_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_version TEXT,
    run_id TEXT REFERENCES model_runs(run_id),
    UNIQUE(payout_curve_id, rank_from, rank_to, observed_at),
    CHECK(rank_from <= rank_to),
    CHECK(valid_to IS NULL OR valid_to > valid_from)
) STRICT;

CREATE INDEX idx_contests_slate_as_of
    ON contests(slate_id, site, observed_at, valid_from, valid_to);
CREATE INDEX idx_contest_payouts_curve_as_of
    ON contest_payouts(payout_curve_id, rank_from, rank_to, observed_at, valid_from, valid_to);
