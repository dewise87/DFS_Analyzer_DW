# NARRATIVE ALPHA
## Design Document v0.3: An NFL DFS Decision Engine for Context, Field Behavior, and Uncertainty

**Author:** Daniel Wise  
**Original draft:** v0.1 developed with Claude; v0.2 external LLM review; v0.3 reconciliation by Claude  
**Revision date:** September 1, 2026  
**Status:** DRAFT — reconciled; ready for build planning  
**Scope:** NFL daily fantasy on DraftKings and FanDuel, classic and showdown slates. Components should be reusable for NBA and MLB where the data-generating process permits.

### What changed in v0.2

1. Reframed the objective from finding “best plays” to maximizing after-fee expected utility for a defined contest portfolio.
2. Added an **availability gate** and a first-class **dependence/correlation channel** alongside mean, distribution shape, and ownership.
3. Split factual context, reported claims, and narrative propagation into different evidence classes with different priors and permissions.
4. Replaced the vague narrative-heat adjustment with a concrete, bounded, Bayesian logit-offset ownership model.
5. Replaced per-signal small-sample estimation with event-level deduplication, hierarchical partial pooling, skeptical priors, ROPEs, and prequential validation.
6. Moved contest simulation forward. A leverage score without field construction, payout structure, and duplication is not sufficient for lineup decisions.
7. Demoted `pydfs-lineup-optimizer` from long-term core dependency to a Phase 0 adapter behind an optimizer interface.
8. Added point-in-time snapshots and a replay harness so no backtest can accidentally use information published after lock.
9. Added a dedicated Sunday fast path, separate from asynchronous batch processing.
10. Added data-retention, prompt-injection, model-evaluation, and source-rights controls.
11. Replaced the “immutable raw Reddit archive” assumption with source-specific retention and deletion handling.
12. Answered every original Section 12 question directly in the new Section 12, with Questions 2 and 4 treated as implementation specifications.

### What changed in v0.3

The v0.2 review is accepted nearly in full — the statistical core (episode-level units, hierarchical pooling, the logit-offset ownership model, point-in-time replay) is right and stands. v0.3 makes seven targeted corrections:

1. **Added Phase −1 (immediate): perishable-data capture.** The 2026 season kicks off within two weeks of this revision. Every week of pre-lock snapshots and actual contest ownership not captured is training data lost forever. Snapshot capture starts *now*, before anything else works. (§9.0)
2. **Reinstated the solo-operator constraint as a first-class design rule.** v0.2 specifies a system a small quant team would be proud of; it must be buildable and *operable* by one person with a demanding day job. Added a weekly human-time budget, a complexity budget, and a Minimum Lovable Pipeline definition. (§1.6)
3. **Corrected the "buy contest simulation" decision** with the constraint v0.2 missed: purchased simulators generally run on the vendor's own projections and ownership, so they cannot natively evaluate *our* adjusted numbers. Their Phase 1 role is calibration reference and field intuition, not the decision engine for our deltas — unless a product verifiably accepts user-supplied inputs. (§2.2)
4. **Added the acquisition path for actual ownership labels** — the doc required the dataset without saying where it comes from. Contest-standings exports for contests we enter are the accessible source, and low-stakes probe entries in representative archetypes are reframed as a deliberate data-collection expense. (§4.3)
5. **Flagged the multiplicative zero-gate in the episode-heat formula** and specified floors for soft components so a single zeroed factor cannot silently erase a large episode. (§12.2.2)
6. **Restored illustrative dollar bands** to the budget section. Refusing point estimates until token volume is measured is principled; refusing all dollar context is unhelpful for a tool with a named sponsor writing personal checks. (§10.2)
7. **Named the fast-lane pre-approved rule list as an explicit versioned artifact**, resolving the tension between the eight-minute SLA and the human-approval requirement. (§7.4, §9)

Link hygiene: Appendix E updated (Anthropic docs domain, Reddit access-policy references, and a note that Reddit API approval must be filed immediately given multi-week queues).

---

## 0. Executive Summary (read this first)

Narrative Alpha is a decision system for NFL DFS on DraftKings and FanDuel. It buys the commodity layers — projections, baseline ownership, odds, weather — and builds the one layer no vendor sells: a disciplined engine for the information that lives outside the numbers, and for what that information does to the *field's* behavior.

The organizing idea survives from v0.1: **a piece of information matters through a channel, and the channel matters more than the information.** v0.2 refined the channels into five: an availability gate (will he play, and in what role), mean (expected points), shape (the distribution around them), dependence (how outcomes move together — the lifeblood of stacking), and ownership (what the field does). A revenge-game storyline is worthless on the mean channel and valuable on the ownership channel. A beat writer's Saturday-night snap-count report is the reverse. The system's job is to collect evidence, classify it honestly (fact, reported claim, or narrative), deduplicate copied stories into single episodes, route each episode to its permitted channels under caps, and grade every decision afterward against what actually happened.

Three hypotheses justify the build, any one of which is sufficient: sources sometimes beat purchased projections to news (latency); stories move rosters without moving outcomes (field behavior); and a season of graded claims reveals which sources actually know things (source learning). The architecture is designed so that if all three fail, the system degrades gracefully into a competent, honest wrapper around purchased data — and a no-edge shutdown rule says so out loud.

The unromantic truths the design commits to: season one is primarily an instrumentation season; the perishable asset is point-in-time data captured from Week 1 onward, not any model; and the whole thing must fit inside a few hours of one busy person's week or it will not exist by November.

---

## 1. Vision, Objective, and Epistemic Rules

### 1.1 Product objective

The product is not a projection model with colorful annotations. It is a **decision system** that combines purchased quantitative baselines with context, narrative propagation, uncertainty, contest structure, and portfolio constraints.

The optimization target is:

> **Maximize the expected after-fee utility of a portfolio of lineups for a specified site, slate, contest archetype, field size, payout curve, entry limit, and bankroll policy.**

That wording matters. A player can be a good point-per-dollar play and a bad tournament play. A lineup can project well and still have poor expected value because it is duplicated heavily. A narrative-driven ownership error only matters if it changes lineup-level payout probabilities.

### 1.2 Edge hypotheses

Narrative Alpha begins with three testable hypotheses, not a claim of a proven moat:

1. **Latency hypothesis:** selected primary and beat sources occasionally reveal availability or usage changes before purchased projections fully update.
2. **Field-behavior hypothesis:** stories can move roster rates without moving true player outcomes, creating ownership error.
3. **Source-learning hypothesis:** a claim-type-specific credibility ledger can identify which sources are informative, for which teams and claim types, before the market fully prices their reports.

Any of these hypotheses may fail. The architecture must preserve value even if only one survives.

### 1.3 Outcome channels

A signal can affect several outputs. The registry must not force one mutually exclusive channel.

0. **Availability gate:** changes `P(active)` or `P(full role | active)`. This is modeled before the conditional fantasy-point distribution.
1. **Mean channel:** changes expected fantasy points conditional on availability.
2. **Shape channel:** changes dispersion, skew, tail mass, or floor/ceiling asymmetry without requiring a large mean change.
3. **Dependence channel:** changes how players’ outcomes move together. This includes within-team target redistribution, game-stack correlation, weather-driven passing compression, and quarterback-change effects on pass catchers.
4. **Ownership channel:** changes field behavior, including flex and captain ownership, stack composition, salary allocation, and lineup duplication.

**Decision:** dependence/correlation belongs in the schema now. Full estimation can arrive later, but omitting it from the data contract would force a redesign when simulation and stacking become central.

### 1.4 Evidence classes

The original draft used “soft signal” too broadly. Weather, official injury status, and a contractual incentive are not the same epistemic object as a revenge-game narrative.

| Evidence class | Examples | Default permissions |
|---|---|---|
| **A: Structured or primary fact** | Official inactive list, practice status, sportsbook line, weather model, public contract threshold | May affect any channel under deterministic or validated rules |
| **B: Reported, falsifiable claim** | Beat writer reports expected snap limit; coach states a role change | May affect availability/mean/shape/dependence after source and claim checks |
| **C: Narrative or behavioral evidence** | Revenge-game chatter, birthday posts, viral highlight, public benching discourse | Ownership only by default; no mean adjustment without prospective validation |

### 1.5 Non-negotiable epistemic rules

1. **No adjustment without provenance.** Every changed number must resolve to an evidence item or a deterministic rule.
2. **No retrospective signal creation.** A signal type discovered after outcomes are known is quarantined until it fires prospectively.
3. **No raw count inflation.** Fifty reposts of one report are one narrative episode with broader reach, not fifty independent observations.
4. **No LLM confidence as probability.** Model self-reported confidence is metadata, not a calibrated posterior.
5. **No point estimate without uncertainty.** Mean, ownership, and correlation adjustments should be stored as posterior or scenario distributions.
6. **No backtest without an information cutoff.** Every run must be reproducible using only data available at its recorded `decision_at` timestamp.
7. **No silent fallback.** If a collector, projection source, or model fails, the system reports the degraded mode and reverts to the last valid snapshot or purchased baseline.

### 1.6 Solo-operator constraints and the Minimum Lovable Pipeline

This system is designed, built, operated, and paid for by one person who runs other businesses. That is a hard engineering constraint, equal in rank to the epistemic rules above, and v0.2's ambitions must answer to it.

**Weekly human-time budget (in-season, excluding the Sunday session itself):**

- Phases −1 through 1: ≤ 2 hours/week
- Phase 2: ≤ 3 hours/week
- Phase 3+: ≤ 4 hours/week, trending back down as automation matures

Any component whose *operation* (not construction) blows this budget is redesigned or cut, regardless of statistical merit.

