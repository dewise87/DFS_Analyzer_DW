# Stage 1 refusal diagnosis — 2026-09-05

The same 144 source items went from **106 refusals (73.61%) to 0 (0%)** after five
prompt changes. The diagnostic pass cost **$0.295793**, the corrected-prompt pass
**$0.2549755**, and the combined cost was **$0.5507685**. Both used the unchanged
`claude-haiku-4-5-20251001` model, strict schema, 4,096-token ceiling, and batch rates.
No quote matching or schema constraint was loosened.

## Scope and denominators

The production database and its WAL/SHM files were copied to
`/private/tmp/dfs-refusal-diagnosis/scratch.sqlite3` before any SQLite read. All queries,
migrations, provider reservations, settlement, and verification used scratch or test databases.
Production was neither queried nor migrated. Migration 0025 and failed-output retention were
committed in `46d77ab` before the first provider submission. The scratch upgrade preserved every
historical extraction, claim, player/evidence reference, and episode row, with clean integrity
and foreign-key checks.

The historical **227** are failed attempts, **not 227 unique items**: 176 evidence failures and
51 schema failures cover **144 items / 57 distinct canonical content hashes**; 44 of those items
subsequently succeeded in the historical ledger. We submitted each of the 144 items once per
new prompt version, including those 44. No unrelated items or the 200 pre-existing pending
requests were submitted or retrieved.

The old overall figure was 227 / (606 succeeded + 227 failed) = **27.25%**, excluding nine
security flags and 200 pending requests. It is not comparable to the failure-selected diagnostic
cohort's 73.61%. The before/after comparison here uses the same 144 items for both passes.

Original failed outputs are unavailable. A fresh stochastic response cannot establish the cause
of a lost response. The exhaustive [227-attempt mapping](stage1-refusal-diagnosis-2026-09-05.csv)
links each old attempt to its new diagnostic observation and validation result; the mapped counts
below are explicitly **new observations**, not reconstructed historical causes. No refusal is
silently omitted: 38 items (46 old attempts) did not reproduce a refusal in the diagnostic pass.

## Every observed refusal bucket

First rejecting field determines the primary bucket, so the counts are mutually exclusive.
An item can have additional issues masked by its first refusal.

| Primary diagnostic bucket | Refused items | Old attempts mapped to these items | Fix belongs in | Change / fixture | Refused after |
| --- | ---: | ---: | --- | --- | ---: |
| Non-NFL team reference | 78 | 135 | Prompt | Exclude other sports and college-only stories; return `claims=[]`. `non_nfl_team.json`, commit `0c6c98a`. | 0 |
| Empty, placeholder, or invalid player reference | 19 | 31 | Prompt | Skip claims without an individually named NFL player; no fake name/empty-string claim. `placeholder_player.json`, commit `b7c336e`. | 0 |
| Paraphrased or explanatory disconfirming context | 4 | 7 | Prompt | Copy one contiguous source passage or emit null. `paraphrased_context.json`, commit `f34fd9a`. | 0 |
| `none` combined with an ambiguity flag | 3 | 5 | Prompt | Make `none` exclusive. `contradictory_flags.json`, commit `1bf17fe`. | 0 |
| Inferred NFL team name/code absent from text | 2 | 3 | Prompt | Omit a known affiliation when no team text is present. `inferred_team.json`, commit `2883450`. | 0 |
| No diagnostic refusal reproduced | 38 | 46 | No refusal fix assigned | Included again in the validation pass to expose regressions. | 0 |
| **Total cohort** | **144 (106 refused)** | **227** | | | **0** |

The invalid-player bucket contains 15 schema refusals and four source-match refusals. The other
three schema refusals were flag conflicts. The context bucket includes model explanations of why
an article is out of scope, which are still not source quotations.

