# Narrative Alpha

An NFL DFS decision engine for context, field behavior, and uncertainty. DraftKings and FanDuel, classic and showdown slates.

**Status:** Phase −1 (perishable-data capture). Season one is an instrumentation season — the primary output is a clean, point-in-time labeled dataset and a graded source ledger.

The full specification lives in [docs/design/narrative-alpha-design-doc-v0_3.md](docs/design/narrative-alpha-design-doc-v0_3.md). The build plan, work slices, and per-slice prompts live in [docs/WORK_SLICES.md](docs/WORK_SLICES.md). Standing technical decisions are logged in [docs/DECISIONS.md](docs/DECISIONS.md).

## The one-paragraph version

Narrative Alpha buys the commodity layers — projections, baseline ownership, odds, weather — and builds the one layer no vendor sells: a disciplined engine for the information that lives outside the numbers, and for what that information does to the *field's* behavior. A piece of information matters through a channel (availability, mean, shape, dependence, ownership), and the channel matters more than the information.

## Non-negotiables (see design doc §1.5–1.6)

- **Point-in-time everything.** No backtest may use information published after its `decision_at` cutoff.
- **Provenance.** Every changed number resolves to an evidence item or a deterministic rule.
- **Solo-operator budget.** ≤ 2 hours/week of operation in early phases. The boring version of everything ships first.
- **Minimum Lovable Pipeline** is the fallback state: salaries in → crosswalk → valid lineups out; blended purchased projections + baselines attached; snapshots frozen; results ingested weekly; a readable slate memo.

## Repo layout

```
docs/design/        Design documents (v0.3 is current)
docs/WORK_SLICES.md Build plan: slices, prompts, model recommendations
docs/DECISIONS.md   Decision log
src/narrative_alpha/
  snapshots/        Phase −1: capture & freeze perishable pre-lock data
  store/            L1: operational DB, migrations, snapshot manifests, governance
  ingest/           L2: slate/salary/projection/result loaders, collectors
  identity/         L2: player crosswalk (canonical IDs, aliases, overrides)
  quant/            L3: projection blend, distributions, ownership model, dependence
  narrative/        L4: evidence extraction, episode clustering, signal registry
  portfolio/        L5: contest selection, simulation, optimizer adapter, late swap
  interface/        L6: MCP tools, slate memo, dashboard, alerts
  ops/              L6: operator console (na-ops): batch lane, status, launchd
data/               Local data (gitignored except structure docs)
tests/              pytest; property tests for roster rules; golden-file CSV tests
```

## Setup

