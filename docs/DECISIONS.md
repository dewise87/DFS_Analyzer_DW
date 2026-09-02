# Decision Log

Standing technical decisions. Newest first. Each entry: date, decision, why, revisit-when.

## 2026-09-02 — Slice 17 review outcomes

- **Canonical UTC-Z is enforced by the database, not by a registered SQL function.** The
  delivered migration rebuilt four narrative tables to drop their lexical interval CHECKs and
  routed every comparison through a Python-registered `na_timestamp_after`, on the premise that
  stored timestamps might carry differing offsets. They cannot: every writer goes through the
  canonical chokepoint, and all 3,852 production rows are canonical. The function made every
  write from a bare `sqlite3` connection fail at prepare time. The migration now adds triggers in
  place (no rebuild), the narrative tables refuse non-canonical timestamps at insert, and every
  comparison is lexical. The store must never again depend on a connection-registered function.
- **Ineligibility is per item; ambiguity is narrow; failure is capped.** One tombstoned or
  retention-expired item no longer aborts the whole window — it is listed with a reason and
  skipped. A missing credential (`TypeError` from the SDK) or an undelivered request
  (`APIConnectionError` that is not a timeout) is a definite rejection, so the reservation is
  retryable instead of stranded as `creating` forever; the CLI also refuses to start without a
  key before opening the database. Three `failed` attempts under one prompt/model make an item
  ineligible until reviewed. `na-extract abandon` is the sanctioned exit for a stuck attempt.
- **Review flags are outcomes, not failures.** A flagged item is a terminal success of the
  gate; the run exits 0. Exit 3 means the batch is still processing and the identical command
  resumes it. The injection detector was re-cut for precision after 13 of 28 realistic headlines
  matched (e.g. "Mark Andrews claims he is healthy"): weak nouns like "rules" now require a
  co-occurring steering verb, and bare "claims" never matches. Recall on the attack set is
  unchanged. Output cap is 4096 tokens (12 claims did not fit in 2048). Lease grace is five
  minutes, not thirty, because the leases defend against concurrent workers this project does
  not run.

## 2026-09-02 — Stage 1 extraction boundary and replay contract

- **The model sees only canonical visible headline-plus-summary text, inside a unique delimiter.**
  Current reviewed source policy must allow third-party processing; visible injection markers stop
  before the API. Returned player names are bounded as plausible person names, team references
  must belong to the reviewed NFL lexicon, and every entity/evidence string is checked against that
  same canonical text; one bad span rejects the whole item claim set. The exact authorizing
  `source_policy_id` follows the request, claim, and review flag; text at or beyond its raw TTL and
  any tombstoned item fail closed. Current policy, source enablement, retention, tombstones, and
  source hash are checked again in a short `BEGIN IMMEDIATE` authorization/reservation transaction
  and inside each atomic result-settlement snapshot. That transaction installs scoped fences for the
  relevant policies, source, item, and tombstone, then commits before create; unrelated database
  work remains available and no transaction spans network I/O. The capture policy, current policy
  when present, and every exact policy cited by an extraction attempt constrain retention to their
  minimum TTL. A later policy cannot retroactively extend it. An already accepted result that loses
  authorization is retrieved for accounting but terminally quarantined without storing its text.
- **The native Anthropic batch lane has no tools and uses the exact snapshot model.** Successful
  batch items have no per-item HTTP request ID, so provenance truthfully stores the shared batch
  submission request ID alongside batch ID, custom ID, and message ID; it does not fabricate a
  per-item ID. Prompt text and the transformed strict schema are hashed as one immutable version.
  Per-item reservations are committed before create. After acceptance, the trace is fsynced to the
  sibling `<database>.stage1-receipts/` recovery directory before the accepted IDs and recovery lease
  are committed atomically to SQLite. Startup reconciles a surviving receipt before planning. The
  non-idempotent create POST disables SDK retries and large windows are partitioned below provider
  request-count/byte limits. Timeouts resume the same batch; an ambiguous create stops further fresh
  creates, and neither it nor an accepted result-contract failure is automatically resubmitted.
  Result validity begins when JSONL is received, not when the batch was submitted. The owner renews
  its recovery lease around retrieval and before every terminal item write; losing ownership fences
  a stale worker. Expiry permits atomic takeover, complete lineage capture, and supersession only
  when the displaced run has no other active lease.
