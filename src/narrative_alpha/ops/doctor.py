"""Read-only preflight for every dependency of a live operator week."""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from narrative_alpha.build_cli import DEFAULT_ARTIFACT_DIRECTORY
from narrative_alpha.fast.rules import DEFAULT_FAST_LANE_RULES_PATH, load_fast_lane_rules
from narrative_alpha.grading import DEFAULT_GRADING_CONFIG_PATH, load_grading_config
from narrative_alpha.identity.nflverse import (
    PINNED_ROSTER_RELEASES,
    PinnedRosterRelease,
    pinned_roster_release,
    roster_archive_path,
)
from narrative_alpha.identity.pins import NflversePinError, pin_archive_path
from narrative_alpha.ingest.nflverse_stats import (
    DEFAULT_WORKLOAD_STATS_CONFIG_PATH,
    PINNED_STATS_RELEASES,
    PinnedStatsRelease,
    load_workload_stats_config,
)
from narrative_alpha.ingest.stokastic_stats import (
    DEFAULT_DERIVED_SCORING_PATH,
    load_derived_scoring_config,
)
from narrative_alpha.narrative import (
    DEFAULT_HEAT_CONFIG_PATH,
    DEFAULT_PRICING_PATH,
    DEFAULT_SOURCE_CATALOG_PATH,
    load_batch_pricing,
    load_heat_config,
    load_source_catalog,
    load_synchronous_pricing,
)
from narrative_alpha.ops.backup import DEFAULT_BACKUP_DIRECTORY
from narrative_alpha.ops.config import OpsConfig, load_ops_config
from narrative_alpha.ops.schedule import (
    LaunchctlRunner,
    ScheduledJob,
    build_jobs,
    inspect_schedule,
    run_launchctl,
)
from narrative_alpha.ops.secrets import keychain_item_readable
from narrative_alpha.ops.status import OpsStatus, collect_ops_status
from narrative_alpha.ownership_config import load_ownership_config
from narrative_alpha.portfolio import DEFAULT_CONTEST_POLICIES_PATH, load_contest_policies
from narrative_alpha.readiness import load_readiness_config
from narrative_alpha.report_cli import DEFAULT_REPORT_DIRECTORY
from narrative_alpha.store import DEFAULT_MIGRATIONS_PATH, inspect_migrations

CheckLevel = str
CONFIG_FILENAMES = (
    "ops.toml",
    "heat.toml",
    "ownership_model.toml",
    "contest_policies.toml",
    "readiness.toml",
    "claim_grading.toml",
    "workload_stats.toml",
    "derived_scoring.toml",
    "fast_lane_rules.yaml",
    "narrative_sources.toml",
    "model_pricing.toml",
)
PIN_MAX_AGE = timedelta(days=7)
BACKUP_OK_AGE = timedelta(hours=26)
BACKUP_WARN_AGE = timedelta(hours=48)
WARN_FREE_BYTES = 5 * 1024**3
FAIL_FREE_BYTES = 1024**3


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    level: CheckLevel
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    as_of: datetime
    checks: tuple[DoctorCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.level != "FAIL" for check in self.checks)


@dataclass(frozen=True)
class _ConfigSpec:
    name: str
    filename: str
    loader: Callable[[Path], object]