Requires Python 3.12+. This project uses [uv](https://docs.astral.sh/uv/):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
uv run pytest
```

## Weekly operations

One command runs the week and one screen reports it. Everything below the two commands is
what a person still has to do by hand.

**Install once.** Put the Anthropic API key in the login Keychain (the scheduled jobs read
it at run time; it is never written into a plist, a log, or this repository), then install
the launchd user agents:

```bash
security add-generic-password -s narrative-alpha-anthropic -a "$USER" -w
```

```bash
uv run na-ops schedule install
```

Before installing the schedule, run the lane once by hand with a bounded smoke test and read
the result — the scheduled run will do exactly the same thing unattended:

```bash
uv run na-ops batch --max-items 20
uv run na-ops status
```

The lane does not extract until a roster is seeded (`status` says so): extracting first would
send every name to the manual unresolved queue. The first scheduled run may show a macOS
dialog asking whether `security` may read the Keychain item; choose "Always Allow", or the
extraction step will refuse to submit and say why in `data/logs/`.

That schedules the batch lane on the days and local time in `config/ops.toml` (default
Wed–Fri 09:30, per design-doc §5.3) plus three notification-only reminders at the §9.0
manual capture times — Sat 6:00 p.m., Sun 9:00 a.m., Sun 11:00 a.m. Eastern, converted to
your local zone. Each reminder also appends the exact `na-snapshot` commands to
`data/logs/<job>.log`. `na-ops schedule show` lists what is installed; `na-ops schedule
uninstall` removes only the files `install` wrote and leaves anything else alone. **Unlike
cron, launchd runs a job missed while the Mac was asleep at the next wake**, so a closed
laptop delays the Wednesday batch rather than skipping the week.

**Read the screen any time.**

```bash
uv run na-ops status
```

One page: per-step last success and last failure with age, dead feeds from the last
collection, items collected in the last 7 days, the Stage 1 backlog and what it would cost
to clear, review flags, in-flight attempts, pending accepted-batch receipts, the crosswalk
unresolved queue, whether a roster is seeded at all, this week's snapshot coverage, and
Stage 1 spend month-to-date against `monthly_llm_budget_usd`. `--json` prints the same
data. It answers "did this week run, and what do I need to do by hand" without any other
command.

**Run the lane by hand** when you want it now rather than on Wednesday:

```bash
uv run na-ops batch
```

It collects enabled feeds, purges expired raw text, runs Stage 1 over the window from the
last successful extraction to now (`--window-start` overrides it), and checks the nflverse
refresh in report-only mode. Each step is isolated: a failed step is recorded and the next
safe step still runs — purge always runs, and extraction is skipped with a stated reason
only when collection failed entirely. Every step writes an `ops_runs` row, so `status` can
show history, and nothing is retried silently. It exits nonzero if any step failed.

Each run submits at most `batch.max_items_per_run` fresh items (`--max-items` overrides it);
the rest wait, and the next window reopens at the first deferred item so nothing is skipped.
Raise the cap once a labeled evaluation has passed.

Before submitting anything to Stage 1 the lane prices the batch and compares month-to-date
spend plus that estimate against `monthly_llm_budget_usd`. Over budget, it refuses the
whole batch, records the refusal as a failed step, and prints the numbers. There is no
partial "submit what fits": that would make the covered window a function of the budget,
which no later replay could reconstruct.

### What is still manual

- **Saturday 6:00 p.m. ET** — download DK/FD salaries, purchased projections, and baseline
  ownership; capture each with `na-snapshot capture`, then `na-snapshot fetch` odds and
  weather and `na-snapshot verify` the week. Then load the salary capture and read back the
  slate ids: `na-slate ingest` → `na-slate list` (below).
- **Sunday 9:00 a.m. and 11:00 a.m. ET** — re-capture projections and ownership and
  refresh odds and weather. The 11:00 a.m. capture is irreplaceable: after lock the
  pre-lock state is gone. Re-run `na-slate ingest` on the Sunday salary re-download; it
  versions the salaries rather than overwriting Saturday's.
- **Sunday post-slate** — export contest standings for every probe contest.
- **Whenever `status` says so** — clear the crosswalk unresolved queue
  (`na-crosswalk resolve`), review flagged items (`na-extract review`), and paste a
  reviewed nflverse pin when the refresh check reports a moved roster:
  `na-crosswalk nflverse-refresh --season 2026 --reviewed-at <today>` prints the hash, the
  player diff, and the entry to paste. Add `--allow-missing-prior` when the previous pin's
  bytes were never archived and upstream has overwritten them; the diff is then unavailable
  and the roster file must be reviewed by hand.

Phase −1 rule: **every week the snapshot capture doesn't run destroys irreplaceable
training data.** See Appendix D of the design doc for the full checklist.

## Narrative feed collection

`na-ops batch` runs collection and purge on the weekly cadence. These commands are the
same library calls for one-off use: seeding a reviewed catalog, checking feed health, or
re-running one source after a failure.

Review the tier terms in `config/narrative_sources.toml`, then explicitly attest that
review when seeding. The timestamp is never inferred from the file or current time:

```bash
uv run na-collect seed --catalog config/narrative_sources.toml \
  --terms-reviewed-at 2026-09-01T12:00:00Z --dry-run
uv run na-collect seed --catalog config/narrative_sources.toml \
  --terms-reviewed-at 2026-09-01T12:00:00Z
uv run na-collect seed --catalog config/narrative_sources.toml --check-feeds
```

`na-collect run` attempts every enabled source, reports failures by source, commits per
source, and exits nonzero if any feed failed. `na-collect purge` uses the earliest deadline
implied by the capture policy, the current policy when one exists, and every exact policy
version cited by an extraction attempt. A later, longer policy cannot extend earlier
authorization; an item whose capture policy cannot be reconstructed receives zero retention.

## Structured claim extraction

Stage 1 must run inside the raw-text retention window; `na-ops batch` does that on the
cadence and enforces the monthly budget. Run `na-extract` directly to dry-run a window, to
bound a smoke test, or to work a review queue. Always dry-run first: it renders the plan,
lists ineligible items with reasons, and prices the batch without building an API client or
writing to the database (`--show-prompts` adds every rendered prompt; `--max-items N`
bounds a smoke test):

```bash
uv run na-extract --database data/db/narrative_alpha.sqlite3 \
  --window-start 2026-09-02T00:00:00Z --window-end 2026-09-03T00:00:00Z --dry-run
export ANTHROPIC_API_KEY=...
uv run na-extract --database data/db/narrative_alpha.sqlite3 \
  --window-start 2026-09-02T00:00:00Z --window-end 2026-09-03T00:00:00Z --max-items 20
```

Exit codes: `0` done (review flags are an expected outcome, not a failure); `3` the provider
batch is still processing after `--timeout-seconds` (default one hour; Message Batches can take
up to 24 hours) — rerun the identical command and it resumes the accepted batch without
re-billing; `2` an error. A missing `ANTHROPIC_API_KEY` is refused before the database is
touched. `na-extract review` lists pending review flags (injection markers, prohibited output)
any in-flight attempt, and every held lease with its owner run's status;
`na-extract abandon --extraction-id ID --reason "..."` turns a stuck attempt into a
retryable failure — the only sanctioned way out of an attempt whose provider outcome is
unknown. A process killed while polling (power loss, a terminated terminal) leaves its run
`running` and its lease held for up to the poll timeout; `na-extract release --run-id ID
--reason "..."` marks that run failed and drops its leases so the next run resumes the
accepted batch without re-billing. An item that fails three times under one prompt version and
model is listed as ineligible instead of being billed again; an item past retention,
tombstoned, or purged is listed and skipped, never aborting the rest of the window.

The live command uses Anthropic Message Batches with strict structured output and no tools.
It checks the current source policy before sending text, rejects visible prompt-injection markers,
verifies every evidence span character-for-character, and resolves model-returned names only through
the deterministic player crosswalk. Batch pricing is versioned in `config/model_pricing.toml`; actual
provider token usage and integer USD-nanocost are stored on the extraction attempt, and
`na-ops status` sums them for the month. Reservations are committed before the single-shot
provider POST, the accepted trace is fsynced to a sibling
`<database>.stage1-receipts/` directory before the SQLite commit, and startup reconciles any
surviving receipt so a crash between acceptance and commit never re-bills. Policy, retention,
deletion, and source bytes are checked at submission and again when each result settles.
`--run-at` is dry-run-only so a live policy or retention check cannot be backdated. A process
killed mid-run leaves lease rows that block a second run for at most the poll timeout plus a
few minutes; `na-extract review` and `na-ops status` show them.

After a live run, create the local 50-item prompt/model gate and fill the blank `label_*`
columns. The sample deliberately balances claim, zero-claim, and flagged outcomes when each
stratum exists:

```bash
uv run na-extract sample --database data/db/narrative_alpha.sqlite3 \
  --size 50 --output data/eval/stage1
uv run na-extract eval --database data/db/narrative_alpha.sqlite3 \
  --labels data/eval/stage1/<completed-review.csv>
```

The evaluator reports per-item claim presence, player-reference resolution, claim dimension,
both direction labels, exact evidence spans, and injection precision/recall, then stores the
metrics against the exact prompt/model and label-file hash in `model_evals`. A prompt or model
change ships only when that evaluation is not worse. Review and label CSVs are local data and
remain gitignored; `na-collect purge` and the scheduled batch purge remove rows for tombstoned
items so the files obey the source text's retention policy.

## Narrative episodes

Stage 2 deterministically clusters stored claims at an explicit information cutoff. The default
72-hour rolling window can be changed per build; changing the algorithm or its fixed similarity
thresholds requires a new method version. Repeating an identical method/as-of build reuses the
stored graph, while different inputs or parameters at that identity fail loudly.

```bash
uv run na-episodes build --database data/db/narrative_alpha.sqlite3 \
  --as-of 2026-09-02T16:00:00Z
uv run na-episodes show --database data/db/narrative_alpha.sqlite3 --player 123
uv run na-episodes show --database data/db/narrative_alpha.sqlite3 \
  --episode episode-<sha256>
```

`show` includes each retained canonical source item, relation, similarity, and propagation edge for
eye-level review. A copied report can raise unique-source reach but is derivative and does not raise
`n_events`; unresolved player references are reported and become canonical team-scoped episodes
only when their Stage 1 team reference is unambiguous.

## Narrative heat features

Stage 3 turns the exact Stage 2 snapshot into one immutable Appendix B row for every player in the
point-in-time salary pool. Build episodes and features with the identical cutoff:

```bash
uv run na-episodes build --database data/db/narrative_alpha.sqlite3 \
  --as-of 2026-09-02T16:00:00Z
uv run na-features build --database data/db/narrative_alpha.sqlite3 \
  --slate-id 123 --site dk --as-of 2026-09-02T16:00:00Z
```

Slate ids come from [`na-slate list`](#slate-and-salary-ingestion); ingest the week's salary
capture first, or there is no slate in the store to build features for.

The heat formula, source-class half-lives, quality mappings, 0.15 soft-factor floor, and immutable
`feature_version` live in `config/heat.toml`. A semantic config change without a version bump is
refused. Baseline ownership is selected only from snapshots available at the cutoff and remains
`NULL` when absent; it is never filled with zero. Six-hour velocity and acceleration are rebuilt
from the episode timeline, so no earlier feature build is required. This stage stores features and
their exact episode/snapshot provenance only—it does not calculate an ownership adjustment.

## Salary CSV parsing

DraftKings and FanDuel classic/showdown exports are detected from their headers and parsed
without resolving canonical players yet:

```python
from pathlib import Path

from narrative_alpha.ingest import parse_salary_csv

result = parse_salary_csv(
    Path("downloaded-salaries.csv"),
    slate_id="2026-week-01-main",
    slate_name="Week 1 Sunday Main",
)
print(result.parse_report)
```

Unknown header drift raises `SalarySchemaError` with explicit missing and unexpected columns.
Invalid player rows remain visible in `result.parse_report.rejected` with their CSV row numbers.

## Player crosswalk

Canonical players are seeded from a manually hash-pinned nflverse roster. Matching proceeds by
vendor ID, normalized name plus team, durable alias, then confidence-gated RapidFuzz scoring;
ambiguous identities are never accepted silently. Review them with:

```bash
uv run na-crosswalk --database data/db/narrative_alpha.sqlite3 resolve
uv run na-crosswalk --database data/db/narrative_alpha.sqlite3 resolve \
  --unresolved-id 12 --player-id 345 --note "confirmed against roster"
```

Pins are dated and selected as-of a decision date. Verified roster bytes are kept in the local
content-addressed archive, so a replay does not depend on nflverse's rolling release URL. To
review a weekly refresh and print its hash, player diff, and exact pin entry without changing
the pin table:

```bash
uv run na-crosswalk nflverse-refresh --season 2026 --reviewed-at 2026-09-02
uv run na-crosswalk --database data/db/narrative_alpha.sqlite3 seed \
  --season 2026 --as-of 2026-09-02
```

`seed` requires an explicit `--as-of` cutoff, selects only a reviewed pin available by then,
verifies or fetches its content-addressed archive bytes, and idempotently seeds canonical players
and temporal roster membership. `PlayerCrosswalk.require_all_resolved()` is the fail-closed guard
for lineup generation.

## Slate and salary ingestion

A salary export is the only file that names a slate, so `na-slate` is the only writer of
`slates` rows and the step that turns a capture into the `slate_id` that `na-build`,
`na-features`, and `na-report` all require:

```bash
uv run na-slate ingest --database data/db/narrative_alpha.sqlite3 --season 2026 --week 1 --site dk
uv run na-slate list --database data/db/narrative_alpha.sqlite3 --season 2026 --week 1
```

`ingest` defaults to the newest `salaries` capture for the week under `data/snapshots/`
(`--capture` picks a specific one). It verifies each file's hash against the manifest,
parses it with the strict DK/FD parser, and writes one `slates` row per distinct slate plus
one `salaries` row per player. `observed_at` is the capture's time, never now.

Salary exports carry no slate id, so one is derived from site, season, week, slate type, and
the slate's earliest kickoff — `draftkings:2026:w01:classic:20260913T170000Z`. Sunday's
re-download of the same slate therefore resolves to the same `slate_id` and versions its
salaries; every changed salary is reported as a diff, and nothing is ever updated in place.
Reloading the same capture inserts nothing and says so. FanDuel classic exports omit kickoff
times: pass `--starts-at` with the slate's first kickoff, using the same value every time,
or the load is refused rather than guessed.

Every player goes through the crosswalk with the site player id, name, team, and position.
An unresolved player is queued and printed with the exact `na-crosswalk resolve` command;
the slate is still written, and `require_all_resolved` stops the lineup build until the
queue is clear. `ingest` exits 0 when clean, 1 when something is queued or rejected, and 2
when it refuses (hash mismatch, wrong week, missing capture).

`list` prints the slate ids with site, type, name, lock time, player count, the site's
unresolved-queue count, and the last salary, projection, and ownership observation times.

## Projection and ownership ingestion

`load_projection_capture` reads the Slice 1 `manifest.json`, verifies each file hash, dispatches
the manifest source through an explicit `SourceFormatRegistry`, resolves players through the
crosswalk, and inserts immutable point-in-time rows. Reprocessing the same capture is idempotent;
a later observation remains a distinct row. Vendor adapters are intentionally schema-specific—no
format guessing or silent fallback is allowed.

## Lineup generation and upload

`OptimizationRequest` is solver-independent and carries every design-doc §6.5 control. The Phase
0 `PydfsAdapter` explicitly rejects controls it cannot honor, and its output is checked again by
the independent `validate_lineup` implementation before export. DraftKings and FanDuel classic
uploads support reserved-entry metadata and render deterministic UTF-8 CSV bytes.

Before treating site compatibility as accepted, complete
[the manual upload checklist](docs/manual-lineup-upload-checklist.md) once on each site. Automated
tests cover roster, salary, team, site, and golden-file format rules; they cannot detect a live
site template change.

## Contest results and actual ownership

`parse_contest_standings` strictly recognizes the entrant and athlete sections of synthesized
DraftKings/FanDuel standings exports. `load_contest_standings` stores weekly fantasy points and
contest-specific actual ownership, retaining contest archetype, field size, entry limit, fee,
roster role, lineup/roster counts, file hash, and full point-in-time provenance. Unknown schemas,
unresolved players, and conflicting contest cohort metadata remain explicit rather than guessed.

## Point-in-time replay

Replay a decision snapshot from its captured optimizer request, salary hashes, and projection
hashes:

```bash
uv run na-replay \
  --database data/db/narrative_alpha.sqlite3 \
  --decision-snapshot DECISION_ID \
  --decision-at 2026-09-13T16:55:00Z \
  --artifact-root data/snapshots/2026/week_01/CAPTURE_DIRECTORY \
  --output replay-upload.csv
```

Every replay read passes through `PointInTimeSession`, which refuses a missing as-of bound. Source
rows must be both available at `decision_at` and named by the decision manifest's hash-set. The
command exits 0 for an output-hash match, 1 for a reproducible mismatch, and 2 for a replay error.
