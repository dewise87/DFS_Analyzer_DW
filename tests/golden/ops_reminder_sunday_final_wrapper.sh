#!/bin/sh
# na-ops-managed: com.narrative-alpha.reminder-sunday-final
# Written by `na-ops schedule install`. Reminder only: this job does no data work, opens
# no database, and needs no credential.
# Design-doc section 9.0 fixes this at 11:00 Eastern, which is 08:00 local for season 2026.
set -eu
PATH=/usr/bin:/bin
export PATH
LOG=/opt/narrative-alpha/data/logs/com.narrative-alpha.reminder-sunday-final.log
mkdir -p "$(dirname "$LOG")"
printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" 'Sunday 11:00 a.m. ET final pre-lock capture' >>"$LOG"
printf '%s\n' 'This capture is irreplaceable: after lock the pre-lock state is gone.' >>"$LOG"
printf '%s\n' '  na-snapshot capture --season 2026 --week <WEEK> \' >>"$LOG"
printf '%s\n' '      --kind projections --source <vendor> <files...>' >>"$LOG"
printf '%s\n' '  na-snapshot capture --season 2026 --week <WEEK> \' >>"$LOG"
printf '%s\n' '      --kind ownership --source <vendor> <files...>' >>"$LOG"
printf '%s\n' '  na-snapshot fetch --season 2026 --week <WEEK> --kind odds' >>"$LOG"
printf '%s\n' '  na-snapshot fetch --season 2026 --week <WEEK> --kind weather \' >>"$LOG"
printf '%s\n' '      --games <games.csv>' >>"$LOG"
printf '%s\n' '  na-snapshot verify --season 2026 --week <WEEK>' >>"$LOG"
printf '%s\n' 'Then make the decision — this is the run you upload:' >>"$LOG"
printf '%s\n' '  na-ops slate --season 2026 --week <WEEK> --site dk --lineups <N>' >>"$LOG"
printf '%s\n' 'It prints the memo, the upload CSV, and the replay command.' >>"$LOG"
printf '%s\n' 'After the slate settles, export contest standings for every probe contest.' >>"$LOG"
printf '\n' >>"$LOG"
/usr/bin/osascript -e 'display notification "Final pre-lock capture, then na-ops slate. This is the one that cannot be redone." with title "Narrative Alpha" subtitle "Sunday 11:00 a.m. ET final pre-lock capture"' || true