**Complexity budget:** every subsystem must name its failure mode and its maintenance owner (there is only one candidate). Prefer the boring version of everything: SQLite before Postgres, cron before workers, a simple model that ships before a hierarchical model that doesn't. The Bayesian machinery in Section 12 is the destination, not the entry fee.

**Minimum Lovable Pipeline (MLP)** — the subset that must work for the tool to be worth running at all, and the fallback state whenever anything above it breaks:

1. salary CSVs in, crosswalk resolved, valid lineups out;
2. two purchased projection sources blended, one ownership baseline, odds and weather attached;
3. point-in-time snapshots frozen at every decision;
4. actual results and ownership ingested weekly;
5. a readable slate memo.

Everything else — episode clustering, the ownership model, the fast lane, simulation — is additive on top of a functioning MLP. If, by any checkpoint, the advanced layers are consuming build time while the MLP is unstable, the advanced layers pause. A modest tool that runs every week beats an impressive one that runs in October and dies by Thanksgiving.

---

## 2. What We Buy vs. What We Build

| Capability | Decision | Rationale and boundary |
|---|---|---|
| Base point projections | **Buy 1–2 sources** | Do not build a full player projection model. Preserve each source separately and blend only after point-in-time evaluation. |
| Baseline ownership projections | **Buy at least 1 source** | Use as an offset, not a target to replace. Snapshot every update. |
| Actual ownership labels | **Build ingestion** | Required for fitting the ownership-delta model. Store contest archetype, field size, entry limit, site, slate, and result-source timestamp. |
| Player props and game markets | **Buy/API** | Use as independent market information and latency checks. Store the exact observed-at snapshot. |
| Optimizer | **Use open source for Phase 0; build an abstraction immediately** | `pydfs-lineup-optimizer` is useful for file compatibility and early acceptance tests, but its latest PyPI release is old. Do not couple business logic to its classes. |
| Long-term optimization engine | **Build thin model over OR-Tools or equivalent** | Advanced late swap, portfolio constraints, stochastic objectives, and duplication penalties will exceed a generic optimizer’s interface. |
| Contest simulation | **Buy now; build a shadow simulator later** | Do not defer simulation as a concept. Ownership errors only become decision-relevant through field lineups, correlations, duplication, and payout curves. |
| Salary/slate ingestion | **Build** | Manual CSV upload is acceptable in Phase 0 and is the primary fallback even after automation. |
| Player/entity crosswalk | **Build** | This is load-bearing. It needs confidence scores, alias history, vendor IDs, and manual overrides. |
| Context and narrative collection | **Build selectively** | Favor primary and authorized sources. Each source gets a retention, licensing, and failure policy. |
| Narrative episode clustering | **Build** | Deduplicating one story across many posts is necessary for both ownership modeling and honest sample size. |
| Signal routing and validation | **Build** | This is the core differentiated layer. |
| Source credibility ledger | **Build** | Score by team and claim type, not one universal source score. |
| Point-in-time replay and evaluation | **Build from day one** | Without it, later validation is contaminated by look-ahead bias. |
| Contest selection and bankroll controls | **Build a basic version early** | A small modeling edge can be overwhelmed by poor contest choice or entry sizing. |
| Interface | **Build thinly** | MCP/chat plus a static dashboard is sufficient. Do not build a heavy frontend before the statistical loop works. |

### 2.1 Optimizer dependency decision

Use `pydfs-lineup-optimizer` only behind an interface such as:

```python
class OptimizerAdapter(Protocol):
    def build_lineups(self, request: OptimizationRequest) -> list[Lineup]: ...
    def validate_lineup(self, lineup: Lineup, slate: Slate) -> ValidationResult: ...
    def export_upload_csv(self, lineups: list[Lineup], site: Site) -> bytes: ...
```

The Phase 0 adapter can call `pydfs-lineup-optimizer`. A later adapter can use OR-Tools CP-SAT or another solver without rewriting the signal or portfolio layers.

### 2.2 Contest simulation decision

The correct compromise is:

- **Phase 1:** purchase or use a trusted contest-simulation product if available under the user’s subscription and license.
- **Phase 2:** build a simple internal field generator and outcome simulator in shadow mode.
- **Phase 3+:** use the internal simulator only after calibration tests show it is useful. Until then, it is a research instrument, not a source of false precision.

**Constraint v0.2 missed:** purchased simulators generally run on the *vendor's own* projections and ownership. Unless a product verifiably accepts user-supplied projections and ownership as inputs (confirm per product before subscribing — some optimizers/sims do, many don't), a purchased sim cannot evaluate Narrative Alpha's adjusted numbers; it can only evaluate the vendor's view of the slate. Its honest Phase 1 roles are therefore: (a) field intuition and contest-structure grounding for the human; (b) calibration targets for the later shadow simulator (its published field properties are data); (c) sanity checks on lineups we build. Our own ownership deltas are evaluated in Phase 1–2 by simpler expected-value heuristics, clearly labeled as such, until the shadow simulator or a custom-input sim product closes the gap.

---

## 3. System Architecture

Six layers, with separate batch and fast paths:

```text
┌────────────────────────────────────────────────────────────────────┐
│ L6  INTERFACE                                                      │
│     MCP/chat tools · static dashboard · alerts · decision log      │
├────────────────────────────────────────────────────────────────────┤
│ L5  PORTFOLIO & DECISION                                           │
│     contest selection · simulation · optimizer · exposure · swap   │
├────────────────────────────────────────────────────────────────────┤
│ L4  CONTEXT & NARRATIVE ENGINE                                     │
│     evidence extraction · episode clustering · routing · registry  │
├────────────────────────────────────────────────────────────────────┤
│ L3  QUANT & UNCERTAINTY CORE                                       │
│     projection blend · distributions · ownership model · dependence│
├────────────────────────────────────────────────────────────────────┤
│ L2  INGESTION & IDENTITY                                           │
│     salary/projection/result files · APIs · collectors · crosswalk │
├────────────────────────────────────────────────────────────────────┤
│ L1  STORE, SNAPSHOTS & GOVERNANCE                                  │
│     operational DB · Parquet snapshots · retention · audit · evals │
└────────────────────────────────────────────────────────────────────┘
```

### 3.1 Two execution lanes

**Batch lane**

- Tuesday through Saturday.
- Uses asynchronous batches for high-volume classification.
- Runs source ingestion, deduplication, feature computation, credibility updates, and slate memo preparation.
- Optimized for cost and reproducibility.

**Sunday fast lane**

- Runs from approximately 8:00 a.m. ET through lock and late swap.
- Polls only curated, high-value sources.
- Uses a warm worker, cached prompts, deterministic entity resolution, and low-latency model calls.
- Recomputes only affected players, games, stacks, and lineups.
- Optimized for deadline reliability, not breadth.

### 3.2 Point-in-time state

Every external record should carry:

```text
published_at       when the source says the item was published
observed_at        when Narrative Alpha first saw it
ingested_at        when it entered the database
effective_at       when the underlying fact applies, if different
valid_from/to      version interval for corrected or superseded data
source_version     source file/API/model version
run_id             pipeline run that created the derived record
decision_at        cutoff timestamp for a lineup decision snapshot
```

A weekly retrospective must replay the exact information set available at `decision_at`, not the final corrected data downloaded Monday.

### 3.3 Minimal deployment topology

Phase 0 can run locally. By the time the Sunday fast lane exists, use:

- one long-running Python process or small VPS;
- Postgres or SQLite in WAL mode for operational state;
- filesystem or object storage for point-in-time snapshots;
- a scheduler plus a priority queue implemented in the database;
- structured logs and alerting.

Kafka, Kubernetes, and a distributed feature store are unnecessary.

---

## 4. Data Acquisition Catalog

### 4.1 Salaries and slates

| Source | Method | Phase | Notes |
|---|---|---|---|
| DraftKings salary/contest CSV | Manual download | 0 | Primary, reliable fallback. Validate schema and player IDs every slate. |
| FanDuel player CSV | Manual download | 0 | Same role. Keep site-specific IDs and roster rules. |
| Unofficial site endpoints | Optional HTTP client | 3+ | Enhancement only after current Terms review. Must never be the sole path. |
| Licensed fantasy-data API | REST/API | Optional | Useful only if it materially reduces operations or supplies unique data. |

**Decision:** manual CSV upload is acceptable through Phase 2. Ninety seconds of reliable human work is cheaper than weeks spent maintaining fragile automation.

### 4.2 Player identity and crosswalk

Create canonical `player_id` values and map every external identifier to them.

Required matching inputs:

- normalized full name and suffix;
- team and opponent;
- listed position and eligible site position;
- date of birth where available;
- roster status;
- vendor/site IDs;
- historical aliases and team changes.

Matching output must include `match_method`, `match_confidence`, and `manual_override`. Never silently fuzzy-match a low-confidence player into a slate.

The weekly goal is not “five inevitable mismatches.” It is:

- zero unresolved active-player mismatches at lineup generation;
- all manual fixes persisted as durable aliases;
- automated tests for recurring edge cases such as suffixes, initials, hyphens, and duplicate names.

### 4.3 Projections, ownership, and results

#### Purchased projections

Store every source independently:

```text
source_id
site
slate_id
player_id
projection_mean
projection_floor
projection_ceiling
ownership_projection
source_published_at
observed_at
file_hash
```

Do not overwrite earlier versions. A Sunday 11:00 a.m. projection and a Saturday projection are different information sets.

#### Blending

- Start with equal weights.
- Do not switch automatically to inverse-MAE weighting; projection sources are correlated and MAE alone does not measure tail or lineup value.
- Later, use constrained rolling stacking with weights shrunk toward equal weights and evaluated by held-out week.
- Consider position-specific weights only after enough observations exist.

