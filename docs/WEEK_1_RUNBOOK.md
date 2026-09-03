# Week 1 Runbook — New York Time

`na-*` command prefix: `~/.local/bin/uv run` · Dashboard: `na-ops dashboard` → [127.0.0.1:8765](http://127.0.0.1:8765/)

## Failure actions — literal lane texts

- [ ] **B1 Batch / live history:** `"2 of 104 sources failed — fox-nfl: feed contains no item or entry elements; pfn-nfl: source 'pfn-nfl' fetch failed after 1 attempts HTTP 403"` → record both dead feeds; let purge/history continue; retry only after feed/access repair.
- [ ] **B2 Keychain:** `"ANTHROPIC_API_KEY is not set for this process; a scheduled run reads it from the macOS Keychain through the wrapper \`na-ops schedule install\` writes. Add the Keychain item with \`security add-generic-password -s narrative-alpha-anthropic -a \"$USER\" -w\`"` → add/unlock it; choose **Always Allow**; rerun.
- [ ] **B3 nflverse:** `"the rolling nflverse roster (45a445362ccc…) no longer matches the newest pin: +101 -0 ~452 players. Review with \`na-crosswalk nflverse-refresh --season 2026 --reviewed-at 2026-09-02\` and paste the new pin entry"` → `na-crosswalk nflverse-refresh --season 2026 --reviewed-at <date>`; review; paste the printed pin entry. `"monthly LLM budget guard refused the batch: ... nothing was submitted"` → `na-ops batch --max-items N` or raise budget. `"the provider batch is still processing; rerun \`na-ops batch\` and it resumes the accepted batch without re-billing"` → rerun only.
- [ ] **S1 adapter:** `"no SourceFormat adapter is registered for vendor(s) stokastic; their capture(s) ... were not loaded and nothing was guessed (registered vendors: none)"` → preserve capture only; wait for Slice 9.
- [ ] **S2 identity/projection:** `"N unresolved draftkings identity/identities remain; lineup generation must stop until each is decided"` → Dashboard → Queues → candidate/Ignore → confirm; Done: unresolved identities **0**. `"slate <id> has no draftkings projection row eligible ... so no candidate player can be priced"` → capture missing data; with S1, no generated upload.

## Thu 2026-09-10

- [ ] **09:20 — Wed–Fri launchd/Keychain preflight.** Command: `security add-generic-password -s narrative-alpha-anthropic -a "$USER" -w`; `na-ops schedule install`; `na-ops schedule show`; `na-ops status`. Done: Wed–Fri 09:30 agent installed; roster seeded; batch history visible. Fail: B2/B3.
- [ ] **09:30 — scheduled batch.** Command: launchd `na-ops batch`; recovery `na-ops batch --max-items 200`. Done: Status/Runs shows `collect`, `purge`, `extract`, `nflverse_refresh`, `episodes` succeeded or explicitly skipped. Fail: B1/B2/B3; `"collection failed entirely (no source was collected) ... fix collection and rerun \`na-ops batch\`"` → repair/enable source, rerun.
- [ ] **After DK showdown download — first real export.** Command: `na-snapshot capture --season 2026 --week 1 --kind salaries --source draftkings <dk-showdown.csv>`; `na-slate ingest --season 2026 --week 1 --site dk`; `na-slate list --season 2026 --week 1 --site dk`. Done: SLATE LANE has one ingested salary capture, DK **showdown** slate, zero unresolved. Fail: separate CPT/FLEX rows for one player → retain CSV; do not deduplicate, build, or upload. `"N salary row(s) did not resolve to a canonical player; the slate was written but a build refuses until they are cleared"` → S2.

## Fri 2026-09-11

- [ ] **09:30 — scheduled batch.** Command: launchd `na-ops batch`; recovery `na-ops batch --max-items 200`. Done: new Friday row; purge/history advance despite dead feeds. Fail: B1/B2/B3.

## Sat 2026-09-12

- [ ] **12:00 — Stokastic main-slate capture only.** Command: `na-snapshot capture --season 2026 --week 1 --kind projections --source stokastic <projections.csv>`; `na-snapshot capture --season 2026 --week 1 --kind ownership --source stokastic <ownership.csv>`. Done: SLATE LANE shows projection/ownership **captured**; ingested may be 0. Fail: S1.
- [ ] **18:00 — required capture.** Command: `na-snapshot capture --season 2026 --week 1 --kind salaries --source draftkings <salaries.csv>`; `na-snapshot capture --season 2026 --week 1 --kind projections --source stokastic <projections.csv>`; `na-snapshot capture --season 2026 --week 1 --kind ownership --source stokastic <ownership.csv>`; `na-snapshot fetch --season 2026 --week 1 --kind odds`; `na-snapshot fetch --season 2026 --week 1 --kind weather --games <games.csv>`; `na-snapshot verify --season 2026 --week 1`. Done: Status shows current salary/projection/ownership/odds/weather captures; no verify problem. Fail: retain immutable captures; rerun only failed fetch/verify command.
- [ ] **After capture — Saturday slate lane.** Command: `na-ops slate --season 2026 --week 1 --site dk --lineups 20`. Done: SLATE LANE records salaries, inputs, episodes, features, decision, memo, upload CSV when prerequisites exist. Fail: S1/S2. `"N draftkings slates exist ... rerun with \`--slate-id\` naming the one to play"` → repeat with displayed `--slate-id`.

## Sun 2026-09-13

- [ ] **09:00 — pre-lock refresh.** Command: `na-snapshot capture --season 2026 --week 1 --kind projections --source stokastic <projections.csv>`; `na-snapshot capture --season 2026 --week 1 --kind ownership --source stokastic <ownership.csv>`; `na-snapshot fetch --season 2026 --week 1 --kind odds`; `na-snapshot fetch --season 2026 --week 1 --kind weather --games <games.csv>`; `na-ops slate --season 2026 --week 1 --site dk --lineups 20`. Done: newer capture times; salaries versioned, never overwritten. Fail: S1/S2; `"--decision-at ... is before this run began ... To rebuild an earlier decision use \`na-build --decision-at\`, and to reproduce a frozen one use \`na-replay\`"` → omit stale `--decision-at`.
- [ ] **11:00 — final irreplaceable capture and slate lane.** Command: repeat 09:00 commands; `na-snapshot verify --season 2026 --week 1`; `na-ops slate --season 2026 --week 1 --site dk --lineups 20`. Done: final timestamps; frozen decision, memo, upload CSV for this run. Fail: S1/S2.
- [ ] **11:30 — official inactives (only if a rostered player is ruled out).** Command: `na-fast inactives --season 2026 --week 1 --site dk --paste` then paste the official list, one player per line, Ctrl-D. Done: the printed diff names who came out and who went in, the new decision id, and the upload CSV — upload that CSV (all entries; unchanged lineups are re-sent unchanged). Fail: `"inactive name(s) are unresolved"` → resolve in Dashboard → Queues, rerun; `"above rule ... human must confirm"` → nothing was written; decide by hand and rebuild with `na-ops slate` if you agree; `"rules ... expired"` → re-sign `config/fast_lane_rules.yaml` (`approved_at`, `expires_at`, `approved_by`), rerun.
- [ ] **Before submit — live upload acceptance.** Command/artifact: upload CSV printed by the 11:00 `na-ops slate`. Done: [ ] fresh template/entry metadata [ ] UTF-8, one header/data row, no formulas/blanks [ ] DK `Name (ID)` [ ] site preview: nine players/salary/contest [ ] record SHA-256, contest ID, timestamp, result/error. Fail: site header/ID/salary/roster/team/duplicate error → do **not** submit; preserve error/screenshot.

## Mon 2026-09-14

- [ ] **After settlement — standings exports.** Command: retain each `<external-contest-id>...csv`. Done: Sunday decision remains on Status; files ready for RESULTS LANE. Fail: missing ID → rename from site export; never alter contents.

## Tue 2026-09-15

- [ ] **After settlement — results lane.** Command: `na-ops results --season 2026 --week 1 --site dk <standings-file.csv>`. Done: RESULTS LANE shows all six `results_*` steps succeeded; Week 1/archetype labels, grading counts, and report path appear. Review source cells with `na-report sources --season 2026 --week 1`. Fail: preserve export/filename; correct/add contest metadata; rerun.
- [ ] **Fallback only — Slice 25 absent.** Command: `na-snapshot capture --season 2026 --week 1 --kind standings --source draftkings <standings-file.csv>`. Done: Status lists standings captured. Fail: retain original; retry capture after file/path correction.
