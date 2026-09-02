"""macOS launchd user agents for the batch lane and the manual-capture reminders.

Two kinds of job, deliberately different in what they may touch:

* ``batch`` runs the data lane. Its wrapper is the only place the Anthropic key appears,
  and it reads that key from the login Keychain at run time. The key is never written to
  a plist, a log, or anything inside the repository.
* ``reminder-*`` jobs do no data work at all. They post a macOS notification and append
  the exact manual steps to their log. Their times are fixed by design-doc §9.0 and are
  converted from Eastern to the operator's configured local zone.

Unlike cron, launchd runs a job that was missed while the Mac was asleep at the next
wake, so a closed laptop delays the Wednesday batch rather than skipping the week.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from narrative_alpha.ops.config import WEEKDAY_NUMBERS, OpsConfig

LABEL_PREFIX = "com.narrative-alpha"
WRAPPER_MARKER = "# na-ops-managed:"
EASTERN = ZoneInfo("America/New_York")
KEYCHAIN_ACCOUNT_HINT = (
    'security add-generic-password -s {service} -a "$USER" -w'
)
# §9.0 fixes the manual capture times in Eastern. DST offsets differ between zones only in
# their transition weeks, so the conversion is anchored to a mid-season date rather than
# to whenever `schedule install` happens to run.
SCHEDULE_ANCHOR_MONTH_DAY = (10, 15)


class ScheduleError(RuntimeError):
    """Raised when a launchd agent cannot be rendered, written, or removed."""


@dataclass(frozen=True)
class ReminderSpec:
    """One §9.0 manual capture time and what the operator must do at it."""

    slug: str
    weekday: str
    eastern_time: time
    title: str
    notification: str
    instructions: tuple[str, ...]


REMINDERS: tuple[ReminderSpec, ...] = (
    ReminderSpec(
        slug="saturday-projections",
        weekday="sat",
        eastern_time=time(18, 0),
        title="Saturday 6:00 p.m. ET capture",
        notification=(
            "Download DK/FD salaries, projections, and baseline ownership, then run "
            "na-snapshot capture."
        ),
        instructions=(
            "Download, for every slate you may play:",
            "  1. DraftKings and FanDuel salary CSV exports",
            "  2. purchased projections (all sources)",
            "  3. purchased baseline ownership",
            "Then capture each download (one command per kind/source):",
            "  na-snapshot capture --season {season} --week <WEEK> \\",
            "      --kind salaries --source draftkings <files...>",
            "  na-snapshot capture --season {season} --week <WEEK> \\",
            "      --kind projections --source <vendor> <files...>",
            "  na-snapshot capture --season {season} --week <WEEK> \\",
            "      --kind ownership --source <vendor> <files...>",
            "Odds and weather are fetched, not downloaded:",
            "  na-snapshot fetch --season {season} --week <WEEK> --kind odds",
            "  na-snapshot fetch --season {season} --week <WEEK> --kind weather \\",
            "      --games <games.csv>",
            "Finish with: na-snapshot verify --season {season} --week <WEEK>",
        ),
    ),
    ReminderSpec(
        slug="sunday-early",
        weekday="sun",
        eastern_time=time(9, 0),
        title="Sunday 9:00 a.m. ET capture",
        notification="Re-capture projections and ownership; refresh odds and weather.",
        instructions=(
            "Re-download projections and ownership (they have moved overnight), then:",
            "  na-snapshot capture --season {season} --week <WEEK> \\",
            "      --kind projections --source <vendor> <files...>",
            "  na-snapshot capture --season {season} --week <WEEK> \\",
            "      --kind ownership --source <vendor> <files...>",
            "  na-snapshot fetch --season {season} --week <WEEK> --kind odds",
            "  na-snapshot fetch --season {season} --week <WEEK> --kind weather \\",
            "      --games <games.csv>",
        ),
    ),
    ReminderSpec(
        slug="sunday-final",
        weekday="sun",
        eastern_time=time(11, 0),
        title="Sunday 11:00 a.m. ET final pre-lock capture",
        notification=(
            "Final pre-lock capture: projections, ownership, odds, weather. This is the "
            "one that cannot be redone."
        ),
        instructions=(
            "This capture is irreplaceable: after lock the pre-lock state is gone.",
            "  na-snapshot capture --season {season} --week <WEEK> \\",
            "      --kind projections --source <vendor> <files...>",
            "  na-snapshot capture --season {season} --week <WEEK> \\",
            "      --kind ownership --source <vendor> <files...>",
            "  na-snapshot fetch --season {season} --week <WEEK> --kind odds",
            "  na-snapshot fetch --season {season} --week <WEEK> --kind weather \\",
            "      --games <games.csv>",
            "  na-snapshot verify --season {season} --week <WEEK>",
            "After the slate settles, export contest standings for every probe contest.",
        ),
    ),
)


@dataclass(frozen=True)
class ScheduledJob:
    """One launchd agent this command manages, fully resolved."""

    label: str
    plist_path: Path
    wrapper_path: Path
    log_path: Path
    weekday_numbers: tuple[int, ...]
    local_time: time
    description: str
    script: str
    # The binary the wrapper runs, when it runs one at all. Reminder jobs run no project
    # code, so they have none and nothing needs to exist for them to work.
    executable: Path | None = None

    @property
    def plist(self) -> bytes:
        return plistlib.dumps(
            {
                "Label": self.label,
                "ProgramArguments": ["/bin/sh", str(self.wrapper_path)],
                "StartCalendarInterval": [
                    {
                        "Weekday": weekday,
                        "Hour": self.local_time.hour,
                        "Minute": self.local_time.minute,
                    }
                    for weekday in self.weekday_numbers
                ],
                "RunAtLoad": False,
                "StandardOutPath": str(self.log_path),
                "StandardErrorPath": str(self.log_path),
            },
            sort_keys=True,
        )


@dataclass(frozen=True)
class JobState:
    """What `schedule show` and `schedule uninstall` found on disk for one job."""

    job: ScheduledJob
    plist_installed: bool
    plist_managed: bool
    wrapper_installed: bool
    wrapper_managed: bool


@dataclass(frozen=True)
class ScheduleChange:
    """One file written or removed, or one left alone with the reason why."""

    path: Path
    action: str
    detail: str | None = None


LaunchctlRunner = Callable[[Sequence[str]], tuple[int, str]]


def run_launchctl(command: Sequence[str]) -> tuple[int, str]:
    """Invoke ``launchctl``; the only thing here that cannot be done in-process."""

    try:
        # Fixed argv, absolute path, no shell.
        completed = subprocess.run(
            ["/bin/launchctl", *command],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return 1, f"{type(error).__name__}: {error}"
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def launch_agents_directory(home: Path) -> Path:
    return home / "Library" / "LaunchAgents"


def default_na_ops_executable() -> Path:
    """Find the installed ``na-ops`` this Python would run.

    A launchd agent has almost none of a login shell's environment, so the wrapper must
    name an absolute path. ``sys.executable``'s directory is the usual answer, but it is
    the *running* interpreter's, which is not the project venv when `na-ops` is invoked
    through another runtime. Prefer what is actually on PATH, and let
    :func:`install_schedule` refuse a path that does not exist rather than writing an agent
    that fails silently every Wednesday.
    """

    found = shutil.which("na-ops")
    if found:
        return Path(found).resolve()
    return Path(sys.executable).resolve().parent / "na-ops"


def build_jobs(
    config: OpsConfig,
    *,
    home: Path,
    repository: Path,
    na_ops_executable: Path | None = None,
) -> tuple[ScheduledJob, ...]:
    """Resolve every managed job against one machine's paths."""

    na_ops = na_ops_executable or default_na_ops_executable()
    agents = launch_agents_directory(home)
    wrapper_directory = repository / "data" / "ops" / "bin"
    log_directory = _resolve(repository, config.log_directory)

    jobs = [
        _job(
            label=f"{LABEL_PREFIX}.batch",
            agents=agents,
            wrapper_directory=wrapper_directory,
            log_directory=log_directory,
            weekday_numbers=config.batch_weekday_numbers,
            local_time=config.batch_local_time,
            description=(
                f"batch lane on {', '.join(config.batch_weekdays)} at "
                f"{config.batch_local_time.strftime('%H:%M')} local"
            ),
            script=_batch_script(
                config,
                repository=repository,
                na_ops=na_ops,
                log_path=log_directory / f"{LABEL_PREFIX}.batch.log",
                label=f"{LABEL_PREFIX}.batch",
            ),
            executable=na_ops,
        )
    ]
    for reminder in REMINDERS:
        label = f"{LABEL_PREFIX}.reminder-{reminder.slug}"
        local = eastern_to_local(
            reminder.eastern_time, timezone=config.timezone, season=config.season
        )
        jobs.append(
            _job(
                label=label,
                agents=agents,
                wrapper_directory=wrapper_directory,
                log_directory=log_directory,
                weekday_numbers=(WEEKDAY_NUMBERS[reminder.weekday],),
                local_time=local,
                description=(
                    f"{reminder.title} — {reminder.weekday} at "
                    f"{local.strftime('%H:%M')} local (notification only)"
                ),
                script=_reminder_script(
                    reminder,
                    config=config,
                    log_path=log_directory / f"{label}.log",
                    label=label,
                    local_time=local,
                ),
            )
        )
    return tuple(jobs)