We inspected every evidence reference in every refused diagnostic output. **All were locatable**
with the existing quote/dash folds and deterministic offset repair. Observed unmatched evidence
quotes, whitespace-only failures, dropped-headline spans, invented/paraphrased evidence extracts,
name-suffix failures, overlength names, and empty `verbatim_extract` failures: **zero**. Thus no
new normalization, fuzzy acceptance, or schema relaxation was warranted. `claims=[]` was already
valid; `name_raw` remains 1–64 characters, evidence 1–512, and each emitted claim still requires
at least one player and one evidence reference.

Each bucket commit has a regression built from an actual diagnostic refusal, with consistent
name substitutions and source-item ID remapping. Fixtures record the original output hash and
anonymization method. The tests retain refusal of the original shape and validate the response
the revised prompt instructs the provider to produce.

## Runs, cost, and quality limits

The diagnostic prompt `stage1-refusal-diagnostic-20260905-v1` changed only trailing whitespace
from v1, to get a new immutable prompt artifact without changing instructions. It produced 38
succeeded attempts (21 with claims, 17 empty), 106 failures, and no flags. Its accepted results
stored 31 claims. The corrected `stage1-extraction-v2` produced 144 succeeded attempts (19 with
claims, 125 empty), 25 stored claims, and no flags. Stage 2's default follows the new Stage 1
version; historical v1 artifacts and explicit version selection remain intact.

| Pass | Input tokens | Output tokens | Cost |
| --- | ---: | ---: | ---: |
| Diagnostic v1 | 417,361 | 34,845 | $0.295793 |
| Corrected v2 | 464,881 | 9,014 | $0.2549755 |
| **Total** | **882,242** | **43,859** | **$0.5507685** |

Prompt hashes, batch IDs, counters, and exact integer nanocosts are recorded in the
[content-free metrics](stage1-refusal-diagnosis-2026-09-05.json). Paid responses remain only in the
scratch database; the repository contains anonymized fixtures and content-free audit metadata.

**Zero refusals is not a recall or semantic-quality score.** Manual inspection found a potential
recall regression on source item 298 (a named-player mock-draft headline now returned empty), and
item 647 still included a coach alongside the player. Other newly empty items include unnamed
team/league stories and out-of-scope stories. These are disclosed for the separate labeled Stage 1
quality evaluation; this failure-selected, duplicated-content sample cannot certify prompt
quality on the overall feed. No extra validator behavior was added to conceal these limitations.

## Operator review and retention

`na-extract review` now lists refused attempts grouped by bucket, with counts, item/attempt IDs,
stable codes, and retained structured diagnostics. `--prompt-version-id` filters that review.
Legacy missing responses are honestly labeled `legacy_output_unavailable`. While detail exists,
review can refine an early generic bucket; after redaction it uses the immutable stored category.
`na-ops status` shows the latest Stage 1 run and failures by code, including failures settled by a
recovery run without carrying forward older ancestor failures.

Example inspection commands (scratch only):

```sh
uv run --frozen na-extract review \
  --database /private/tmp/dfs-refusal-diagnosis/scratch.sqlite3 \
  --prompt-version-id stage1-refusal-diagnostic-20260905-v1
uv run --frozen na-ops status \
  --database /private/tmp/dfs-refusal-diagnosis/scratch.sqlite3
uv run --frozen na-collect purge \
  --database /private/tmp/dfs-refusal-diagnosis/scratch.sqlite3
```

A purge verification on a second scratch copy redacted all 106 failed outputs and their
diagnostics while preserving hashes, tokens, costs, codes, and buckets. Integrity and foreign-key
checks remained clean.

Source policy still controls retention. A source tombstone clears failed `output_json`,
`error_message`, and `error_detail_json`, preserving hash, redaction timestamp, code/bucket,
tokens, cost, and lineage. The scratch copy is not part of the production purge schedule; run
the scratch purge command before retaining or reviewing it beyond the source retention window.

Validation: Ruff, mypy, and the full test suite pass (928 tests). Tests cover failed-output
retention/hash fidelity, immutable terminal state, tombstone redaction, no resurrection, schema
paths and lengths, malformed output preservation, bucket review, latest-run isolation, the five
real refusal fixtures, and foreign-key rollback/restoration for table rebuilds.