def collect_doctor(
    *,
    config: OpsConfig,
    database: Path,
    repository: Path,
    home: Path,
    artifact_directory: Path = DEFAULT_ARTIFACT_DIRECTORY,
    report_directory: Path = DEFAULT_REPORT_DIRECTORY,
    snapshot_root: Path | None = None,
    backup_directory: Path = DEFAULT_BACKUP_DIRECTORY,
    dashboard_host: str = "127.0.0.1",
    dashboard_port: int = 8765,
    now: datetime | None = None,
    config_paths: Mapping[str, Path] | None = None,
    migrations_path: Path = DEFAULT_MIGRATIONS_PATH,
    launchctl: LaunchctlRunner = run_launchctl,
    secret_reader: Callable[[OpsConfig], object] = keychain_item_readable,
    roster_releases: Mapping[int, tuple[PinnedRosterRelease, ...]] = PINNED_ROSTER_RELEASES,
    stats_releases: Mapping[int, tuple[PinnedStatsRelease, ...]] = PINNED_STATS_RELEASES,
    na_ops_executable: Path | None = None,
) -> DoctorReport:
    """Collect one immutable preflight snapshot.  No check creates or repairs state."""

    checked_at = (now or datetime.now(UTC)).astimezone(UTC)
    root = repository.resolve()
    checks: list[DoctorCheck] = []

    try:
        present = bool(secret_reader(config))
    except Exception as error:  # a diagnostic must degrade to a named check
        present = False
        secret_detail = f"credential lookup failed: {type(error).__name__}: {error}"
    else:
        secret_detail = (
            "credential is readable; its value was not printed"
            if present
            else (
                "add or unlock the login-Keychain item with "
                f'`security add-generic-password -s {config.keychain_service} -a "$USER" -w`'
            )
        )
    checks.append(DoctorCheck("Keychain / Anthropic", "OK" if present else "FAIL", secret_detail))

    paths = _config_paths(root, config.path, overrides=config_paths)
    for spec in _config_specs():
        path = paths[spec.filename]
        try:
            spec.loader(path)
            digest = _sha256(path)
        except Exception as error:
            checks.append(
                DoctorCheck(
                    f"config {spec.filename}",
                    "FAIL",
                    f"{type(error).__name__}: {error}; repair {path} and rerun doctor",
                )
            )
        else:
            checks.append(DoctorCheck(f"config {spec.filename}", "OK", f"sha256 {digest} ({path})"))

    connection: sqlite3.Connection | None = None
    status: OpsStatus | None = None
    migration_current = False
    try:
        connection = _read_only_database(database)
        migration_status = inspect_migrations(connection, migrations_path)
        if migration_status.pending:
            names = ", ".join(migration.name for migration in migration_status.pending)
            checks.append(
                DoctorCheck(
                    "database migrations",
                    "FAIL",
                    f"pending: {names}; run a normal mutating lane to apply them (doctor will not)",
                )
            )
        else:
            migration_current = True
            latest = (
                "no migrations are shipped"
                if not migration_status.applied
                else f"current through {migration_status.applied[-1].name}"
            )
            checks.append(
                DoctorCheck(
                    "database migrations",
                    "OK",
                    latest,
                )
            )
    except Exception as error:
        checks.append(
            DoctorCheck(
                "database migrations",
                "FAIL",
                f"{type(error).__name__}: {error}; restore or initialize the configured database",
            )
        )

    if connection is not None and migration_current:
        try:
            status = collect_ops_status(
                connection,
                config=config,
                database=database,
                now=checked_at,
                pricing_path=paths["model_pricing.toml"],
                fast_lane_rules_path=paths["fast_lane_rules.yaml"],
                report_directory=report_directory,
                backup_directory=backup_directory,
                workload_stats_releases=stats_releases,
            )
        except Exception as error:
            checks.append(
                DoctorCheck(
                    "operator status snapshot",
                    "FAIL",
                    f"{type(error).__name__}: {error}; repair the store before preflight",
                )
            )

    jobs = build_jobs(
        config,
        home=home,
        repository=root,
        na_ops_executable=na_ops_executable,
    )
    for state in inspect_schedule(jobs):
        installed = (
            state.plist_installed
            and state.plist_managed
            and state.wrapper_installed
            and state.wrapper_managed
        )
        code, output = launchctl(["print", f"gui/{os.getuid()}/{state.job.label}"])
        loaded = code == 0
        next_fire = _next_fire(state.job, now=checked_at, timezone=config.timezone)
        if installed and loaded:
            checks.append(
                DoctorCheck(
                    f"launchd {state.job.label}",
                    "OK",
                    f"installed and loaded; next {next_fire.isoformat(timespec='minutes')}",
                )
            )
        else:
            gaps = []
            if not installed:
                gaps.append("agent files missing or unmanaged")
            if not loaded:
                gaps.append(f"not loaded{': ' + output if output else ''}")
            checks.append(
                DoctorCheck(
                    f"launchd {state.job.label}",
                    "FAIL",
                    f"{'; '.join(gaps)}; run `na-ops schedule install`; next would be "
                    f"{next_fire.isoformat(timespec='minutes')}",
                )
            )

    _append_roster_check(
        checks,
        connection=connection if migration_current else None,
        status=status,
        config=config,
        now=checked_at,
        releases=roster_releases,
    )
    _append_stats_check(
        checks,
        status=status,
        config=config,
        now=checked_at,
        releases=stats_releases,
    )
    _append_fast_lane_check(checks, status=status)

    for name, path in (
        ("decision artifact directory", artifact_directory),
        ("report directory", report_directory),
        ("snapshot directory", snapshot_root or config.snapshot_root),
    ):
        checks.append(_directory_check(name, path))

    checks.append(_port_check(dashboard_host, dashboard_port))
    _append_backup_check(checks, status=status, now=checked_at)

    if connection is not None:
        connection.close()
    return DoctorReport(as_of=checked_at, checks=tuple(checks))


