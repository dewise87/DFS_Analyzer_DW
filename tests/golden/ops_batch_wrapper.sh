#!/bin/sh
# na-ops-managed: com.narrative-alpha.batch
# Written by `na-ops schedule install`. Edit config/ops.toml and reinstall instead of
# editing this file: `schedule uninstall` only removes wrappers carrying the marker above.
#
# The Anthropic key is read from the login Keychain at run time. It is never written to
# the plist, to this file, or to the log. Create the Keychain item once with:
#   security add-generic-password -s narrative-alpha-anthropic -a "$USER" -w
set -eu
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
LOG=/opt/narrative-alpha/data/logs/com.narrative-alpha.batch.log
mkdir -p "$(dirname "$LOG")"
cd /opt/narrative-alpha

if ANTHROPIC_API_KEY="$(/usr/bin/security find-generic-password \
    -s narrative-alpha-anthropic -w 2>/dev/null)"; then
    export ANTHROPIC_API_KEY
else
    printf '%s no Keychain item %s; Stage 1 extraction will refuse to submit\n' \
        "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" narrative-alpha-anthropic >>"$LOG"
fi

printf '%s starting %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" com.narrative-alpha.batch >>"$LOG"
# `set -e` must not swallow the finish line: a failed lane is exactly the run whose log
# the operator reads, so the exit code is captured rather than allowed to abort the shell.
status=0
/opt/narrative-alpha/.venv/bin/na-ops batch --config config/ops.toml \
    >>"$LOG" 2>&1 || status=$?
printf '%s finished %s exit=%s\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" com.narrative-alpha.batch "$status" >>"$LOG"
exit "$status"
