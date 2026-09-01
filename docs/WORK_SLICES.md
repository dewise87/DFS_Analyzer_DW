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
- [ ] **Re-pin nflverse to a dated artifact.** The current pin targets a rolling
  `roster_2026.csv` release asset that upstream overwrites weekly; the hash check fails
  closed but will break on every refresh. Archive fetched bytes locally.
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

Remaining Phase 1 slices (prompts written when reached):

- **Slice 9b — Second projection source + equal-weight blend across sources** (§6.1) once a
  second subscription exists. Workhorse tier.
- **Slice 10 — Player outcome distributions** fit from mean/floor/ceiling with
  position-specific calibration (§6.2). Frontier tier — distribution-fitting subtleties.
- **Slice 11 — Contest archetype/payout schema + heuristic EV labeling** (§2.2, §6.4):
  everything not simulator-backed is marked "heuristic only". Workhorse tier.
- **Slice 12 — Slate memo generator + baseline evaluation report** (§6.8): purchased
  baseline error by position and week; the first artifact you actually read on Saturdays.
  Workhorse tier.

## Phase 2+ — Narrative ownership MVP and beyond

Prompts deferred until Phase 1 (design doc orders it: Families 3 & 8 first, §9 Phase 2).
Slice boundaries will follow §9's deliverable list: source policies & collectors → Stage 1
extraction → episode clustering → heat features → logit-offset ownership model (§12.2.4
first-season simplification) → prequential evaluation. The extraction and clustering slices
are frontier-tier; collectors are workhorse-tier.

---

## Standing review checklist (project lead applies to every slice)

1. Point-in-time fields present and populated on every external record touched.
2. No silent fallback — failures are loud and structured (§1.5 rule 7).
3. Provenance: any derived number traceable to inputs (§8.3).
4. Tests actually exercise the failure modes, not just the happy path.
5. Nothing imports `pydfs_lineup_optimizer` outside its adapter.
6. Solo-operator test: does this add weekly operational burden? If yes, is it worth it?
7. Secrets stay in env vars; nothing sensitive in manifests, logs, or git.