#### Actual ownership

This dataset is mandatory for the proposed ownership model. For each representative contest cohort, store:

```text
contest_id
site
slate_id
contest_archetype     cash | single_entry | 3max | 20max | mass_multi_entry | showdown
field_size
entry_limit
entry_fee
payout_curve_id
player_id
role                  classic | flex | captain
lineup_count
roster_count
actual_ownership
source_observed_at
```

Do not mix single-entry, 3-max, and mass-multi-entry ownership as if they were one population. A narrative can propagate differently across those fields.

**Acquisition path (v0.3 addition):** this dataset does not come from an API; it comes from contest results, which are readily exportable for **contests we enter**. That constraint is a feature: the contests we enter are by definition the representative cohorts we care about. Operationalize it as deliberate data collection — enter a small, fixed set of probe contests each week across the tracked archetypes (e.g., one single-entry, one 3-max, one mass-multi-entry at low stakes, plus one showdown), export full standings/lineups after settlement, and book the entry fees as a data-acquisition line item, not as bankroll deployed for profit. Broad third-party resale of historical contest data exists but is optional; verify licensing before relying on it. Post-lock exports are fine for labels — the point-in-time rule applies to *predictors*, not to outcome data used in grading.

### 4.4 Market and environment data

| Source family | Use | Design note |
|---|---|---|
| Sportsbook spreads/totals | Team expectation and game environment | Snapshot at each observation time. Closing lines cannot replace pre-lock lines in backtests. |
| Player props | Independent player-level market view | Useful but request-expensive. Pull only selected markets and players. |
| Open-Meteo/NWS or equivalent | Point-in-time stadium weather | Store forecast run and lead time. Historical reanalysis is not a substitute for the forecast users saw. |
| nflverse | Play-by-play, rosters, IDs, snaps and derived context | Pin data release or file hash for reproducibility. |
| Stadium metadata | Roof, surface, altitude, orientation | Static table with versioned manual edits. |

Structured weather belongs in the quant core. Claude should handle only unstructured exceptions, such as a report that a retractable roof decision changed or a field was damaged by a prior event.

### 4.5 Context and narrative sources

| Source family | Primary use | Caution |
|---|---|---|
| Official team/NFL sources | Availability, transactions, coach statements | Highest factual priority, but coach language can still be strategic or vague. |
| Beat writers and local insiders | Usage, role, health, expected actives | Score by team and claim type. |
| DFS analysts and high-reach shows | Field-information propagation | Often more predictive of ownership than team fan chatter. Track audience overlap. |
| Team and fantasy communities | Narrative formation, fan divergence | Deduplicate heavily; normalize by community; comply with platform policies. |
| Podcasts and video transcripts | Long-form local context and field narratives | Use authorized/public feeds and respect rights. Store time-coded extracts, not unnecessary full archives. |
| News wires and fantasy news feeds | Fast factual updates | Prefer licensed or clearly permitted access. |
| Contracts and incentives | Week 17–18 factual context | Verify against primary or reputable contract sources. |
| Search/social trend proxies | Narrative velocity and reach | Treat as noisy field-behavior indicators, not player-performance evidence. |

### 4.6 Source-specific retention and rights

A single “raw_items are immutable forever” rule is not acceptable.

Each source gets a policy:

```text
source_id
permitted_use
raw_retention_days
personal_data_fields_allowed
must_honor_deletions
redistribution_allowed
third_party_processing_allowed
commercial_use_status
terms_reviewed_at
```

For Reddit specifically, current official documentation requires authenticated access for eligible use, imposes rate limits, and requires deletion handling. The architecture should therefore:

- obtain approved access before depending on the API;
- avoid an indefinite raw-content archive;
- purge or tombstone deleted content and author-identifying data;
- retain non-reconstructive derived features only where permitted;
- use a short raw-text TTL, configurable by policy;
- treat any commercial pivot as a separate permissions and contracting question.

For every platform, raw text is **untrusted input**. It must never be able to instruct a model to call tools, reveal secrets, alter system prompts, or bypass source rules.

---

## 5. Context and Narrative Signal Engine

### 5.1 Signal taxonomy

#### Family 1: Availability and usage information

Examples: expected inactive status, snap cap, route participation change, backfield split, special package, starting-role change.

- Evidence classes: A or B.
- Channels: availability, mean, shape, dependence.
- Default prior: potentially material, but source- and claim-type-specific.
- Validation target: first usage mediators, then fantasy points.

#### Family 2: Fan-evidence divergence

The useful object is not generic sentiment. It is a weighted, evidence-tagged claim that close observers believe a player’s role, health, efficiency, or deployment changed more than projections or markets acknowledge.

- Evidence class: mostly C, occasionally B when linked to video/quotes.
- Channels: ownership by default; mean only after prospective validation.
- Default prior: weak.
- Important filter: distinguish “film/usage evidence” from enthusiasm, anger, jokes, and box-score recency.

#### Family 3: Narrative heat

Examples: revenge game, milestone, homecoming, primetime redemption, bounce-back, birthday, contract-year story, viral highlight.

- Evidence class: C.
- Channel: ownership.
- Mean prior: centered tightly at zero.
- Statistical treatment: narrative labels are UI tags. The model should estimate effects from shared continuous features such as reach, velocity, source mix, and direction rather than fit a separate unrestricted coefficient for every label.

#### Family 4: Contractual and organizational incentives

Examples: yardage bonuses, playing-time escalators, roster bonuses, team record incentives, late-season contract thresholds.

- Evidence class: A when contract terms and thresholds are verified.
- Channels: mean and right-tail shape; sometimes ownership.
- Seasonality: concentrated late in the season.
- Caution: an incentive is not proof a coach will alter play-calling. Model the mechanism and team discretion.

#### Family 5: Environment and logistics

Examples: wind vector, precipitation, field surface, altitude, short week, travel and circadian effects, roof decision.

- Evidence class: A for structured data; B for unusual local reports.
- Channels: mean, shape, dependence.
- Placement: structured components live in Layer 3, not the LLM signal engine.

#### Family 6: Team-context turbulence

Examples: quarterback change, play-caller change, offensive line disruption, role reassignment, rest/tank incentives, pace shift, eliminated-team youth movement.

- Evidence classes: A and B.
- Channels: availability, mean, shape, dependence, and sometimes ownership.
- Key requirement: update all affected teammates and stack relationships, not only the named player.

#### Family 7: Public life events and human factors

Examples: publicly reported childbirth travel, bereavement, illness, legal absence, public contract signing, birthday.

- Evidence class: B or C.
- Channels: availability when factual; ownership otherwise.
- Ethics: public reporting only. Do not infer private conditions or surveil personal accounts.

#### Family 8: Field-information propagation

This family was missing from v0.1 and may be more valuable for ownership than team subreddits.

Examples:

- a high-reach DFS show names a player a core play;
- several optimizers or content sites add a value tag after news;
- a viral lineup-construction rule spreads through DFS media;
- a salary-relief player becomes the obvious enabler for an expensive stack;
- a late-week analyst consensus forms despite unchanged projection inputs.

- Evidence class: C, with some A when a public optimizer tag or projection update is observed directly.
- Channel: ownership and lineup duplication.
- Key features: source influence, audience overlap, timing, contest archetype, salary structure, and whether the recommendation is actionable in lineup construction.

### 5.2 Narrative episode model

Do not classify each post as an independent signal firing.

Pipeline:

1. Normalize and hash the item.
2. Resolve players, teams, games, and named events.
3. Cluster semantically similar items into a `narrative_episode`.
4. Identify the likely origin and propagation path.
5. Compute unique-source reach, source entropy, audience overlap, velocity, and recency.
6. Link every episode to its evidence items.

A copied report increases estimated reach but not `n_events` or statistical sample size.

### 5.3 Collection cadence

| Window | Batch lane | Fast lane |
|---|---|---|
| Tue–Wed | Results, source grading, incentives, team-context refresh | Off |
| Wed–Fri | Injury diffs, beat/news scan, fan evidence, podcast processing | High-priority injury alerts only |
| Saturday | Full slate build, first ownership adjustment, weather, simulations | Selected late news |
| Sunday 8–11 a.m. ET | No large new batches | Curated polls, official updates, fast extraction, incremental recompute |
| Sunday in-slate | Archive lock snapshots | Late swap for later games |
| Mon–Tue | Validation and replay | Off |

### 5.4 LLM pipeline

#### Stage 0: deterministic preprocessing

- source-policy check;
- HTML/text cleanup;
- duplicate detection;
- language and spam filtering;
- player/team candidate resolution;
- content-length limits;
- prompt-injection markers;
- source metadata attachment.

#### Stage 1: structured extraction

Use a low-cost model with strict schema conformance. It extracts claims and evidence features. It does not decide projection deltas.

Outputs should include:

- player IDs;
- claim dimension;
- direction toward player outcome and separately toward roster behavior;
- evidence basis;
- falsifiability;
- exact extract and source item ID;
- suggested channels;
- uncertainty and ambiguity flags.

#### Stage 2: episode synthesis

Aggregate evidence items into a narrative episode. This stage determines whether items are corroborating, derivative, contradictory, or independent.

#### Stage 3: deterministic feature computation

Compute narrative heat from the structured episode graph. The LLM should not invent the final heat score.

#### Stage 4: channel routing and scenario proposal

A stronger model may propose channel effects, but deterministic permissions and magnitude caps govern what can be applied.

#### Stage 5: red-team review

For the largest proposed changes:

- identify contrary evidence;
- test whether the purchased baseline already moved;
- test for duplicate-source illusion;
- identify confounders;
- produce a “do nothing” case.

### 5.5 Structured outputs and citations

