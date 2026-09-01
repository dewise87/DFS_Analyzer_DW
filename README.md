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
  ingest/           L2: salary/projection/result parsers, collectors
  identity/         L2: player crosswalk (canonical IDs, aliases, overrides)
  quant/            L3: projection blend, distributions, ownership model, dependence
  narrative/        L4: evidence extraction, episode clustering, signal registry
  portfolio/        L5: contest selection, simulation, optimizer adapter, late swap
  interface/        L6: MCP tools, slate memo, dashboard, alerts
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

See Appendix D of the design doc for the in-season checklist. Phase −1 rule: **every week the snapshot capture doesn't run destroys irreplaceable training data.**

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

`PlayerCrosswalk.require_all_resolved()` is the fail-closed guard for lineup generation.

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