- **Player resolution never trusts model metadata.** Stage 1 accepts only names as written. An exact
  canonical name or durable alias can supply one deterministic historical team to the existing
  crosswalk; a missing or ambiguous team uses `UNK`, disables fuzzy acceptance, and creates an
  unresolved queue link on the stored claim. Match method, confidence, and manual-override status
  are preserved on the claim reference.
- **An extraction attempt is separate from its zero-or-more claims.** Successful empty responses and
  security blocks are durable terminal outcomes, while definite transport/schema failures remain
  retryable. Canonical output JSON, claim ordering, request bytes, and hashes make replay a
  stored-result operation, not a second model call. Submission-time rates and request/batch lineage
  are store-immutable, so a resumed batch cannot be redirected or repriced by a later config. A
  transaction-local `settling` state is the only time claims and references may be inserted; after
  success the graph cannot be appended. Valid accepted output that encounters any local settlement
  failure remains submitted and resumable, never newly billable. Recovery records every originating
  or displaced run in immutable `model_run_parents`; `model_runs.parent_run_id` is only the
  one-parent convenience field. Tombstones alone authorize redaction of
  output/evidence/context text; hashes, offsets, taxonomy, and provider/policy lineage remain, and
  audit rows cannot be deleted. Source-item identity, source/hash, timestamps, and provenance are
  likewise immutable; only an exact matching tombstone may clear title, raw bytes, and cleaned text.

## 2026-09-02 — Dated nflverse roster pins and byte archive

- **Roster pins are append-only observations selected by review date.** A season can carry
  multiple `(url, sha256, reviewed_at)` entries, and callers must supply an as-of date. This
  prevents an earlier replay from silently resolving identities through a later roster.
- **A successfully verified roster is content-addressed locally by its full sha256.** Archive
  hits are re-verified and require no network. Bytes that fail the reviewed hash are never
  written; losing an old archive after the rolling URL moves therefore fails closed rather
  than manufacturing a historical roster.
- **Refresh is review assistance, not authority.** The helper downloads the rolling asset and
  reports its hash plus player additions, removals, field changes, and malformed or conflicting
  rows, but it never changes `PINNED_ROSTER_RELEASES`. A maintainer reviews and pastes the
  emitted entry. (Review fix, 2026-09-02:) the helper does archive the downloaded bytes under
  their own sha256 — self-verifying, and not a pin — so the entry it prints is fetchable
  offline once pasted even if upstream overwrites the asset again before the next seed. It
  rejects a future `reviewed_at`, and a same-day re-pin later in the table wins ties.

## 2026-09-02 — Collection run durability (found in first live operation)

- **`na-collect run` commits per source, not once for the whole batch.** A full run takes
  ~40s of HTTP against 104 feeds, and it previously held a single write transaction for that
  entire window. Two consequences, both hit on the first real operator run: a second
  invocation failed outright with "database is locked", and any store error late in the batch
  rolled back every source already collected. Per-source commits release the lock between
  sources, so concurrent runs now interleave and complete rather than one dying.
- **Store errors are isolated per source, like feed errors already were.** Slice 15 isolated
  `CollectionError` but let `sqlite3.Error` escape the loop, which meant a database problem
  discarded the whole batch — the opposite of the requirement. A lock error additionally
  stops the loop rather than continuing, since every remaining source would burn its full
  10s busy timeout against the same lock.
- **Operator-facing errors must name the likely cause.** "database is locked" told the
  operator nothing; it now says another run is probably in progress and that already-collected
  sources were kept. Same class of fix as the future-attestation message: both were found by a
  real person running the tool, not by tests.

## 2026-09-01 — Slice 15 seeding decisions

- **Credential-free feed fetches follow redirects; credentialed ones still must not.** The
  collector and the feed health check both inherited `follow_redirects=False` from the
  odds/weather client, where it is deliberate and correct: that client puts `apiKey` in the
  query string, and following a redirect would hand the key to another host. Feed requests
  carry no credentials, so the rationale does not transfer, and refusing a publisher's 301
  silently dropped that source from collection while the health check reported it dead.
  Confirmed live against the catalog (`yahoo-nfl` answers 301 and now collects 50 items).
  `nflverse.py` already set the same precedent for credential-free public artifacts.