The pipeline should use strict JSON schemas for machine-consumed output. Native model citations and strict structured output may not be available in the same response mode. Therefore provenance should be represented explicitly in the schema:

```json
{
  "evidence_refs": [
    {
      "item_id": "uuid",
      "extract_start": 102,
      "extract_end": 246,
      "claim_text": "verbatim bounded extract"
    }
  ]
}
```

This is more reliable for the database than asking for prose citations.

### 5.6 Model routing

Do not hard-code model names into architecture. At startup or deployment:

1. query the provider’s Models API;
2. map capability tiers to available model IDs;
3. run a small golden-set evaluation;
4. choose the cheapest model that passes the task threshold;
5. record exact model ID, prompt version, schema version, token use, latency, and cost.

Suggested roles as of this revision:

- low-cost current model for bulk extraction;
- current Sonnet-class model for episode synthesis and most slate work;
- Opus/Fable-class model only if blind evaluation shows enough incremental accuracy to justify cost and latency.

The expensive model is not automatically the correct Tier 3 choice.

### 5.7 Signal Registry

Separate type definitions, weekly episodes, and posterior effects.

#### `signal_types`

```text
signal_type_id
family
mechanism_class
evidence_class
label
description
eligible_channels
prior_spec_json
rope_spec_json
hard_cap_json
default_half_life_hours
status
created_from               pre_registered | prospectively_discovered | retrospective_only
```

#### `signal_instances`

```text
signal_instance_id
signal_type_id
narrative_episode_id
slate_id
player_id
game_id
first_observed_at
last_observed_at
feature_vector_json
channel_proposal_json
applied_adjustment_json
run_id
decision_snapshot_id
```

#### `signal_effect_posteriors`

```text
signal_type_id
channel
context_key
posterior_mean
posterior_sd
posterior_quantiles_json
prob_positive
prob_negative
prob_in_rope
n_events
n_effective
last_validated_at
model_version
```

### 5.8 Status rules

Replace the v0.1 “20 observations and no effect means retire” rule.

- **UNVALIDATED:** prior only, retrospective discovery, or no prospective fires.
- **TESTING:** prospectively observed but posterior and predictive evidence are weak.
- **PROVISIONAL:** posterior probability of a practically meaningful effect is at least 0.80 and prequential scoring is not worse than baseline.
- **VALIDATED:** posterior probability of the expected-direction effect beyond the ROPE is at least 0.95, with positive held-out predictive evidence across independent blocks.
- **NULL-SUPPORTED:** at least 0.80 posterior probability lies inside the ROPE.
- **RETIRED:** low expected decision value, persistent predictive harm, data-rights failure, or mechanism superseded.

No status should be triggered by raw `n` alone.

### 5.9 Source credibility ledger

A source does not get one number. Use a multidimensional ledger:

```text
source_id
team_id
claim_type
claim_dimension
n_graded_claims
accuracy_posterior
calibration_score
precision
coverage
average_lead_time_minutes
correction_rate
last_claim_at
decay_weight
```

Examples:

- a beat writer may be strong on actives and weak on projected workload;
- a national reporter may be accurate but late;
- a coach quote may be primary evidence but strategically uninformative;
- a subreddit may be useful for ownership propagation and useless for player mean.

Grade only falsifiable claims. Vague commentary should not improve a source score merely because the player later performed well.

---

## 6. Quant Core, Synthesis, and Lineup Construction

### 6.1 Projection blend

Phase 1 blend:

```text
baseline_mean = simple average of valid purchased means
baseline_floor/ceiling = source-specific quantiles retained separately
```

Later blend:

- fit constrained nonnegative weights;
- shrink weights toward equal weight;
- evaluate by held-out week and position;
- use proper scoring for distributions, not only MAE for means;
- avoid re-estimating unstable weights every week.

### 6.2 Player outcome distribution

Represent each player as a mixture:

```text
P(active)
P(full_role | active)
conditional mean
conditional scale
skew/tail parameters or calibrated quantiles
```

A simple initial implementation can fit a distribution to purchased mean/floor/ceiling using historical position-specific calibration. Do not treat vendor “ceiling” columns from different sources as necessarily the same quantile.

Signal effects modify parameters rather than overwrite a single projection:

- official inactive news changes `P(active)`;
- expected snap cap changes `P(full_role | active)` and mean;
- weather can reduce passing mean and tail width;
- an incentive can alter the right tail more than the median;
- a quarterback change can alter pass-catcher means and dependence jointly.

### 6.3 Ownership adjustment

The canonical model is specified in Section 12.2. In operational terms:

1. take purchased ownership as a logit offset;
2. compute narrative-episode features deterministically;
3. estimate a bounded residual ownership shift;
4. generate posterior ownership scenarios;
5. calibrate predicted ownership to site roster-slot totals;
6. preserve separate outputs by contest archetype and role.

The engine should output:

```text
ownership_p10
ownership_p50
ownership_p90
ownership_delta_p50
probability_delta_positive
baseline_source
model_version
```

### 6.4 Leverage and lineup value

Do not define player leverage only as “projection percentile minus ownership.” That is a dashboard heuristic, not an objective.

Use two levels:

**Player diagnostic:**

```text
value percentile
ceiling percentile
ownership percentile
narrative ownership residual
stack compatibility
late-swap optionality
```

**Lineup decision metric:**

```text
expected payout
expected ROI
cash probability
top-1% probability
duplication distribution
downside or utility-adjusted ROI
```

The player diagnostic explains why a lineup may be attractive. The simulation decides whether the lineup is attractive in the contest.

### 6.5 Optimizer requirements

The optimizer request should include:

```text
site and slate rules
contest archetype
salary cap
candidate player scenario
stack rules
bring-back rules
team and game exposure limits
player exposure ranges
lineup uniqueness
ownership-sum range
duplication penalty
late-game optionality value
portfolio-level covariance penalty
number of lineups
time limit
```

Avoid a single fixed “QB + 2 pass catchers + bring-back” rule. Stacking policy should depend on slate size, site scoring, quarterback archetype, ownership, game environment, and contest type.

### 6.6 Contest simulation

Minimum required components:

1. **Player outcome generator** with calibrated marginals.
2. **Dependence model** using game/team latent factors and position relationships.
3. **Field lineup generator** that reproduces ownership marginals, stack rates, salary use, roster construction, and lineup uniqueness.
4. **Payout evaluator** using actual contest structure.
5. **Duplication model** for tied payouts.
6. **Calibration suite** comparing simulated ownership, score distributions, stack distributions, and duplication against historical contests.

A shadow internal simulator may begin with:

- purchased player distributions;
- a Gaussian or t copula with football-specific latent factors;
- field lineups generated by a stochastic optimizer with ownership-weighted objectives;
- empirical calibration by contest archetype.

It must remain clearly labeled experimental until it reproduces historical contest properties.

### 6.7 Late swap

Late swap is not “rerun the optimizer.” It must condition on:

- points already scored;
- current contest standing if available;
- remaining roster slots and salaries;
- opponent/field late-player ownership;
- current payout objective;
- news and updated outcomes for remaining games;
- the value of variance given current state.

The recommended swap can differ for a lineup currently ahead versus one far behind.

### 6.8 Output artifacts per slate

- baseline and adjusted projection CSV;
- ownership scenario CSV;
- signal and evidence audit table;
- leverage/diagnostic board;
- lineup portfolio CSV in site-upload format;
- contest-simulation report;
- late-swap state file;
- human-readable slate memo;
- immutable decision snapshot manifest.

---

## 7. Anthropic / Claude Integration Architecture

### 7.1 MCP interface

A local MCP server can expose tools such as:

```text
get_slate_summary
get_player_dossier
get_narrative_episode
get_ownership_scenarios
get_portfolio_report
compare_lineup_scenarios
search_evidence
log_decision
replay_snapshot
```

Every tool response should include an `as_of` timestamp and source/run identifiers.

### 7.2 API engine

Use the native provider SDK for:

- strict structured outputs;
- batch requests;
- prompt caching;
- token counting;
- retries and request IDs;
- model discovery.

Do not rely on an OpenAI-compatibility layer for production schema guarantees when the native API exposes stronger controls.

### 7.3 Batch processing

Use asynchronous message batches for Wednesday through Saturday extraction and synthesis. Current provider documentation prices batch processing below synchronous requests, but batch completion time is unsuitable for the Sunday fast lane.

### 7.4 Fast path

- prewarm prompts and schemas;
- use a current low-latency model that passes the extraction eval;
- restrict each call to one new item plus cached player/team context;
- prohibit open-ended web search in the critical path;
- use deterministic rules for official inactives and known status changes;
- require human approval for large mean changes unless the source is official and the rule is pre-approved.

**Pre-approved rule list (v0.3):** the phrase "pre-approved" above is a versioned artifact, `fast_lane_rules.yaml`, not a vibe. Each rule names: trigger source class (e.g., official inactive list, specific A-graded beat account), claim type, maximum automatic adjustment per channel, and expiry. Anything not covered by a rule pages the human; the eight-minute SLA in §12.5 is only promised for covered rules. Reviewing and re-signing this file is a weekly checklist item, and every automatic firing is graded like any other signal.

### 7.5 Prompt and model evaluation

Maintain a golden set of labeled items and episodes:

- player resolution;
- claim type;
- evidence class;
- channel routing;
- roster-direction classification;
- duplicate/episode assignment;
- contradiction detection;
- exact evidence extraction.

Release a prompt/model change only if it passes defined precision and recall thresholds. A compelling slate memo is not evidence that the extraction model is reliable.

### 7.6 Prompt-injection controls

