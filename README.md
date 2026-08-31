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