def _job(
    *,
    label: str,
    agents: Path,
    wrapper_directory: Path,
    log_directory: Path,
    weekday_numbers: tuple[int, ...],
    local_time: time,
    description: str,
    script: str,
    executable: Path | None = None,
) -> ScheduledJob:
    return ScheduledJob(
        label=label,
        plist_path=agents / f"{label}.plist",
        wrapper_path=wrapper_directory / f"{label}.sh",
        log_path=log_directory / f"{label}.log",
        weekday_numbers=weekday_numbers,
        local_time=local_time,
        description=description,
        script=script,
        executable=executable,
    )


def eastern_to_local(value: time, *, timezone: ZoneInfo, season: int) -> time:
    """Convert a fixed §9.0 Eastern capture time to the operator's wall clock."""

    month, day = SCHEDULE_ANCHOR_MONTH_DAY
    anchor = datetime.combine(date(season, month, day), value, tzinfo=EASTERN)
    return anchor.astimezone(timezone).time().replace(second=0, microsecond=0)


def _batch_script(
    config: OpsConfig,
    *,
    repository: Path,
    na_ops: Path,
    log_path: Path,
    label: str,
) -> str:
    quoted_repository = shlex.quote(str(repository))
    quoted_log = shlex.quote(str(log_path))
    return f"""#!/bin/sh
{WRAPPER_MARKER} {label}
# Written by `na-ops schedule install`. Edit config/ops.toml and reinstall instead of
# editing this file: `schedule uninstall` only removes wrappers carrying the marker above.
#
# The Anthropic key is read from the login Keychain at run time. It is never written to
# the plist, to this file, or to the log. Create the Keychain item once with:
#   {KEYCHAIN_ACCOUNT_HINT.format(service=config.keychain_service)}
set -eu
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
LOG={quoted_log}
mkdir -p "$(dirname "$LOG")"
cd {quoted_repository}

if ANTHROPIC_API_KEY="$(/usr/bin/security find-generic-password \\
    -s {shlex.quote(config.keychain_service)} -w 2>/dev/null)"; then
    export ANTHROPIC_API_KEY
else
    printf '%s no Keychain item %s; Stage 1 extraction will refuse to submit\\n' \\
        "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" {shlex.quote(config.keychain_service)} >>"$LOG"
fi

printf '%s starting %s\\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" {shlex.quote(label)} >>"$LOG"
# `set -e` must not swallow the finish line: a failed lane is exactly the run whose log
# the operator reads, so the exit code is captured rather than allowed to abort the shell.
status=0
{shlex.quote(str(na_ops))} batch --config {shlex.quote(str(config.path))} \\
    >>"$LOG" 2>&1 || status=$?
printf '%s finished %s exit=%s\\n' \\
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" {shlex.quote(label)} "$status" >>"$LOG"
exit "$status"
"""