1. Delimit source text as data.
2. State explicitly that source text may contain malicious instructions.
3. Give extraction models no external tools.
4. Strip or isolate embedded markup and hidden text.
5. Validate output against a strict schema.
6. Reject attempts to emit secrets, tool calls, or new instructions.
7. Keep credentials and configuration outside prompts.
8. Record injection flags for source review.

---

## 8. Tech Stack and Data Model

### 8.1 Recommended stack

- **Language:** Python 3.12+
- **Tabular work:** Polars or pandas; standardize on one for core pipelines
- **Schemas:** Pydantic
- **HTTP:** `httpx` with retries and timeouts
- **Bayesian modeling:** PyMC + ArviZ, or CmdStanPy if preferred
- **Baseline models:** scikit-learn or statsmodels
- **Optimization:** Phase 0 `pydfs-lineup-optimizer` adapter; long-term OR-Tools CP-SAT/MathOpt adapter
- **Operational store:** SQLite in Phase 0; Postgres by the fast-lane phase
- **Analytical snapshots:** Parquet + DuckDB
- **Text/audio:** `faster-whisper` where source rights permit
- **Entity matching:** RapidFuzz plus deterministic aliases
- **Scheduling:** cron/APScheduler initially; persistent worker for Sunday
- **API/UI:** FastAPI for local endpoints if needed; MCP for conversational access
- **Testing:** pytest, property-based tests for roster rules, golden-file CSV tests
- **Quality:** ruff, mypy, pre-commit
- **Observability:** structured JSON logs, run metrics, source-lag alerts, model-cost dashboard

### 8.2 Core tables

```text
players
player_aliases
external_player_ids
teams
games
slates
contests
contest_payouts
salaries
projection_snapshots
ownership_baselines
actual_ownership
odds_snapshots
weather_snapshots
source_policies
sources
source_items
content_tombstones
claims
narrative_episodes
episode_items
signal_types
signal_instances
signal_effect_posteriors
source_claim_scores
player_distributions
dependence_parameters
simulation_runs
field_lineups
candidate_lineups
portfolio_lineups
results
decision_snapshots
decision_log
model_runs
prompt_versions
evaluation_results
```

### 8.3 Provenance rule

Every applied adjustment must return:

```text
adjusted field
baseline value
adjusted value
posterior/scenario
signal instance IDs
narrative episode IDs
evidence item IDs
model/rule version
decision timestamp
```

The “two joins” aspiration from v0.1 is useful but not always realistic after proper normalization. The actual requirement is deterministic traceability through a stable lineage view or API.

### 8.4 Snapshot manifest

Every decision snapshot should include hashes of:

- salary file;
- projection/ownership files;
- market/weather snapshots;
- signal feature matrix;
- model parameters;
- optimizer request;
- generated lineup file.

This makes every lineup auditable and replayable.

---

## 9. Build Roadmap

### 9.0 Calendar anchor and Phase −1 (v0.3 addition)

This revision is dated September 1, 2026; the NFL season opens within two weeks. The phase numbering below is unchanged, but two consequences are now explicit:

1. **Season one is an instrumentation season by arithmetic, not by choice.** Phase 2's ownership model arrives mid-season at best; Section 12.2.7 already concedes week-one predictions are prior-driven. The season's primary output is a clean, point-in-time labeled dataset and a graded source ledger; profitable deployment is season two's job. Any in-season profit is upside, not the success criterion.
2. **Phase −1 starts immediately, before Phase 0, with zero modeling:** from Week 1, capture and freeze (a) pre-lock snapshots of purchased projections and ownership at fixed times (e.g., Sat 6 p.m., Sun 9:00/11:00 a.m. ET), (b) salary CSVs, (c) odds and weather at the same timestamps, (d) probe-contest entries per §4.3 and their post-settlement standings exports, and (e) raw saved copies of the week's key news/beat items with observed-at times. A folder of hashed, timestamped files is sufficient; the database can ingest them retroactively. Every week this doesn't run destroys irreplaceable training data — it is the single highest-ROI activity in this document and requires roughly an hour a week.

Also filed under Phase −1: submit the Reddit API access request now (approval queues run weeks and the outcome is uncertain), and confirm which purchased sim/optimizer products, if any, accept user-supplied projections and ownership (per §2.2).

### Phase 0: Data contracts and valid lineups, Weeks 1–2

**Deliverables**

- database schema and migrations;
- DK/FD salary parsers;
- player crosswalk with confidence and manual overrides;
- projection and ownership snapshot ingestion;
- actual ownership/result ingestion for at least one representative contest;
- optimizer adapter;
- valid upload CSV;
- decision snapshot manifest;
- replay of one historical slate using only pre-lock files.

**Acceptance tests**

- generated lineup uploads successfully to a free or low-stakes contest;
- all active players resolve with no low-confidence silent matches;
- replay output is byte-for-byte stable given the same snapshot;
- roster, salary, team, and site rules pass property tests.

### Phase 1: Quant floor and contest context, Weeks 3–4

**Deliverables**

- second projection source;
- equal-weight blend;
- odds and point-in-time weather;
- contest archetype and payout schema;
- purchased simulation integration or simulation-result import;
- initial dashboard;
- baseline evaluation report.

**Acceptance tests**

- projection and ownership inputs are timestamped and versioned;
- no backtest uses post-lock updates;
- lineup report includes expected payout/ROI or is explicitly marked “heuristic only”;
- purchased baseline error is measured by position and week.

### Phase 2: Narrative ownership MVP, Weeks 5–8

**Scope intentionally narrow:** Families 3 and 8 first.

**Deliverables**

- source policies and approved collectors;
- Stage 1 structured extraction;
- narrative episode clustering;
- deterministic heat features;
- first logit-offset ownership model;
- contest-specific actual ownership labels;
- posterior ownership scenarios;
- prequential evaluation against purchased ownership;
- signal/evidence audit view.

**Acceptance tests**

- every ownership adjustment has episode and evidence provenance;
- duplicate copies do not increase statistical event count;
- held-out-week ownership MAE/log score is reported against the vendor baseline;
- the system falls back to baseline when the model does not add value;
- unvalidated signals are capped according to status policy.

### Phase 3: Mean-channel fast path and source learning, Weeks 9–12

**Deliverables**

- Sunday priority queue and warm worker;
- curated beat/official source polling;
- source claim-type credibility ledger;
- Families 1 and 6;
- incremental player/game recomputation;
- MCP server;
- late-swap MVP;
- dependence-channel schema and first latent-factor adjustments.

**Acceptance tests**

- critical item to refreshed lineup within eight minutes at p95 during load test;
- official inactive update can bypass LLM extraction safely;
- source scores are claim-type-specific and time-decayed;
- large adjustments require approved rules or explicit human confirmation.

### Phase 4: Validation and shadow simulation, Rest of season

**Deliverables**

- hierarchical signal-effect models;
- ROPE/status automation;
- matched-control diagnostics;
- shadow field/outcome simulator;
- duplication calibration;
- bankroll and contest-selection reporting;
- season retrospective.

**Acceptance tests**

- all signal claims are based on prospective observations;
- posterior predictive checks and held-out-week results are published;
- internal simulation reproduces selected historical field properties before any live reliance;
- season report identifies positive, null, harmful, and unresolved signals separately.

### Phase 5: Off-season research

- multi-season refit;
- decide whether Family 2 has enough evidence for mean-channel use;
- evaluate whether internal simulation can replace purchased simulation;
- simplify or retire low-value signal types;
- run blinded prompt/model comparisons;
- only then consider another sport.

---

## 10. Budget and Cost Controls

### 10.1 Planning model

Do not use a single monthly estimate until token volume is measured. Track:

```text
items collected per source
items retained after dedupe
input tokens per extraction
output tokens per extraction
episode synthesis calls
Sunday synchronous calls
weekly deep-review calls
transcription minutes
```

Monthly model cost:

```text
input_MTok × input_rate
+ output_MTok × output_rate
+ transcription cost
+ web/tool cost
```

Current Anthropic model and batch rates should be loaded from official documentation or configuration, not copied permanently into code. As of this revision, batch processing is priced at half the standard token rate, and current model families differ materially in cost. Token counts should be measured against the exact chosen model because tokenization can change between generations.

### 10.2 Illustrative budget bands

| Line item | Lean research | Standard in-season | Expanded research |
|---|---:|---:|---:|
| Projection/ownership subscriptions | 1 source (~$50) | 2 sources (~$100–130) | 2–3 plus sims ($200+) |
| Odds and market data | free/low tier ($0) | paid modest tier (~$25) | historical/props-heavy (~$50–100) |
| Social/news data | manual/authorized free ($0) | approved APIs or low-cost providers (~$25–50) | licensed feeds ($100+) |
| Model API | strict cap (~$25) | measured batch + fast path (~$60–120) | broader transcripts and research ($250+) |
| Probe-contest entries (data acquisition, §4.3) | ~$20 | ~$40–60 | ~$100 |
| Hosting | local ($0) | small VPS (~$6–12) | VPS + storage/monitoring (~$25) |
| Contest/simulation tools | none or existing sub ($0) | purchased sim access (bundled–$50) | multiple comparison tools ($100+) |
| **Indicative monthly total** | **~$95–120** | **~$275–400** | **$800+** |

Dollar figures are illustrative 2026 planning bands, not quotes — v0.2's rule stands that model costs are measured, not assumed, and the token-volume tracking in §10.1 supersedes these bands as soon as real numbers exist. The target is not a predetermined dollar total. The target is positive expected decision value after subscriptions, entry fees, and time.

### 10.3 Cost guardrails

