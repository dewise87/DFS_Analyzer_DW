-- Slice 30 review: keep the Stage 4 routing decision, and its reason, beside the snapshot.
--
-- The manifest records what a decision applied. A replay of an unrouted decision can only
-- re-derive "the manifest carries no set", which is not the reason: "no set existed", "the
-- evaluation lost", and "the set missed three candidates" are different facts, and the
-- memo and the status screen must print the real one for as long as the decision exists.

CREATE TABLE decision_ownership_routing (
    decision_snapshot_id TEXT PRIMARY KEY
        REFERENCES decision_snapshots(decision_snapshot_id),
    applied INTEGER NOT NULL CHECK(applied IN (0, 1)),
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    scenario_run_id TEXT,
    scenario_set_sha256 TEXT CHECK(
        scenario_set_sha256 IS NULL OR length(scenario_set_sha256) = 64
    ),
    governance_status TEXT,
    status_multiplier REAL CHECK(
        status_multiplier IS NULL OR status_multiplier BETWEEN 0 AND 1
    ),
    model_eval_id TEXT,
    held_at_baseline INTEGER NOT NULL DEFAULT 0 CHECK(held_at_baseline >= 0),
    created_at TEXT NOT NULL CHECK(length(created_at) = 27 AND substr(created_at, -1) = 'Z'),
    CHECK(
        (applied = 1 AND scenario_run_id IS NOT NULL AND scenario_set_sha256 IS NOT NULL)
        OR (applied = 0 AND scenario_run_id IS NULL AND scenario_set_sha256 IS NULL)
    )
) STRICT;

CREATE TRIGGER decision_ownership_routing_immutable_update
BEFORE UPDATE ON decision_ownership_routing
BEGIN
    SELECT RAISE(ABORT, 'decision ownership routing is immutable');
END;

CREATE TRIGGER decision_ownership_routing_no_delete
BEFORE DELETE ON decision_ownership_routing
BEGIN
    SELECT RAISE(ABORT, 'decision ownership routing may not be deleted');
END;
