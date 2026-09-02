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
printf '%s\n' 'After the slate settles, export contest standings for every probe contest.' >>"$LOG"
printf '\n' >>"$LOG"
/usr/bin/osascript -e 'display notification "Final pre-lock capture: projections, ownership, odds, weather. This is the one that cannot be redone." with title "Narrative Alpha" subtitle "Sunday 11:00 a.m. ET final pre-lock capture"' || true