- batch by default outside the fast lane;
- deduplicate before LLM calls;
- cache stable player/team context;
- cap deep-review calls by count;
- use the cheapest model that passes the golden-set threshold;
- record cost per useful classified episode, not cost per raw item;
- monthly source-value review;
- automatic disable of high-cost collectors with low unique-signal yield.

---

## 11. Risks and Failure Modes

1. **The edge may not exist.** Narrative-adjusted ownership may fail to beat purchased ownership. The system must revert cleanly to baseline.
2. **Causal overclaiming.** Observational narrative effects are confounded by recent performance, salary, news, and projection movement. Call them incremental predictive effects unless identification is credible.
3. **Tiny effective sample sizes.** Reposts, repeated players, and shared media events make raw `n` misleading. Use event-level clustering and hierarchical models.
4. **Look-ahead leakage.** Final injury, weather, projection, and line data can contaminate backtests unless snapshots are point-in-time.
5. **Contest heterogeneity.** Ownership and narrative propagation differ across single-entry, small-field, and mass-multi-entry contests.
6. **Simulation false precision.** A sophisticated simulator can be wrong in ways that are harder to see than a simple heuristic.
7. **Source rights and retention.** Platform terms, deletion obligations, and commercial-use rules can invalidate a collector or stored archive.
8. **Prompt injection and contaminated text.** Social content is adversarial input by default.
9. **LLM hallucination.** No generated claim may enter the registry without evidence references.
10. **Source feedback loops.** Many sites may repeat one origin, creating false corroboration and overstated reach.
11. **Stale optimizer dependency.** A generic package may break when site CSV formats or rules change. Keep adapters and tests.
12. **Operational latency.** A technically correct batch pipeline can still miss the only valuable eight-minute window.
13. **Ownership label quality.** A single contest is not “the field.” Select and preserve representative cohorts.
14. **Showdown complexity.** Captain ownership, flex ownership, duplication, and lineup structure require separate models.
15. **Bankroll and contest-selection risk.** Even a positive model can lose money through variance, poor entry sizing, or bad contests.
16. **Human override drift.** Manual adjustments can become unlogged narrative betting. Every override requires a reason and later grading.
17. **This remains gambling.** Track deposits, entries, fees, returns, drawdown, and predefined stop rules from day one.

---

## 12. Direct Answers to the Original Reviewer Questions

### 12.1 Question 1: Is mean/variance/ownership the right decomposition? Should correlation be added now?

**Answer: add dependence/correlation now as a first-class schema channel, and add availability as a gate rather than another channel.**

Variance describes one player’s marginal outcome distribution. Correlation describes joint outcomes and cannot be reconstructed from marginal variances. It matters for:

- quarterback/pass-catcher stacks;
- bring-backs and game-script effects;
- running back versus opposing passing-game exposure;
- quarterback changes that redistribute targets;
- weather that compresses an entire game environment;
- late-swap portfolio risk;
- duplicate lineup and payout simulation.

Do not wait to add the field to the registry. Otherwise early signals will be stored in a way that loses dependence information.

Implementation now:

```json
{
  "channel_effects": {
    "availability": null,
    "mean": null,
    "shape": null,
    "dependence": {
      "latent_factor": "team_pass_volume",
      "direction": -1,
      "magnitude_prior": 0.10
    },
    "ownership": null
  }
}
```

Implementation later:

- adjust latent game/team factors rather than arbitrary pairwise correlations;
- let those factors produce pairwise covariance changes in simulation;
- validate by held-out joint outcomes and lineup-level score distributions.

**Phase decision:** schema in Phase 0; first operational dependence effects in Phase 3; full calibration in Phase 4.

---

### 12.2 Question 2: Concrete functional form for narrative heat to ownership delta

#### 12.2.1 Modeling target

For player `i`, slate `s`, contest cohort `c`, and role `r`:

- `N_scr` = observed lineups in the cohort;
- `Y_iscr` = lineups containing the player in that role;
- `p0_iscr` = purchased baseline ownership;
- `mu_iscr` = Narrative Alpha’s latent actual ownership probability.

Use separate models or hierarchical effects for:

- DraftKings classic;
- FanDuel classic;
- DraftKings showdown flex;
- DraftKings showdown captain;
- FanDuel single-game roles;
- contest archetype.

#### 12.2.2 Episode-level heat construction

First cluster raw mentions into narrative episodes. For player `i` and episode `e` observed at decision time `t`:

\[
 h_{ie}(t) = d_{ie}\;q_e\;s_e\;n_e\;o_e\;\log(1 + R_e)\;
 \exp\left(-\ln(2)\frac{a_e(t)}{\tau_{k(e)}}\right)
\]

Where:

- \(d_{ie}\in[-1,1]\): direction toward rostering the player, not generic sentiment;
- \(q_e\in[0,1]\): evidence/source quality weight;
- \(s_e\in[0,1]\): specificity and actionability;
- \(n_e\in[0,1]\): novelty relative to information already in the baseline;
- \(o_e\in[0,1]\): audience-independence factor that discounts overlapping sources;
- \(R_e\): reach proxy, using unique audience/source measures rather than raw post count;
- \(a_e(t)\): episode age in hours;
- \(\tau_{k(e)}\): source-class half-life.

**Zero-gate caution (v0.3):** the product form means any single factor at zero erases the episode entirely. That is intended for novelty (a story fully reflected in the baseline should contribute nothing) but not for the soft judgment factors. Floor \(q_e\), \(s_e\), and \(o_e\) at a small positive value (e.g., map model scores from \([0,1]\) to \([0.15, 1]\)) so an extraction model's harsh judgment on one dimension cannot silently delete a large, real episode; let \(d_{ie}\) and the novelty term retain true zeros.

Create a low-dimensional feature vector, standardized within slate and source class using only pre-lock information:

```text
H_signed          sum of signed episode heat
H_absolute        sum of absolute heat
H_mainstream      heat from national/mainstream sources
H_dfs             heat from DFS analysts/tools
H_team_fan        heat from team communities
H_velocity_6h     change in heat over six hours
H_acceleration    change in velocity
H_consensus       abs(signed heat) / absolute heat
H_source_entropy  diversity of independent source classes
H_novelty_share   share of heat not already reflected in baseline updates
```

Winsorize standardized features, for example at ±4, before modeling.

#### 12.2.3 Recommended functional form

Use a hierarchical beta-binomial logit-offset model:

\[
Y_{iscr} \sim \text{BetaBinomial}(N_{scr}, \mu_{iscr}, \phi_c)
\]

\[
\text{logit}(\mu_{iscr}) =
\text{logit}(p^0_{iscr})
+ \alpha_{s,c,r}
+ g_{c,r}(\text{logit}(p^0_{iscr}))
+ A_{c,r}\tanh\left(\frac{\eta_{iscr}}{A_{c,r}}\right)
\]

\[
\eta_{iscr} =
\beta^\top H_{is}
+ \gamma^\top Z_{iscr}
+ H_{is}^\top\Gamma Z_{iscr}
+ u_{source\_mix}
+ u_{week}
\]

Where:

- the purchased ownership is an **offset**;
- \(g\) is a low-flexibility calibration function for systematic vendor residuals;
- \(H\) contains the heat features above;
- \(Z\) contains context features;
- `tanh` creates saturation so viral volume cannot create an unbounded delta;
- \(A_{c,r}\) sets the maximum logit-scale narrative effect by contest/role;
- the beta-binomial absorbs more variability than an independent binomial model.

Context features should be limited and pre-specified:

```text
baseline ownership
salary rank and value rank
position and flex scarcity
slate size
team implied total
recent projection change
recent baseline-ownership change
late-news indicator
player popularity prior
contest archetype
site
showdown role
number and quality of close alternatives
```

The “number and quality of close alternatives” is important. The same story creates a larger ownership shift when there are few substitutes in the salary/position band.

#### 12.2.4 First-season simplification

Do not begin with the full interaction model. The first deployable model should be:

\[
\text{logit}(\mu_i)=\text{logit}(p^0_i)
+ A\tanh\left(\frac{\beta_1 H_{signed,i}+\beta_2 H_{dfs,i}+\beta_3 H_{velocity,i}}{A}\right)
\]

Use one global slope plus contest/role intercept calibration. Add source-class slopes and interactions only when held-out performance supports them.

Suggested skeptical priors on standardized features:

```text
beta_signed     HalfNormal(0.20) because direction is encoded in H_signed
beta_dfs        Normal(0, 0.15)
beta_velocity   Normal(0, 0.10)
interaction     Normal(0, 0.05)
random-effect SD HalfNormal(0.10)
```

These are engineering priors on logit scale, not claimed truths. They should be checked with prior-predictive simulations.

#### 12.2.5 Probability-space delta and caps

For each posterior draw:

\[
\Delta p_{iscr} =
\sigma\left(\text{logit}(p^0_{iscr})+\delta_{iscr}\right)-p^0_{iscr}
\]

Then apply governance caps in probability space:

- UNVALIDATED: display-only or 25% of modeled effect, maximum ±2 percentage points in classic;
- TESTING: 50% of modeled effect, maximum ±5 points in classic;
- PROVISIONAL/VALIDATED: full posterior scenario, maximum initially ±10 points in classic;
- showdown: separate, wider caps because ownership is structurally more concentrated.

Caps are safety rails, not evidence. Re-estimate them from historical residuals.

#### 12.2.6 Roster-total calibration

Ownership probabilities cannot drift independently because a lineup has a fixed number of roster slots.

After player-level prediction, solve for slate/position offsets so predicted ownership sums match expected roster totals:

\[
\sum_i \sigma(\text{logit}(\hat p_i)+\kappa_{position(i)})
= E[\text{slots at position}]
\]

Use iterative proportional fitting or a constrained calibration step. For showdown, captain and flex totals are calibrated separately.

