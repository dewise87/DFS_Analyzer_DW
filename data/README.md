# data/

Local data only — nothing in here is committed except this file and `.gitkeep` markers.

## Layout

```
data/
  archive/nflverse/sha256/<prefix>/<sha256>.csv
                            # exact hash-verified roster bytes; fetched once, replayed locally
  snapshots/<season>/week_<NN>/<capture_ts_utc>/
    manifest.json          # file list, sha256 hashes, observed_at, source labels
    salaries/              # raw DK/FD salary CSVs as downloaded
    projections/           # raw purchased projection exports, one file per source per capture
    ownership/             # raw purchased ownership exports
    odds/                  # odds API responses at capture time
    weather/               # forecast API responses (store forecast run + lead time)
    news/                  # saved copies of key news/beat items with observed-at times
  results/<season>/week_<NN>/
    contest_standings/     # post-settlement standings/lineup exports for entered contests
    actuals/               # nflverse or equivalent outcome data
  db/                      # SQLite operational store (WAL mode)
    narrative_alpha.sqlite3.stage1-receipts/
                            # fsynced accepted-batch recovery receipts, when present
```

## Rules

- Snapshot captures are **append-only**. Never edit or overwrite a captured file; a corrected
  file is a new capture with its own timestamp.
- Every capture directory gets a `manifest.json` with sha256 hashes before it counts as done.
- Fixed capture times (ET), per design doc §9.0: Sat 6:00 p.m., Sun 9:00 a.m., Sun 11:00 a.m.
- Post-lock exports (standings, actuals) are labels, not predictors — the point-in-time rule
  applies to predictors only.
- `archive/nflverse/` is the only local copy of the exact roster bytes behind every dated pin;
  upstream overwrites the rolling asset, so a lost archive makes older pins unfetchable and
  earlier replays fail closed. Back it up with the database.
- Every stored timestamp is canonical UTC (`YYYY-MM-DDTHH:MM:SS.ffffffZ`, 27 characters) and the
  narrative tables refuse any other spelling at insert. Ad-hoc writes from the `sqlite3` CLI work;
  no registered SQL function is needed. Write timestamps in that exact form.
- Treat the SQLite database and its sibling `.stage1-receipts/` directory as one recovery unit.
  If receipts exist, preferably run extraction once at the original path to reconcile them before
  moving or restoring anything; otherwise preserve both together at matching sibling paths. Never
  back up, rename, move, or restore only the database while an accepted-batch receipt remains.

## Weather games CSV

Until schedule ingestion exists, `na-snapshot fetch --kind weather --games <path>` accepts a
CSV with a timezone-aware ISO 8601 `kickoff` (also `kickoff_at` or `commence_time`) and either
`stadium`/`stadium_name` or `home_team` (also `host_team`). Stadium names and team abbreviations/full names resolve
through the versioned static table in `src/narrative_alpha/snapshots/stadiums.py`. Indoor games
are intentionally skipped; outdoor and retractable-roof games are fetched.