def _reminder_script(
    reminder: ReminderSpec,
    *,
    config: OpsConfig,
    log_path: Path,
    label: str,
    local_time: time,
) -> str:
    quoted_log = shlex.quote(str(log_path))
    instructions = "\n".join(
        f"printf '%s\\n' {shlex.quote(line.format(season=config.season))} >>\"$LOG\""
        for line in reminder.instructions
    )
    notification = reminder.notification.replace('"', "'")
    timing = (
        f"Design-doc section 9.0 fixes this at "
        f"{reminder.eastern_time.strftime('%H:%M')} Eastern, which is "
        f"{local_time.strftime('%H:%M')} local for season {config.season}."
    )
    title = reminder.title.replace('"', "'")
    return f"""#!/bin/sh
{WRAPPER_MARKER} {label}
# Written by `na-ops schedule install`. Reminder only: this job does no data work, opens
# no database, and needs no credential.
# {timing}
set -eu
PATH=/usr/bin:/bin
export PATH
LOG={quoted_log}
mkdir -p "$(dirname "$LOG")"
printf '%s %s\\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" {shlex.quote(reminder.title)} >>"$LOG"
{instructions}
printf '\\n' >>"$LOG"
/usr/bin/osascript -e 'display notification "{notification}" with title "Narrative Alpha" \
subtitle "{title}"' || true
"""


