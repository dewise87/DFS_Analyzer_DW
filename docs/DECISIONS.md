# Decision Log

Standing technical decisions. Newest first. Each entry: date, decision, why, revisit-when.

## 2026-09-02 — Slate identity is derived, and a slate is an identity, not an observation

- **The slate key is derived from what the export carries.** DK and FD salary exports have no
  slate id, so `external_slate_id` is `site:season:wNN:type:earliest-kickoff`. Sunday's
  re-download of the same slate therefore resolves to the same key, and a slate whose first game
  moved resolves to a different one — which is correct, because a moved first kickoff is a
  different slate. Revisit if a site ever publishes a stable slate id in the export.
- **Slates, teams, and games are reused by identity; only salaries are versioned.** `slates` has
  `UNIQUE(site, external_slate_id, observed_at)`, so the point-in-time pattern used everywhere
  else would mint a new `slate_id` per capture and split a slate's salaries between them. The
  loader instead inserts each of these once and reuses it whatever order captures are loaded in;
  content that later disagrees (a slate's name, a game's kickoff) is reported as an error, never
  updated in place. Salaries stay strictly insert-only, keyed by `(slate_id, player_id,
  observed_at)`, and every changed salary is reported as a diff.
- **The salary export is the only writer of `teams` and `games`.** Nothing else in the pipeline
  writes either, and `salaries.team_id` is NOT NULL, so a franchise first appears when a slate
  is ingested. Only what the file states is stored — the canonical code as key and name — and a
  later source with real franchise names versions the row rather than being blocked by it. Home
  and away come from the export's own `AWAY@HOME` field (`ParsedSalaryRow.is_home`), never from
  the alphabetical key order; a FanDuel classic export carries no kickoff, so it writes no game
  at all and names the affected matchups instead of inventing one.