def render_doctor(report: DoctorReport) -> str:
    width = max(len(check.name) for check in report.checks)
    lines = [
        "NARRATIVE ALPHA — DOCTOR (read-only)",
        f"  as of {report.as_of.isoformat().replace('+00:00', 'Z')}",
        "",
    ]
    lines.extend(
        f"{check.level:<4}  {check.name:<{width}}  {' '.join(check.detail.split())}"
        for check in report.checks
    )
    lines.append("")
    return "\n".join(lines)


def _config_specs() -> tuple[_ConfigSpec, ...]:
    def pricing(path: Path) -> object:
        return load_batch_pricing(path), load_synchronous_pricing(path)

    def inactive_rules(path: Path) -> object:
        return load_fast_lane_rules(path, require_active=False)

    return (
        _ConfigSpec("ops", "ops.toml", load_ops_config),
        _ConfigSpec("heat", "heat.toml", load_heat_config),
        _ConfigSpec("ownership", "ownership_model.toml", load_ownership_config),
        _ConfigSpec("contest policies", "contest_policies.toml", load_contest_policies),
        _ConfigSpec("slate readiness", "readiness.toml", load_readiness_config),
        _ConfigSpec("claim grading", "claim_grading.toml", load_grading_config),
        _ConfigSpec("workload stats", "workload_stats.toml", load_workload_stats_config),
        _ConfigSpec("derived scoring", "derived_scoring.toml", load_derived_scoring_config),
        _ConfigSpec("fast lane", "fast_lane_rules.yaml", inactive_rules),
        _ConfigSpec("narrative sources", "narrative_sources.toml", load_source_catalog),
        _ConfigSpec("model pricing", "model_pricing.toml", pricing),
    )


def _config_paths(
    repository: Path,
    ops_path: Path,
    *,
    overrides: Mapping[str, Path] | None,
) -> dict[str, Path]:
    defaults = {
        "ops.toml": ops_path if ops_path.is_absolute() else repository / ops_path,
        "heat.toml": repository / DEFAULT_HEAT_CONFIG_PATH,
        "ownership_model.toml": repository / Path("config/ownership_model.toml"),
        "contest_policies.toml": repository / DEFAULT_CONTEST_POLICIES_PATH,
        "readiness.toml": repository / Path("config/readiness.toml"),
        "claim_grading.toml": repository / DEFAULT_GRADING_CONFIG_PATH,
        "workload_stats.toml": repository / DEFAULT_WORKLOAD_STATS_CONFIG_PATH,
        "derived_scoring.toml": repository / DEFAULT_DERIVED_SCORING_PATH,
        "fast_lane_rules.yaml": repository / DEFAULT_FAST_LANE_RULES_PATH,
        "narrative_sources.toml": repository / DEFAULT_SOURCE_CATALOG_PATH,
        "model_pricing.toml": repository / DEFAULT_PRICING_PATH,
    }
    if overrides:
        defaults.update(overrides)
    return defaults


def _read_only_database(path: Path) -> sqlite3.Connection:
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"database does not exist: {path}")
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    return connection