#### 12.2.7 Fitting with only approximately 18 weeks

The premise “only 18 observations” is too pessimistic but the data are still dependent. There are many player-slate-contest rows each week, yet effective information is limited by shared slates, repeated players, and common stories.

Use:

1. the purchased baseline as an offset, so the model estimates only residual error;
2. a small feature set;
3. strong priors and partial pooling across site, role, and contest type;
4. expanding-window weekly updates;
5. leave-one-week-out or forward-chaining validation, never random row splits;
6. one or a few representative contest cohorts, not an uncontrolled mixture;
7. posterior predictive checks;
8. a fallback to the purchased baseline when out-of-week performance does not improve.

Week 1 should be prior-driven. The first season is primarily an instrumentation and calibration season unless historical point-in-time ownership data can be acquired.

#### 12.2.8 Evaluation

Compare Narrative Alpha against the untouched vendor baseline on:

- mean absolute ownership error in percentage points;
- log score or beta-binomial predictive log density;
- Brier score;
- calibration slope/intercept;
- rank correlation among the highest-owned players;
- top-chalk identification;
- directional accuracy of material deltas;
- lineup-level expected ROI and duplication improvement in a fixed simulator.

The model ships only when it improves held-out prediction or decision utility. An interesting coefficient is not enough.

#### 12.2.9 Operational output

Return distributions, not one adjustment:

```json
{
  "baseline_ownership": 0.118,
  "ownership_p10": 0.109,
  "ownership_p50": 0.137,
  "ownership_p90": 0.168,
  "delta_p50": 0.019,
  "prob_delta_positive": 0.86,
  "status_multiplier": 0.50,
  "applied_ownership": 0.128,
  "calibrated_to_roster_totals": true
}
```

This allows the simulator to propagate uncertainty rather than pretend the delta is known.

---

### 12.3 Question 3: Actual fan-sentiment versus projection-divergence computation

**Answer: use claim-dimension residuals, not raw sentiment. Normalize within each community and shrink heavily.**

#### Step 1: extract evidence-weighted claims

For each player mention, classify:

```text
claim_dimension      role | usage | health | separation/efficiency | coaching_trust | matchup | generic_sentiment
claim_direction      -1 to +1
ownership_direction  -1 to +1
evidence_basis       film | play_by_play | quote | stats | firsthand_report | vibe | joke
specificity
falsifiability
unique_author
```

Generic sentiment, jokes, and unsupported hype should have little or no mean-channel weight.

#### Step 2: deduplicate and author-cap

- cluster copied claims;
- count one author once per episode;
- cap prolific posters;
- discount replies that merely agree;
- track unique authors and source diversity.

#### Step 3: normalize each community

For subreddit/community `g`, dimension `d`, player `i`, week `w`:

1. compute weighted claim score per active unique author;
2. subtract the community’s rolling dimension-specific center;
3. divide by robust rolling scale such as MAD;
4. subtract a team-week mood component so a bad loss does not make every player negative;
5. shrink toward zero based on unique-author count and historical reliability.

A conceptual form:

\[
F_{igwd} = \lambda_{igwd}
\frac{\bar x_{igwd}-m_{gd}-m_{gw}}{1.4826\,MAD_{gd}+\epsilon}
\]

Where \(\lambda\) is an empirical-Bayes reliability factor that approaches zero with little independent evidence.

#### Step 4: compare like with like

Do not compare “fans feel good” directly with a fantasy-point projection. Map both to the same dimension.

Examples:

- fan role/usage claim versus vendor snap, route, touch, or target-share change;
- fan health claim versus availability and conditional-role change;
- fan performance claim versus market prop or projection change;
- fan ownership buzz versus vendor ownership change.

Define standardized quantitative response `Q` for the corresponding dimension, then:

\[
D_{igwd} = F_{igwd} - Q_{iwd}
\]

Aggregate dimensions with pre-specified weights and preserve the vector. A positive role divergence and negative health divergence should not be collapsed blindly.

#### Step 5: use divergence as a flag first

The first-season output should be:

```text
fan_divergence_direction
fan_divergence_strength
independent_author_count
evidence_basis_mix
community_reliability
projection_response
```

Do not convert it to fantasy points until prospective validation shows incremental value. Ownership use can begin with conservative caps because visible fan narratives plausibly affect roster behavior even when they do not predict performance.

---

### 12.4 Question 4: Honest validation of rare signals with tiny sample sizes

#### 12.4.1 First fix the unit of analysis

The smallest valid observation is a **signal episode**, not a post, comment, source mention, player game, or model extraction.

If one contract-incentive story is repeated by 40 outlets, the sample size for that signal is one episode. Reach may be high, but inferential `n` is not 40.

Compute effective sample size from episode weights when needed:

\[
n_{eff}=\frac{(\sum_e w_e)^2}{\sum_e w_e^2}
\]

Also model repeated players, teams, and weeks explicitly.

#### 12.4.2 Avoid self-inflicted tiny cells

Do not estimate an isolated coefficient for every label such as:

```text
revenge_game_wr
revenge_game_rb
birthday_qb
500th_catch_milestone_te
```

Those labels are useful for search and explanation, but the statistical model should borrow through mechanism features:

```text
narrative family
source reach
source mix
recency
novelty
positive/negative direction
career salience
mainstream penetration
contest type
baseline ownership
```

A rare subtype inherits the family effect. It gets its own deviation only after the data support one.

#### 12.4.3 Channel-specific validation targets

Validate against the mechanism’s nearest measurable target.

| Signal channel | Primary target | Secondary target |
|---|---|---|
| Availability | active/full-role outcome | fantasy points |
| Mean via usage | snap share, route share, carries, targets | fantasy points residual |
| Shape | distributional log score, tail calibration, CRPS | threshold exceedance |
| Dependence | residual co-movement or latent-factor fit | lineup score calibration |
| Ownership | actual ownership residual versus baseline | lineup duplication/ROI |

A usage rumor should not be judged solely by whether the player happened to score a touchdown.

#### 12.4.4 Hierarchical model

For a continuous standardized residual `r_e` from episode `e`:

\[
r_e \sim t_\nu\left(
\theta_{t[e]}
+ x_e^\top\gamma
+ u_{team[e]}
+ u_{player[e]}
+ u_{week[e]},
\sigma_{channel}
\right)
\]

Signal-type effects partially pool within mechanism family:

\[
\theta_t \sim N\left(\mu_{f[t]} + z_t^\top\lambda,\tau_{f[t]}^2\right)
\]

\[
\mu_f \sim N(0,s_f^2), \qquad
\tau_f \sim \text{HalfNormal}(h_f)
\]

Use the appropriate likelihood by target:

- beta or logistic-normal for snap/route shares;
- beta-binomial for ownership counts;
- Bernoulli/logistic for availability;
- robust Student-t for standardized continuous residuals;
- multivariate/latent-factor model for dependence.

A single universal effect model is less important than a shared hierarchy and consistent decision policy.

#### 12.4.5 Priors

Use skeptical, mechanism-informed priors.

Examples in standardized effect units:

- generic narrative mean effect: `Normal(0, 0.05)` or tighter;
- verified usage/availability report: wider prior, such as `Normal(0, 0.25)`;
- narrative ownership family: `Normal(0, 0.20)` on logit residual;
- subtype deviation around family: small hierarchical SD;
- many exploratory interactions: regularized horseshoe or strong normal shrinkage.

Prior-predictive checks should confirm that the model rarely permits implausible adjustments before data.

#### 12.4.6 Confounding and matched controls

These data are observational. For each signal episode, construct matched or weighted controls using pre-signal variables:

```text
position
salary and value rank
baseline projection
baseline ownership
recent fantasy production
recent usage
injury status
team total and spread
slate size
primetime/status exposure
team and player popularity
```

Estimate the residual effect after those controls. Call it incremental predictive association unless the design supports a causal statement.

#### 12.4.7 Multiplicity

Many tested signals guarantee false discoveries under isolated p-values.

Use:

- one joint hierarchical model;
- partial pooling by mechanism family;
- shrinkage on exploratory features;
- prospective registration;
- quarantine of retrospective discoveries;
- held-out-week predictive scoring.

Do not declare a signal real because one posterior interval excludes zero after trying dozens of labels.

#### 12.4.8 Practical ROPEs

Define a region of practical equivalence by channel. Initial governance values could be:

```text
mean: effect smaller than max(0.4 fantasy points, 1.5% of baseline)
usage: less than 3 percentage points of snap/route share
ownership classic: less than 1 percentage point at a representative baseline
ownership showdown: less than 2 percentage points
shape: less than 5% change in predictive SD or tail probability
correlation: absolute change below 0.03
```

These should be adjusted after decision-value analysis.

#### 12.4.9 Status decisions without eternal agnosticism

Use posterior probabilities and predictive utility:

- **PROVISIONAL:** `P(effect beyond ROPE in expected direction) >= 0.80` and no held-out predictive harm.
- **VALIDATED:** probability at least 0.95 plus positive prequential/held-out scoring across independent blocks.
- **NULL-SUPPORTED:** `P(effect inside ROPE) >= 0.80`.
- **INCONCLUSIVE/TESTING:** posterior still broad.
- **RETIRED:** expected value of learning or using the signal is low, or predictive harm persists.

Do not require an arbitrary 20 observations. Five precise, independent, mechanism-consistent episodes can be more informative than 40 noisy, correlated episodes.

To avoid doing nothing forever, use a posterior-discounted deployable effect:

\[
\theta_{deploy} = E[\theta\mid D]\times
\max\left(0,\;2P(\text{expected sign}\mid D)-1\right)
\]

Then multiply by the status cap. This smoothly moves from near zero under uncertainty toward the posterior mean as sign confidence grows.

