"""`na-ops`: one command for the week's batch lane, and one screen for its state."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

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
from narrative_alpha.ops.schedule import (
    LaunchctlRunner,
    ScheduleError,
    build_jobs,
    inspect_schedule,
    install_schedule,
    run_launchctl,
    uninstall_schedule,
)
from narrative_alpha.ops.status import collect_ops_status, render_status, status_payload
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
        description="Operator console: run the weekly batch lane and read its state.",
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

    status = commands.add_parser("status", help="one screen: what ran, what failed, what is due")
    status.add_argument("--json", action="store_true")

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
) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        config = load_ops_config(arguments.config)
        if arguments.command == "schedule":
            return _schedule(arguments, config)
        if arguments.command == "batch":
            return _batch(arguments, config, dependencies)
        return _status(arguments, config)
    except (
        MigrationError,
        OpsConfigError,
        OSError,
        ScheduleError,
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


def _status(arguments: argparse.Namespace, config: OpsConfig) -> int:
    database = _database(arguments, config)
    with connect_database(database) as connection:
        apply_migrations(connection)
        status = collect_ops_status(
            connection,
            config=config,
            database=database,
            now=datetime.now(UTC),
        )
    if arguments.json:
        print(json.dumps(status_payload(status), indent=2, sort_keys=True))
    else:
        print(render_status(status), end="")
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


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