- **`sources` is append-only versioned, keyed by a separate `source_keys` identity table.**
  Migration 0005 made `source_id` the primary key, which cannot represent a changed feed URL
  without overwriting the row and destroying the record of what was actually being polled
  when an item was captured. Every read now takes the latest version at the cutoff, and
  `na-collect run` enumerates by latest-version-then-enabled so disabling a source actually
  disables it.
- **Re-attesting is a new policy version even when the terms are unchanged.** A fresh review
  is new information with its own provenance, so `terms_reviewed_at` participates in change
  detection. Re-seeding an unchanged catalog with the same attestation is a true no-op
  (verified: 104 inserted, then 0).
- **A catalog containing `terms_reviewed_at` anywhere is rejected outright**, checked
  recursively before validation. The attestation must come from the operator on the command
  line; letting a file supply it would forge the review the policy gate exists to require.
- **RSS gives headlines and summaries, not article bodies.** Captured items run ~50–150
  characters of cleaned text. That is enough for availability and usage claims ("put on the
  commissioner exemption list") but not for full beat-writer analysis. Do not let the
  extraction slice assume it is working with full articles.

## 2026-09-01 — Narrative source selection and the X/Twitter question

- **X/Twitter is not collected, and the collector cannot reach it today.** The migration
  only permits `rss_atom` and `official_team_feed`. X killed RSS in 2013, and in February
  2026 it dropped the free tier and moved new developers to pay-per-use at **$0.005 per post
  read**. Scraping and third-party mirrors violate its terms, so §4.6's "prefer licensed or
  clearly permitted access" rules them out. Revisit if the mean-channel timing edge is ever
  shown to be worth the metered cost — X is where beat reporters break news first, and that
  latency is the one thing the RSS tier genuinely cannot replicate.
- **Bluesky is the free alternative but is not a drop-in replacement.** Its API is free with
  no paid tier and a public unauthenticated firehose. Querying it directly confirmed real
  NFL presence (Adam Schefter, Mina Kimes, Jordan Schultz, and per-team Athletic writers),
  but coverage is partial, its actor search is too weak to build a list automatically, and
  unofficial *mirror* accounts republishing X content are present. Mirrors are excluded on
  sight: they launder the same terms problem and can vanish without notice. Any Bluesky
  collector needs a hand-verified handle list.
- **Per-reporter feeds are a category error; per-team outlet feeds are the real unit.**
  Beat reporters publish at outlets and post on X; almost none run a personal feed. The
  catalog therefore carries three feeds per team — the club's official site, ESPN's per-team
  feed, and the team's SB Nation community — plus The Athletic's NFL feed, which is where
  most top beat writers actually publish.
- **The catalog ships without `terms_reviewed_at`, on purpose.** Auto-stamping a review date
  would forge exactly the human judgment the Slice 14 policy gate exists to require. Seeding
  must take the timestamp from the operator, who is attesting they read the terms.

## 2026-09-01 — Slice 14 collector decisions

- **Malformed feed HTML must cost at most its own element, never the article body.** The
  first visible-text extractor tracked hidden regions with a depth counter. Void elements
  (`<br>`, `<img>`) never emit an end tag and unclosed `<p>`/`<li>` are routine in feeds, so
  each one read as an open level that never closed: a single hidden tracking pixel silently
  truncated everything after it. The extractor now keeps the open-element stack. Silent
  truncation is the worst failure mode available in this slice, because the evidence cannot
  be re-fetched later.
- **Item hash uniqueness is source-scoped, deliberately.** Identical text at two outlets is
  two rows sharing a content hash, not one row. Collapsing them here would destroy the reach
  signal rule 1.5.3 depends on, before the clustering slice ever sees it.
- **`observed_at` is always the capture instant and is never backfilled from the item.**
  `published_at` carries the item's own claim about its time. Rule 1.5.2 admissibility rests
  on the first, not the second.
- **A tombstone survives; the text does not.** Retention expiry and platform deletions both
  clear title, raw bytes, and cleaned text while keeping the row and its hash. One tombstone
  per item, so purge and deletion handling are idempotent. Cleared cleaned text means the
  later extraction slice must run inside the retention window — the derived claims are what
  outlive it, per §4.6.
- **No policy is never permission.** Collection and deletion handling both require a current
  reviewed policy; a missing or stale `terms_reviewed_at` refuses. Same shape as Slice 10's
  empty quantile table.

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
