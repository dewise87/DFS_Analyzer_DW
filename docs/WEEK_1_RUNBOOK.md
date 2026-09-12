# Week 1 Runbook — 2026-09-10 → 2026-09-15, New York Time

`na-*` prefix: `uv run` · Dashboard: `na-ops dashboard` → [127.0.0.1:8765](http://127.0.0.1:8765/)

## Saturday 2026-09-12 addendum — what actually happened, and what changed

Read this before the Sunday rows. Thursday and Friday did not run as written:

- **The scheduled batch had failed every morning since 9/3.** The launchd wrapper invoked
  `na-ops batch --config config/ops.toml`, but `--config` is a top-level option, so every run
  exited 2 with "unrecognized arguments" and nothing was collected for a week. Fixed on 9/12
  (`ops/schedule.py`, golden wrapper, ordering assertions); agents reinstalled. Two manual
  batches ran on 9/12: the first (default window) extracted 200 of the oldest deferred items
  under prompt **v2** (197 succeeded, 2 refused, 1 flagged — the v2 refusal rate is a fraction
  of v1's); the second used `--window-start 2026-09-12T00:00:00Z` to reach the 2,259 items
  collected that afternoon. Use that flag whenever the backlog is older than the slate.
- **`na-ops slate` died at `slate_features` on every run since Slice 50.** The episode
  builder clusters only the current prompt's claims (v2), but the features completeness check
  looked at every succeeded claim, so v1 claims for slate players tripped it. Fixed on 9/12:
  the check and the episode load are scoped to the same prompt version, so earlier-prompt
  claims mean "no narrative signal", not "missing snapshot".
- **Identity queue.** The build refuses on any pending draftkings-site identity. On 9/12 the
  61 salary rows marked OUT by DraftKings and their 60 Stokastic twins were ignored, and 13
  unambiguous name variants were resolved. Eight rows were left for a human decision (see the
  dashboard Queues page): Al-Jay Henderson, River Cracraft, and Eli Mitchell are not on the
  nflverse roster; Nate Carter (roster KC, feed ATL) and Tutu Atwell (roster LA/INA, feed MIA)
  disagree with the feed. Tonight's fresh salary file may add a few more.
- **Roster pin** reviewed and pasted for 2026-09-12 (+17, ~310), seeded 17 players.
- **Weather** needs a games CSV (`home_team,kickoff`): `data/scratch/games_2026_week_01.csv`
  is generated from the store's 12 games. **Odds** needs `ODDS_API_KEY`, which lives in
  `~/.zshrc`; a non-login shell must `source ~/.zshrc` first.
- **Vendor drop folders** for tonight: `data/vendor/draftkings/2026-09-12/`,
  `data/vendor/fanduel/2026-09-12/`, `data/vendor/stokastic/2026-09-12/` (git-ignored).
- **Known, not fixed:** the batch lane builds episodes at `started_at`, so claims ingested by
  the same run's extract step never make that run's episode snapshot (they are picked up by
  the next run, or by `na-ops slate`, which builds at the decision instant). The FanDuel
  slate still passes odds/weather vacuously because it has no game rows.

## Week 1 has its projection source

Slice 9 landed on 2026-09-06: the `stokastic` adapter is registered for projections and ownership
as well as stats, so a captured Data Hub Projections export loads and `projection_coverage` /
`ownership_coverage` read from real rows: every step below runs, including the build and the
upload CSV.
Stokastic ships **one file per site** and both projections and ownership live in the same file, so
capture each site's export under **both** kinds; the loader attributes each file by its defense
position token (`DST` → DraftKings, `D` → FanDuel) and lists the other site's file as skipped, by
name. Show the state: `na-slate list --season 2026 --week 1` for the id, then
`na-ops readiness --slate-id N`.

Readiness names seven checks; a classic slate prints five, a showdown six. `projection_coverage`
and `projection_age` ← no ingested projection (or Sunday building on Saturday's capture, 6 h bound);
`ownership_coverage` ← classic, `ownership_coverage_captain`/`_flex` ← showdown, no baseline;
`odds_coverage`/`weather_coverage` ← the load matched no game or skipped the forecast.
`--accept-readiness <check>` excuses exactly one named check, is frozen into the decision manifest,
and is printed in the memo; an unknown name is refused.

## Failure actions — literal lane texts

- [ ] **B1 Collect:** `"2 of 104 sources failed — fox-nfl: feed contains no item or entry elements; pfn-nfl: source 'pfn-nfl' fetch failed after 1 attempts HTTP 403"` → record both dead feeds; purge/history continue; retry only after feed/access repair. `"collection failed entirely (no source was collected), so the window has no new input to extract; fix collection and rerun \`na-ops batch\`"` → repair/enable a source, rerun.
- [ ] **B2 Extract (normal):** `"extraction reported 103 item failure(s) beside 95 succeeded — 92 evidence_validation_error, 11 schema_violation; item ids are in the run summary (\`na-ops status\` run history)"` → the step fails whenever **any** item is refused; that is expected. Done: succeeded > 0 **and** `episodes` succeeded. Inspect: `na-extract review` → `refused_attempts_by_bucket` (pre-Slice-50 attempts are one `legacy_output_unavailable` bucket of 330; new refusals carry real buckets). `"the provider batch is still processing; rerun \`na-ops batch\` and it resumes the accepted batch without re-billing"` → rerun only. `"monthly LLM budget guard refused the batch: ... nothing was submitted"` → `na-ops batch --max-items N` or raise `monthly_llm_budget_usd`.
- [ ] **B3 Keychain / pins:** `"ANTHROPIC_API_KEY is not set for this process; a scheduled run reads it from the macOS Keychain through the wrapper \`na-ops schedule install\` writes. Add the Keychain item with \`security add-generic-password -s narrative-alpha-anthropic -a \"$USER\" -w\`"` → add/unlock, choose **Always Allow**, rerun. `"the rolling nflverse roster (01ed4adec274…) no longer matches the newest pin: +145 -0 ~526 players. Review with \`na-crosswalk nflverse-refresh --season 2026 --reviewed-at 2026-09-05\` and paste the new pin entry"` → hash and counts move daily in cutdown week; see Thu 09:20.
- [ ] **S1 No adapter:** `"no SourceFormat adapter is registered for vendor(s) <vendor>; their capture(s) <stamp>, <stamp> were not loaded and nothing was guessed (registered vendors: stokastic)"` → only a genuinely unregistered vendor reaches this now; `stokastic` is registered. The capture is preserved, nothing was guessed. Do not work around it. Not a failure, printed by the same step: `"skipped <stamp>/projections/FD_NFL_Main_Data_Hub_Projections.csv (the adapter attributed it to fanduel, not draftkings)"` → expected, one file per site in one capture.
- [ ] **S2 Identity:** `"3 salary row(s) did not resolve to a canonical player; the slate was written but a build refuses until they are cleared:"` + one `na-crosswalk resolve` line each → resolve in Dashboard → Queues or by command; Done: `na-slate list` unresolved **0**. `"N unresolved draftkings identity/identities remain; lineup generation must stop until each is decided:"` → same fix.
- [ ] **S3 Build / slate choice:** `"slate 1 has no draftkings projection row eligible at <stamp>, so no candidate player can be priced — projections: captured but not ingested (see the slate_projections step); ownership: captured but not ingested (see the slate_projections step)"` (or `NOT CAPTURED for this week` when nothing was captured) → with Slice 9 landed this means the capture is genuinely missing or was skipped as another site's file; check the `slate_projections` step before anything else. `"2 draftkings slates exist for 2026 week 01 — 1 (classic, locks …), 2 (showdown, locks …); rerun with \`--slate-id\` naming the one to play"` → repeat with the printed id. `"--decision-at … is before this run began …; To rebuild an earlier decision use \`na-build --decision-at\`, and to reproduce a frozen one use \`na-replay\`"` → drop the stale `--decision-at`.

## Thu 2026-09-10

- [ ] **09:20 — preflight, pins, schedule.** Command: `na-ops doctor`; `na-ops schedule install`; `na-crosswalk nflverse-refresh --season 2026 --reviewed-at 2026-09-10` → read the status diff → paste its `paste_entry` into `PINNED_ROSTER_RELEASES` in `src/narrative_alpha/identity/nflverse.py`, gates, commit → `na-crosswalk seed --season 2026 --as-of 2026-09-10`; `na-ops status`. Done: doctor shows no FAIL other than `nflverse stats pin` — that one stands until Tue 9/15 because nflverse publishes `stats_player_week_2026.csv` only after Week 1 games (the refresh fails with `HTTPStatusError`, a 404, until then); today it also reports `nflverse roster pin … is not seeded` (the 09-05 pin was pasted but never seeded) and a missing backup agent; `fast-lane signature 2026-week-2-v1, signed by Daniel Wise, expires 2026-09-30T23:59:59+00:00` is **valid for all of Week 1** — re-sign before the 10-04 slate, not this one. Fail: B3.
- [ ] **09:30 — scheduled batch.** Command: launchd `na-ops batch`; recovery `na-ops batch --max-items 200`. Done: Status/Runs shows `collect`, `purge`, `extract`, `nflverse_refresh`, `episodes` succeeded or explicitly skipped, with `extract` succeeded > 0. This is the first batch under prompt **v2**. Fail: B1/B2/B3.
- [ ] **After the batch — v2 labeled evaluation (once).** Command: `na-extract sample --size 50 --output data/eval/stage1`; fill the blank `label_*` columns; `na-extract eval --labels data/eval/stage1/<completed-review.csv>`. Done: a new `model_evals` row whose metrics are **not worse** than the v1 baseline (`stage1-extraction-v1` / `claude-haiku-4-5-20251001`, 50 items: claim_presence acc 0.900 / F1 0.865, evidence_span_exactness 0.905, player_reference_resolution 0.952, claim_dimension 0.762, outcome_direction 0.667, roster_behavior_direction 0.619, injection_flag 1.000). Zero refusals under v2 is **not** recall — this eval is the instrument. Fail: record the regression; do not promote v2 further.
- [ ] **After DK showdown download — first native export.** Command: `na-snapshot capture --season 2026 --week 1 --kind salaries --source draftkings <dk-showdown.csv>`; `na-slate ingest --season 2026 --week 1 --site dk`; `na-slate list --season 2026 --week 1 --site dk`. Done: **either** ingest is clean (one showdown slate; same-ID CPT/FLEX pairs coalesce to one base player at the FLEX salary with both roles) **or** it exits 1 and names the conflict. Distinct CPT/FLEX site IDs are **not supported**: they are not refused — they load the CPT row only, at 1.5× salary with role `["CPT"]`, and print `"salaries key conflict for slate_id=1 player_id=<id> observed_at=<stamp>: an existing row for this key has different content"` per player, with `na-slate list` showing half the export's players. If you see that, **stop** — retain the CSV, do not build, do not work around it. Also refused by name: `"…has ambiguous CPT/FLEX salary rows; expected exactly one of each"`, `"…has conflicting identity or game data between CPT and FLEX rows"`, `"…CPT salary N is not 1.5x FLEX salary M"`. FanDuel single game is six slots — one MVP at 1.5× salary and points plus five FLEX. Fail: S2.

## Fri 2026-09-11

- [ ] **09:30 — scheduled batch.** Command: launchd `na-ops batch`; recovery `na-ops batch --max-items 200`. Done: a new Friday row; purge/history advance despite dead feeds. Fail: B1/B2/B3.

## Sat 2026-09-12

- [ ] **12:00 — Data Hub Stats capture and load.** Command: `na-snapshot capture --season 2026 --week 1 --kind stats --source stokastic Stats_Passing.csv Stats_Rushing.csv Stats_Receiving.csv` (three files, **one** command); `na-slate load-stats --season 2026 --week 1 --site dk`; `na-slate stats --season 2026 --week 1 --site dk --slate-id N`. Done: rows written, unresolved names listed, out-of-slate rows counted; derived DK means read sensibly. `load-stats` exits 0 clean, 1 identities queued, 2 held (>10% unresolved, nothing written) or refused. These means never enter a build. Fail: S2; a held capture is a failed step that does not stop the lane.
- [ ] **18:00 — required capture; odds/weather belong here, not earlier.** Command: `na-snapshot capture --season 2026 --week 1 --kind salaries --source draftkings <salaries.csv>`; `… --kind projections --source stokastic DK_NFL_Main_Data_Hub_Projections.csv FD_NFL_Main_Data_Hub_Projections.csv`; `… --kind ownership --source stokastic DK_NFL_Main_Data_Hub_Projections.csv FD_NFL_Main_Data_Hub_Projections.csv` (Stokastic exports one file per site and puts projections **and** ownership in it, so the same two files are captured under both kinds); `na-snapshot fetch --season 2026 --week 1 --kind odds`; `na-snapshot fetch --season 2026 --week 1 --kind weather --games <games.csv>`; `na-snapshot verify --season 2026 --week 1`. Done: `na-ops status` SNAPSHOTS shows current salary/projection/ownership/odds/weather captures; no verify problem. **Open-Meteo's run horizon is about a week**, so a fetch made before Saturday does not reach Sunday's kickoffs: `na-slate load-weather` then skips it by name — `"SKIPPED: weather/03_lambeau_field.json Lambeau Field kickoff=2026-09-13 17:00:00+00:00: kickoff hour 2026-09-13T17:00 has 0 forecast values; expected one"` — and readiness reads `FAIL weather_coverage weather for the outdoor games: 2 of 2 game(s) missing — CHI@GB, NYG@DAL`. Fail: retain the immutable capture; rerun only the failed fetch/verify.
- [ ] **After capture — Saturday slate lane.** Command: `na-ops slate --season 2026 --week 1 --site dk --lineups 20`. Done: through `slate_memo`, with a decision, memo, and upload CSV; `slate_projections` succeeds, skipping the FanDuel file by name and reporting `zero_projection_rows`, `range_dropped`, and any `salary_mismatches` (vendor salary vs the slate's — reported, never a refusal). Odds note (not a failure): `"NOTE: 272 event(s) matched no ingested game for 2026 week 1"` — the feed is league-wide, the store is slate-scoped. Fail: S1/S2/S3.

## Sun 2026-09-13

- [ ] **09:00 — pre-lock refresh.** Command: `na-ops doctor`; re-capture projections and ownership as at 18:00; `na-snapshot fetch … --kind odds`; `na-snapshot fetch … --kind weather --games <games.csv>`; `na-ops readiness --slate-id N`; `na-ops slate --season 2026 --week 1 --site dk --lineups 20`. Done: newer capture times; salaries versioned, never overwritten; readiness read before any build. Fail: the named doctor remedy, then S1/S2/S3.
- [ ] **11:00 — final irreplaceable capture.** Command: repeat 09:00; `na-snapshot verify --season 2026 --week 1`; `na-ops slate --season 2026 --week 1 --site dk --lineups 20`. Done: final timestamps stored, and a frozen decision, memo, and upload CSV from this run — that is the one you upload. A build on Saturday's projections fails `projection_age` (6 h); either re-capture this morning or pass `--accept-readiness projection_age`, which is frozen into the decision and shown in the memo. Fail: S1/S2/S3.
- [ ] **11:30 — official inactives (only if a rostered player is ruled out).** Command: `na-fast inactives --season 2026 --week 1 --site dk --paste`, paste the official list one player per line, Ctrl-D. Done: the printed diff names who came out and who went in, the new decision id, and the upload CSV — upload that CSV, all entries. Fail: `"N inactive name(s) are unresolved; the whole command was refused and no availability row was written. Clear the unresolved queue, then rerun:"` → resolve, rerun. `"rule 'official-inactives-v1' does not authorize a full unavailable status; a human must confirm the action"` → nothing was written; decide by hand. `"fast-lane rules … expired at …; a human must review and re-sign"` → not expected before 2026-09-30.
- [ ] **Before submit — live upload acceptance.** Command/artifact: the upload CSV printed by the 11:00 lane. Done: [ ] fresh template/entry metadata [ ] UTF-8, one header/data row, no formulas/blanks [ ] DK `Name (ID)` [ ] site preview: nine players for classic, CPT + five FLEX for DK showdown, correct salary/contest [ ] record SHA-256, contest ID, timestamp, result/error. Fail: any site header/ID/salary/roster/team/duplicate error → do **not** submit; keep the error and a screenshot.

## Mon 2026-09-14

- [ ] **After settlement — standings exports.** Command: retain each `<external-contest-id>…csv`. Done: files ready for the results lane, each with its contest id in the filename. Fail: missing id → rename from the site export; never alter contents.

## Tue 2026-09-15

- [ ] **After settlement — results lane.** Command: `na-ops results --season 2026 --week 1 --site dk <standings-file.csv>`. Done: all seven `results_*` steps succeeded **except** `results_stats`, which skips — `na-ops status` reads `WORKLOAD STATS PIN  none reviewed — na-crosswalk nflverse-stats-refresh, then paste the entry`. `PINNED_STATS_RELEASES` is empty and nflverse does not publish 2026 weekly stats until after Week 1's games, so until the pin is reviewed and pasted **every usage claim stays ungradable, by design**. Review sources with `na-report sources --season 2026 --week 1`. Fail: preserve the export and its filename; correct or add the contest metadata (`na-contest add`), rerun.
- [ ] **After the games — review and paste the workload pin.** Command: `na-crosswalk nflverse-stats-refresh --season 2026 --reviewed-at 2026-09-15` → paste the printed `PinnedStatsRelease` entry → gates, commit → rerun `na-ops results`. Done: `results_stats` writes a stat line per salaried player-game and grading counts appear. Fail: nflverse has not published yet — leave the step skipping and try next week.
- [ ] **Fallback only — results lane refuses.** Command: `na-snapshot capture --season 2026 --week 1 --kind standings --source draftkings <standings-file.csv>`. Done: `na-ops status` lists standings captured. Fail: retain the original; retry after correcting the path.

## Every command here, and where the README documents it

| Command | README |
| --- | --- |
| `security add-generic-password …` | README.md:61 |
| `na-ops schedule install` | README.md:65 |
| `na-ops batch [--max-items N]` | README.md:72, README.md:130 |
| `na-ops status` | README.md:93 |
| `na-ops readiness --slate-id N` | README.md:153 |
| `na-ops slate` / `--accept-readiness` | README.md:176, README.md:166 |
| `na-ops dashboard` | README.md:246 |
| `na-snapshot capture` / `fetch` / `verify` | README.md:286, README.md:287, README.md:564 |
| `na-ops results` | README.md:294 |
| `na-report sources` | README.md:299 |
| `na-contest add` (results-lane remedy) | README.md:601 |
| `na-crosswalk resolve` | README.md:302, README.md:473 |
| `na-crosswalk nflverse-refresh` | README.md:304, README.md:484 |
| `na-crosswalk nflverse-stats-refresh` | README.md:308 |
| `na-extract review` | README.md:359 |
| `na-extract sample` / `eval` | README.md:390, README.md:392 |
| `na-crosswalk seed` | README.md:485 |
| `na-slate ingest` / `list` | README.md:513, README.md:514 |
| `na-slate load-stats` / `stats` | README.md:565, README.md:566 |
| `na-slate load-projections` | README.md:515 |
| `na-ops doctor` | README.md:606 |
| `na-fast inactives` | README.md:650 |
| `na-slate load-odds` / `load-weather` | README.md:683, README.md:684 |
