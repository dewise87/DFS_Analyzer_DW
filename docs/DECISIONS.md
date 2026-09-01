# Decision Log

Standing technical decisions. Newest first. Each entry: date, decision, why, revisit-when.

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
