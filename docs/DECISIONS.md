# Decision Log

Standing technical decisions. Newest first. Each entry: date, decision, why, revisit-when.

## 2026-09-01 — Slice 12 point-in-time correctness fixes

Building the evaluation layer exposed three defects in the decision path it was written to
measure. All three are fixed; the rules below are binding from here.

- **Point-in-time SQL compares timestamps as text, never through `julianday()`.**
  `julianday()` returns a float whose resolution near 2026 is ~47µs, so timestamps up to
  ~100µs apart compared equal — a row observed *after* a decision cutoff passed the
  `<= :as_of` filter. That is a silent look-ahead leak in the exact boundary the system
  exists to enforce (rule 1.5.6). Every point-in-time predicate now uses
  `rtrim(col,'Z') <= rtrim(:as_of,'Z')`. `ingest/timestamps.py` already documented text
  comparison as the intended design; the `julianday()` calls were the deviation.
- **`StoreRow.db_values()` now emits the same canonical timestamp string as
  `ingest/timestamps.utc_timestamp()`.** It previously used pydantic's JSON mode, which
  drops the fractional part when microseconds are zero: the same instant serialized as
  `...T12:00:00Z` from one write path and `...T12:00:00.000000Z` from the other. Two
  strings for one moment defeats `UNIQUE(..., observed_at)` duplicate detection and any
  text comparison between the paths. One formatter, always microseconds, always `Z`.
- **Replay now verifies candidate *values*, not just the set of player IDs**, and binds
  manifest artifacts by `(source, sha256)` rather than by hash alone. The old check passed
  a replay in which a projection's value had changed, which made the byte-stability
  guarantee weaker than it read. `DecisionManifestHash.source` is consequently required for
  salary and projection artifacts — a breaking manifest-contract change, acceptable only
  because no production database exists yet.

- **The FanDuel salary parser was dropping `Injury Indicator`** into a `player_status`
  column that already existed in migration 0001. It is now populated, and it is one of the
  two explicit-evidence paths the baseline report accepts for classifying a player inactive.
  DraftKings exports carry no equivalent column, so DK rows are `NULL` — which the report
  treats as *unknown*, never as active.
- **Inactivity is never inferred from a missing or zero result.** Only explicit stored
  evidence counts: a result `stat_line_json` activity flag, or a point-in-time salary status
  in the visible inactive set. Inferring inactivity from a zero would silently drop the
  worst outcomes from the error metrics and flatter the purchased baseline — the single
  easiest way to make this system lie to its operator. Zero-point results with unknown
  activity are scored, and additionally counted as their own visible diagnostic.
- **`na-report --evaluation-as-of` is optional.** Omitted, the bundle renders the memo alone
  and states that no baseline was requested. The memo is the pre-kickoff Saturday artifact;
  requiring a result-label cutoff to produce it would have forced the operator to invent one.

## 2026-09-01 — Slice 10 player-distribution decisions

- **A vendor's mean/floor/ceiling triplet is interpreted as conditional on the player being
  active**, not as the unconditional expectation. Consequence, and the thing that will
  surprise whoever wires this in: `PlayerOutcomeDistribution.mean` is
  `p_active × vendor_mean`, which is strictly below the vendor's published number whenever
  `p_active < 1`. Revisit when a vendor documents that its projection already prices in
  availability — then the fitter needs a per-source flag, not a silent reinterpretation.
- **The fit matches the vendor mean exactly and the floor/ceiling *ratio* exactly, then
  validates the level against a visible tolerance** (default 2%), rather than
  least-squaring across all three targets. Two parameters cannot honor three constraints;
  choosing which one is exact beats an opaque compromise, and the residual is stored per
  row as `fit_max_relative_error`. A triplet that a zero-location log-normal cannot
  represent is refused, not silently approximated.
- **`SOURCE_POSITION_QUANTILES` ships empty on purpose.** No vendor has documented what its
  floor/ceiling columns mean, and §6.2 forbids assuming they match across sources, so the
  production fitter raises until a source/position pair is configured (rule 1.5.7, no
  silent fallback). Callers pass an explicit table; there is no cross-source or
  cross-position fallback. Revisit per source as documented semantics or position-level
  historical calibration arrive.