def _append_roster_check(
    checks: list[DoctorCheck],
    *,
    connection: sqlite3.Connection | None,
    status: OpsStatus | None,
    config: OpsConfig,
    now: datetime,
    releases: Mapping[int, tuple[PinnedRosterRelease, ...]],
) -> None:
    refresh = (
        f"`na-crosswalk nflverse-refresh --season {config.season} "
        f"--reviewed-at {now.date().isoformat()}`"
    )
    try:
        release = pinned_roster_release(config.season, now, releases=releases)
    except NflversePinError as error:
        checks.append(
            DoctorCheck("nflverse roster pin", "FAIL", f"{error}; refresh with {refresh}")
        )
        return
    age = now.date() - release.reviewed_at
    if status is None or connection is None:
        checks.append(
            DoctorCheck(
                "nflverse roster pin",
                "FAIL",
                f"pin {release.sha256} exists but store status is unavailable; repair the store",
            )
        )
        return
    seeded = connection.execute(
        "SELECT 1 FROM players WHERE source_version LIKE ? LIMIT 1",
        (f"%sha256:{release.sha256}",),
    ).fetchone()
    archive = roster_archive_path(config.nflverse_archive, release.sha256)
    archived = _verified_file(archive, release.sha256)
    if status.player_rows == 0 or seeded is None:
        checks.append(
            DoctorCheck(
                "nflverse roster pin",
                "FAIL",
                f"pin {release.sha256} is not seeded; run `na-crosswalk seed --season "
                f"{config.season} --as-of {now.date().isoformat()}`",
            )
        )
    elif not archived:
        checks.append(
            DoctorCheck(
                "nflverse roster pin",
                "FAIL",
                f"pinned bytes are missing or corrupt at {archive}; refresh with {refresh}",
            )
        )
    elif age > PIN_MAX_AGE:
        checks.append(
            DoctorCheck(
                "nflverse roster pin",
                "FAIL",
                f"reviewed {release.reviewed_at.isoformat()} ({age.days}d old), not current for "
                f"this week; refresh with {refresh}",
            )
        )
    else:
        week = (
            "current operator week"
            if status.snapshot_week is None
            else (f"{status.snapshot_week.season} week {status.snapshot_week.week:02d}")
        )
        checks.append(
            DoctorCheck(
                "nflverse roster pin",
                "OK",
                f"{week}; reviewed {release.reviewed_at.isoformat()}; sha256 {release.sha256}",
            )
        )


def _append_stats_check(
    checks: list[DoctorCheck],
    *,
    status: OpsStatus | None,
    config: OpsConfig,
    now: datetime,
    releases: Mapping[int, tuple[PinnedStatsRelease, ...]],
) -> None:
    refresh = (
        f"`na-crosswalk nflverse-stats-refresh --season {config.season} "
        f"--reviewed-at {now.date().isoformat()}`"
    )
    if status is None or status.workload_stats_pin is None:
        checks.append(
            DoctorCheck(
                "nflverse stats pin", "FAIL", f"no current workload pin; refresh with {refresh}"
            )
        )
        return
    pins = tuple(
        release for release in releases.get(config.season, ()) if release.reviewed_at <= now.date()
    )
    if not pins:
        checks.append(
            DoctorCheck(
                "nflverse stats pin", "FAIL", f"no current workload pin; refresh with {refresh}"
            )
        )
        return
    release = max(enumerate(pins), key=lambda item: (item[1].reviewed_at, item[0]))[1]
    weekly = pin_archive_path(
        config.nflverse_archive, release.weekly_sha256, label="nflverse weekly player stats"
    )
    snaps = pin_archive_path(
        config.nflverse_archive, release.snaps_sha256, label="nflverse snap counts"
    )
    age = now.date() - release.reviewed_at
    if not _verified_file(weekly, release.weekly_sha256) or not _verified_file(
        snaps, release.snaps_sha256
    ):
        checks.append(
            DoctorCheck(
                "nflverse stats pin",
                "FAIL",
                f"pinned workload bytes are missing or corrupt; refresh with {refresh}",
            )
        )
    elif age > PIN_MAX_AGE:
        checks.append(
            DoctorCheck(
                "nflverse stats pin",
                "FAIL",
                f"reviewed {release.reviewed_at.isoformat()} ({age.days}d old), not current for "
                f"this week; refresh with {refresh}",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "nflverse stats pin",
                "OK",
                f"reviewed {release.reviewed_at.isoformat()}; weekly {release.weekly_sha256}; "
                f"snaps {release.snaps_sha256}",
            )
        )