- **A team defense is one canonical player per franchise, keyed `dst:<code>`** (shared
  `identity/defense.py`, used by the salary and the projection/ownership loaders). No roster
  carries a defense and every site names it differently ("Green Bay Defense", "New York
  Defense"), so the crosswalk queued every DST row on every slate — 32 by-hand resolutions a
  week and a blocked lineup build. Review fix: a DST/D row resolves deterministically to the
  franchise's defense row, inserted once on first sight with position `DST`.
- **A missing kickoff is refused, not defaulted.** FanDuel classic omits game times, so the
  slate's `starts_at`/`locks_at` cannot be derived and `--starts-at` is required. The same value
  must be used for every re-download, because it is part of the slate key.

## 2026-09-02 — Slices 20 and 21 review outcomes

- **Text similarity creates a Stage 2 link; direction only labels it.** The delivered
  clustering required both claims to carry agreeing (or opposing) non-unknown directions
  before any link existed, so a byte-identical copy of an `unknown`/`neutral` claim was
  `independent` and counted as a second event. Half of the first live corpus has such
  directions, and on production data no derivative, corroborating, or contradicting relation
  ever fired. Now: an opposing link above threshold is `contradicting` (it outranks a near-copy,
  because "will start" versus "will not start" is the point), a near-copy is `derivative` from
  any source — a same-source repost is not a second event — and everything else linked is
  `corroborating`. Stop words and team references are dropped before Jaccard; the fixture
  thresholds are unchanged and remain labelled-set work under a future method version.
- **Episode time is the story's time.** Ordering, the rolling gap, `opened_at`,
  `last_item_at`, velocity, and recency use `min(published_at, observed_at)` when the feed
  carries a publication time; availability is still gated on `observed_at`/`ingested_at`.
  Production items carry five distinct `observed_at` values across 4,879 rows (one per
  collection run), so fetch time made every episode instantaneous and chose origins by claim
  id. An episode also closes 168 hours after it opened, and the build loads only the last 14
  days, because a daily collector never leaves a 72-hour gap.
- **Stage 2 pins the Stage 1 prompt version** (`narrative_episodes.prompt_version_id`, part of
  the episode id). A re-extraction under prompt v2 is a new snapshot, never a rebuild conflict
  with v1. `claim_player_refs` are read as of the cutoff like every other input; the
  per-source family is the family of that source's latest item, so a mid-season
  reclassification cannot make families outnumber sources; dropped team references are
  reported by name so the nickname table can be extended.
- **Novelty needs a material move and is decided once.** The gate zeroed a whole episode on a
  0.1-point baseline tick and, re-decided at t−6h and t−12h, turned a gate flip into a large
  spurious velocity. It now requires `novelty_min_baseline_move` (0.01, hashed into the feature
  version) and the as-of decision is held across the velocity instants. An episode whose origin
  was not yet extracted at an earlier instant did not exist then and contributes nothing,
  instead of aborting the slate build.
- **Stated, not changed:** independence is computed over all sources including derivatives
  (a same-family copy lowers it) — the earlier text saying derivatives do not affect factors
  was wrong; velocity re-evaluates the as-of clustering at earlier instants rather than
  re-clustering; no-episode players carry 0 for consensus, entropy, and novelty share, so any
  model must gate on `unique_episode_count`; a code change to the formula without a
  `formula_version` bump is caught only by a replay conflict. `na-slate list` (Slice 22)
  supplies the slate ids `na-features` requires.

## 2026-09-02 — Deterministic Stage 3 heat and Appendix B boundary

- **The §12.2.2 product is literal, and only soft judgments receive the 0.15 floor.** Direction is
  the mean roster-behavior direction across unique non-derivative items. Per-claim quality is the
  arithmetic mean of configured evidence-class, evidence-basis, and frozen source-family scores;
  item scores are averaged so multiple claims from one item do not manufacture weight. Specificity
  is the corresponding mean of `(specificity + actionability) / 2`. Quality, specificity, and the
  unique-family/unique-source independence proxy are affine-mapped from `[0,1]` to `[0.15,1]`.
  Direction and novelty keep real zeros. Reach includes every unique source, including a derivative;
  derivatives do not refresh factors, event count, decay age, or independent-source entropy
  (they do lower the independence proxy: see the review entry above).
- **An episode's origin fixes its source class and half-life.** Official-team and national-media
  origins are mainstream, fantasy aggregators are DFS, and team communities are team/fan. Age runs
  from the most recent non-derivative item's observation to the evaluation instant. This avoids a
  copied late headline changing both an episode's identity and decay regime. The mappings,
  provisional quality priors, half-lives, floor, six-hour window, and method names are all in
  `config/heat.toml`; its canonical values are hash-bound to an immutable `feature_version`.
- **Novelty is a deliberately coarse placeholder, not a learned attribution claim.** It defaults to
  `1.0`. When the latest eligible ownership snapshot and a snapshot from the same vendor at or
  before the episode opening both exist, a nonzero baseline move aligned with the episode direction
  sets novelty to `0.0`; an absent, unchanged, or opposing baseline leaves it at `1.0`. This binary
  gate is the only inference supportable before labeled timing data exists. Revisit with prospective
  ownership histories; do not add a fractional rule without calibration. Every baseline row used by
  current heat, velocity, or acceleration is retained in feature provenance.
- **Player features use one full point-in-time salary pool as their standardization cohort.** Each
  raw heat channel—including mainstream, DFS, and team/fan channels—is population-z-scored across
  all eligible players on that slate, preserving zero exposure as information, then winsorized at
  ±4. `H_velocity_6h = H(t)-H(t-6h)` and acceleration is the difference between consecutive
  six-hour velocities; both reconstruct the member timeline and decay/novelty state directly, never
  prior feature rows. Consensus is `abs(H_signed)/H_absolute`; source-class entropy is normalized by
  the three configured classes; overlap is `1 - unique families / unique sources`; novelty share is
  actual absolute heat divided by its novelty-one counterfactual.
- **Unavailable Appendix B dimensions stay NULL.** The row grain has no contest cohort and the
  current feed schema captures no author, so `contest_archetype` and author count are unknown. Value
  rank, position scarcity, alternative quality, and `model_version` also await explicit contracts.
  Salary and available six-hour projection/baseline changes are populated from exact pre-cutoff
  rows. Classic slates use the classic baseline role; showdown rows use flex, because the required
  player-level key cannot represent separate flex and captain vectors. Revisit showdown grain before
  a captain ownership model consumes these rows.

## 2026-09-02 — Deterministic Stage 2 episode boundary

- **Stage 2 uses token-set Jaccard before any synthesis model.** Claims are sessionized by
  resolved player (or the one explicit canonical team for unresolved/team-only claims), claim dimension,
  and a configurable 72-hour rolling gap. Within a session, Unicode-normalized case-folded token
  sets are transparent, cheap to replay, and adequate for the first measured version: similarity
  at least 0.35 creates a directional link, and similarity at least 0.80 from a different source
  is derivative. Exact canonical-content hashes still identify copies after retention has purged
  reconstructive text; other text-unavailable claims remain independent and are counted in the
  build report. Stage 1 team references are normalized from accepted names/nicknames to one of the
  32 codes; ambiguous city-only or generic nicknames stay unclustered. Revisit the thresholds or add
  synthesis only against a labeled episode set, under a new `method_version`.
- **A rolling gap defines the broad episode; relation labels define its event evidence.** The first
  stable `(source item observed_at, claim_id)` member is the origin. Same-direction linked text is
  corroborating, near-copy linked text is derivative, opposite-direction linked text is
  contradicting, and an in-window claim without a directional text link is independent. The stored
  `linked_claim_id` makes every propagation decision inspectable. A repeated method/as-of build is
  accepted only when the complete stored graph equals a fresh deterministic rebuild.
- **Reach and event count deliberately diverge.** Reach is the number of unique sources, so a copied
  report can raise it. `n_events` counts unique items carrying origin, independent, or corroborating
  relations and excludes derivatives and contradictions. Source entropy is Shannon entropy over
  unique-item counts by source; velocity is unique items per six hours over the episode span, with
  one six-hour minimum denominator. Recency is measured from `as_of` to the last non-derivative
  item. All features use unique source items rather than claim-row multiplicity.
- **The cutoff is availability-aware.** Both a source item and its stored claim must have
  `observed_at`, `ingested_at`, and validity admitting `as_of`; a retroactive build records its real
  build time separately and cannot masquerade as a prospective artifact. One resolved claim can
  belong to multiple player episodes. Unresolved references use a single explicit team when one is
  present, otherwise a claim-scoped unclustered episode, and are always surfaced in the report.

## 2026-09-02 — Slice 19 review outcomes

- **Evidence offsets are located by the store, not trusted from the model.** The first live
  run (21 items, $0.046) stored zero claims: 16 of 21 items failed evidence validation. The
  raw outputs showed the model's verbatim extracts were right — 33 of 36 spans were in the
  source, 30 with wrong character offsets and 3 differing only in typographic quotes — and
  only 1 span had correct offsets. Counting characters is not something a language model does
  reliably, and the §5.5 contract never depended on it: the verbatim extract is the evidence.
  Validation now locates each extract in the canonical text (one-to-one quote/dash folding so
  lengths hold, nearest occurrence to the model's offset when repeated), stores the located
  offsets and the source's own bytes, and still rejects any extract that is not in the source.
  Player and team references are checked under the same folding. The prompt is unchanged, so
  prompt v1 lineage stands; the repair is a deterministic function of raw output plus source
  text, so replay is unaffected.
- **Player identity is resolved as of settlement, not as of the headline.** The retry
  stored 26 claims and resolved none of their 32 player references — Dak Prescott and George
  Kittle included — because the roster was seeded on September 2 and the items were observed
  September 1, and the crosswalk only sees roster rows observed before the identity's
  instant. A canonical player id is a key, not a predictor, so Stage 1 now resolves names
  against the roster known when the claim settles; the item's `observed_at` still governs
  evidence and every point-in-time column on the claim. Replay is unaffected: claims are
  stored, never re-resolved. The 32 references already queued stay in the unresolved queue.
- **A killed process must not lock its batch.** The review's own smoke run was terminated by
  a tool timeout while polling; its run stayed `running` and held the batch-recovery lease
  for an hour, blocking the resume. `na-extract release --run-id` marks such a run failed and
  drops its leases, and `na-extract review` lists held leases with their owner's status.
- **Scheduled runs are capped per run, and a cap never loses items.** `batch.max_items_per_run`
  in `config/ops.toml` (200 for the bounded first runs) bounds what one unattended run may
  submit, which also bounds the budget-guard estimate: the guard prices the full output
  ceiling, so an uncapped 4,700-item backlog was one week of collection away from being
  refused forever. With a cap, the plan is truncated in `observed_at` order and the next
  window reopens at the first deferred item (`next_window_start` in the step summary); the
  delivered lane advanced the watermark to "now" and would have skipped every deferred item.
- **The indexed name lookup falls back to the full scan when it finds nothing.** The SQL
  prefilter mirrors `normalize_name` except that SQL cannot strip diacritics, so an accented
  canonical name would have stopped resolving. A proper normalized-name column is the right
  fix later; until then an empty prefilter re-runs the unindexed query.
- **The live checkpoint was run under review, not by the implementer**, because the key was
  not in the executing process. Twenty items through the real lane, results below in the
  Slice 19 status note.

## 2026-09-02 — Slice 18 review outcomes

- **The lane does not extract against an empty roster.** With zero `players` rows every
  extracted name would land in the unresolved queue as a by-hand `na-crosswalk resolve`,
  and seeding afterwards does not resolve them retroactively. `na-ops batch` records the
  extract step as skipped with that reason and leaves the watermark alone. The production
  store is in exactly this state today, so the first scheduled run would otherwise have
  created thousands of manual tasks.
- **The extraction window closes after collection, not at batch start.** With one shared
  instant, items collected at `observed_at == started_at` fell outside the exclusive window
  end and waited a whole cadence. The lane now reads the clock again after collection; an
  injected `now` still pins everything to one instant for tests and replays.
- **A moved roster is a recorded failure with the re-pin command in it, and the refresh
  helper can bootstrap without the prior bytes.** The 2026 pin's bytes were never archived
  and upstream overwrote the rolling asset on or before 2026-09-02, so the weekly check
  would have failed closed forever while the helper refused to produce a new entry. The
  helper now accepts `--allow-missing-prior`: it archives the current bytes, prints the
  hash and paste entry, and states that the diff is unavailable rather than inventing one.
  The lane also treats "rolling hash ≠ pin" as a failed step, because a roster the store
  cannot see is a by-hand task the screen must show. Review dates are UTC dates.
- **Reminder notifications are one shell word.** The AppleScript is escaped and
  `shlex`-quoted, so a title or message with an apostrophe cannot break the wrapper.

## 2026-09-02 — Slice 18 operator console

- **`na-ops` is the UI for season one, and the CLI stays the source of truth.** The batch
  lane calls the existing library functions directly — never a subprocess, never a second
  copy of their logic. The one piece that had to move is the enabled-source enumeration and
  per-source isolation loop, which lived inside `collect_cli._run`; it is now
  `collectors.collect_enabled_sources`, and `na-collect run` is a thin renderer over it.
  The queued dashboard and the Phase 3 MCP tools wrap the same calls, so they can only ever
  render functions that already work from the terminal.
- **Every step is isolated and every outcome is recorded, including the ones that did not
  run.** `ops_runs` (migration 0008) is append-only with the same canonical UTC-Z insert
  trigger the narrative tables use, and each step commits its own row before the next step
  starts, so a later rollback cannot take an earlier step's history with it. Purge always
  runs — retention is a legal obligation, not a consequence of a good fetch. Extraction is
  skipped, with a stated reason, only when collection failed *entirely*; a partial feed
  failure still extracts. A `skipped` step is not a failure for the exit code but is shown
  as the latest non-success on the status screen, because an operator who cannot see the
  skip will assume the window was covered. A still-processing provider batch is recorded as
  a failed extract step rather than earning a fourth status word: `na-extract` already has
  exit 3 for that, but at the lane level the fact that matters is identical to a failure —
  the window is not covered, so the watermark must not advance and the screen must not
  imply it did. The recorded text says the next run resumes it without re-billing.
- **The extraction watermark advances only on success.** The next window starts at the
  `window_end` of the last *succeeded* extract step; a failed or budget-refused run leaves
  the watermark where it was, so a bad week is retried rather than silently stepped over.
  With no recorded run the window opens at the earliest retained item, not at "now".
- **The budget guard is all-or-nothing on purpose.** Submitting "what fits" under the
  monthly ceiling would make the covered window a function of the budget, which no later
  replay could reconstruct. Over budget, the lane refuses the whole batch, records a failed
  step, and prints month-to-date spend, the worst-case estimate, and the ceiling. The
  estimate is the plan's worst case (input plus the full output ceiling for every item):
  measured against the real store today that is $40.46 for the 3,852-item backlog, which is
  the number the default $50 budget was chosen against. Month boundaries are calendar
  months in the operator's configured zone, and spend is keyed to `ingested_at` — when the
  attempt was billed — not `observed_at`, which is when the news broke.
- **The API key is never written anywhere the repository or launchd can keep it.** The
  batch wrapper reads it from the login Keychain at run time; no plist contains the string
  `ANTHROPIC` at all, and the reminder jobs neither touch a credential nor open a database.
  Wrappers live under gitignored `data/ops/bin/` and carry a marker line; `schedule
  uninstall` removes a plist only when it parses as ours (matching `Label` and
  `ProgramArguments` pointing at our wrapper path) and a wrapper only when the marker is
  present — anything hand-edited or foreign is reported and left alone.
- **The §9.0 capture times are constants, not configuration.** Sat 6:00 p.m., Sun 9:00
  a.m., Sun 11:00 a.m. Eastern are converted to the operator's local zone against a
  mid-season anchor date, so a DST-transition install cannot shift them by an hour. Only
  the timezone, the batch days, and the batch time are configurable. launchd, unlike cron,
  runs a job missed while the Mac was asleep at the next wake — that is why a sleeping
  laptop delays the Wednesday batch instead of skipping the week, and it is stated in the
  README rather than left as folklore.
- **Status refuses to report a healthy-looking zero.** An empty `players` table renders as
  "NOT SEEDED", not as `0`. The extraction backlog is the real `plan_extraction` result —
  the same policy, retention, and injection gates the lane applies — rather than a row
  count that would flatter the operator; if the plan cannot be built, the screen says the
  backlog is unknown and why. The whole screen renders in under a second against the
  3,852-item store.

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
