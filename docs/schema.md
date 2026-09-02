# Operational Schema

SQLite stores timestamps as timezone-aware ISO 8601 text and percentage-like ownership or
probability values as fractions from 0 through 1. Externally sourced tables are append-only
versions: select them using both the observation cutoff and `valid_from`/`valid_to` interval.

## `applied_migrations`

Records each numbered SQL migration after its transaction commits successfully.  
The filename and SHA-256 detect edited migration history; `version` makes reruns idempotent.

## `model_runs`

Tracks ingestion, model, replay, simulation, and optimizer executions with status and code version.  
Optional parent-run and configuration-hash fields preserve derived-run lineage and failures.

## `model_run_parents`

Preserves complete many-parent Stage 1 recovery lineage. Immutable `stage1_recovery` and
`stage1_recovery_takeover` relationships supplement `model_runs.parent_run_id`, which remains only
the convenience field for a run with one predecessor. Canonical observation and ingestion
timestamps record when each recovery edge was created.

## `model_evals`

Stores immutable Stage 1 label-set evaluations by exact prompt version, model ID, and label-file
SHA-256. The metrics JSON, evaluation run, item/row counts, and complete §3.2 fields make every
prompt/model release comparison reproducible without retaining a committed label file.

## `teams`

Stores versioned canonical NFL franchise identities and abbreviations from an identified source.  
`team_key` is the logical identity; the §3.2 columns distinguish corrections and effective periods.

## `players`

Stores versioned canonical player names, positions, and optional birth dates.  
`player_key` is stable across versions while each `player_id` identifies one point-in-time row.

## `player_aliases`

Persists source-specific player-name resolutions, including method, confidence, and manual override.  
Aliases reference exact player/team versions and carry the complete §3.2 provenance interval.

## `external_player_ids`

Maps a vendor or site player identifier to an exact canonical player version.  
Mappings are versioned and retain the match method, confidence, and manual-override flag.

## `player_team_history`

Stores a player's temporal team, position, roster status, season, and week membership.  
The §3.2 validity interval makes trades and roster changes resolvable as of an input observation.

## `unresolved_player_matches`

Queues ambiguous or below-threshold source identities with candidate scores and input-file hash.  
Review status, decision note, canonical player, and manual audit fields make resolutions durable.

## `games`

Stores versioned schedule records with season, week, kickoff, venue, and home/away team versions.  
The source external game ID plus observation time identifies each captured schedule state.

## `slates`

Stores DraftKings/FanDuel classic or showdown slate definitions and their start/lock times.  
Each source observation is a separate version, preserving slate corrections before a decision cutoff.

## `salaries`

Stores a player's site salary, eligible roster positions, status, teams, game, and source-file hash.  
Rows are immutable slate/player observations with the full §3.2 block for exact replay.

## `projection_snapshots`

Stores each vendor independently: player mean, optional floor/ceiling, and optional ownership view.  
Vendor, slate, player, observed time, file hash, and §3.2 fields prevent later files replacing earlier ones.

## `player_distributions`

Stores activity/full-role gates and fitted zero-location log-normal active-outcome parameters.
`source` identifies the fitted vendor. The v1 writer accepts one exact projection reference,
derives every fit column from a validated fit result, and lets SQLite allocate the row ID;
fit/config hashes, the as-of cutoff, and §3.2 fields reproduce each marginal.

## `ownership_baselines`

Stores purchased player ownership fractions separately by vendor, site, slate, and roster role.  
Every observation retains its input-file hash and §3.2 fields for pre-lock baseline reconstruction.

## `actual_ownership`

Stores player ownership labels for a specific contest cohort, archetype, field, fee, and roster role.  
Contest/site/player/role observations remain separate and retain result-file and point-in-time provenance.

## `contests`

Stores versioned contest cohorts, fees, prize totals, entry limits, and payout-curve identifiers.
Site/contest observation identity and the full §3.2 provenance block preserve lobby history.

## `contest_payouts`

Stores inclusive rank bands and per-place prizes for manually captured payout curves.
Positive ordered ranks are SQL-checked; overlaps are refused within one observation, while a
later re-observation of the same curve is a new version — read one version via `as_of`.