def _append_fast_lane_check(checks: list[DoctorCheck], *, status: OpsStatus | None) -> None:
    rules = None if status is None else status.fast_lane_rules
    if rules is None:
        checks.append(
            DoctorCheck(
                "fast-lane signature",
                "FAIL",
                "rule set is missing or invalid; repair and re-sign config/fast_lane_rules.yaml",
            )
        )
    elif not rules.active:
        checks.append(
            DoctorCheck(
                "fast-lane signature",
                "FAIL",
                f"{rules.rules_version} expired or is not yet active (expires "
                f"{rules.expires_at.isoformat()}); review and re-sign it",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "fast-lane signature",
                "OK",
                f"{rules.rules_version}, signed by {rules.approved_by}, expires "
                f"{rules.expires_at.isoformat()}",
            )
        )


def _directory_check(name: str, path: Path) -> DoctorCheck:
    if not path.is_dir():
        return DoctorCheck(name, "FAIL", f"{path} does not exist; create the directory")
    if not os.access(path, os.W_OK):
        return DoctorCheck(name, "FAIL", f"{path} is not writable; repair its permissions")
    try:
        free = shutil.disk_usage(path).free
    except OSError as error:
        return DoctorCheck(name, "FAIL", f"cannot read free space for {path}: {error}")
    if free < FAIL_FREE_BYTES:
        return DoctorCheck(
            name,
            "FAIL",
            f"{path} is writable but has only {_human_bytes(free)} free; free at least 1 GiB",
        )
    if free < WARN_FREE_BYTES:
        return DoctorCheck(
            name,
            "WARN",
            f"{path} is writable with {_human_bytes(free)} free; snapshots may exhaust it",
        )
    return DoctorCheck(name, "OK", f"{path} is writable; {_human_bytes(free)} free")


def _port_check(host: str, port: int) -> DoctorCheck:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        probe.bind((host, port))
    except OSError:
        # A listener on the dashboard port is most often the dashboard itself, which is a
        # healthy state; say so rather than failing the preflight for it.
        return DoctorCheck(
            "dashboard port",
            "WARN",
            f"{host}:{port} is in use — the dashboard may already be running; if not, "
            "stop the listener or choose another --port",
        )
    finally:
        probe.close()
    return DoctorCheck("dashboard port", "OK", f"{host}:{port} is free")


def _append_backup_check(
    checks: list[DoctorCheck], *, status: OpsStatus | None, now: datetime
) -> None:
    backup = None if status is None else status.newest_backup
    if backup is None:
        checks.append(
            DoctorCheck(
                "newest backup",
                "FAIL",
                "none exists; run `na-ops backup` before relying on this store",
            )
        )
        return
    age = now - backup.created_at
    detail = f"{backup.stamp} is {_human_duration(age)} old ({backup.path})"
    if age <= BACKUP_OK_AGE:
        level = "OK"
    elif age <= BACKUP_WARN_AGE:
        level = "WARN"
        detail += "; the nightly backup is late"
    else:
        level = "FAIL"
        detail += "; run `na-ops backup` now and repair the nightly agent"
    checks.append(DoctorCheck("newest backup", level, detail))


def _next_fire(job: ScheduledJob, *, now: datetime, timezone: ZoneInfo) -> datetime:
    local_now = now.astimezone(timezone)
    for offset in range(8):
        day = local_now.date() + timedelta(days=offset)
        launchd_weekday = (day.weekday() + 1) % 7
        if launchd_weekday not in job.weekday_numbers:
            continue
        candidate = datetime.combine(day, job.local_time, tzinfo=timezone)
        if candidate > local_now:
            return candidate
    raise AssertionError("every scheduled job must fire within seven days")


def _verified_file(path: Path, expected: str) -> bool:
    try:
        return path.is_file() and _sha256(path) == expected
    except OSError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{value} B"
        amount /= 1024
    raise AssertionError("unreachable")


def _human_duration(value: timedelta) -> str:
    seconds = max(int(value.total_seconds()), 0)
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes}m"


__all__ = [
    "CONFIG_FILENAMES",
    "DoctorCheck",
    "DoctorReport",
    "collect_doctor",
    "render_doctor",
]
