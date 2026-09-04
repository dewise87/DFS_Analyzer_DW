"""`na-ops`: the weekly batch, slate decision, Tuesday results, and one status screen."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from narrative_alpha.build_cli import DEFAULT_ARTIFACT_DIRECTORY
from narrative_alpha.ingest.slates import SlateIngestError
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.ops.backup import (
    DEFAULT_BACKUP_DIRECTORY,
    BackupError,
    create_backup,
    restore_backup,
)
from narrative_alpha.ops.batch import (
    DEFAULT_DEPENDENCIES,
    BatchDependencies,
    BatchReport,
    run_batch,
)
from narrative_alpha.ops.config import (
    DEFAULT_OPS_CONFIG_PATH,
    OpsConfig,
    OpsConfigError,
    load_ops_config,
)
from narrative_alpha.ops.dashboard import (
    DEFAULT_DASHBOARD_DEPENDENCIES,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DashboardDependencies,
    DashboardError,
    build_dashboard,
    serve_dashboard,
)
from narrative_alpha.ops.doctor import collect_doctor, render_doctor
from narrative_alpha.ops.results import (
    DEFAULT_RESULTS_DEPENDENCIES,
    ResultsDependencies,
    ResultsReport,
    run_results,
)
from narrative_alpha.ops.schedule import (
    LaunchctlRunner,
    ScheduleError,
    build_jobs,
    inspect_schedule,
    install_schedule,
    run_launchctl,
    uninstall_schedule,
)
from narrative_alpha.ops.slate import (
    DEFAULT_SLATE_DEPENDENCIES,
    SlateDependencies,
    SlateReport,
    run_slate,
)
from narrative_alpha.ops.status import collect_ops_status, render_status, status_payload
from narrative_alpha.portfolio import ContestArchetype, DfsSite, parse_upload_entries
from narrative_alpha.report_cli import DEFAULT_REPORT_DIRECTORY
from narrative_alpha.store import (
    MigrationError,
    StoreConfigurationError,
    apply_migrations,
    connect_database,
)

EXIT_OK = 0
EXIT_STEP_FAILED = 1
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="na-ops",
        description=(
            "Operator console: run the weekly batch, slate, and results lanes, "
            "and read their state."
        ),
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_OPS_CONFIG_PATH)
    parser.add_argument(
        "--database",
        type=Path,
        help="override the database in the operator config",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    batch = commands.add_parser(
        "batch",
        help="collect, purge, extract, and check the roster refresh; every step isolated",
    )
    batch.add_argument(
        "--window-start",
        type=_timestamp,
        help="override the extraction window start (default: end of the last successful run)",
    )
    batch.add_argument(
        "--max-items",
        type=_positive_int,
        help=(
            "submit at most this many fresh items to Stage 1; the rest wait for the next "
            "run (default: batch.max_items_per_run in the operator config)"
        ),
    )
    batch.add_argument("--json", action="store_true", help="print the report as JSON")

    slate = commands.add_parser(
        "slate",
        help="ingest, build episodes and features, freeze the decision, write the memo",
    )
    slate.add_argument("--season", type=_positive_int, required=True)
    slate.add_argument("--week", type=_positive_int, required=True)
    slate.add_argument("--site", choices=("dk", "fd"), required=True)
    slate.add_argument(
        "--decision-at",
        type=_timestamp,
        help=(
            "the one cutoff handed to episodes, features, and the build "
            "(default: now, so the decision is replayable at the instant it was made)"
        ),
    )
    slate.add_argument(
        "--lineups",
        type=_positive_int,
        default=1,
        help="lineups to generate (default: 1)",
    )
    slate.add_argument(
        "--contest-archetype",
        choices=tuple(archetype.value for archetype in ContestArchetype),
        default=ContestArchetype.CASH.value,
    )
    slate.add_argument(
        "--slate-id",
        type=_positive_int,
        help="required only when the week has more than one slate for the site",
    )
    slate.add_argument(
        "--capture",
        type=Path,
        help="salary capture directory (default: the newest salaries capture for the week)",
    )
    slate.add_argument("--slate-name", help="operator label for the slate")
    slate.add_argument(
        "--starts-at",
        type=_timestamp,
        help=(
            "first kickoff, required only for exports that omit game times (FanDuel "
            "classic); use the same value for every re-download of the slate"
        ),
    )
    slate.add_argument(
        "--artifact-directory",
        "--artifact-dir",
        "--artifact-root",
        dest="artifact_directory",
        type=Path,
        default=DEFAULT_ARTIFACT_DIRECTORY,
    )
    slate.add_argument("--report-directory", type=Path, default=DEFAULT_REPORT_DIRECTORY)
    slate.add_argument(
        "--simulate",
        action="store_true",
        help="run the optional experimental contest simulation after the memo",
    )
    slate.add_argument(
        "--simulation-contest",
        help="external contest id (required only when more than one contest is available)",
    )
    slate.add_argument("--simulation-draws", type=_positive_int)
    slate.add_argument("--simulation-seed", type=_non_negative_int)
    slate.add_argument("--simulation-independent", action="store_true")
    slate.add_argument(
        "--simulation-config",
        type=Path,
        default=Path("config/simulation.toml"),
    )
    slate.add_argument(
        "--upload-template",
        type=Path,
        help="site reserved-entry CSV; freezes entry IDs and writes the entry ledger",
    )
    slate.add_argument("--json", action="store_true", help="print the report as JSON")

    results = commands.add_parser(
        "results",
        help=(
            "capture standings, ingest labels, verify replay, write the baseline report, "
            "and grade claims"
        ),
    )
    results.add_argument("--season", type=_positive_int, required=True)
    results.add_argument("--week", type=_positive_int, required=True)
    results.add_argument("--site", choices=("dk", "fd"), required=True)
    results.add_argument(
        "standings_files",
        metavar="STANDINGS_FILE",
        nargs="+",
        type=Path,
        help="site-exported standings CSV(s), with the external contest id in each filename",
    )
    results.add_argument(
        "--artifact-directory",
        "--artifact-dir",
        "--artifact-root",
        dest="artifact_directory",
        type=Path,
        default=DEFAULT_ARTIFACT_DIRECTORY,
    )
    results.add_argument("--report-directory", type=Path, default=DEFAULT_REPORT_DIRECTORY)
    results.add_argument("--json", action="store_true", help="print the report as JSON")

    dashboard = commands.add_parser(
        "dashboard",
        help="serve the same screen, queues, and two interactive lanes as one local web page",
    )
    dashboard.add_argument("--port", type=_port, default=DEFAULT_PORT)
    dashboard.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="loopback address to bind (default: %(default)s); nothing else is accepted",
    )
    dashboard.add_argument(
        "--artifact-directory",
        "--artifact-dir",
        "--artifact-root",
        dest="artifact_directory",
        type=Path,
        default=DEFAULT_ARTIFACT_DIRECTORY,
    )
    dashboard.add_argument("--report-directory", type=Path, default=DEFAULT_REPORT_DIRECTORY)

    status = commands.add_parser("status", help="one screen: what ran, what failed, what is due")
    status.add_argument("--json", action="store_true")
    status.add_argument(
        "--report-directory",
        type=Path,
        default=DEFAULT_REPORT_DIRECTORY,
        help="directory containing monthly reports (default: data/reports)",
    )
    status.add_argument(
        "--backup-directory",
        type=Path,
        default=DEFAULT_BACKUP_DIRECTORY,
    )

    doctor = commands.add_parser("doctor", help="read-only preflight of every live-week dependency")
    doctor.add_argument("--repository", type=Path, default=Path.cwd())
    doctor.add_argument("--home", type=Path)
    doctor.add_argument("--executable", type=Path)
    doctor.add_argument("--artifact-directory", type=Path, default=DEFAULT_ARTIFACT_DIRECTORY)
    doctor.add_argument("--report-directory", type=Path, default=DEFAULT_REPORT_DIRECTORY)
    doctor.add_argument("--snapshot-directory", type=Path)
    doctor.add_argument("--backup-directory", type=Path, default=DEFAULT_BACKUP_DIRECTORY)
    doctor.add_argument("--port", type=_port, default=DEFAULT_PORT)

    backup = commands.add_parser("backup", help="create and verify a UTC-stamped backup generation")
    backup.add_argument("--artifact-directory", type=Path, default=DEFAULT_ARTIFACT_DIRECTORY)
    backup.add_argument("--report-directory", type=Path, default=DEFAULT_REPORT_DIRECTORY)
    backup.add_argument("--backup-directory", type=Path, default=DEFAULT_BACKUP_DIRECTORY)
    backup.add_argument(
        "--include-snapshots",
        action="store_true",
        help="include immutable snapshot captures (excluded by default)",
    )
    backup.add_argument(
        "--keep-newest",
        type=_positive_int,
        help="override backup.keep_newest from the operator config",
    )

    restore = commands.add_parser("restore", help="verify and restore one backup out of place")
    restore.add_argument("--backup", required=True, help="UTC generation stamp")
    restore.add_argument("--into", type=Path, required=True)
    restore.add_argument("--backup-directory", type=Path, default=DEFAULT_BACKUP_DIRECTORY)

    schedule = commands.add_parser("schedule", help="manage the macOS launchd user agents")
    schedule.add_argument("action", choices=("install", "show", "uninstall"))
    schedule.add_argument(
        "--repository",
        type=Path,
        default=Path.cwd(),
        help="repository root the agents run from (default: the current directory)",
    )
    schedule.add_argument(
        "--home",
        type=Path,
        help="override the home directory holding Library/LaunchAgents",
    )
    schedule.add_argument(
        "--executable",
        type=Path,
        help="absolute path to the na-ops binary the agent runs (default: the one on PATH)",
    )
    schedule.add_argument(
        "--no-launchctl",
        action="store_true",
        help="write or remove the files only; do not load or unload the agents",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    dependencies: BatchDependencies = DEFAULT_DEPENDENCIES,
    slate_dependencies: SlateDependencies = DEFAULT_SLATE_DEPENDENCIES,
    results_dependencies: ResultsDependencies = DEFAULT_RESULTS_DEPENDENCIES,
    dashboard_dependencies: DashboardDependencies = DEFAULT_DASHBOARD_DEPENDENCIES,
) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        config = load_ops_config(arguments.config)
    except OpsConfigError as error:
        if arguments.command == "doctor":
            print("NARRATIVE ALPHA — DOCTOR (read-only)")
            print(f"FAIL  config ops.toml  {error}; repair {arguments.config} and rerun doctor")
            return EXIT_STEP_FAILED
        print(f"error: {error}", file=sys.stderr)
        return EXIT_ERROR
    try:
        if arguments.command == "schedule":
            return _schedule(arguments, config)
        if arguments.command == "batch":
            return _batch(arguments, config, dependencies)
        if arguments.command == "slate":
            return _slate(arguments, config, slate_dependencies)
        if arguments.command == "results":
            return _results(arguments, config, results_dependencies)
        if arguments.command == "dashboard":
            return _dashboard(arguments, config, dashboard_dependencies)
        if arguments.command == "doctor":
            return _doctor(arguments, config)
        if arguments.command == "backup":
            return _backup_store(arguments, config)
        if arguments.command == "restore":
            return _restore(arguments)
        return _status(arguments, config)
    except (
        BackupError,
        DashboardError,
        MigrationError,
        OpsConfigError,
        OSError,
        ScheduleError,
        SlateIngestError,
        StoreConfigurationError,
        ValueError,
        sqlite3.Error,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_ERROR


def _database(arguments: argparse.Namespace, config: OpsConfig) -> Path:
    return arguments.database or config.database


def _batch(
    arguments: argparse.Namespace,
    config: OpsConfig,
    dependencies: BatchDependencies,
) -> int:
    database = _database(arguments, config)
    with connect_database(database) as connection:
        apply_migrations(connection)
        report = run_batch(
            connection,
            config=config,
            window_start=arguments.window_start,
            max_items=arguments.max_items or config.batch_max_items_per_run,
            dependencies=dependencies,
        )
    if arguments.json:
        print(json.dumps(_batch_payload(report), indent=2, sort_keys=True))
    else:
        print(_render_batch(report), end="")
    return EXIT_OK if report.ok else EXIT_STEP_FAILED


def _slate(
    arguments: argparse.Namespace,
    config: OpsConfig,
    dependencies: SlateDependencies,
) -> int:
    database = _database(arguments, config)
    site = DfsSite.DRAFTKINGS if arguments.site == "dk" else DfsSite.FANDUEL
    upload_entries = (
        ()
        if arguments.upload_template is None
        else parse_upload_entries(arguments.upload_template, site)
    )
    with connect_database(database) as connection:
        apply_migrations(connection)
        report = run_slate(
            connection,
            config=config,
            database=database,
            season=arguments.season,
            week=arguments.week,
            site=arguments.site,
            decision_at=arguments.decision_at,
            number_of_lineups=arguments.lineups,
            contest_archetype=arguments.contest_archetype,
            upload_entries=upload_entries,
            slate_id=arguments.slate_id,
            capture=arguments.capture,
            slate_name=arguments.slate_name,
            starts_at=arguments.starts_at,
            artifact_directory=arguments.artifact_directory,
            report_directory=arguments.report_directory,
            simulate=arguments.simulate or arguments.simulation_contest is not None,
            simulation_contest_external_id=arguments.simulation_contest,
            simulation_draws=arguments.simulation_draws,
            simulation_seed=arguments.simulation_seed,
            simulation_independent=arguments.simulation_independent,
            simulation_config_path=arguments.simulation_config,
            dependencies=dependencies,
        )
    if arguments.json:
        print(json.dumps(_slate_payload(report), indent=2, sort_keys=True))
    else:
        print(_render_slate(report), end="")
    return EXIT_OK if report.ok else EXIT_STEP_FAILED


def _results(
    arguments: argparse.Namespace,
    config: OpsConfig,
    dependencies: ResultsDependencies,
) -> int:
    database = _database(arguments, config)
    with connect_database(database) as connection:
        apply_migrations(connection)
        report = run_results(
            connection,
            config=config,
            season=arguments.season,
            week=arguments.week,
            site=arguments.site,
            standings_files=arguments.standings_files,
            artifact_directory=arguments.artifact_directory,
            report_directory=arguments.report_directory,
            dependencies=dependencies,
        )
    if arguments.json:
        print(json.dumps(_results_payload(report), indent=2, sort_keys=True))
    else:
        print(_render_results(report), end="")
    return EXIT_OK if report.ok else EXIT_STEP_FAILED


def _dashboard(
    arguments: argparse.Namespace,
    config: OpsConfig,
    dependencies: DashboardDependencies,
) -> int:
    server = build_dashboard(
        config=config,
        database=_database(arguments, config),
        host=arguments.host,
        port=arguments.port,
        dependencies=dependencies,
        artifact_directory=arguments.artifact_directory,
        report_directory=arguments.report_directory,
    )
    print(
        f"na-ops dashboard on {server.url}\n"
        "  It binds loopback only: nothing off this machine can reach it, and it asks for "
        "no password because it never has one to check.\n"
        "  Stop it with Ctrl-C."
    )
    serve_dashboard(server)
    return EXIT_OK


def _status(arguments: argparse.Namespace, config: OpsConfig) -> int:
    database = _database(arguments, config)
    with connect_database(database) as connection:
        apply_migrations(connection)
        status = collect_ops_status(
            connection,
            config=config,
            database=database,
            now=datetime.now(UTC),
            report_directory=arguments.report_directory,
            backup_directory=arguments.backup_directory,
        )
    if arguments.json:
        print(json.dumps(status_payload(status), indent=2, sort_keys=True))
    else:
        print(render_status(status), end="")
    return EXIT_OK


def _doctor(arguments: argparse.Namespace, config: OpsConfig) -> int:
    report = collect_doctor(
        config=config,
        database=_database(arguments, config),
        repository=arguments.repository,
        home=arguments.home or Path.home(),
        artifact_directory=arguments.artifact_directory,
        report_directory=arguments.report_directory,
        snapshot_root=arguments.snapshot_directory,
        backup_directory=arguments.backup_directory,
        dashboard_port=arguments.port,
        na_ops_executable=arguments.executable,
    )
    print(render_doctor(report), end="")
    return EXIT_OK if report.ok else EXIT_STEP_FAILED


def _backup_store(arguments: argparse.Namespace, config: OpsConfig) -> int:
    report = create_backup(
        database=_database(arguments, config),
        artifact_directory=arguments.artifact_directory,
        report_directory=arguments.report_directory,
        pin_archive=config.nflverse_archive,
        snapshot_root=config.snapshot_root,
        backup_directory=arguments.backup_directory,
        include_snapshots=arguments.include_snapshots,
        keep_newest=arguments.keep_newest or config.backup_keep_newest,
    )
    print(f"backup {report.stamp}")
    print(f"  path      {report.path}")
    print(f"  manifest  {report.manifest_path}")
    print(f"  files     {len(report.files)} verified")
    if report.pruned:
        print("  pruned    " + ", ".join(path.name for path in report.pruned))
    else:
        print("  pruned    none")
    return EXIT_OK


def _restore(arguments: argparse.Namespace) -> int:
    report = restore_backup(
        backup=arguments.backup,
        into=arguments.into,
        backup_directory=arguments.backup_directory,
    )
    print(f"restored {report.backup_stamp}")
    print(f"  path      {report.path}")
    print(f"  files     {report.files_verified} verified")
    print(f"  tables    {len(report.row_counts)} row counts verified")
    print(f"  flags     {report.flags}")
    return EXIT_OK


def _schedule(arguments: argparse.Namespace, config: OpsConfig) -> int:
    home = arguments.home or Path.home()
    repository = arguments.repository.resolve()
    jobs = build_jobs(
        config,
        home=home,
        repository=repository,
        na_ops_executable=arguments.executable,
    )
    launchctl = None if arguments.no_launchctl else _default_launchctl()

    if arguments.action == "show":
        for state in inspect_schedule(jobs):
            print(f"{state.job.label}")
            print(f"  when     {state.job.description}")
            print(
                f"  plist    {state.job.plist_path} "
                f"{_state_word(state.plist_installed, state.plist_managed)}"
            )
            print(
                f"  wrapper  {state.job.wrapper_path} "
                f"{_state_word(state.wrapper_installed, state.wrapper_managed)}"
            )
            print(f"  log      {state.job.log_path}")
        print(
            "\nlaunchd runs a job missed while the Mac was asleep at the next wake, "
            "unlike cron, which skips it."
        )
        return EXIT_OK

    changes = (
        install_schedule(jobs, launchctl=launchctl)
        if arguments.action == "install"
        else uninstall_schedule(jobs, launchctl=launchctl)
    )
    for change in changes:
        detail = "" if change.detail is None else f" — {change.detail}"
        print(f"{change.action:<11} {change.path}{detail}")
    if arguments.action == "install":
        print(
            "\nCreate the Keychain item once (the agents read it at run time; it is never "
            "stored in a plist, a log, or the repository):\n"
            f'  security add-generic-password -s {config.keychain_service} -a "$USER" -w'
        )
    return EXIT_OK


def _default_launchctl() -> LaunchctlRunner:
    return run_launchctl


def _state_word(installed: bool, managed: bool) -> str:
    if not installed:
        return "(not installed)"
    return "(installed)" if managed else "(installed, NOT written by na-ops)"


def _render_batch(report: BatchReport) -> str:
    lines = [f"batch {report.batch_run_id}"]
    for step in report.steps:
        seconds = (step.finished_at - step.started_at).total_seconds()
        lines.append(f"  {step.step:<16} {step.status:<9} {seconds:6.1f}s")
        if step.error_text:
            lines.append(f"  {'':<16} {step.error_text}")
    lines.append("  " + ("all steps ok" if report.ok else "one or more steps FAILED"))
    lines.append("")
    return "\n".join(lines)


def _batch_payload(report: BatchReport) -> dict[str, object]:
    return {
        "batch_run_id": report.batch_run_id,
        "ok": report.ok,
        "started_at": report.started_at.isoformat().replace("+00:00", "Z"),
        "finished_at": report.finished_at.isoformat().replace("+00:00", "Z"),
        "steps": [
            {
                "step": step.step,
                "status": step.status,
                "started_at": step.started_at.isoformat().replace("+00:00", "Z"),
                "finished_at": step.finished_at.isoformat().replace("+00:00", "Z"),
                "summary": step.summary,
                "error_text": step.error_text,
            }
            for step in report.steps
        ],
    }


def _render_slate(report: SlateReport) -> str:
    lines = [
        f"slate {report.slate_run_id}  {report.season} week {report.week:02d} {report.site}",
        f"  decision at      {utc_timestamp(report.decision_at)}",
    ]
    for step in report.steps:
        seconds = (step.finished_at - step.started_at).total_seconds()
        lines.append(f"  {step.step:<18} {step.status:<9} {seconds:6.1f}s")
        if step.error_text:
            for line in step.error_text.splitlines():
                lines.append(f"  {'':<18} {line}")
    lines.append("")
    lines.append(f"  slate id         {_or_none(report.slate_id)}")
    lines.append(f"  decision         {_or_none(report.decision_snapshot_id)}")
    lines.append(f"  upload CSV       {_or_none(report.upload_csv_path)}")
    lines.append(f"  memo             {_or_none(report.memo_path)}")
    lines.append(f"  simulation       {_or_none(report.simulation_path)}")
    lines.append(f"  replay           {_or_none(report.replay_command)}")
    lines.append("  " + ("all steps ok" if report.ok else "one or more steps FAILED"))
    lines.append("")
    return "\n".join(lines)


def _or_none(value: object) -> str:
    return "none — the step that produces it did not succeed" if value is None else str(value)


def _slate_payload(report: SlateReport) -> dict[str, object]:
    return {
        "slate_run_id": report.slate_run_id,
        "ok": report.ok,
        "season": report.season,
        "week": report.week,
        "site": report.site,
        "decision_at": utc_timestamp(report.decision_at),
        "started_at": utc_timestamp(report.started_at),
        "finished_at": utc_timestamp(report.finished_at),
        "slate_id": report.slate_id,
        "decision_snapshot_id": report.decision_snapshot_id,
        "upload_csv": None if report.upload_csv_path is None else str(report.upload_csv_path),
        "memo": None if report.memo_path is None else str(report.memo_path),
        "simulation": (None if report.simulation_path is None else str(report.simulation_path)),
        "replay_command": report.replay_command,
        "steps": [
            {
                "step": step.step,
                "status": step.status,
                "started_at": utc_timestamp(step.started_at),
                "finished_at": utc_timestamp(step.finished_at),
                "summary": step.summary,
                "error_text": step.error_text,
            }
            for step in report.steps
        ],
    }


def _render_results(report: ResultsReport) -> str:
    lines = [
        f"results {report.results_run_id}  {report.season} week {report.week:02d} {report.site}",
        f"  evaluation as of {utc_timestamp(report.evaluation_as_of)}",
    ]
    for step in report.steps:
        seconds = (step.finished_at - step.started_at).total_seconds()
        lines.append(f"  {step.step:<18} {step.status:<9} {seconds:6.1f}s")
        if step.error_text:
            for line in step.error_text.splitlines():
                lines.append(f"  {'':<18} {line}")
    lines.append("")
    lines.append(f"  baseline report  {_or_none(report.report_path)}")
    lines.append("  " + ("all steps ok" if report.ok else "one or more steps FAILED"))
    lines.append("")
    return "\n".join(lines)


def _results_payload(report: ResultsReport) -> dict[str, object]:
    return {
        "results_run_id": report.results_run_id,
        "ok": report.ok,
        "season": report.season,
        "week": report.week,
        "site": report.site,
        "evaluation_as_of": utc_timestamp(report.evaluation_as_of),
        "started_at": utc_timestamp(report.started_at),
        "finished_at": utc_timestamp(report.finished_at),
        "report": None if report.report_path is None else str(report.report_path),
        "steps": [
            {
                "step": step.step,
                "status": step.status,
                "started_at": utc_timestamp(step.started_at),
                "finished_at": utc_timestamp(step.finished_at),
                "summary": step.summary,
                "error_text": step.error_text,
            }
            for step in report.steps
        ],
    }


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


def _port(value: str) -> int:
    """A TCP port. 0 is allowed and means "any free port", which the banner then names."""

    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if not 0 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("must be between 0 and 65535")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
