# Manual Lineup Upload Acceptance Checklist

Run this once for DraftKings and once for FanDuel against a free or lowest-stakes NFL classic
contest. Site templates can change; the freshly downloaded contest template is authoritative.

1. Reserve one entry in the target contest, then download that contest's current upload template.
2. Copy the reserved entry metadata exactly into `OptimizationRequest.upload_entries`:
   DraftKings needs entry ID, contest name, contest ID, and entry fee; FanDuel needs entry ID,
   contest ID, and contest name.
3. Generate one lineup and export it with `PydfsAdapter.export_upload_csv`.
4. Open the CSV as plain text. Confirm UTF-8, one header, one data row, no formulas, no blank
   roster cells, and the same reserved-entry metadata as the downloaded template.
5. Confirm DraftKings roster cells are `Name (ID)` and FanDuel roster cells are site player IDs.
6. Upload the file through the site's lineup-upload screen. Do not submit if the site reports a
   header, player ID, salary, roster-position, team-limit, or duplicate-player error.
7. Confirm the site preview shows the intended nine players, valid salary, and correct contest.
8. Cancel or withdraw the test entry if the site's rules permit it.
9. Record site, timestamp, contest ID, generated CSV SHA-256, result, and any site error text in
   the run notes. Save a screenshot outside version control if operational evidence is needed.

The automated golden and property tests validate deterministic bytes and known roster rules, but
they do not replace this live-site acceptance step.
