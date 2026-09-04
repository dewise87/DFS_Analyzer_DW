# Repository and architecture review — 2026-09-05

The architecture is a sound basis for an instrumented DFS workflow: immutable captures,
an explicit identity crosswalk, isolated optimizer integration, replayable decisions,
bounded ownership adjustments, and an experimental simulator. The principal remaining
risk is operational acceptance on real data. More model complexity is not the next milestone.

The review examined the design document, decision and work-slice logs, project configuration,
ingestion contracts, build/candidate selection/replay, portfolio rules, ownership routing,
simulation boundaries, and operations/test coverage. It used temporary test databases; it
did not run collectors, incur model charges, modify the operator's database, enter contests,
or upload lineups.

## Correctness changes

| Finding | Resulting behavior |
| --- | --- |
| FanDuel Single Game used the dependency's obsolete five-player, unchanged-MVP-salary format. | Six players: one MVP and five FLEX, 1.5× MVP salary and points, at least two teams. A local settings class inside the existing adapter supplies the current format and permits defenses in FLEX. Build, export, validation, and replay share the same rules. |
| Classic candidate selection ignored separately ingested ownership files. | The latest eligible dedicated baseline per player/role takes precedence. Embedded classic ownership remains the fallback when no dedicated row exists. Every consumed dedicated source/hash is recorded in the scenario and decision manifest, including showdown ownership. |
| Decision input reads checked observation time but omitted ingestion time on salaries, projections, availability, and joined entities. | Both timestamps must admit the cutoff. A later database import cannot appear in an earlier decision merely by carrying an earlier observation timestamp. |
| A salary-feed OUT label was retained for evaluation but ignored by lineup generation. | The build and evaluator share explicit inactive-status interpretation. Official availability decisions take precedence; questionable/doubtful labels alone do not mark a player inactive. |
| Independent validation trusted solver-reported salary and other player attributes and accepted empty/incomplete portfolios. | Validation checks candidate metadata and role multipliers, totals, unavailable players, site limits, count, exclusions, and exact pinned-prefix preservation. Build, replay, and frozen reads enforce it independently of the selected adapter. New estimates must match the scenario; pinned lineups intentionally retain their historical estimates. |
| Full-slate builds could generate fresh uploads after lock, without preserving locked slots. | Build and fast-inactive replacement refuse at or after lock. Historical pre-lock builds and replay remain available; late swap needs a separate implementation. |
| Optimizer contracts admitted infinite projections and whitespace-only eligibility. | Finite numeric inputs and nonempty normalized slots are required. Uniqueness cannot exceed roster size. |

The fast-lane and ownership-routing test fixtures previously performed nominally pre-lock
operations at/after their 17:00 lock. Their decision instants now leave enough time before
lock; the production guard is not relaxed to accommodate those fixtures.

## Current rule references

Verified during this review against primary sources:

- [FanDuel's new Single Game format](https://www.fanduel.com/research/fanduel-launches-new-single-game-daily-fantasy-format): six slots; 1.5× MVP salary and points.
- [FanDuel rules, lineup restrictions](https://www.fanduel.com/rules): Single Game requires players from at least two teams. With six players, this permits at most five from one team.
- [DraftKings Showdown overview](https://help.draftkings.com/hc/en-us/articles/24808583978003-Game-Style-Showdowns-Overview-US): six athletes, both teams represented, $50,000 cap, and 1.5× captain salary/points.

The legacy five-player FanDuel decisions were built under incorrect current-season rules.
They are not silently converted into valid six-player decisions. Retain their original
artifacts for audit and rebuild from current captures before lock. Supporting pre-2025
FanDuel historical contests would require explicitly versioned historical site rules.

## Next acceptance milestones, in order

1. **Finish the real vendor path.** The production `SourceFormatRegistry` is empty.
   Implement adapters from actual purchased exports, retain scrubbed representative fixtures,
   and verify a complete capture → ingest → build → replay → results cycle. Verify floor and
   ceiling semantics before enabling distribution fitting. Unknown vendor schemas should
   continue to fail visibly; this review does not fabricate an adapter.
2. **Validate each native site format.** Follow the upload checklist for each site/slate
   pair. The current DK dual-row coalescer groups by a single site ID; distinct CPT/FLEX IDs
   are not represented by the salary/candidate contract. Before accepting such exports,
   preserve both role IDs through ingestion, storage, export, and replay. The existing
   same-ID synthetic fixture is not proof of native compatibility. Native standings also
   need representative acceptance fixtures, as the README already states.
3. **Surface input readiness per slate.** Add an operator-visible coverage/freshness summary
   for active salaried players, projections by source, ownership, odds, and weather. Candidate
   selection currently includes only players with projections and can mix embedded and
   dedicated ownership across players. Count omissions and fallback use explicitly; calibrate
   appropriate refusal thresholds on real pools instead of assuming every salary row has a
   meaningful projection. A healthy collector or doctor screen alone is not slate readiness.
4. **Prove the season-one loop before expanding models.** Preserve pre-lock captures, record
   actual entered decisions, and collect settled labels weekly. Evaluate ownership against
   the vendor baseline on held-out weeks within the same site/archetype/role cohort. Keep
   source grading and simulation experimental until prospective evidence supports activation.
   The current optimizer's projection objective and ownership constraints do not establish
   positive expected profit or implement the design's full after-fee utility objective.

Keep the design's two-hour weekly operations budget as an acceptance criterion. The existing
SQLite, CLI, and local dashboard are appropriate; this review does not justify a database
migration, new web framework, or distributed workers.

## Verification

Final verification: **838 tests passed in 93.04 seconds**, Ruff passed, strict mypy passed
for 111 source files, and `git diff --check` passed.

The unchanged baseline passed 814 tests with local loopback sockets enabled. Regression
coverage was added for current FanDuel roster/role behavior and replay, separate ownership
captures and manifest stability, one-microsecond late ingestion, inactive salary labels,
lock boundaries, malformed solver output, exclusions/pins/counts, and invalid numeric inputs.
Run the repository gates from its root:

```sh
uv run --frozen ruff check .
uv run --frozen mypy src/narrative_alpha
uv run --frozen pytest -q
```

Dashboard tests require permission to bind temporary loopback ports. The initial sandboxed
run's socket failures were environmental; an unrestricted local test run established the
clean baseline. Formatting all existing files is not a release gate in this change: the
installed formatter reported 48 pre-existing files needing reformatting. Only touched files
were formatted to keep behavioral changes reviewable.
