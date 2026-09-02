# Work Slices

The build plan, cut into PR-sized slices. Each slice has: goal, design-doc references, a
ready-to-paste prompt for the executing model, and a model recommendation for both Claude
and ChatGPT.

**Process:** paste the prompt into a fresh session of the recommended model, work in a
branch, bring the diff back to the project-lead chat for review before merging. Every slice
ships with tests.

**Model tiers, not names.** Model names drift (design doc §5.6). Map by tier:

| Tier | Claude (today) | ChatGPT (today) | Use for |
|---|---|---|---|
| Frontier reasoning | Fable 5 / Opus 5 | GPT-5.1 Pro or Thinking (max effort) | Statistics, architecture, anything where a subtle mistake is expensive |
| Workhorse | Sonnet 5 | GPT-5.1 Thinking | Standard implementation with tests |
| Cheap/mechanical | Haiku 4.5 | GPT-5.1 (standard) | Boilerplate, format conversions, doc chores |

When both a Claude and a ChatGPT model are listed, use either — the second is a fallback or
a second opinion, not a required duplicate run.

---

## Phase −1 (NOW — season opens within two weeks)

### Slice 1 — Snapshot capture CLI ⚡ highest ROI in the project

**Goal:** a `na-snapshot` command that freezes a capture directory of hashed, timestamped
files. Manual-first: you download the files; the tool organizes, hashes, and manifests them.
No database, no parsing, no cleverness.

**Design doc:** §9.0 (Phase −1), §3.2 (point-in-time fields), `data/README.md` layout.

**Model:** Claude **Sonnet 5** · ChatGPT **GPT-5.1 Thinking**

**Prompt:**

> You are working in the `DFS_Analyzer_DW` repo (Python 3.12, uv, Polars, Pydantic; see
> `pyproject.toml`). Read `data/README.md` and §9.0 and §3.2 of
> `docs/design/narrative-alpha-design-doc-v0_3.md` first.
>
> Build the Phase −1 snapshot capture tool in `src/narrative_alpha/snapshots/`:
>
> 1. `na-snapshot init --season 2026 --week 1` creates the capture directory tree for a week.
> 2. `na-snapshot capture --season 2026 --week 1 --kind salaries --source draftkings <files...>`
>    copies files into a new timestamped capture directory
>    (`data/snapshots/2026/week_01/<UTC ISO timestamp>/salaries/`), records for each file:
>    sha256, byte size, original filename, `observed_at` (now, UTC), `source` label, and
>    `kind` — into `manifest.json` (Pydantic model, versioned schema).
> 3. `na-snapshot verify --season 2026 --week 1` re-hashes every file under every manifest
>    and reports mismatches or unmanifested files. Exit nonzero on any problem.
> 4. `na-snapshot status` prints, per week, which capture kinds exist and when the last
>    capture of each kind happened — so a missed Saturday 6 p.m. capture is visible at a glance.
>
> Constraints: captures are append-only (never modify an existing capture directory —
> `capture` always creates a new timestamped dir); stdlib `argparse` or `typer` (add typer to
> deps if used); no network calls in this slice; wire the CLI entry point into
> `pyproject.toml` `[project.scripts]` as `na-snapshot`. Write pytest tests using `tmp_path`
> covering: manifest round-trip, hash verification catching a corrupted file, append-only
> behavior, and status output for a missing kind. Run `ruff check`, `mypy`, and `pytest`
> before declaring done.

### Slice 2 — Odds & weather auto-capture

**Goal:** extend `na-snapshot` with `na-snapshot fetch --kind odds|weather` that pulls The
Odds API and Open-Meteo for the week's games and writes raw JSON responses into a capture
directory via the Slice 1 machinery. This is the only Phase −1 piece that can't be a manual
download.

**Design doc:** §4.4, §9.0. Store forecast run + lead time for weather; snapshot
observed-at for odds.

**Model:** Claude **Sonnet 5** · ChatGPT **GPT-5.1 Thinking**

**Prompt:**

> Same repo; read `src/narrative_alpha/snapshots/` (Slice 1) first — reuse its capture/manifest
> machinery, do not duplicate it. Add `na-snapshot fetch --season N --week N --kind odds` and
> `--kind weather`.
>
> Odds: The Odds API v4 (`https://the-odds-api.com/liveapi/guides/v4/`), NFL spreads and
> totals, API key from env var `ODDS_API_KEY` (never logged, never in the manifest). Save the
> raw JSON response body verbatim, one file per request, plus request URL (with the key
> redacted) and response headers relevant to quota.
>
> Weather: Open-Meteo forecast API for a static stadium table — create
> `src/narrative_alpha/snapshots/stadiums.py` with lat/lon/roof/surface for all 30 NFL
> stadiums as a typed constant (design doc §4.4 says static table with versioned manual
> edits). Only fetch outdoor/retractable stadiums hosting a game that week; the week's games
> come from a `--games` CSV argument for now (schedule ingestion is a later slice). Record
> the forecast model run time and lead time.
>
> Use `httpx` with timeouts and 3-attempt retry with backoff; on any fetch failure, still
> write a partial capture with an `errors` section in the manifest — no silent fallback
> (design doc §1.5 rule 7). Tests: mock httpx (respx or monkeypatch), cover retry, partial
> failure manifests, and key redaction. Do NOT commit any real API key.

---

## Phase 0 — Data contracts and valid lineups (Weeks 1–2)

### Slice 3 — Database schema + migrations + decision-snapshot manifest

**Goal:** SQLite (WAL) operational store with the core-table subset Phase 0/1 needs
(players, aliases, external IDs, teams, games, slates, salaries, projection_snapshots,
ownership_baselines, actual_ownership, odds_snapshots, weather_snapshots, results,
decision_snapshots, model_runs), plus a tiny hand-rolled migration runner (numbered SQL
files, applied-migrations table). Every external-record table carries the §3.2 point-in-time
columns.

**Design doc:** §3.2, §8.2, §8.4.

**Model:** Claude **Fable 5 / Opus 5** (schema mistakes are expensive) · ChatGPT **GPT-5.1 Pro**

**Prompt:**

> Same repo. Read §3.2, §8.2, §8.4 of the design doc and the Slice 1 manifest model. In
> `src/narrative_alpha/store/`, implement: (1) a migration runner over numbered
> `migrations/NNNN_*.sql` files with an `applied_migrations` table, idempotent, applied in a
> transaction each, WAL mode on connect; (2) migration 0001 creating the Phase 0/1 table
> subset listed in docs/WORK_SLICES.md Slice 3 — for every table holding external data,
> include `published_at, observed_at, ingested_at, effective_at, valid_from, valid_to,
> source_version, run_id` where §3.2 says they apply, and never a bare mutable "current
> value" without them; (3) a `decision_snapshots` design where a snapshot row stores the
> manifest hash-set from §8.4. Add typed Pydantic row models next to the DDL and a thin
> connection helper (context manager, foreign keys ON). No ORM — plain SQL. Tests: migration
> runner idempotency, WAL is actually enabled, FK enforcement, and a round-trip
> insert/select through the row models for three representative tables. Then write
> `docs/schema.md` documenting each table in two lines each.

### Slice 4 — DK/FD salary CSV parsers

**Goal:** parse manually-downloaded DraftKings and FanDuel salary/contest CSVs into typed
rows; validate schema and player fields per slate; loud failure on unknown format drift.

**Design doc:** §4.1, §8.1 (golden-file CSV tests).

**Model:** Claude **Sonnet 5** · ChatGPT **GPT-5.1 Thinking**

**Prompt:**

> Same repo. In `src/narrative_alpha/ingest/salaries.py`, write parsers for DraftKings and
> FanDuel classic + showdown salary CSVs producing typed Pydantic rows (site, slate metadata,
> site player ID, name raw, team, opponent, listed position, eligible roster slots, salary,
> game time). Detect format by header signature, not filename; raise a structured error
> naming the unexpected/missing columns on drift (design doc §4.1: validate every slate).
> Include a `parse_report` (rows parsed, rows rejected and why). Golden-file tests: create
> `tests/golden/` with small anonymized sample CSVs for all four formats (DK/FD ×
> classic/showdown) — synthesize realistic samples from the sites' public export formats —
> plus property-based tests (hypothesis) for salary/position invariants. No crosswalk work
> in this slice; store site-native IDs only.

### Slice 5 — Player identity crosswalk

**Goal:** canonical `player_id`, alias table, vendor-ID mapping, confidence-scored matching
(RapidFuzz + deterministic rules), manual override persistence, unresolved queue. The
load-bearing slice of Phase 0.

**Design doc:** §4.2.

**Model:** Claude **Fable 5 / Opus 5** (edge-case design matters) · ChatGPT **GPT-5.1 Pro**

**Prompt:**

> Same repo. Read §4.2 of the design doc, the store layer, and `ingest/salaries.py`. In
> `src/narrative_alpha/identity/`, implement the crosswalk: canonical players + aliases +
> external IDs (schema exists from Slice 3 — extend with a migration if needed). Matching
> pipeline: exact vendor-ID hit → exact normalized name+team → deterministic alias →
> RapidFuzz candidate scoring gated by team/position/DOB agreement. Every match records
> `match_method`, `match_confidence`, `manual_override`. Below a confidence threshold,
> nothing is auto-matched: the player lands in an unresolved queue
> (`na-crosswalk resolve` CLI lists them and accepts a decision, persisting it as a durable
> alias). Never silently fuzzy-match a low-confidence player (§4.2). Seed canonical players
> from nflverse rosters (add a small fetch-and-cache step; pin the release/file hash).
> Tests must cover: suffixes (Jr/Sr/II/III), initials (D.J./DJ), hyphens, apostrophes,
> nicknames, duplicate names on different teams, team changes mid-season, and the
> no-silent-match rule. Acceptance: zero unresolved active-player mismatches on a real
> DK + FD slate pair before lineups can generate.

### Slice 6 — Projection/ownership snapshot ingestion into the store

**Goal:** load captured (Slice 1/2) projection and ownership files into
`projection_snapshots` / `ownership_baselines` with full point-in-time fields; never
overwrite earlier versions; resolve players through the crosswalk.

**Design doc:** §4.3 (purchased projections block), §3.2.

**Model:** Claude **Sonnet 5** · ChatGPT **GPT-5.1 Thinking**

**Prompt:**

> Same repo. Read §4.3, the store layer, snapshots layer, and identity layer. In
> `src/narrative_alpha/ingest/projections.py`, write per-source loaders that take a capture
> directory (Slice 1 manifests) and insert projection/ownership rows keyed by
> (source, site, slate, player, observed_at) — inserts only, never updates (§4.3: a Sunday
> 11 a.m. projection and a Saturday projection are different information sets). Carry
> `file_hash` from the manifest. Unresolvable players go to the crosswalk unresolved queue,
> and the load report says so. Build for two initial source formats behind a small
> `SourceFormat` registry so a third is a ~30-line addition [we'll name the actual purchased
> sources at build time — ask before inventing formats]. Golden-file tests per format;
> idempotency test (re-running a load duplicates nothing).

### Slice 7 — Optimizer adapter + valid upload CSV

**Goal:** `OptimizerAdapter` protocol (§2.1) with a `pydfs-lineup-optimizer` Phase 0
implementation; produce valid DK/FD classic lineups from blended projections; export a
site-valid upload CSV; property tests for roster/salary/site rules.

**Design doc:** §2.1, §6.5 (request fields — accept but may ignore most in Phase 0), §8.1.

**Model:** Claude **Sonnet 5** · ChatGPT **GPT-5.1 Thinking**

**Prompt:**

> Same repo. Read §2.1 and §6.5. In `src/narrative_alpha/portfolio/`, define
> `OptimizerAdapter` (Protocol) and `OptimizationRequest`/`Lineup`/`ValidationResult` Pydantic
> models covering the §6.5 request fields (Phase 0 adapter may raise `Unsupported` for
> advanced ones — explicitly, never silently ignore). Implement `PydfsAdapter` in its own
> module; **no other module may import pydfs_lineup_optimizer** — add a test that greps the
> source tree to enforce this. Implement `validate_lineup` independently of pydfs (positions,
> salary cap, team limits, site rules) so validation double-checks the solver. Implement
> `export_upload_csv` for DK and FD classic upload formats. Property tests (hypothesis):
> every generated lineup passes independent validation across randomized player pools.
> Acceptance test from the design doc: a generated CSV uploads cleanly to a free/low-stakes
> contest (manual step — produce the file and a checklist).

