# data/

Local data only — nothing in here is committed except this file and `.gitkeep` markers.

## Layout

```
data/
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
```

## Rules

- Snapshot captures are **append-only**. Never edit or overwrite a captured file; a corrected
  file is a new capture with its own timestamp.
- Every capture directory gets a `manifest.json` with sha256 hashes before it counts as done.
- Fixed capture times (ET), per design doc §9.0: Sat 6:00 p.m., Sun 9:00 a.m., Sun 11:00 a.m.
- Post-lock exports (standings, actuals) are labels, not predictors — the point-in-time rule
  applies to predictors only.
