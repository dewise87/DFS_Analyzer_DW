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
Collection fails closed when no current policy exists or its `terms_reviewed_at` is stale.

## `source_items`

Stores inert feed bytes, separately cleaned visible text, item/capture times, and a content hash.
Hash uniqueness is source-scoped so cross-source copies remain separate evidence of reach.

## `content_tombstones`

Durably records retention expiry or a platform deletion after reconstructive item text is cleared.
One tombstone per item makes purge and deletion handling idempotent without silently deleting rows.

## `decision_snapshots`

Freezes a slate decision cutoff with canonical JSON containing the complete §8.4 artifact hash-set.  
`manifest_hash_set_sha256` authenticates that set; the optional run reference links its producing pipeline.