## `odds_snapshots`

Stores sportsbook spread, total, and optional American-price observations for a versioned game.  
The raw-response hash and full §3.2 fields ensure closing lines cannot replace pre-lock markets.

## `weather_snapshots`

Stores stadium forecasts with explicit model run, forecast-valid time, lead time, and weather values.  
Each row references its raw response hash and carries §3.2 provenance for point-in-time replay.

## `results`

Stores player fantasy outcomes and an optional raw stat-line object for a game and DFS site.  
Rows retain the result-file hash and §3.2 source fields even though they are post-lock labels.

## `sources`

Configures only supported public RSS/Atom and official-team feeds, with an explicit collector kind.
Each source configuration is an append-only version behind a stable `source_keys` identity; no
source configuration implies collection permission.

## `source_keys`

Holds stable source identifiers referenced by policies, items, tombstones, and configuration versions.
It contains identity only; collection uses the latest point-in-time-valid row from `sources`.

## `source_policies`

Records the reviewed rights, retention, deletion, redistribution, processing, and commercial terms.
Collection fails closed when no current policy exists or its `terms_reviewed_at` is stale. Exact
versions cannot be edited or deleted; the only allowed mutation closes a current validity interval.
Timestamps must be canonical UTC-Z (27 characters) at insert, so lexical ordering is exact.
Purge uses the minimum TTL across the capture policy, the current policy when present, and every
policy version that authorized an extraction attempt.

## `source_items`

Stores inert feed bytes, separately cleaned visible text, item/capture times, and a content hash.
Hash uniqueness is source-scoped so cross-source copies remain separate evidence of reach. Identity,
hash, timestamps, and provenance are immutable, and delete/replace is forbidden. Only an exact
matching tombstone may clear title, raw bytes, and cleaned text. Timestamps must be canonical
UTC-Z at insert. Migration 0007 adds triggers to these tables in place; it does not rebuild them.

## `content_tombstones`

Durably records retention expiry or a platform deletion after reconstructive item text is cleared.
One tombstone per item makes purge and deletion handling idempotent without silently deleting rows.
The item ID, source ID, and content hash must match exactly; tombstones cannot be edited or deleted.

## `prompt_versions`

Stores the exact Stage 1 system prompt, user template, provider-compatible strict schema, and their
joint SHA-256. A stable ID cannot be reused for changed prompt bytes or a changed schema.

## `source_item_extractions`

Records every provider attempt, including successful zero-claim results, security blocks, and
retryable failures. `creating` reservations plus scoped submission fences close the concurrent
double-submit and authorization-mutation races without keeping a transaction open over the POST;
`submitted` rows carry the accepted batch ID and resume instead of rebilling. The exact authorizing policy,
source-content hash/family, request hash, max-output limit, batch/message/request trace, token usage,
submission-time pricing rates, cost, and §3.2 fields make the terminal result replayable without
calling the model again. Request, policy, batch, and pricing lineage is immutable across the explicit
lifecycle transitions; accepted contract errors remain submitted for reconciliation. Canonical
validated output JSON is hash-checked until tombstone-authorized compliance redaction. A successful
writer passes through a transaction-local `settling` state while inserting the claim graph, then
becomes `succeeded`; child rows can be inserted only during that settlement, so terminal graphs
cannot be appended later. A validated, already-paid result that hits a local settlement error stays
submitted for recovery rather than authorizing another batch create.

`succeeded` and `flagged` are terminal for that (item, prompt version, model): a dismissed review
flag does not reopen the item; a new prompt version does. `na-extract abandon` moves a stuck
`creating`/`submitted` attempt to `failed` with `error_code = 'operator_abandoned'`, which makes the
item retryable. The planner lists an item as ineligible after three `failed` attempts so a
deterministic provider failure is never billed indefinitely.

## `stage1_execution_leases`

Operational (not external point-in-time) leases serialize the non-idempotent batch-create window and
batch-result recovery. An active owner prevents a concurrent run from polling or superseding it;
accepted-ID persistence atomically hands ownership from submission leases to a batch-recovery lease.
The owner renews that lease around retrieval and before each terminal item write; ownership loss
rejects the stale writer, while expiry permits an atomic takeover and lineage record. A displaced run
with another active lease is not terminalized. Normal exits release owned leases and abrupt process
loss is recovered after expiry. Lease rows contain only operation/run identifiers and timestamps,
are intentionally mutable/deletable, and never carry source or model content.