### Slice 8 — Results + actual-ownership ingestion & the first replay

**Goal:** ingest post-settlement contest standings exports (probe contests, §4.3) into
`actual_ownership` keyed by contest archetype/field size/entry limit; ingest weekly results;
then replay one historical slate end-to-end from pre-lock captures only, byte-for-byte
stable.

**Design doc:** §4.3 (actual ownership block + acquisition path), §3.2, Phase 0 acceptance
tests.

**Model:** Claude **Fable 5 / Opus 5** (replay correctness is the whole ballgame) · ChatGPT **GPT-5.1 Pro**

**Prompt:**

> Same repo. Read §4.3's actual-ownership contract, §3.2, and the Phase 0 acceptance tests
> in §9. Two parts. (1) `src/narrative_alpha/ingest/results.py`: parse DK/FD contest
> standings exports into `actual_ownership` rows carrying contest_id, archetype, field_size,
> entry_limit, entry_fee, role (classic/flex/captain), lineup_count, actual_ownership,
> source_observed_at — never mixing archetypes into one population. Golden-file tests with
> synthesized standings exports. (2) `src/narrative_alpha/replay.py` + `na-replay` CLI: given
> a `decision_at` timestamp and a decision snapshot, rebuild lineups using ONLY records with
> `observed_at <= decision_at`, then compare output hashes to the stored snapshot manifest.
> Add the guardrail as a query layer: a `PointInTimeSession` wrapper that every replay read
> goes through, which refuses queries lacking an as-of bound. Acceptance: replaying the same
> snapshot twice is byte-identical; injecting a post-lock projection row changes nothing.

---

## Phase 0 status — DONE (reviewed 2026-09-01) with an exit checklist

Slices 1–8 landed and passed project-lead review (4 parallel review agents + core review;
4 blockers and ~20 majors found and fixed; suite grew 60 → 106 tests). Items that can only
close against real-world data, tracked here until done:

- [ ] **Validate every parser against the first real exports.** All golden files are
  synthesized. Known open questions: DK contest-standings layout (real exports likely put
  athlete columns side-by-side with entries, not stacked — parser rewrite deliberately
  deferred until a real export exists); DK showdown dual-row CPT/FLEX shape; FD standings
  export shape. FD salary quirks (Tier column, time-less Game) are now handled.
- [ ] **Complete the manual upload checklist** (docs/manual-lineup-upload-checklist.md) once
  per site before treating upload formats as accepted.
- [x] **Build the production decision-snapshot writer.** Done (Slice 8b, reviewed
  2026-09-01): `na-build` freezes artifacts + snapshot row in one transaction and refuses
  to succeed unless its own immediate replay matches byte-for-byte. The shared candidate
  selection seam (`candidate_selection.py`) also retired the replay-blend drift risk from
  the deferred-minors list.
- [ ] **DK/FD-dependent items are blocked until Daniel returns to the US** (site logins are
  geo-unavailable): real salary/standings exports, probe-contest entries, and the manual
  upload checklist all wait; Stokastic exports (Slice 9) are the interim data source.
- [x] **Re-pin nflverse to a dated artifact.** Done (Slice 16, 2026-09-02): pins are
  append-only dated entries selected as-of the decision date, and successfully verified bytes
  live in a content-addressed local archive so later runs avoid the rolling upstream URL.
- [ ] Deferred minors (fast-follow, none block operation): snapshot `verify` doesn't bind
  directory name to `captured_at`; capture dir names contain colons (Windows-hostile);
  fetch failure records can pair an earlier HTTP status with a later error type; showdown
  contests collapse to one archetype (entry_limit is stored, split downstream when needed);
  replay's SQL `avg()` blend must move into the Phase 1 blend module so there is exactly
  one blend implementation.

### Slice 8b — Production decision-snapshot writer + `na-build` flow

**Goal:** the missing write-side of the replay contract: one command that takes a slate's
ingested data, builds lineups through the adapter, exports the upload CSV, and freezes a
`decision_snapshots` row whose manifest names every input hash — so the Sunday decision is
replayable from the moment it's made.