def install_schedule(
    jobs: Sequence[ScheduledJob],
    *,
    launchctl: LaunchctlRunner | None = run_launchctl,
) -> tuple[ScheduleChange, ...]:
    """Write every wrapper and plist, then ask launchd to (re)load each agent."""

    _require_runnable_executables(jobs)
    changes: list[ScheduleChange] = []
    for job in jobs:
        for path in (job.wrapper_path.parent, job.plist_path.parent, job.log_path.parent):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise ScheduleError(f"cannot create {path}: {error}") from error
        try:
            job.wrapper_path.write_text(job.script, encoding="utf-8")
            job.wrapper_path.chmod(0o700)
            job.plist_path.write_bytes(job.plist)
        except OSError as error:
            raise ScheduleError(f"cannot write agent {job.label}: {error}") from error
        changes.append(ScheduleChange(job.wrapper_path, "wrote"))
        changes.append(ScheduleChange(job.plist_path, "wrote"))
        if launchctl is not None:
            changes.extend(_reload(job, launchctl))
    return tuple(changes)


def _require_runnable_executables(jobs: Sequence[ScheduledJob]) -> None:
    """Refuse to install an agent that names a binary launchd could not run."""

    for job in jobs:
        binary = job.executable
        if binary is None:
            continue
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ScheduleError(
                f"{job.label} would run {binary}, which is not an executable file. "
                "Install the project first (`uv sync`), or pass the absolute path with "
                "`na-ops schedule install --executable /path/to/na-ops`"
            )


def _reload(job: ScheduledJob, launchctl: LaunchctlRunner) -> tuple[ScheduleChange, ...]:
    domain = f"gui/{_user_id()}"
    launchctl(["bootout", f"{domain}/{job.label}"])  # a not-loaded agent is not an error
    code, output = launchctl(["bootstrap", domain, str(job.plist_path)])
    if code != 0:
        return (
            ScheduleChange(
                job.plist_path,
                "not loaded",
                f"launchctl bootstrap exited {code}: {output or 'no output'}",
            ),
        )
    return (ScheduleChange(job.plist_path, "loaded"),)


def _user_id() -> int:
    return os.getuid()


def inspect_schedule(jobs: Sequence[ScheduledJob]) -> tuple[JobState, ...]:
    """Report, per job, what exists and whether this command is allowed to remove it."""

    return tuple(
        JobState(
            job=job,
            plist_installed=job.plist_path.exists(),
            plist_managed=plist_is_managed(job),
            wrapper_installed=job.wrapper_path.exists(),
            wrapper_managed=wrapper_is_managed(job),
        )
        for job in jobs
    )


def plist_is_managed(job: ScheduledJob) -> bool:
    """True only for a plist this command would have written for exactly this job."""

    try:
        parsed = plistlib.loads(job.plist_path.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False
    if not isinstance(parsed, dict) or parsed.get("Label") != job.label:
        return False
    arguments = parsed.get("ProgramArguments")
    return (
        isinstance(arguments, list)
        and len(arguments) == 2
        and arguments[1] == str(job.wrapper_path)
    )


def wrapper_is_managed(job: ScheduledJob) -> bool:
    """True only for a wrapper carrying this job's marker line."""

    try:
        head = job.wrapper_path.read_text(encoding="utf-8")[:512]
    except (OSError, UnicodeDecodeError):
        return False
    return f"{WRAPPER_MARKER} {job.label}" in head


def uninstall_schedule(
    jobs: Sequence[ScheduledJob],
    *,
    launchctl: LaunchctlRunner | None = run_launchctl,
) -> tuple[ScheduleChange, ...]:
    """Remove only files this command wrote; anything else is reported and left alone."""

    changes: list[ScheduleChange] = []
    for state in inspect_schedule(jobs):
        job = state.job
        if state.plist_installed and launchctl is not None:
            launchctl(["bootout", f"gui/{_user_id()}/{job.label}"])
        for installed, managed, path in (
            (state.plist_installed, state.plist_managed, job.plist_path),
            (state.wrapper_installed, state.wrapper_managed, job.wrapper_path),
        ):
            if not installed:
                changes.append(ScheduleChange(path, "absent"))
                continue
            if not managed:
                changes.append(
                    ScheduleChange(path, "left alone", "not written by `na-ops schedule`")
                )
                continue
            try:
                path.unlink()
            except OSError as error:
                raise ScheduleError(f"cannot remove {path}: {error}") from error
            changes.append(ScheduleChange(path, "removed"))
    return tuple(changes)


def _resolve(repository: Path, path: Path) -> Path:
    return path if path.is_absolute() else (repository / path)