## Accepted-batch recovery receipts

These are fsynced filesystem artifacts, not a table. After provider acceptance, the trace is written
to `<database>.stage1-receipts/` before SQLite persistence; startup promotes a complete temporary
receipt, verifies its integrity, and reconciles the accepted IDs without another create POST. The
database and sibling directory are one backup/move/restore unit until all receipts are reconciled.

## `source_item_review_flags`

Queues prompt-injection, prohibited-output, missing-provider-trace, and result-time policy/retention
findings against their exact source item, authorizing policy, and source. Preflight flags truthfully
carry no provider ID; output flags retain their batch trace for review. Provenance is immutable;
only the pending-to-reviewed control transition is writable.

## `claims`

Stores Stage 1 claim taxonomy, separate outcome and roster-behavior directions, evidence class and
basis, falsifiability, qualitative channel routing, uncertainty metadata, exact model, prompt, and
provider trace. It deliberately has no projection- or ownership-adjustment field.
The graph is immutable after insertion except that reconstructive context is cleared on tombstone.

## `claim_player_refs`

Links each name copied from source text either to a deterministic canonical-player match or to the
durable unresolved queue, never both. Resolved references retain crosswalk method and confidence.
The `manual_override` bit distinguishes human identity decisions from automatic exact matches.

## `claim_evidence_refs`

Stores zero-based Unicode character offsets with an exclusive end, the canonical-source hash, and
the bounded verbatim extract. Offsets index the canonical Stage 1 string
`normalize_item_text(title, cleaned_text)`, not `cleaned_text` alone; its SHA-256 is the item's
`content_sha256`, copied here as `source_text_sha256`. The Stage 1 writer validates the whole claim set atomically against
the NFKC canonical source string before insertion and rechecks that source under the same savepoint.
SQLite additionally binds the reference to the retained item/hash and forbids later mutation. A
tombstone clears the reconstructive extract and output JSON while retaining hashes, offsets, claim
taxonomy, and provider/policy lineage.

## `narrative_episodes`

Stores one immutable deterministic Stage 2 cluster for an exact method version, Stage 1 prompt
version, and information cutoff, scoped to a resolved player, explicit team, or reported
unclustered claim. Item times are publication times when the feed carried one, else observation. Origin and item
times, rolling-window parameter, unique-source reach/diversity, entropy, velocity, recency, and
event count are stored with their actual build run and complete point-in-time provenance.

## `episode_claims`

Links every episode to its Stage 1 claims and source items as origin, independent, corroborating,
derivative, or contradicting evidence. Similarity, method, optional prior-claim propagation edge,
frozen source family, method/as-of identity, and point-in-time fields make each relation auditable;
rows cannot be edited or deleted.

## `narrative_feature_versions`

Binds each immutable Stage 3 `feature_version` to the canonical parsed heat configuration, its
SHA-256, and the formula implementation version. Reusing a version name after any semantic config
change is refused before feature computation; rows cannot be edited or deleted.

## `narrative_features`

Stores one immutable Appendix B vector per `(player, slate, site, as_of, feature_version)`. Raw and
population-z-scored narrative fields are retained together, with standardized values winsorized at
±4. Baseline ownership, six-hour baseline/projection changes, and salary point to their exact input
rows; missing pre-cutoff baselines remain `NULL`. Sorted episode and ownership-snapshot ID arrays,
the Stage 2 method, canonical heat-config hash, semantic input hash, and Stage 3 run make every value
replayable. The current source schema has no author identity, and a player-level build has no contest
cohort, so author count, contest archetype, value/scarcity alternatives, and ownership-model version
remain explicitly `NULL` rather than fabricated.

## `decision_snapshots`

Freezes a slate decision cutoff with canonical JSON containing the complete §8.4 artifact hash-set.  
`manifest_hash_set_sha256` authenticates that set; the optional run reference links its producing pipeline.