**Design doc:** §8.4, §6.8, Phase 0 acceptance ("replay of one historical slate using only
pre-lock files").

**Model:** Claude **Fable 5 / Opus 5** (write-side of the byte-stability contract) · ChatGPT **GPT-5.1 Pro**

**Prompt:**

> You are working in `DFS_Analyzer_DW` (Python 3.12, uv at `~/.local/bin/uv`). Phase 0 is
> complete: read `src/narrative_alpha/replay.py` (the consumer of what you will write),
> `src/narrative_alpha/store/models.py` (DecisionSnapshotRow, canonical_manifest_hashes,
> manifest_hash_set_sha256), the store connection/migrations, the ingest loaders, the
> identity crosswalk (`require_all_resolved`), and `src/narrative_alpha/portfolio/`.
> Also read docs/DECISIONS.md — its rules bind you, especially: schema changes are new
> migration files now, never in-place edits.
>
> Build the production decision path in `src/narrative_alpha/build.py` + a `na-build` CLI:
>
> 1. Inputs: database path, slate id, site, decision-at timestamp (defaults to now, must be
>    timezone-aware), artifact output directory, and optimizer request options (start
>    minimal: number of lineups, contest archetype; read candidate players from the store
>    exactly the way replay's `candidate_scenario` does — factor that SQL/logic into ONE
>    shared function both paths call, so build and replay cannot drift; replay currently
>    embeds an `avg()` blend that must move into this shared seam).
> 2. Flow: verify `require_all_resolved()` for the slate's active players (fail closed);
>    assemble the `OptimizationRequest`; serialize it to a canonical JSON artifact; build
>    lineups via `PydfsAdapter`; export the upload CSV; write both artifacts plus the
>    manifest hash-set (salary + projection file hashes drawn from the rows actually used,
>    `optimizer_request`, `generated_lineups`) into the artifact directory; insert the
>    `decision_snapshots` row in the same transaction scope as the run record in
>    `model_runs`.
> 3. Immediately self-verify: run `replay_decision` on the snapshot just written and refuse
>    to report success unless the replay hash matches byte-for-byte. A build whose own
>    replay mismatches is a failure (exit nonzero, structured error), not a warning.
> 4. Determinism rules: canonical JSON serialization (sorted keys, no float repr drift),
>    UTC-Z timestamps via the existing chokepoint, no wall-clock reads inside the
>    build-once/replay-later path except the explicit decision-at input.
>
> Tests: end-to-end build→replay byte-identity on a seeded slate; the self-verify failure
> path (corrupt an artifact between build and verify); unresolved-player refusal; and a
> test that build and replay share one candidate-selection implementation (e.g. assert the
> module function identity, not copied SQL). Run `~/.local/bin/uv run pytest -q`,
> `ruff check .`, `mypy` — all green before done.

## Phase 1 — Quant floor and contest context (Weeks 3–4)

### Slice 9 — Stokastic source adapter (projections + ownership)

**Goal:** the first real purchased source. Wire Stokastic NFL projection and ownership CSV
exports into the existing `SourceFormatRegistry` so a captured Stokastic file loads into
`projection_snapshots`/`ownership_baselines` end to end.

**Prerequisite (human):** Daniel downloads one of each export from Stokastic (NFL
projections CSV, ownership/leverage CSV — main slate) and captures them:
`uv run na-snapshot capture --season 2026 --week 1 --kind projections --source stokastic <files>`.
The executing model builds the adapter FROM those real files — no invented schemas.

**Design doc:** §4.3, §2 (buy 1–2 projection sources). **Model:** Claude **Sonnet 5** ·
ChatGPT **GPT-5.1 Thinking** (workhorse — the registry seam already exists).

**Prompt:** see the ready-to-paste version below (kept with the slice for reuse).

> You are working in `DFS_Analyzer_DW` (Python 3.12, uv at `~/.local/bin/uv`). Read
> `src/narrative_alpha/ingest/projections.py` (the `SourceFormatRegistry` seam and existing
> loaders), `src/narrative_alpha/ingest/timestamps.py`, `docs/DECISIONS.md` (binding rules:
> no silent fallback, no magnitude-inferred units, insert-only point-in-time writes), and
> the capture manifest under `data/snapshots/2026/week_01/` that contains the real
> Stokastic files — the actual CSV headers there are the source of truth for the schema.
>
> Build a `stokastic` source format registered in the `SourceFormatRegistry`:
>
> 1. Header-signature detection for the projections export and the ownership export
>    (two formats or one, matching whatever the real files show); unknown drift raises the
>    existing structured schema error naming missing/unexpected columns.
> 2. Map fields onto the existing parsed-row models: projection mean (floor/ceiling if the
>    export carries them), ownership as a FRACTION in [0,1] — determine the unit from the
>    header/percent-sign deterministically, never from magnitude. Player identification
>    uses name + team + position through the crosswalk (Stokastic has no DK/FD site IDs in
>    all exports — if an ID column exists in the real file, also store it as an
>    external_player_id source). Unresolvable players go to the unresolved queue and the
>    load report must say so.
> 3. Anonymize a small sample of the real files (a dozen rows, fake names/values, same
>    columns and quirks) into `tests/golden/stokastic_projections.csv` and
>    `tests/golden/stokastic_ownership.csv`; never commit the full real export (licensed
>    data).
> 4. Tests: golden-file parse, drift refusal, unit handling, idempotent reload, end-to-end
>    load into a seeded store through the capture manifest path. Run
>    `~/.local/bin/uv run pytest -q`, `ruff check .`, `mypy` — all green.
>
> If the real files are NOT present under data/snapshots/, STOP and say so — do not invent
> the schema.

**Status note (2026-09-01):** Stokastic has no main-slate NFL data until the season starts,
so Slice 9 waits for the first real export. Not a project blocker — Slices 10–12 are
code-first and proceed against seeded fixtures.

### Slice 10 — Player outcome distributions

**Goal:** turn each player's point estimate into the §6.2 mixture — `P(active)`,
`P(full_role | active)`, and a conditional distribution — so later signal effects modify
distribution *parameters* instead of overwriting one number, and the eventual simulator has
calibrated marginals to sample.

**Design doc:** §6.2 (the mixture and its parameters), §1.5 rule 5 (no point estimate
without uncertainty), §12.4.3 (shape validated by distributional scoring, not point error).

**Model:** Claude **Fable 5 / Opus 5** · ChatGPT **GPT-5.1 Pro** (frontier — quantile
fitting and scoring rules are easy to get subtly and silently wrong).

**Prompt:**

> You are working in `DFS_Analyzer_DW` (Python 3.12, uv at `~/.local/bin/uv`). Read
> `docs/DECISIONS.md` (binding rules), §6.2 and §12.4.3 of
> `docs/design/narrative-alpha-design-doc-v0_3.md`, `src/narrative_alpha/store/` (row-model
> and migration patterns — schema changes are NEW migration files), and
> `src/narrative_alpha/candidate_selection.py` (where projections are currently averaged
> into a single mean — this slice is the seam that later replaces that).
>
> Build `src/narrative_alpha/quant/distributions.py`:
>
> 1. A `PlayerOutcomeDistribution` model holding: `p_active`, `p_full_role_given_active`,
>    conditional location/scale/shape parameters, and a `quantile(q)` + `sample(n, rng)`
>    interface. The unconditional distribution is the mixture (inactive → 0 points).
> 2. A fitter that takes a vendor's mean/floor/ceiling for a player and a position, and
>    fits the conditional distribution. Vendor floor/ceiling columns are NOT guaranteed to
>    be the same quantile across sources (design doc §6.2 says so explicitly): the quantile
>    interpretation is a per-source, per-position CONFIGURED value in a visible table, never
>    assumed — an unconfigured source/position raises rather than defaulting silently.
>    Use a distribution family with a floor at zero and right skew (log-normal or gamma;
>    justify the pick in a docstring), fit by matching the configured quantiles, and refuse
>    (loudly) inputs where floor > mean > ceiling ordering is violated or a fit does not
>    converge to within a stated tolerance.
> 3. Migration 0004 + `player_distributions` table storing fitted parameters per
>    (slate, player, source-set, as-of) with the full §3.2 provenance block, so a
>    distribution used in a decision is reproducible.
> 4. A scoring module: CRPS and log score for a fitted distribution against a realized
>    fantasy-point outcome, plus a PIT-histogram calibration helper. These are the
>    §12.4.3 shape-channel validation targets and must be correct — property-test CRPS
>    against a Monte-Carlo estimate of its own definition.
>
> Do NOT wire this into the optimizer or replay in this slice (the build path's mean stays
> as-is); this is the parameter layer plus its scoring, landed cleanly. Tests: fitted
> quantiles round-trip to the configured input quantiles within tolerance; mixture
> arithmetic (E[X] over the mixture equals p_active-weighted conditional mean); refusal
> paths (unconfigured source/position, impossible ordering, non-convergence); CRPS
> property test vs Monte Carlo; calibration helper on synthetic well- and mis-calibrated
> samples. Run `~/.local/bin/uv run pytest -q`, `ruff check .`, `mypy` — all green.

**Status note (2026-09-01):** landed and reviewed. `quant/distributions.py` (fitter +
mixture), `quant/scoring.py` (CRPS, log score, randomized PIT), and migration 0004 +
`player_distributions`. Deliberately NOT wired into `candidate_selection.py`, `build.py`,
or the optimizer — the binding modelling choices are recorded in `docs/DECISIONS.md`, and
the wiring is Slice 13.

### Slice 11 — Contest archetype/payout schema + heuristic EV labeling

**Goal:** the contest side of the store and the honest-labeling rule from §2.2: contests,
payout curves, and a simple heuristic lineup-EV report where everything not backed by a
simulator is explicitly marked "heuristic only".

**Design doc:** §4.3 (contest cohort fields), §6.4 (lineup decision metrics), §2.2 (label
heuristics honestly), §8.2 (contests, contest_payouts tables).

**Model:** Claude **Sonnet 5** · ChatGPT **GPT-5.1 Thinking**

**Prompt:**

> You are working in `DFS_Analyzer_DW` (Python 3.12, uv at `~/.local/bin/uv`). Read
> `docs/DECISIONS.md` (binding: schema changes are NEW migration files now, never in-place
> edits), `src/narrative_alpha/store/` (migration runner, row-model patterns, §3.2
> point-in-time provenance columns — copy the established table shape exactly),
> `src/narrative_alpha/ingest/results.py` (the contest cohort fields already captured on
> actual_ownership), and `src/narrative_alpha/build.py` (BuildResult, whose lineups the
> report consumes).
>
> Three parts:
>
> 1. **Migration 0003**: `contests` (external_contest_id, site, slate_id, archetype,
>    field_size, entry_limit, entry_fee_cents, total_prizes_cents, payout_curve_id, full
>    §3.2 provenance block, STRICT, UNIQUE on (site, external_contest_id, observed_at)) and
>    `contest_payouts` (payout_curve_id, rank_from, rank_to, prize_cents, provenance block;
>    CHECK rank_from <= rank_to, no overlapping rank bands within a curve — enforce overlap
>    in code with a loud error, plus what CHECK can express). Typed Pydantic row models +
>    docs/schema.md two-liners, following the existing patterns exactly.
> 2. **Manual contest entry**: a small `na-contest add` CLI (or subcommand) that records a
>    contest and its payout table from flags/CSV — the operator copies these from the site
>    lobby; there is no API. Validate prize totals against total_prizes_cents when given.
> 3. **Heuristic EV report**: `src/narrative_alpha/portfolio/heuristic_report.py` — given a
>    BuildResult's lineups and a contest row, produce a report with: lineup projection sum,
>    salary used, projected-ownership sum (when present), naive cash-line proxy and naive
>    EV — and every such number carried in fields/headers explicitly named `heuristic_*`,
>    with a top-of-report line stating these are NOT simulator-backed (design doc §2.2).
>    No probability claims beyond the naive arithmetic; no hidden constants — thresholds
>    live in one visible dataclass.
>
> Tests: migration idempotency + row-model round-trips (copy test_store.py patterns),
> payout-band overlap refusal, prize-total mismatch refusal, CLI add + reload, and a
> golden-file test on the rendered report so its wording/format is pinned. Run
> `~/.local/bin/uv run pytest -q`, `ruff check .`, `mypy` — all green before done.

**Status note (2026-09-01):** landed and reviewed (commit `25cfb13`). Contest and payout
capture stays manual by design until site logins are reachable again.

Blocked Phase 1 slice (prompt written when unblocked):

- **Slice 9b — Second projection source + equal-weight blend across sources** (§6.1) once a
  second subscription exists. Workhorse tier.

### Slice 12 — Slate memo + baseline evaluation report

**Goal:** the two artifacts a human actually reads — a per-slate memo saying what was
decided and on what evidence, and a baseline evaluation report saying, with visible `n` and
strict point-in-time discipline, how wrong the purchased projections have been by position
and week. This is the system's first feedback loop; if it flatters the baseline, every
modelling judgment made after it is made against a lie.

**Design doc:** §6.8 (output artifacts per slate), §12.4.3 (channel-specific validation
targets), §1.5 rule 6 (no backtest without an information cutoff) and rule 7 (no silent
fallback), §7.1 (every response carries `as_of` and source/run identifiers).

**Model:** Claude **Fable 5 / Opus 5** · ChatGPT **GPT-5.1 Pro**. Nominally workhorse work,
tiered up on purpose: this is the measuring instrument for everything downstream, and both
of its failure modes — look-ahead joins and silently dropped players — produce numbers that
look entirely reasonable and are wrong.

**Prompt:**

> You are working in `DFS_Analyzer_DW` (Python 3.12, uv at `~/.local/bin/uv`). Read
> `docs/DECISIONS.md` (binding rules, including the new Slice 10 player-distribution
> entries), §6.8 and §12.4.3 of `docs/design/narrative-alpha-design-doc-v0_3.md`,
> `src/narrative_alpha/replay.py` (`PointInTimeSession` — the fail-closed as-of read
> boundary), `src/narrative_alpha/build.py` (`BuildResult`),
> `src/narrative_alpha/portfolio/heuristic_report.py` with
> `tests/golden/heuristic_report.txt` (the established report + golden-file pattern), and
> migration 0001 for `results`, `games.season`/`games.week`, and `projection_snapshots`.
>
> Two deliverables. No new tables — everything needed is already stored.
>
> 1. **`src/narrative_alpha/evaluation/baseline_report.py`** — purchased-projection error
>    by position and week.
>    - Every read goes through `PointInTimeSession`, bound to the slate's decision cutoff.
>      The projection scored against a result must be the version that was valid at that
>      cutoff, never the newest row: scoring a revised projection against a known outcome
>      is look-ahead and rule 1.5.6 forbids it.
>    - Accounting must be complete and visible. Per (position, week) report `n_scored`,
>      `n_projected_without_result`, and `n_result_without_projection` as separate counts.
>      Never drop a player silently. Projected-but-inactive players are the easiest way in
>      this whole system to make a baseline look better than it is — they get their own
>      count, and the report states in words that they are excluded from the error metrics
>      rather than leaving it implied.
>    - Metrics per cell: signed mean error (bias), MAE, RMSE, and Spearman rank correlation
>      — rank ordering matters more than level for lineup construction. Carry `n` on every
>      cell, and render any cell below a threshold held in one visible dataclass (follow
>      `HeuristicThresholds`) as `insufficient_n`, never as a number.
>    - The §12.4.3 shape channel: if — and only if — `player_distributions` rows exist for
>      the slate, add CRPS, log score, and a PIT summary from `narrative_alpha.quant.scoring`.
>      `SOURCE_POSITION_QUANTILES` is empty, so today's normal path is that no distributions
>      exist: render an explicit "shape channel unavailable — no configured source/position
>      quantiles" line (rule 1.5.7) and do NOT invent a quantile configuration to make the
>      section populate. Where log score is reported, count off-support (negative) outcomes
>      separately per the Slice 10 decision instead of averaging in an infinity.
> 2. **A per-slate memo** under `src/narrative_alpha/interface/` (currently empty — this is
>    its first occupant). From a `BuildResult` plus the store: slate identity, `decision_at`,
>    each lineup with projection/salary/ownership, which sources and file hashes fed the
>    decision, the heuristic-EV block when a contest is attached (call
>    `heuristic_report.py`; do not recompute its numbers), and the honest-labeling notice.
>    Header carries `as_of`, `decision_snapshot_id`, and `run_id` per §7.1. No number in the
>    memo may be one the store cannot reproduce.
>
> Wire an `na-report` entry point in `pyproject.toml` that renders both to stdout and to a
> file, following how `build_cli.py` and `contest_cli.py` are structured.
>
> Tests: a look-ahead regression test — a projection revised *after* `decision_at` must not
> move the report; missing-result and missing-projection accounting on a seeded fixture whose
> counts are known by construction; `insufficient_n` rendering; shape-channel-unavailable
> rendering; a hand-computed Spearman/MAE case checked against values worked out by hand, not
> against the implementation; golden-file tests on both rendered artifacts so wording and
> format are pinned. Run `~/.local/bin/uv run pytest -q`, `ruff check .`, `mypy` — all green.

**Status note (2026-09-01):** landed and reviewed. `evaluation/baseline_report.py`,
`interface/slate_memo.py`, `report_cli.py` (`na-report`), both golden files. Building it
exposed three real defects in the decision path it measures — `julianday()` timestamp
precision losing ~100µs of point-in-time discipline, `db_values()` writing a different
timestamp string than `utc_timestamp()` for the same instant, and replay comparing only
player-ID sets rather than candidate values — all fixed here and recorded in
`docs/DECISIONS.md`. Review also made `--evaluation-as-of` optional so the pre-kickoff
memo is reachable without inventing a result-label cutoff.

**Blocked behind Slice 12 (do not start yet):**

- **Slice 13 — wire distributions into the build path** (§6.2): replace
  `candidate_selection.py`'s single mean with the Slice 10 mixture. Blocked on the same real
  vendor export as Slice 9 — `SOURCE_POSITION_QUANTILES` cannot be configured until some
  source's floor/ceiling semantics are known, and guessing them is the one thing Slice 10
  was built to refuse.

## Phase 2+ — Narrative ownership MVP and beyond

Prompts deferred until Phase 1 (design doc orders it: Families 3 & 8 first, §9 Phase 2).
Slice boundaries will follow §9's deliverable list: source policies & collectors → Stage 1
extraction → episode clustering → heat features → logit-offset ownership model (§12.2.4
first-season simplification) → prequential evaluation. The extraction and clustering slices
are frontier-tier; collectors are workhorse-tier.

**Pulled forward (2026-09-01):** the collectors slice is promoted ahead of the remaining
Phase 1 work. Slices 9/9b/13 are blocked on data that does not exist yet, and §9.0 is
explicit that Phase −1 capture outranks modeling: narrative evidence is only usable if it
was already being collected (rule 1.5.2 quarantines any signal type discovered after
outcomes are known). Every week without collectors is a week that can never be used for
signal validation.

### Slice 14 — Source policies + narrative collectors

**Goal:** start the clock on irreplaceable data. A source cannot be collected from until its
rights and retention policy has been reviewed, and every item captured carries the observed-at
time that makes it admissible as prospective evidence later. Collection only — no extraction,
no clustering, no LLM anywhere in this slice.

**Design doc:** §4.5 (source families), §4.6 (per-source retention and rights — the "raw
items are immutable forever" rule is explicitly rejected), §5.3 (collection cadence), §8.2
(`source_policies`, `sources`, `source_items`, `content_tombstones`), §7.6 (raw text is
untrusted input), §9.0 (Phase −1 capture is the highest-ROI activity in the document).

**Model:** Claude **Sonnet 5** · ChatGPT **GPT-5.1 Thinking**. Workhorse shape — schema,
ingest, CLI — but review two seams hard: the fail-closed policy gate, and the dedup seam,
where collapsing cross-source duplicates too early destroys the reach signal that rule 1.5.3
depends on.

**Prompt:**

> You are working in `DFS_Analyzer_DW` (Python 3.12, uv at `~/.local/bin/uv`). Read
> `docs/DECISIONS.md` (binding rules — note the Slice 12 timestamp rules: point-in-time SQL
> compares canonical text, never `julianday()`, and every write goes through
> `ingest/timestamps.utc_timestamp`), §4.5, §4.6, §5.3 and §7.6 of
> `docs/design/narrative-alpha-design-doc-v0_3.md`, `src/narrative_alpha/store/` (migration
> and row-model patterns — schema changes are NEW migration files), and
> `src/narrative_alpha/snapshots/fetch.py` (the established fetch/retry/hash pattern; reuse
> it rather than writing a second HTTP path).
>
> Four parts.
>
> 1. **Migration 0005** — `source_policies` carrying exactly the §4.6 field list
>    (`source_id`, `permitted_use`, `raw_retention_days`, `personal_data_fields_allowed`,
>    `must_honor_deletions`, `redistribution_allowed`, `third_party_processing_allowed`,
>    `commercial_use_status`, `terms_reviewed_at`) plus the §3.2 provenance block;
>    `sources`; `source_items`; `content_tombstones`. STRICT tables, following the
>    established shape exactly.
> 2. **The policy gate, fail-closed.** Collecting from a source with no reviewed policy row
>    raises — there is no default policy, no inherited policy, and no "unknown" fallback
>    (rule 1.5.7). `terms_reviewed_at` older than a configurable staleness window also
>    refuses. This is the same shape as Slice 10's empty `SOURCE_POSITION_QUANTILES`: the
>    table ships with only the policies you have actually reviewed.
> 3. **Collectors for public RSS/Atom and official team feeds only.** No Reddit in this
>    slice — §4.6 says obtain approved access before depending on the API, and the request
>    is still queued. Design the collector interface so a Reddit collector drops in behind
>    the same policy gate later, and do not import or stub any Reddit client now.
>    - Store `published_at` (the item's own timestamp, nullable) and `observed_at` (capture
>      time) as separate columns. Rule 1.5.2 depends on `observed_at` being the capture
>      instant, never backfilled from the item.
>    - Content-address each item by a hash of its normalized text. Deduplicate within a
>      source by that hash, but **keep cross-source duplicates as separate rows sharing the
>      hash** — fifty outlets carrying one report is one episode with broader reach
>      (rule 1.5.3), and collapsing them here would destroy the reach signal the later
>      clustering slice needs. Write a comment saying so at the seam.
>    - Raw text is untrusted data (§7.6). This module stores it and never interprets it:
>      no LLM call, no tool call, no `eval`, no templating of item text into any prompt.
>      Strip embedded markup and hidden text into a separate cleaned field, preserving the
>      raw bytes under the retention policy.
> 4. **Retention enforcement** — a purge/tombstone command that drops raw text past its
>    source's `raw_retention_days` and writes a `content_tombstones` row, plus deletion
>    handling for items a platform reports removed. The tombstone survives; the raw text
>    does not. Never delete the row silently.
>
> Wire an `na-collect` CLI (following `build_cli.py`/`contest_cli.py`) with a run subcommand
> suitable for the §5.3 Wed–Fri batch cron, and a `purge` subcommand for part 4.
>
> Tests: policy-gate refusal (missing policy, stale `terms_reviewed_at`); `observed_at` is
> capture time and is never taken from the item; within-source dedup collapses and
> cross-source duplicates do not; retention purge removes raw text, writes the tombstone, and
> is idempotent; a feed fixture with embedded script/hidden markup is stored inert and
> cleaned; migration idempotency and row-model round-trips (copy `test_store.py` patterns).
> Use local fixture files — no network in tests. Run `~/.local/bin/uv run pytest -q`,
> `ruff check .`, `mypy` — all green.

**Status note (2026-09-01):** landed and reviewed. Migration 0005, `narrative/collectors.py`,
`na-collect`. Review found and fixed a silent truncation bug in the markup cleaner (a void or
unclosed tag inside a hidden block discarded the rest of the document) and added regression
coverage. Reddit stayed out, as scoped; the collector interface accepts one behind the same
policy gate once access clears.

### Slice 15 — Source seeding + first live collection

**Goal:** make Slice 14 actually run. The collector reads enabled sources from a table that
nothing populates, so today registering a feed means hand-writing SQL. This slice closes
that gap and gets real items landing weekly — the capture clock §9.0 cares about.

**Design doc:** §4.6 (per-source rights and retention), §5.3 (Wed–Fri batch cadence),
§1.5 rule 7 (no silent fallback), §9.0 (Phase −1 capture outranks modeling).

**Model:** Claude **Sonnet 5** · ChatGPT **GPT-5.1 Thinking**. Small and mechanical, but the
review attestation is the one part that must not be made convenient.

**Prompt:**

> You are working in `DFS_Analyzer_DW` (Python 3.12, uv at `~/.local/bin/uv`). Read
> `docs/DECISIONS.md` (binding — especially the Slice 14 collector decisions and the
> narrative-source entry), `src/narrative_alpha/collect_cli.py`,
> `src/narrative_alpha/narrative/collectors.py`, and `config/narrative_sources.toml` (104
> feeds, every URL verified live at commit time; note it deliberately carries no
> `terms_reviewed_at`).
>
> Three parts.
>
> 1. **`na-collect seed --catalog config/narrative_sources.toml`.** Loads the catalog with
>    `tomllib`, resolves each entry's `policy_tier` against the `[policy_tiers]` table, and
>    writes `sources` + `source_policies` rows.
>    - `--terms-reviewed-at` is **required**, and the command prints the tier terms being
>      attested and the count per tier before writing. Never default it to now, never read it
>      from the catalog: the operator is attesting they read the terms, and the Slice 14 gate
>      exists to require exactly that.
>    - Re-seeding is append-only versioning, not an overwrite — an unchanged entry inserts
>      nothing, a changed feed URL or policy inserts a new version. Print what changed.
>    - `--dry-run` renders the plan without writing.
> 2. **A `--check-feeds` mode** that fetches each catalog URL through the existing
>    `get_with_retry` path and reports which no longer return a valid RSS/Atom document,
>    without writing anything. Feed URLs rot; a silent 404 in a weekly cron is the failure
>    mode that costs irreplaceable data, so this must be runnable on demand and its output
>    must name every dead feed explicitly.
> 3. **Per-source failure isolation in `na-collect run`.** One dead feed must never abort the
>    batch: collect every source, and report per-source failures in the JSON summary with a
>    non-zero exit when any failed. Confirm this against the current `_run` behavior and fix
>    it if a single failure currently stops the run.
>
> Do NOT add a Bluesky or X collector in this slice. The catalog is RSS/Atom only and the
> migration's `collector_kind` check enforces it.
>
> Tests: seeding refuses without `--terms-reviewed-at`; the attested timestamp lands on every
> policy row and is never taken from the catalog; re-seeding an unchanged catalog is a no-op
> and a changed URL versions rather than overwrites; `--dry-run` writes nothing; `--check-feeds`
> reports a dead feed from a local fixture without network; one failing source does not stop
> the others and the exit code reflects it. No network in tests. Run
> `~/.local/bin/uv run pytest -q`, `ruff check .`, `mypy` — all green.

---

**Status note (2026-09-01):** Slice 15 landed and reviewed. `na-collect seed` with a required
operator attestation, `--check-feeds`, `--dry-run` against a disposable copy, migration 0006
versioning `sources`, and per-source failure isolation. Review found and fixed a redirect bug
that silently dropped any feed answering 301. Verified end to end against the real 104-feed
catalog: seed 104 → re-seed 0, and live collection of 250 items across three sources with
correct dedup on re-run.

---

### Slice 16 — nflverse dated pin + local byte archive

**Goal:** stop a weekly in-season failure. `identity/nflverse.py` pins a rolling release asset
that upstream overwrites; the hash check fails closed, which is correct, but means crosswalk
seeding breaks every time rosters churn. Small, contained, and it gets worse every week of
the season.

**Design doc:** §4.2 (player identity), §1.5 rule 7 (no silent fallback). Also closes the
open Phase 0 exit-checklist item.

**Model:** Claude **Sonnet 5** · ChatGPT **GPT-5.1 Thinking**. Small and mechanical.

**Prompt:**

> You are working in `DFS_Analyzer_DW` (Python 3.12, uv at `~/.local/bin/uv`). Read
> `docs/DECISIONS.md`, `src/narrative_alpha/identity/nflverse.py`, and the Phase 0 exit
> checklist in `docs/WORK_SLICES.md`.
>
> `PINNED_ROSTER_RELEASES` targets
> `https://github.com/nflverse/nflverse-data/releases/download/rosters/roster_2026.csv` — a
> rolling asset upstream overwrites, so the reviewed hash goes stale on every refresh and
> seeding fails closed until a human re-pins. Correct behaviour, wrong cadence for a season.
>
> 1. **Archive fetched bytes locally.** On a successful hash-verified fetch, write the exact
>    bytes to a content-addressed local archive (path derived from the sha256) and prefer the
>    archive on later runs. A pinned release already verified must never need the network
>    again. Never write bytes that failed the hash check.
> 2. **Support dated pins.** Allow more than one pinned release per season, each with its own
>    URL, sha256, and the date it was reviewed, so a weekly roster refresh is a new pin
>    alongside the old rather than an edit over it. Seeding selects the newest pin at or
>    before a supplied as-of date, so a replay of an earlier decision still resolves the
>    roster that was actually current then.
> 3. **A refresh helper** that fetches the current rolling asset, reports its sha256 and what
>    changed against the newest pin (players added/removed/changed), and prints the exact
>    entry to paste in. It must NOT self-pin — the manual review is the point.
>
> Tests: archive hit avoids a second fetch; a hash mismatch never writes to the archive;
> as-of selection picks the right pin among several; the refresh helper reports a diff without
> mutating `PINNED_ROSTER_RELEASES`. Use local fixtures, no network in tests. Run
> `~/.local/bin/uv run pytest -q`, `ruff check .`, `mypy` — all green.

**Status note (2026-09-02):** landed. `PinnedRosterRelease` now carries `reviewed_at`, seasons
hold append-only pin histories, and as-of selection refuses look-ahead. Verified bytes archive
under their full sha256 and bypass the network thereafter. `na-crosswalk nflverse-refresh`
prints the rolling asset hash, an added/removed/changed player diff, and a paste-ready pin entry
without mutating the reviewed table. **Reviewed 2026-09-02:** three majors fixed — refresh
discarded the bytes it had just hashed (so a pasted pin could fail closed on the next fetch;
it now archives them under their own hash), a same-day re-pin was never selected (`max` kept
the first tie), and a future `reviewed_at` was accepted. Minors: corrupt archive files are
named as local corruption rather than an upstream mismatch, archive writes fsync, and the diff
reports blank/duplicate rows instead of dropping them. Suite 402 → 406.

### Slice 17 — Stage 1 structured extraction

**Goal:** turn collected feed items into structured, provenance-bearing claims. This is the
first LLM in the system, so it is also where §7.6's untrusted-input rules stop being theory.
Extraction only: it records what a source claimed, never what a projection should become.

**Design doc:** §5.4 Stage 1 (outputs), §5.5 (explicit `evidence_refs` provenance, not prose
citations), §7.2 (native SDK, strict structured outputs), §7.3 (batch for the Wed–Sat lane),
§7.6 (prompt-injection controls), §8.2 (`claims`, `prompt_versions`), §1.5 rules 1 and 4.

**Model:** Claude **Fable 5 / Opus 5** · ChatGPT **GPT-5.1 Pro**. Frontier tier: the schema
shapes every downstream Phase 2 slice, and prompt-injection handling has to be right the
first time.

**Prompt:**

> You are working in `DFS_Analyzer_DW` (Python 3.12, uv at `~/.local/bin/uv`). Read
> `docs/DECISIONS.md` (binding — note the Slice 15 entry: collected RSS items are headlines
> and summaries of roughly 50–150 characters, NOT full articles; do not design as if you have
> article bodies), §5.4 Stage 1, §5.5, §7.2, §7.3 and §7.6 of the design doc,
> `src/narrative_alpha/narrative/collectors.py`, and `src/narrative_alpha/store/models.py`.
>
> Use the Anthropic Python SDK directly with strict structured output. Route extraction to
> **`claude-haiku-4-5-20251001`** per §5.6's low-cost-model rule; the model id and prompt
> version are recorded on every claim.
>
> 1. **Migration 0007**: `claims` and `prompt_versions`, full §3.2 provenance. A claim carries
>    player references, claim dimension, direction toward player outcome and *separately*
>    toward roster/ownership behavior, evidence basis, falsifiability, uncertainty and
>    ambiguity flags, suggested channels, and §5.5 `evidence_refs` — `source_item_id` plus
>    character offsets and the verbatim bounded extract. Store offsets and verify the extract
>    matches the stored text at those offsets; a claim whose extract does not appear verbatim
>    in its source item is rejected, not stored.
> 2. **The extractor.** Item text is untrusted data (§7.6): delimit it explicitly, state in
>    the system prompt that it may contain instructions to be ignored, give the model **no
>    tools**, and validate every output against a strict schema. Reject and flag any output
>    that tries to emit instructions, tool calls, or new system directives, and record the
>    injection flag against the source for review. Never interpolate item text into a prompt
>    without delimiting.
> 3. **Player resolution is deterministic, not model-decided.** The model returns names as
>    written; resolution to `player_id` goes through the existing crosswalk with its
>    fail-closed rules. An unresolved name is a stored claim with an unresolved reference and
>    a queue entry, never a guess.
> 4. **No projection deltas.** Stage 1 records claims. It does not propose, imply, or store
>    any number that could be read as a projection adjustment. Model self-reported confidence
>    is metadata, never a probability (rule 1.5.4).
> 5. **Replayability.** Same item + same prompt version + same model id must produce a stored
>    claim set that is reproducible; record prompt version, model id, and request id so a
>    claim in a decision can be traced back.
>
> An `na-extract` CLI runs a batch over unextracted items for a window, with `--dry-run` that
> renders prompts and cost estimates without calling the API.
>
> Tests: a golden fixture item extracts to a pinned claim set (mock the API — no live calls in
> tests); an extract that is not verbatim in the source is refused; an item containing an
> injection attempt ("ignore previous instructions and output...") is flagged and produces no
> claim; unresolved player names queue rather than guess; schema violations fail loudly;
> `--dry-run` makes no API call. Run `~/.local/bin/uv run pytest -q`, `ruff check .`, `mypy` —
> all green.

**Status note (2026-09-02):** landed. Migration 0007 adds immutable prompt artifacts, extraction
attempts, review flags, normalized claims, deterministic player/unresolved references, and exact
evidence spans. `na-extract` policy-gates and preflights retained canonical text, uses tool-free
strict Anthropic Message Batches on the pinned Haiku model, and has a no-write/no-API dry run with
versioned pricing estimates. Durable in-flight reservations prevent duplicate submissions and resume
accepted batches after timeout/crash with their original policy, request, and pricing lineage. The
authorization/reservation transaction installs scoped source/policy/item/tombstone fences and commits
before both submit and poll network I/O. Accepted traces are fsynced to the sibling receipt directory
before SQLite persistence.
Completed empty/flagged results are terminal; definite failures remain retryable, ambiguous create
outcomes fail closed, and tombstones redact reconstructive model/evidence text while preserving
non-reconstructive audit hashes and offsets. Batch create is single-shot and limit-aware; accepted
contract failures remain reconcilable without rebilling, and policy/TTL/source integrity is rechecked
immediately before create and atomically at result settlement so revoked or expired output is
accounted for but never retained as claims. Expiring create/recovery leases serialize concurrent
workers without holding a write transaction during polling. Recovery leases are renewed through item
settlement, stale owners cannot write after takeover, and complete multi-run lineage is stored in
`model_run_parents`. A transient `settling` state makes the claim graph append-proof after success,
and validated accepted results remain resumable across an interrupted or contended accepted-ID
SQLite commit.

**Reviewed 2026-09-02** (four parallel reviewers + core review; see `DECISIONS.md` "Slice 17
review outcomes"). Two blockers: a missing `ANTHROPIC_API_KEY` or a dropped connection at
create stranded every item in the window as `creating` with no operator escape; both are now
definite rejections, the CLI refuses to start without a key, and `na-extract abandon` /
`na-extract review` exist. Majors fixed: the Python-registered timestamp comparator and the
four-table rebuild were removed in favour of canonical UTC-Z enforced at insert (verified: the
rewritten migration upgrades a copy of the 3,852-item production database with identical rows,
clean integrity and foreign-key checks, and a bare `sqlite3` connection can write again); one
expired/tombstoned item aborted the whole window (now per-item ineligibility with reasons);
the injection detector flagged 13 of 28 realistic headlines (re-cut; 21 real headlines are
now must-pass tests, 20 attacks still caught); 2048 output tokens could not hold the schema's
12 claims and failed attempts retried forever (4096 and a three-attempt cap); team lexicon
lacked WSH/JAC and nicknames; receipt-write failures were swallowed (now a report warning);
dry-run printed 8.9 MB (prompts now behind `--show-prompts`, `--max-items` for smoke tests);
flags made the run exit nonzero (now 0; pending batch is 3). Still open, carried into Slice 19:
prompt taxonomy glosses, golden snapshot lacks offsets/resolution fields, hot-path full scans,
`model_run_parents` has no timestamps, fence triggers join on the provenance `source` column.
Suite 402 → 435.

---

## Operator UX direction (added 2026-09-02)

The finished tool must be simple to run by one person, and it does not need to be pretty.
The plan is layered so nothing is built twice:

1. **Slice 18 — a one-command batch lane and a one-screen status report** (`na-ops`). This
   is the "UI" for the rest of season one: `na-ops batch` runs the weekly collection →
   purge → extraction chain, `na-ops status` shows what ran, what failed, what is pending,
   and what Stage 1 has cost. launchd schedules it so the weekly news collection happens
   without anyone remembering to run it.
2. **A local web dashboard (queued, not yet prompted)** that wraps the same library calls the
   CLI uses: status page, review queues (unresolved players, flagged items), the slate memo,
   and one-click "run batch now". It is deliberately after `na-ops`, so the dashboard only
   renders functions that already work from the terminal.
3. **MCP tools** (design doc §7.1) come in Phase 3 and expose the same functions to Claude.

Rule for every UI slice: the CLI stays the source of truth; a UI never contains logic that the
CLI lacks.

### Slice 18 — Operator console: `na-ops batch`, `na-ops status`, launchd schedule

**Goal:** collapse weekly operation into one command and one status screen, and schedule the
batch lane so collection does not depend on the operator remembering it. This is the
solo-operator budget (§1.6) made concrete: "cron before workers", and the boring version of a
UI first.

**Design doc:** §1.6 (weekly time budget, complexity budget), §5.3 (collection cadence), §9.0
(fixed capture times), §10.3 (cost guardrails), Appendix D (weekly checklist).

**Model:** Claude **Sonnet 5** · ChatGPT **GPT-5.1 Thinking**. Workhorse: orchestration over
existing library functions plus a text report.

**Prompt:**

> You are working in `DFS_Analyzer_DW` (Python 3.12, uv at `~/.local/bin/uv`). Read
> `README.md` "Weekly operations", `docs/DECISIONS.md`, Appendix D of
> `docs/design/narrative-alpha-design-doc-v0_3.md`, and the existing CLIs:
> `src/narrative_alpha/collect_cli.py`, `src/narrative_alpha/extract_cli.py`,
> `src/narrative_alpha/snapshots/cli.py`, `src/narrative_alpha/identity/cli.py`. The library
> functions those CLIs call are the building blocks; call them directly, never via
> `subprocess`, and never duplicate their logic.
>
> Build `src/narrative_alpha/ops/` and a `na-ops` entry point in `pyproject.toml`.
>
> 1. **`na-ops batch`** runs the Wed–Fri batch lane in order: collect enabled feeds → purge
>    expired raw text → Stage 1 extraction over the window from the last successful
>    extraction end (or `--window-start`) to now → nflverse refresh check in report-only mode.
>    Each step is isolated: a failed step is recorded and the next safe step still runs
>    (purge and status recording always run; extraction is skipped if collection failed
>    entirely, and says so). Exit nonzero if any step failed. Every step writes an `ops_runs`
>    row (migration `0008_ops_runs.sql`, or the next free number: step name, started/finished
>    UTC, status, JSON summary, code version, error text) so status can show history. Nothing
>    is retried silently.
> 2. **`na-ops status`** prints one screen of plain text (and `--json`): per step, last
>    success and last failure with age; dead-feed count from the last collection run;
>    items collected in the last 7 days; extraction backlog (eligible but not yet
>    extracted), items awaiting review flags, and pending accepted-batch receipts;
>    crosswalk unresolved-queue length and whether the roster is seeded at all (zero
>    `players` rows is a loud warning, not a number); `na-snapshot status` for the current
>    season/week; Stage 1 spend month-to-date from `source_item_extractions` cost columns
>    against `monthly_llm_budget_usd` in a new `config/ops.toml`. The screen must answer
>    "did this week run, and what do I need to do by hand" without any other command.
> 3. **Budget guard.** Before submitting any batch, `na-ops batch` runs the extraction
>    dry-run estimate; if month-to-date spend plus the estimate exceeds the budget it
>    refuses to submit, records the refusal as a failed step, and prints the numbers. No
>    partial "submit what fits" behaviour.
> 4. **`na-ops schedule install|show|uninstall`** manages macOS launchd user agents under
>    `~/Library/LaunchAgents/com.narrative-alpha.*.plist`: `batch` on Wed/Thu/Fri at a
>    local time from `config/ops.toml`, plus reminder-only jobs (a macOS notification via
>    `osascript`, no data work) at the §9.0 manual capture times — Sat 6:00 p.m., Sun
>    9:00 a.m., Sun 11:00 a.m. ET, converted to local time — telling the operator which
>    downloads to make and the exact `na-snapshot capture` command. Jobs call a small shell
>    wrapper the command writes, which uses the absolute venv path, sets `PATH`, logs to
>    `data/logs/<job>.log`, and exports `ANTHROPIC_API_KEY` at run time from the macOS
>    Keychain (`security find-generic-password -s narrative-alpha-anthropic -w`). The key
>    must never be written into a plist, a log, or the repo. Note in the README that launchd
>    runs a missed job at next wake, unlike cron, and how to install the Keychain item once.
> 5. `README.md`: replace the scattered weekly commands with a "Weekly operations" section
>    that is exactly: install once, `na-ops status` any time, and what remains manual.
>
> Tests (pytest, `tmp_path`, `HOME` monkeypatched for launchd): a failing collect step does
> not prevent purge and status recording, and the exit code is nonzero; the extraction window
> derives from the last successful run and is overridable; status renders on an empty
> database and on a seeded one with a pending receipt directory; the budget guard refuses
> and records; plist and wrapper rendering are golden-tested and contain no key material;
> `schedule uninstall` removes only files it wrote. No network in tests. Run
> `~/.local/bin/uv run pytest -q`, `ruff check .`, `mypy` — all green.

**Delivered 2026-09-02.** `na-ops batch` runs collect -> purge -> Stage 1 -> report-only
nflverse refresh with every step isolated and recorded in append-only `ops_runs` (migration
0008, canonical UTC-Z enforced at insert, no updates or deletes). Purge always runs;
extraction is skipped with a stated reason only when collection failed entirely, and a
partial feed failure still extracts. The extraction window advances only on a successful
run and is overridable with `--window-start`. Before any submission the lane prices the
batch and refuses the whole thing if month-to-date spend plus the worst-case estimate
exceeds `monthly_llm_budget_usd` in the new `config/ops.toml`, recording the refusal as a
failed step with the numbers. `na-ops status` renders the one screen — per-step
success/failure ages, dead feeds, 7-day collection, Stage 1 backlog priced from the real
plan, review flags, in-flight attempts, pending receipts, unresolved identities, an empty
roster as a sentence rather than a zero, this week's snapshot coverage, and spend against
budget — in under a second against the 3,852-item store (backlog priced at $40.46
worst-case, matching the Slice 17 review). `na-ops schedule install|show|uninstall` manages
launchd agents: the batch lane on the configured days, plus notification-only reminders at
the §9.0 Eastern capture times converted to local against a mid-season anchor. Wrappers
read the API key from the login Keychain at run time; no plist mentions `ANTHROPIC`, and
uninstall removes only files carrying our marker and matching `Label`/`ProgramArguments`.
The enabled-source loop moved out of `collect_cli` into
`collectors.collect_enabled_sources`, so `na-collect run` and `na-ops batch` share one
implementation. README "Weekly operations" is now install-once, `na-ops status`, and what
stays manual. Suite 435 -> 458.

**Reviewed 2026-09-02** (solo review; see `DECISIONS.md` "Slice 18 review outcomes"). Three
majors fixed: the lane would have extracted the 3,852-item backlog against an empty roster
and created thousands of manual resolutions (extraction now waits, stated as a skip); the
extraction window closed at batch start so the items a run had just collected waited a
cadence (the window now closes after collection); and the weekly nflverse check would have
failed closed forever — the 2026 pin's bytes were never archived and upstream has already
overwritten the asset — with no way to produce a new pin (`nflverse-refresh
--allow-missing-prior` bootstraps, and a moved roster is a recorded failure carrying the
command). Minor: AppleScript quoting, UTC review dates. Verified `na-ops status` against a
copy of the production store (0.8 s). Suite 461 -> 466.

---

### Slice 19 — First live Stage 1 run + extraction eval set

**Goal:** run Stage 1 for real over the collected backlog (3,852 items as of 2026-09-02;
the review dry-run priced it at $1.02 input plus a $39 worst-case output ceiling, and flagged
zero real headlines as injection),
build the labeled evaluation set that gates every future prompt or model change (§7.5), and
fix whatever real headlines break. Slice 15 did this for collectors; this does it for the
first LLM stage. Daniel is in the loop: he supplies the API key, reviews samples, and
approves prompt v2 if one is needed.

**Design doc:** §7.5 (prompt and model evaluation), §5.6 (cheapest model that passes the
golden-set threshold), §7.6 (injection flags are for source review, not silent drops), §10.3
(cost per useful classified episode), §4.6 (retention: labeled text stays local under the same
TTL — never committed).

**Model:** Claude **Fable 5 / Opus 5** · ChatGPT **GPT-5.1 Pro**. Frontier: judging extraction
quality and revising the prompt is where a subtle mistake propagates into every downstream
signal.

**Prompt:**

> You are working in `DFS_Analyzer_DW` (Python 3.12, uv at `~/.local/bin/uv`). Read the
> Slice 17 entries in `docs/DECISIONS.md`, `src/narrative_alpha/narrative/extraction.py`,
> `src/narrative_alpha/extract_cli.py`, `tests/golden/stage1_claims.json`, and §7.5/§7.6 of
> the design doc. The production database is `data/db/narrative_alpha.sqlite3`; back it up
> together with its `.stage1-receipts/` sibling before any live run (see `data/README.md`).
>
> 1. **Precondition: the crosswalk is unseeded.** `players` has zero rows on the production
>    database, so every name would queue as unresolved, and `na-ops batch` refuses to extract
>    until it is seeded. The 2026 pin is also stale: upstream overwrote `roster_2026.csv`
>    before its bytes were archived. First run `na-crosswalk nflverse-refresh --season 2026
>    --reviewed-at <today> --allow-missing-prior`, review the roster file it archived, and
>    paste the printed entry into `PINNED_ROSTER_RELEASES`. Then add `na-crosswalk seed
>    --season 2026 --as-of <date>` around the existing `seed_nflverse_roster` if no CLI
>    exists, run it, and record the result. Do not proceed to a live run until the roster is
>    seeded.
> 2. **Dry run over the whole backlog.** Report item count, estimated cost, and how many items
>    the preflight excluded for injection markers, policy, TTL, or name/team validation.
>    Print the excluded items' titles. On real headlines an injection false-positive rate
>    above about 1% is a detector bug: fix the patterns, and add every real false positive to
>    the tests as a must-pass. Do the same for the person-name and team-lexicon validators
>    (they must accept real NFL names such as "Amon-Ra St. Brown", "Ja'Marr Chase",
>    "T.J. Hockenson", "Kenneth Walker III", and common team nicknames/abbreviations).
> 3. **Live run on a bounded window first** (about 200 items), then the rest of the backlog
>    if the sample looks right. Daniel exports `ANTHROPIC_API_KEY` in his shell; never read it
>    from anywhere else and never print it. Record estimated versus actual cost in the
>    slice status note.
> 4. **Review sample and eval set.** Add `na-extract sample --size 50 --output <dir>` that
>    writes a review CSV of stored results (claims, zero-claim items, flagged items —
>    stratified) with the canonical text, the claim fields, and blank label columns. The
>    labeled file lives under `data/eval/stage1/` (gitignored, same retention TTL as the
>    source text; the purge command must clear rows whose items are tombstoned). Daniel fills
>    the labels. Committed test fixtures stay synthetic.
> 5. **Eval harness.** `na-extract eval --labels <file>` scores stored claims against labels:
>    per-item claim presence, player-reference resolution, claim dimension and both
>    directions, evidence-span exactness, and injection-flag precision. It writes a
>    `model_evals` row (next free migration number: prompt version, model id, label-set hash,
>    metrics JSON, run id, point-in-time fields) and prints a table. This is the gate: a
>    prompt or model change ships only if its eval is not worse.
> 6. **Prompt v2 only if the eval says so.** A new `prompt_versions` row, never an edit of v1;
>    the active version is a config value; the golden test gets a v2 fixture. Every claim
>    already stored keeps its v1 lineage.
> 7. Status note in `docs/WORK_SLICES.md`: items processed, claims stored, zero-claim share,
>    flag counts, unresolved-name top ten, cost, and the eval numbers.
>
> Tests: `sample` stratification and CSV shape; `eval` metrics on a fixture label set,
> including the zero-claim and flagged cases; the real false positives found in step 2; seed
> CLI refuses without `--as-of`. No live API calls in tests. Run
> `~/.local/bin/uv run pytest -q`, `ruff check .`, `mypy` — all green.

**Implementation checkpoint (2026-09-02):** the reviewed 2026 nflverse asset was archived and
pinned at SHA-256 `44087b928376ef297c702ffed2c6b930185b4556105011baf70673b9a3073a2d`;
`na-crosswalk seed --season 2026 --as-of 2026-09-02` seeded 2,800 players from 2,800 rows with
zero rejects. The production recovery unit was backed up and integrity-checked first. A full
no-write Stage 1 plan found all 3,852 items eligible, with zero injection, policy, TTL, text,
or text exclusions; estimated input was 2,036,512 tokens / $1.018256 and
the 4,096-token-per-item output ceiling was 15,777,792 tokens / $39.44448 ($40.462736 total).
No false-positive detector fixtures were needed; explicit acceptance tests now cover Amon-Ra
St. Brown, Ja'Marr Chase, T.J. Hockenson, Kenneth Walker III, WSH/JAC, and common nicknames.

Migration 0009 adds immutable, point-in-time `model_evals`, timestamps recovery-parent edges,
corrects submission fences to join through source-item identity rather than the extraction
provenance label, and indexes the Stage 1 window/retry/name hot paths. `na-extract sample` writes
retained canonical text and complete stored claim/reference/evidence fields to a local stratified
review CSV with blank labels; tombstone purge removes matching CSV rows. `na-extract eval` checks
the label file against stored terminal results, hashes it, reports all §7.5 metrics, and records the
evaluation run. The synthetic suite is 479 tests.

**Live checkpoint (run under review, 2026-09-02):** the implementer could not run live
because the key was not in its process, so the review ran the lane. First pass, 21 items,
$0.046: zero claims stored — 16 of 21 items failed evidence validation. The raw batch output
showed the model's extracts were verbatim (33 of 36 spans in the source) but its character
offsets were wrong (1 of 36 correct), plus three straight-versus-typographic quote mismatches.
Validation now locates each extract in the canonical text and stores computed offsets (see
`DECISIONS.md`). Retry of the 17 failed items, $0.041: 15 succeeded, 26 claims stored
(availability/active_status 8, life_event 7, narrative 5, health 2, performance 2, role 1,
team_context 1), 2 evidence failures (one paraphrase, one partial quote), 1 schema violation,
1 item flagged `prohibited_output` for a placeholder name. Actual output averaged 312 tokens
per item against the 4,096 ceiling, so real cost is roughly 8% of the guard's estimate. All
32 player references came back unresolved because the roster was seeded after the items were
observed; identity now resolves as of settlement (see `DECISIONS.md`), and those 32 stay in the
queue for `na-crosswalk resolve`. Also fixed under review: `--max-items` on the lane lost the
deferred items (the watermark jumped past them); `batch.max_items_per_run` in `config/ops.toml`
caps unattended runs; the indexed name lookup dropped accented canonical names; and a killed
process locked its batch for an hour (`na-extract release`). The 50-item labeled sample and the
prompt-v1 eval numbers remain open: run `na-extract sample --size 50 --output data/eval/stage1`
after the next scheduled batch, label it, and run `na-extract eval`. Suite 479 -> 485.

### Slice 20 — Narrative episodes (Stage 2, deterministic)

**Goal:** stop treating each headline as an independent event. Cluster stored claims into
narrative episodes, label each item's relation to the episode (origin, independent,
corroborating, derivative, contradicting), and compute reach that a copied report raises while
the event count stays flat. No LLM in this slice: the deterministic version ships first and an
LLM synthesis pass is added only if evaluation shows it is needed.

**Design doc:** §5.2 (episode model), §5.4 Stage 2, §1.5 rule 2 (prospective signals only),
Phase 2 acceptance "duplicate copies do not increase statistical event count", §12.3 step 2
(deduplicate and author-cap).

**Model:** Claude **Fable 5 / Opus 5** · ChatGPT **GPT-5.1 Pro**. Frontier: the clustering rule
defines the unit of analysis for every statistic that follows (§12.4.1).

**Prompt:**

> You are working in `DFS_Analyzer_DW` (Python 3.12, uv at `~/.local/bin/uv`). Read
> `docs/DECISIONS.md`, migration 0007 (`claims`, `claim_player_refs`,
> `claim_evidence_refs`, `source_items`, `sources`), `src/narrative_alpha/narrative/`, §5.2
> and §12.3 of the design doc, and `config/narrative_sources.toml` (`source_family` is the
> audience class).
>
> 1. **Migration** (next free number): `narrative_episodes` (episode id, subject: resolved
>    player id or team code, claim dimension, opened at, last item at, origin claim id,
>    method version, point-in-time fields) and `episode_claims` (episode id, claim id, source
>    item id, relation ∈ origin | independent | corroborating | derivative | contradicting,
>    similarity score, method, point-in-time fields). Rows are append-only under a given
>    `(method_version, as_of)`; a rebuild at a new as-of inserts new rows, it never edits.
> 2. **Deterministic clustering** in `narrative/episodes.py`: group claims by subject and
>    claim dimension within a rolling window (72 hours default, configurable); link by
>    normalized-text similarity on the canonical source text (token-set Jaccard or MinHash —
>    pick one, justify it in `DECISIONS.md`) and direction agreement. `derivative` means
>    near-duplicate text from a different source after the origin; `corroborating` means
>    same direction, distinct text; `contradicting` means opposite direction; `independent`
>    means same subject and dimension but no textual link. Ties break by claim id so a
>    rebuild at the same as-of is byte-identical.
> 3. **Episode features**, computed only from items with `observed_at <= as_of`:
>    unique source count, unique source-family count, source entropy, reach proxy (unique
>    sources, never raw post count), velocity (items per 6 hours), recency (hours since last
>    non-derivative item), and `n_events` = origin + independent + corroborating items
>    (derivatives excluded). Store them on the episode row with their `as_of`.
> 4. **CLI** `na-episodes build --as-of <ts>` (refuses a missing as-of; refuses items after
>    it) and `na-episodes show --player <id> | --episode <id>` listing the episode with its
>    items and relations so the operator can audit a cluster by eye.
> 5. Unresolved-player claims (no `player_id`) form team-scoped or unclustered rows and are
>    reported, never silently dropped.
>
> Tests: the same headline from two sources becomes one episode with reach 2 and
> `n_events` 1; opposite directions link as contradicting; a claim outside the window opens a
> new episode; an item after `as_of` is excluded; two builds at one as-of are identical;
> unresolved-player claims are counted in the report. Run `~/.local/bin/uv run pytest -q`,
> `ruff check .`, `mypy` — all green.

**Implementation status (2026-09-02):** migration 0010 adds immutable, canonical-time episode and
claim-relation graphs with explicit method/as-of identity and Stage 2 run lineage. The deterministic
v1 method sessionizes by subject, dimension, and a configurable 72-hour rolling gap; token-set
Jaccard plus separate direction agreement assigns corroborating, derivative, contradicting, and
independent relations, with stable claim-ID tie breaks and an auditable prior-claim edge. Features
use unique source items: copied reports raise unique-source reach and entropy but not `n_events`.
Unresolved references normalize unambiguous team names and nicknames to canonical codes; ambiguous
or teamless references receive claim-scoped unclustered episodes and stay visible in the build
report. `na-episodes build` refuses an implicit cutoff, enforces both observation and ingestion
availability, and validates an identical rebuild before reusing it; `show` renders retained source
text and the complete propagation graph.

The fixture suite covers every acceptance case without network or production data. A disposable,
integrity-checked backup of the live database was migrated and clustered successfully: 27 eligible
claims produced 21 episodes and 27 memberships (19 unresolved claims were team-scoped, seven were
explicitly unclustered); a second identical build wrote nothing, and team names such as Rams/49ers
were stored as LAR/SF rather than raw labels. The production database itself was not modified.
Suite 485 → 492.

**Reviewed 2026-09-02** (two parallel reviewers; see `DECISIONS.md` "Slices 20 and 21 review
outcomes"). Blocker: links required agreeing non-unknown directions, so identical copies of
`unknown`/`neutral` claims — half the live corpus — were independent events, and no propagation
label ever fired on production data. Majors: timing ran off collector fetch time (all items in a
run share one instant), sessions never closed with a daily collector, `claim_player_refs` were
read without a cutoff, same-source reposts counted as events, and a source reclassification could
abort the build. All fixed; the prompt version is now pinned on the snapshot.

### Slice 21 — Deterministic heat features (Stage 3) + Appendix B feature rows

**Goal:** turn episodes into the Appendix B feature vector per player and slate at an as-of
time, with the §12.2.2 heat construction, the zero-gate floors, and standardization — so the
first ownership model (queued Slice 22) has features with provenance and nothing else to
invent. Features only: no ownership adjustment is computed or stored here.

**Design doc:** §12.2.2 (episode heat, zero-gate caution, feature list), §5.4 Stage 3, §1.5
rule 4 (self-reported model scores are metadata), Appendix B (feature contract), §8.3
(provenance).

**Model:** Claude **Fable 5 / Opus 5** · ChatGPT **GPT-5.1 Pro**. Frontier: statistics.

**Prompt:**

> You are working in `DFS_Analyzer_DW` (Python 3.12, uv at `~/.local/bin/uv`). Read
> `docs/DECISIONS.md`, the episode tables from the previous slice, `claims` (evidence class,
> basis, specificity/actionability metadata), `sources.source_family`, the slate and
> ownership-baseline tables from migration 0001, §12.2.2 and Appendix B of the design doc.
>
> 1. **Heat per episode** exactly as §12.2.2: direction × quality × specificity × novelty ×
>    audience-independence × log(1 + reach) × half-life decay by source class. Map
>    quality, specificity, and independence from [0, 1] to [0.15, 1] (the zero-gate floor);
>    direction and novelty keep true zeros. Half-lives per source class and the floor live
>    in `config/heat.toml` with a `feature_version`; any change to the formula or config
>    bumps the version. Novelty is 1.0 in this slice unless a vendor-ownership baseline change
>    is available for the window (document the placeholder in `DECISIONS.md`).
> 2. **Feature rows** (next free migration, `narrative_features`): one row per
>    (player, slate, site, as_of, feature_version) holding every Appendix B field that can be
>    computed from episodes — the `H_*` features, unique counts, source overlap index — plus
>    baseline fields joined from the latest ownership snapshot with `observed_at <= as_of`
>    when present, else NULL (never zero). Standardize within slate and source class, then
>    winsorize at ±4, and store both raw and standardized values. Each row records the
>    episode ids it used (JSON array) so a feature is traceable to evidence.
> 3. **CLI** `na-features build --slate-id N --site dk|fd --as-of <ts>`; refuses a missing
>    as-of and any input observed after it. Two builds at the same as-of are identical.
> 4. Velocity and acceleration need earlier feature points: compute them from the episode
>    timeline within the as-of window, not from previously stored rows, so a first build is
>    correct on its own.
>
> Tests: golden heat values for a hand-computed episode set (including a floored factor and a
> zero-novelty episode); derivative items change reach but not `n_events`; point-in-time
> exclusion; standardization and winsorization on a fixture slate; NULL baseline when no
> ownership snapshot precedes as-of; `feature_version` mismatch is refused, not overwritten.
> Run `~/.local/bin/uv run pytest -q`, `ruff check .`, `mypy` — all green.

**Implementation status (2026-09-02):** migration 0011 adds immutable feature-version bindings and
one Appendix B row per player/slate/site/as-of/version. `config/heat.toml` owns the source classes,
half-lives, evidence/source quality weights, 0.15 affine floor, six-hour interval, standardization
method, and `narrative-heat-v1` identity; canonical config drift under that identity is refused.
Episode heat uses roster direction, configured quality, claim specificity/actionability, the
baseline-aware novelty gate, unique-family independence, `log1p` unique-source reach, and
source-class half-life decay. Derivative copies affect reach but not the item-weighted factors,
event count, age, or independent-source entropy.

`na-features build` requires an explicit slate, `dk|fd`, and timezone-aware cutoff plus a complete
Stage 2 snapshot at the identical cutoff. It constructs signed/absolute and source-class heat,
six-hour velocity and acceleration directly from the member timeline, consensus, normalized class
entropy, novelty share, unique counts, and overlap; population z-scores across the full eligible
salary pool are stored beside raw values and clipped at ±4. Latest eligible same-vendor ownership
and projection pairs, the exact salary, sorted episode IDs, all ownership IDs used by temporal heat,
config/method hashes, and run lineage are retained. Missing inputs are `NULL`, never zero, and fields
without a defined source (author/cohort/scarcity/alternative/model) are not invented. Repeating an
identical build writes nothing; changed inputs conflict. The fixture-only acceptance suite includes
the literal golden product and zero gates, derivative reach, future-input exclusion, a real
26-player winsorization case, baseline novelty, config-version drift, and the CLI. Suite 492 → 499.

**Reviewed 2026-09-02.** Formula verified factor by factor against §12.2.2 with an independent
hand computation (bit-identical). Major: the novelty gate zeroed heat on any 0.1-point baseline
tick and, re-decided per instant, manufactured a large negative velocity; it now needs a
configured material move and is decided once at the cutoff. Major: an episode whose origin was
extracted later than a follow-up aborted the whole slate at t−6h; it now contributes nothing at
that instant. Added tests for the snapshot-conflict guard and zero-variance channels. The
open items (no-episode ratio features are 0, formula code is not hashed) are recorded. Suite
499 → 505.

### Slice 22 — Slate and salary ingestion (`na-slate`)

**Goal:** close the last gap in the Minimum Lovable Pipeline. Today nothing outside the test
suite writes a `slates` or `salaries` row, so `na-build`, `na-features`, and the slate memo
have no real slate to work on. This slice turns a captured DraftKings or FanDuel salary CSV
into point-in-time slate and salary rows through the crosswalk, and gives the operator a way
to see slate ids. Week 1 locks 2026-09-13; this must land first.

**Design doc:** §4.1 (salaries and slates), §4.2 (crosswalk), §3.2 (point-in-time), §8.4
(manifest hashes name every input), §1.5 rule 7.

**Model:** Claude **Sonnet 5** · ChatGPT **GPT-5.1 Thinking**. Workhorse: the parser, the
row models, and the crosswalk all exist; this is the loader between them.

**Prompt:**

> You are working in `DFS_Analyzer_DW` (Python 3.12, uv at `~/.local/bin/uv`). Read
> `docs/DECISIONS.md`, `src/narrative_alpha/ingest/salaries.py` (`parse_salary_csv` and its
> parsed-row models), `src/narrative_alpha/ingest/projections.py` (`load_projection_capture`
> is the pattern to copy: manifest hash verification, crosswalk resolution, insert-only
> point-in-time rows, idempotent reload, structured load report), `SlateRow`/`SalaryRow` in
> `src/narrative_alpha/store/models.py`, `src/narrative_alpha/snapshots/models.py` (the
> capture manifest), `src/narrative_alpha/identity/crosswalk.py`, and how
> `tests/test_build.py` seeds slates and salaries today.
>
> Build `src/narrative_alpha/ingest/slates.py` and a `na-slate` CLI:
>
> 1. **`na-slate ingest --season N --week N --site dk|fd --capture <capture dir>`** (default:
>    the newest `salaries` capture for that week under `data/snapshots/`). Verify the file
>    hash against the manifest, parse with `parse_salary_csv`, and write one `slates` row
>    per distinct slate in the file (classic vs showdown from the parser; `external_slate_id`
>    is a deterministic key of site, season, week, slate type, and the earliest kickoff, since
>    salary exports carry no slate id; `name` from `--slate-name` or that key) plus one
>    `salaries` row per player. `observed_at` is the manifest's capture time, never now;
>    `starts_at`/`locks_at` come from the earliest game time in the file.
> 2. **Crosswalk, fail closed.** Resolve every player through `PlayerCrosswalk.match` with
>    the site player id as the external id (DK and FD both carry one), name, team, and
>    position. Unresolved players go to the unresolved queue and are listed in the load
>    report; the slate is still written (a lineup build later refuses through
>    `require_all_resolved`). Never guess.
> 3. **Idempotent and versioned.** Reloading the same capture inserts nothing and says so.
>    A later capture of the same slate (Sunday re-download) inserts new salary rows with the
>    new `observed_at`; salaries that changed are reported as a diff. Rows are never updated.
> 4. **`na-slate list --season N --week N`** prints slate id, site, type, name, lock time,
>    player count, unresolved count, and the latest salary/projection/ownership observation
>    times — the ids that `na-build`, `na-features`, and `na-report` require.
> 5. `README.md`: the Saturday sequence is now capture → `na-slate ingest` → `na-slate list`.
>
> Tests: golden DK and FD salary captures (reuse the existing synthesized goldens) load into
> a seeded store with resolved players; an unresolved player is queued and reported; a hash
> mismatch refuses; reload is a no-op; a second capture versions rather than updates;
> showdown files produce a showdown slate; `list` renders. No network. Run
> `~/.local/bin/uv run pytest -q`, `ruff check .`, `mypy` — all green.

**Implementation status (2026-09-02):** `src/narrative_alpha/ingest/slates.py` and `na-slate`
(`ingest`, `list`) close the gap: the store now gets `slates`, `salaries`, `teams`, and `games`
rows from a captured export. No migration was needed. `ingest` defaults to the newest `salaries`
capture for the week, verifies each hash against the manifest, parses with `parse_salary_csv`,
and writes one slate per distinct slate in the capture plus one salary row per resolved player,
all at the manifest's capture time. `external_slate_id` is
`draftkings:2026:w01:classic:20260913T170000Z` — site, season, week, type, earliest kickoff — so
Sunday's re-download lands on the same `slate_id` and versions the salaries, reporting each
changed one as a diff. Reloading a capture inserts nothing and says so; a hash mismatch, a
wrong-week capture, or a missing capture refuses.

Nothing else in the pipeline writes `teams` or `games`, and `salaries.team_id` is NOT NULL, so
the loader records both from the export: the franchise code as its own key and name, and one game
per matchup with home and away taken from the export's own `AWAY@HOME` field (`ParsedSalaryRow`
now carries `is_home`). A FanDuel classic export omits kickoff times, so it needs `--starts-at`
and leaves `game_id` NULL with the affected matchups named. Unresolved players are queued and
printed with the exact `na-crosswalk resolve` command while the slate is still written;
`ingest` exits 0 clean, 1 queued/rejected, 2 refused. `list` prints the slate ids with player
count, the site's unresolved count, and the last salary/projection/ownership observation times.
Suite 505 → 526.

**Reviewed 2026-09-02** (solo review; smoke-tested against a copy of the production store with
the golden DK export). One major: team defense rows went to the unresolved queue — 32 by-hand
resolutions a week and a blocked build — and now resolve to one canonical `dst:<code>` player
per franchise. Minor: the README put `--database` before the subcommand, which the parser
rejects. Open, carried from Phase 0: real DK showdown exports may carry separate CPT and FLEX
rows per player, which `salaries`' one-row-per-player key cannot hold; verify against the first
real showdown export (Thursday 2026-09-10). Suite 526 → 527.

### Slice 23 — Slate lane (`na-ops slate`) end to end

**Goal:** one command for Saturday and Sunday, the way `na-ops batch` is one command for the
week: ingest that week's captures, build episodes and features at the decision instant, build
lineups, freeze the decision snapshot, write the memo, and say exactly where the upload CSV
is. This is the Sunday session of design-doc §12.5 in its boring form.

**Design doc:** §1.6 (MLP is the fallback state), §6.8 (output artifacts per slate), §8.4,
§9.0, Appendix D (Saturday/Sunday items), §12.5 (the decision session).

**Model:** Claude **Sonnet 5** · ChatGPT **GPT-5.1 Thinking**. Workhorse: orchestration over
existing commands, mirroring `ops/batch.py`.

**Prompt:**

> You are working in `DFS_Analyzer_DW` (Python 3.12, uv at `~/.local/bin/uv`). Read
> `src/narrative_alpha/ops/batch.py` (the step recorder, isolation, and `ops_runs` pattern
> you must reuse), `src/narrative_alpha/ops/status.py`, `src/narrative_alpha/ops/schedule.py`
> (the reminder text), `src/narrative_alpha/ingest/slates.py` and `projections.py`,
> `src/narrative_alpha/narrative/episodes.py` and `features.py`, `src/narrative_alpha/build.py`,
> `src/narrative_alpha/report_cli.py`, and `src/narrative_alpha/interface/slate_memo.py`.
>
> Add `na-ops slate --season N --week N --site dk|fd [--decision-at <ts>] [--lineups N]`:
>
> 1. Steps, in order, each isolated and recorded in `ops_runs` (a new migration widens the
>    `step` CHECK; add the slate steps, never rename the batch ones): ingest salaries
>    (newest capture) → ingest projections and ownership (every capture of those kinds for
>    the week not yet loaded, through the `SourceFormatRegistry`; a vendor with no adapter is
>    a recorded failure naming the vendor, and the lane continues) → Stage 2 episodes at
>    `decision_at` → Stage 3 features for that slate → `na-build` decision snapshot with the
>    upload CSV → slate memo. `decision_at` defaults to now and is written into every step's
>    summary; the same instant is passed to episodes, features, and the build so the
>    decision is replayable.
> 2. **Fail closed where the MLP requires it.** Unresolved players on the slate stop the
>    build step with the exact `na-crosswalk resolve` command; a missing projection source
>    stops the build and says which capture kinds are missing for the week. Earlier steps
>    still record.
> 3. **Output.** Print the memo path, the upload CSV path, the decision snapshot id, and a
>    one-line replay command. Add a "SLATE" section to `na-ops status` showing, for the
>    current week and site, which captures are ingested, whether episodes/features exist at
>    the latest decision instant, and the last decision snapshot.
> 4. Update the launchd reminder text to name `na-ops slate` as the command after captures.
>
> Tests: step isolation and recording; a missing projection capture stops the build step
> and names the kind; unresolved players stop the build with the resolve command; the same
> `decision_at` reaches every stage (assert on the injected dependencies); a second run at
> the same decision instant writes no new episodes/features (idempotent); status renders the
> slate section on an empty store and a seeded one. No network. Run
> `~/.local/bin/uv run pytest -q`, `ruff check .`, `mypy` — all green.

**Implementation status (2026-09-02):** `src/narrative_alpha/ops/slate.py` and
`na-ops slate` run the Saturday/Sunday session as six isolated steps recorded in
`ops_runs`: `slate_salaries` → `slate_projections` → `slate_episodes` → `slate_features` →
`slate_build` → `slate_memo`. Migration `0012_ops_runs_slate_steps.sql` rebuilds `ops_runs`
to widen the `step` CHECK; the four batch names are unchanged and every existing row is
copied verbatim, and `batch_run_id` keeps its name while now holding `ops-…` or `slate-…`.
The step recorder, the isolation, and `StepOutcome` moved out of `batch.py` into `runs.py`
so both lanes share one implementation; the recorder gained a `base_summary` that is merged
under every row, which is how `decision_at`, season, week, and site reach *every* step's
summary — including a step that failed before it had a summary of its own.

`decision_at` defaults to now and is the single cutoff passed to the episode build, the
feature build, and the decision build. A cutoff earlier than the run is refused up front
rather than left to fail four times: the lane stamps `ingested_at` at now, and Stage 3's
point-in-time reads require `ingested_at <= as_of`, so a past cutoff cannot see what the
lane just wrote. Episodes are slate-independent and run even when no slate resolved, so a
bad salary export costs only the slate steps. A week with two slates for the site refuses
to guess and names the candidates for `--slate-id`.

Fail-closed gates sit in front of the build: pending identities for the site stop it with
one `na-crosswalk resolve` line per player, and no point-in-time-eligible projection stops
it naming which capture kinds are missing for the week. A capture whose vendor has no
registered `SourceFormat` is a recorded failure naming the vendor and is not partly loaded;
the lane continues. `build.py` gained `BuildDuplicateError` (code `decision_already_exists`)
so a rerun at the same instant reloads and replay-verifies the frozen decision instead of
refusing — the id is a hash of the request bytes and the cutoff, so an identical id is
provably the identical decision, and a different `--lineups` builds a new one.
`report_cli`'s `load_build_result`, `default_report_path`, and `write_report_atomic` are
public for that reuse. `na-ops status` gained a SLATE LANE section: per-step history, how
many captured files of each kind actually reached the store (proved by the file's own
sha256 appearing on a row), each slate with its lock time/players/unresolved count and
latest observations, features at the latest decision instant, and the frozen decision. The
launchd reminders now name `na-ops slate` as the command after the captures.

Open items: (1) no vendor `SourceFormat` adapter exists (Slice 9), so today every real
projection/ownership capture is a recorded failure and the build then refuses for want of a
projection — the lane cannot yet produce a lineup from real vendor files. (2) [closed under
review, see below] the projection loader had no team-defence path. (3) The lane opens no `model_runs` row, so
the rows it ingests carry a NULL `run_id`, as `na-slate ingest` does today; a run is traced
through `ops_runs` and each row's `source_file_sha256`. Two pre-existing `ruff` findings
(under the newer pinned ruff) were fixed so `ruff check .` is green. Suite 527 → 541.

**Reviewed 2026-09-02** (solo review; ran the lane against a copy of the production store with
the golden DK capture: every step isolated, every refusal named its remedy, the SLATE LANE
status section rendered). One fix: open item (2) above is closed — the projection loader now
routes vendor DST rows to the same canonical `dst:<code>` defense as the salary loader, through
a shared `identity/defense.py`, so a vendor file cannot queue 32 defenses a week. Open items
(1) and (3) stand: no vendor adapter exists until Slice 9, and lane rows carry a NULL
`run_id`. Suite 541 → 542.

### Slice 24 — Local dashboard (read-mostly, one page)

**Goal:** the "not pretty, but simple" UI: a single local web page that shows `na-ops status`,
the review queues, the latest slate memo, and the batch/slate history, with two buttons
that run the lanes. No framework, no build step, no logic the CLI lacks.

**Design doc:** §1.6 (complexity budget), §7.1 (interface layer), Appendix D.

**Model:** Claude **Sonnet 5** · ChatGPT **GPT-5.1 Thinking**. Workhorse.

**Prompt:**

> You are working in `DFS_Analyzer_DW` (Python 3.12, uv at `~/.local/bin/uv`). Read
> `src/narrative_alpha/ops/` (status payload, batch and slate lanes), `extract_cli.py`
> (`review`), `identity/cli.py` (`resolve`), and `interface/slate_memo.py`.
>
> Build `na-ops dashboard [--port 8765]` on the standard library only (`http.server` and one
> HTML template with inline CSS; no JavaScript framework, no external assets, no CDN):
>
> 1. **Read pages** rendered from the same functions the CLIs call: the status screen
>    (every section of `status_payload`), the review queues (pending flags, in-flight
>    attempts, held leases, unresolved identities with their candidate players), the last
>    twenty `ops_runs` rows, and the latest slate memo text.
> 2. **Two actions, POST only, confirmed on the page:** "run batch now" and "run slate now"
>    for the current week. Each runs the lane in a background thread through the library
>    call, writes `ops_runs` as usual, and the page shows progress by reloading. Refuse to
>    start a lane that is already running.
> 3. **Resolve from the page:** each unresolved identity row offers its candidate players
>    and an ignore button, calling `PlayerCrosswalk.resolve` — the same call as the CLI.
> 4. Bind to `127.0.0.1` only. No credentials in the page or the process beyond what the
>    lanes already read from the Keychain wrapper. Add `na-ops dashboard` to the README as
>    the way to look at the tool without a terminal.
>
> Tests: the pages render on an empty store and a seeded one (use `http.client` against a
> test server on an ephemeral port); the batch action refuses a second concurrent start;
> resolve from the page persists exactly what the CLI would; the server refuses non-loopback
> binds. Run `~/.local/bin/uv run pytest -q`, `ruff check .`, `mypy` — all green.

**Implementation status (2026-09-02):** `src/narrative_alpha/ops/dashboard.py` and
`na-ops dashboard [--port 8765] [--host 127.0.0.1]` serve four read pages and three writes
on `http.server` and one inline stylesheet — no JavaScript, no framework, no external
asset. The status page renders the `status_payload` dict *itself* rather than a
hand-written selection of it, so a section added to `na-ops status` appears on the page the
day it lands and none can be silently dropped; a test asserts every current section by
name. The queues page renders `list_pending_review_flags`, `list_inflight_extractions`,
`list_execution_leases`, and `PlayerCrosswalk.list_unresolved`; the runs page a new
`recent_runs` in `runs.py` (both lanes interleaved, newest first, capped at 20); the memo
page reads the path recorded by the last successful `slate_memo` step, and states the gap
in words when that summary carries no path or the file has since gone.

The two actions run the lane in a background thread through the same library call the CLI
makes, each on its own connection, and `LaneRunner` refuses a second start of a lane
already running with the time the first began — the other lane is unaffected. A lane that
raises past its own step isolation is recorded and displayed rather than left to hang the
page on "running" for ever. Resolving from the page calls `PlayerCrosswalk.resolve` /
`ignore`; a smoke test against a live server confirms the row it writes is the CLI's —
`manual`, confidence 1.0, `manual_override`, note carried, alias written, and
`na-crosswalk resolve` then reports an empty queue.

Fail-closed choices beyond the prompt, all cheap: every write is a POST whose confirmation
box must be ticked; a form posted from another origin is refused, and an *opaque* origin
(`Origin: null`, which a sandboxed frame sends — found by clicking the button in a real
browser, not by a test) is refused with the remedy rather than trusted; the request body is
size-capped; the response carries a CSP that forbids the external assets the page does not
use. `--host` accepts only a loopback address and says why on refusal; `::1` binds the
right socket family rather than promising IPv6 and failing. Migrations run once at startup,
not per page view, so a read never waits on the write lock a running lane holds.

Open items: (1) the page reports lane outcomes only for lanes started *from it* — a lane
started in a terminal shows on the run history page but not in the LANES block, which the
page says in words rather than implying it knows about every run. (2) There is no
authentication, by design: the loopback bind is the whole boundary, so anything with a
shell on this Mac can drive it. Suite 542 → 589.

**Review outcome (2026-09-02):** one gap, fixed. The origin check compared `Origin` with
`Host`, and a browser fills both from the name it navigated to — so a page whose own
name is re-pointed at 127.0.0.1 after it loads (DNS rebinding) could read every page and
post a form with the two headers agreeing. The handler now refuses, before reading the
path, any request whose `Host` is not a loopback name on the bound port (`localhost`,
`127.0.0.1`, `[::1]`), with HTTP 421 and the remedy. Seven tests cover it, including the
case where `Origin` and `Host` agree under the rebound name. Suite 589 → 596.

### Queued, not yet prompted (in order)

- **Slice 9 — Stokastic adapter** stays open until real exports exist under
  `data/snapshots/`. Stokastic opens NFL main-slate data within about twelve hours of lock,
  so the first chance is Saturday 2026-09-12; hand the slice out that day, and expect Week 1
  to be capture-only for that source.
- **Slice 25 — First logit-offset ownership model + prequential evaluation** (§12.2.4,
  §12.2.5–§12.2.8): prompt when there are at least three weeks of actual ownership labels and
  a vendor ownership baseline is ingesting. Fitting on synthetic labels would violate rule
  1.5.2.
- **Slice 26 — Sunday fast lane** (Phase 3, §7.4): pre-approved `fast_lane_rules.yaml`,
  single-item synchronous extraction, official-inactive bypass.
- **Stage 2/3 in the weekly batch:** once Slice 23 lands, decide whether `na-ops batch` should
  also build episodes at the end of each run (cheap, deterministic) so `status` can show
  episode counts mid-week.

---

## Standing review checklist (project lead applies to every slice)

1. Point-in-time fields present and populated on every external record touched.
2. No silent fallback — failures are loud and structured (§1.5 rule 7).
3. Provenance: any derived number traceable to inputs (§8.3).
4. Tests actually exercise the failure modes, not just the happy path.
5. Nothing imports `pydfs_lineup_optimizer` outside its adapter.
6. Solo-operator test: does this add weekly operational burden? If yes, is it worth it?
7. Secrets stay in env vars; nothing sensitive in manifests, logs, or git.