An even better implementation propagates posterior draws directly through contest simulation and selects a robust portfolio. Uncertainty then reduces aggressive exposure naturally instead of being hidden inside one coefficient.

#### 12.4.10 Validation cadence

Weekly:

1. freeze pre-lock predictions and evidence;
2. ingest outcomes and actual ownership;
3. grade nearest-mechanism targets;
4. update hierarchical posteriors;
5. run posterior predictive checks;
6. score the prior week as a true forward prediction;
7. update status and caps;
8. publish changes with reasons.

Season end:

- distinguish null-supported from unresolved;
- report family-level and subtype-level posteriors;
- show sensitivity to priors and matched-control specifications;
- assess expected decision value, not only effect size;
- merge rare labels that the data cannot distinguish.

**Bottom line:** hierarchical partial pooling is necessary but not sufficient. The full answer is episode-level units, mechanism-based features, skeptical priors, nearest-target validation, confounder adjustment, prospective scoring, ROPEs, and decision-theoretic deployment.

---

### 12.5 Question 5: Can the Sunday pipeline move from a 10:42 post to an adjusted lineup by 10:50?

**Answer: not with the v0.1 batch-centric path. It requires a dedicated fast lane.**

Minimal path:

```text
T+00–30 sec   curated collector ingests item
T+30–60 sec   deterministic source check, player resolution, dedupe
T+60–120 sec  strict low-latency claim extraction
T+120–180 sec corroboration check and permission rule
T+180–240 sec recompute affected player/game distributions and ownership
T+240–360 sec incremental simulations or scenario scoring
T+360–450 sec re-optimize affected lineups
T+450–480 sec alert with change, evidence, and upload artifact
```

Requirements:

- warm worker, not serverless cold start;
- curated source list, not broad web search;
- prompt caching;
- priority queue by source credibility, player ownership, and lock time;
- deterministic handling of official inactive lists;
- cached team/player context;
- incremental recomputation;
- hard timeouts and baseline fallback;
- human confirmation only where it does not make the SLA impossible.

The batch API should never sit in the critical path.

---

### 12.6 Question 6: Attack the buy-versus-build table, especially contest sims

1. **Buy projections:** correct.
2. **Buy one ownership baseline and build a residual layer:** correct, but actual ownership ingestion is equally important.
3. **Blend two sources automatically:** reasonable initially, but do not assume two always beat one. Evaluate point-in-time.
4. **Use `pydfs-lineup-optimizer` as the long-term solver:** incorrect. Use it as a Phase 0 adapter only.
5. **Defer contest simulation:** incorrect. Buy or import simulation output early. Build internal simulation later in shadow mode.
6. **Manual salary CSVs:** correct and should remain the fallback.
7. **Build a custom projection system:** still incorrect for this product.
8. **Build point-in-time replay:** missing and mandatory.
9. **Build contest selection/bankroll tracking:** missing and should begin early.
10. **Buy broad social firehose access:** probably premature. Start curated and measure unique signal yield.

---

### 12.7 Question 7: What is missing?

The most important missing components were:

1. **Point-in-time replay and data lineage.** Without it, validation is not trustworthy.
2. **Actual ownership by contest cohort.** A narrative ownership model cannot be fit against vendor projections alone.
3. **Dependence/correlation.** Necessary for stacks and simulation.
4. **Narrative episode deduplication and propagation graph.** Raw mention counts are misleading.
5. **Field-information sources.** DFS analysts and lineup-construction ecosystems may move ownership more than team fan communities.
6. **Contest selection, payout curves, and duplication.** Player leverage is not lineup EV.
7. **Uncertainty propagation.** Every signal effect should enter simulations as scenarios/posterior draws.
8. **Prompt/model evals.** LLM components need golden sets and release gates.
9. **Prompt-injection and source-content security.** Social text is untrusted.
10. **Source-specific retention and permissions.** Raw data cannot all be immutable.
11. **Showdown-specific models.** Captain ownership and duplication are structurally different.
12. **Fallback and degraded modes.** The system needs explicit behavior when feeds or models fail.
13. **Human override grading.** Manual judgment must be logged and validated like any other signal.
14. **Mechanism-level validation targets.** Usage reports should be graded on usage before fantasy points.
15. **No-edge shutdown rule.** If the ownership layer does not beat baseline after a defined evaluation window, pause or narrow it rather than rationalize failure.

---

## Appendix A: Tier 1 Extraction Schema v0.2

```json
{
  "item_id": "uuid",
  "source_id": "string",
  "published_at": "iso8601|null",
  "observed_at": "iso8601",
  "content_hash": "sha256",
  "source_policy_id": "string",
  "players": [
    {
      "player_id": "canonical_id|null",
      "name_raw": "as written",
      "resolution_confidence": 0.0
    }
  ],
  "teams": ["team_id"],
  "claim_type": "availability|usage|health|performance_observation|narrative|life_event|environment|team_context|field_propagation|none",
  "claim_dimension": "active_status|snap_share|route_share|touch_share|target_share|role|health|efficiency|mean|tail|dependence|ownership|none",
  "evidence_class": "A|B|C",
  "evidence_basis": "official|direct_quote|beat_report|film_claim|play_by_play|statistics|community_observation|generic_sentiment|joke|unknown",
  "falsifiable": true,
  "claim_direction": -1.0,
  "ownership_direction": -1.0,
  "specificity": 0.0,
  "actionability": 0.0,
  "novelty": "new|corroborating|contradicting|derivative|stale",
  "claim_text": "verbatim bounded extract",
  "extract_start": 0,
  "extract_end": 0,
  "suggested_channels": {
    "availability": 0.0,
    "mean": 0.0,
    "shape": 0.0,
    "dependence": 0.0,
    "ownership": 0.0
  },
  "disconfirming_context": "string|null",
  "ambiguity_flags": ["player_resolution", "sarcasm", "conditional_claim"],
  "prompt_injection_flag": false
}
```

---

## Appendix B: Narrative Ownership Feature Contract

```text
player_id
slate_id
contest_archetype
site
role
as_of
baseline_ownership
baseline_ownership_change_6h
projection_change_6h
salary
value_rank
position_scarcity
alternative_quality_index
H_signed
H_absolute
H_mainstream
H_dfs
H_team_fan
H_velocity_6h
H_acceleration
H_consensus
H_source_entropy
H_novelty_share
unique_episode_count
unique_source_count
unique_author_count
source_overlap_index
model_version
feature_version
```

All features must be computed from records with `observed_at <= as_of`.

---

## Appendix C: Weekly Validation Output

For every signal type and channel, publish:

```text
prior specification
posterior mean and interval
probability positive
probability negative
probability inside ROPE
n raw items
n narrative episodes
n effective
teams/players/weeks represented
held-out predictive score delta
matched-control estimate
status before and after update
live adjustment cap
known confounders
```

Never publish only a win rate.

---

## Appendix D: Weekly Operations Checklist

- [ ] Tuesday: results and actual ownership ingested; pre-lock snapshots verified.
- [ ] Tuesday: nearest-target grading completed; hierarchical update ran.
- [ ] Wednesday: source-policy and collector health reviewed.
- [ ] Wed–Fri: official injury diffs and curated beat alerts monitored.
- [ ] Saturday: salary/projection/ownership files imported and hashed.
- [ ] Saturday: player crosswalk unresolved queue cleared.
- [ ] Saturday: narrative episodes, ownership scenarios, weather, and simulation run.
- [ ] Saturday: slate memo and largest adjustments red-teamed.
- [ ] Sunday pre-lock: fast lane healthy; prompt cache warm; baseline fallback current.
- [ ] Sunday: final lineups exported, validated, and decision snapshot frozen.
- [ ] Sunday in-slate: late-swap state maintained for eligible contests.
- [ ] Monthly: source yield, API cost, model evals, prompt versions, and signal statuses reviewed.
- [ ] Season end: no-edge shutdown test and architecture simplification review.

---

## Appendix E: Current Implementation References to Re-Verify Before Build

These links are implementation references, not permanent assumptions. (v0.3 note: Anthropic documentation now resolves under `docs.claude.com`; the `docs.anthropic.com` paths below generally redirect, but verify at build time.)

- Anthropic docs home / site map: `https://docs.claude.com/en/docs_site_map.md`
- Anthropic model overview: `https://docs.anthropic.com/en/docs/about-claude/models/overview`
- Anthropic Models API: `https://docs.anthropic.com/en/api/models-list`
- Anthropic batch processing: `https://docs.anthropic.com/en/docs/build-with-claude/batch-processing`
- Anthropic prompt caching: `https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching`
- Anthropic strict tool use / structured outputs: `https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/implement-tool-use`
- Reddit Responsible Builder Policy (access is approval-gated; file the request in Phase −1, expect a multi-week queue and possible denial): `https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy`
- Reddit data access/help: `https://support.reddithelp.com/hc/en-us/articles/14945211791892-Developer-Platform-Accessing-Reddit-Data`
- Reddit API retention/rate-limit guidance: `https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki`
- `pydfs-lineup-optimizer` package: `https://pypi.org/project/pydfs-lineup-optimizer/`
- Google OR-Tools: `https://developers.google.com/optimization`
- nflverse: `https://github.com/nflverse`
- Open-Meteo forecast API: `https://open-meteo.com/en/docs`
- The Odds API: `https://the-odds-api.com/liveapi/guides/v4/`
- Stan multilevel modeling overview: `https://mc-stan.org/docs/stan-users-guide/regression.html`
- Vehtari, Gelman, and Gabry on Bayesian predictive evaluation: `https://arxiv.org/abs/1507.04544`

---

*End v0.3.*