- **`p_full_role_given_active` is stored but is not yet part of the marginal.** Inventing a
  limited-role scoring distribution to make the field "used" would be a fabricated number.
  The marginal is exactly the inactive atom at zero plus the fitted active component until
  the second conditional component is modeled.
- **`p_active` is currently caller-supplied with no provenance column.** It is the one
  number in `player_distributions` that does not resolve to a stored evidence row, which
  is tolerable only while nothing consumes these rows. Revisit when the availability
  channel lands or when Slice 13 wires distributions into the build path — whichever comes
  first — since rule 1.5.1 binds the moment one reaches a decision.
- **Negative realized scores are off-support.** DK/FD fantasy points can go below zero;
  the zero-floored family gives them zero density, so `log_score` returns `+inf` there
  while CRPS stays finite and well-behaved. Any aggregate log score must therefore report
  off-support counts separately rather than averaging an infinity.

## 2026-09-01 — Phase 0 review decisions

- **Phase 0 `PydfsAdapter` explicitly rejects player-exposure ranges.** pydfs 3.6.1's
  progressive exposure semantics contradict our strict portfolio validator, so satisfiable
  requests aborted; honest rejection beats wrong output. The OR-Tools adapter implements
  them properly. `ownership_sum_range` is honored by converting sum→average bounds
  (÷ roster size) at the adapter boundary.
- **Captain-only showdown standings rows are structured errors, never guessed divisors.**
  Site captain multipliers vary; the base result comes from the FLEX row or not at all.
- **Fuzzy crosswalk matching is fail-closed on position.** An input identity without a
  position is never fuzzy-matched — it queues. Fuzzy/suffix accepts are not persisted as
  durable aliases, so a 0.92 guess can never resurface as a 1.0 "deterministic" match.
- **Team codes are canonicalized at the crosswalk boundary** (all 32 franchises + known
  vendor variants: LA/LAR, JAX/JAC, WSH/WAS, etc.).
- **Ownership units are never magnitude-inferred.** A `%` in the value or a
  percentage-named column decides; anything else is a parse error.
- **All ingest timestamps flow through one canonical UTC-Z formatter**
  (`ingest/timestamps.py`) so lexicographic comparisons on stored TEXT are safe.
- **Migrations 0001/0002 were amended in place** during review — allowed only because no
  production database existed anywhere yet. From the first real database onward, schema
  changes are new migration files, no exceptions.

## 2026-09-01 — Repo scaffold decisions

- **DataFrame library: Polars.** The design doc (§8.1) says standardize on one; Polars is
  faster, stricter about types, and the codebase is greenfield. Revisit only if a required
  library forces pandas interop beyond `.to_pandas()` at the boundary.
- **Package/env manager: uv.** Machine had no Python 3.12+; uv installs and pins it in one
  step and replaces pip/venv/pyenv. Boring and fast.
- **Operational store: SQLite (WAL mode) until the Sunday fast lane exists.** Per §1.6 and
  §3.3. Postgres arrives with Phase 3, not before.
- **Optimizer: `pydfs-lineup-optimizer` strictly behind `OptimizerAdapter`** (§2.1). No
  business logic may import it directly — enforce with a lint rule or test once the adapter
  exists.
- **Layout: single package `narrative_alpha` with one subpackage per architecture layer.**
  Matches §3 layer diagram; avoids premature microservice-style splitting.
- **License: none yet (private repo, all rights reserved).** Decide before any public release.

## 2026-09-01 — Process decisions

- **This chat (Claude Code) is project lead:** reviews all code, writes per-slice prompts,
  recommends which model executes each slice.
- **Work is sliced per docs/WORK_SLICES.md.** Each slice lands as a PR-sized change with
  tests; project lead reviews before merge.
- **Prompts for Phase 2+ slices are written when their phase begins**, so they can reference
  the real code state instead of guesses.
