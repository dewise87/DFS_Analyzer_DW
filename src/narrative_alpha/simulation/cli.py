"""Command-line entry point for shadow simulation and historical comparisons."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from narrative_alpha.build_cli import DEFAULT_ARTIFACT_DIRECTORY, DEFAULT_DATABASE_PATH
from narrative_alpha.report_cli import DEFAULT_REPORT_DIRECTORY
from narrative_alpha.simulation.calibration import (
    SimulationCalibrationError,
    calibrate_week,
)
from narrative_alpha.simulation.config import (
    DEFAULT_SIMULATION_CONFIG_PATH,
    SimulationConfigError,
)
from narrative_alpha.simulation.evaluation import ContestSimulationError
from narrative_alpha.simulation.field import FieldGenerationError
from narrative_alpha.simulation.outcomes import OutcomeSimulationError
from narrative_alpha.simulation.runner import SimulationRunError, run_simulation
from narrative_alpha.snapshots.core import DEFAULT_SNAPSHOT_ROOT
from narrative_alpha.store import (
    MigrationError,
    StoreConfigurationError,
    apply_migrations,
    connect_database,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="na-simulate",
        description="Run the experimental point-in-time contest simulator.",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument(
        "--decision-snapshot-id",
        "--decision-snapshot",
        dest="decision_snapshot_id",
        required=True,
    )
    parser.add_argument("--contest", dest="contest_external_id", required=True)
    parser.add_argument("--draws", type=_positive_int)
    parser.add_argument("--seed", type=_non_negative_int)
    parser.add_argument("--independent", action="store_true")
    parser.add_argument(
        "--artifact-directory",
        "--artifact-dir",
        "--artifact-root",
        dest="artifact_root",
        type=Path,
        default=DEFAULT_ARTIFACT_DIRECTORY,
    )
    parser.add_argument("--report-directory", type=Path, default=DEFAULT_REPORT_DIRECTORY)
    parser.add_argument("--config", type=Path, default=DEFAULT_SIMULATION_CONFIG_PATH)
    return parser


def build_calibrate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="na-simulate calibrate",
        description="Compare saved simulations with immutable realized contest standings.",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--season", type=_positive_int, required=True)
    parser.add_argument("--week", type=_positive_int, required=True)
    parser.add_argument("--snapshot-root", type=Path, default=DEFAULT_SNAPSHOT_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    try:
        if values and values[0] == "calibrate":
            arguments = build_calibrate_parser().parse_args(values[1:])
            with connect_database(arguments.database) as connection:
                apply_migrations(connection)
                results = calibrate_week(
                    connection,
                    season=arguments.season,
                    week=arguments.week,
                    snapshot_root=arguments.snapshot_root,
                )
            for calibration_result in results:
                print(calibration_result.comparison_path)
            return 0

        arguments = build_parser().parse_args(values)
        with connect_database(arguments.database) as connection:
            apply_migrations(connection)
            simulation_result = run_simulation(
                connection,
                decision_snapshot_id=arguments.decision_snapshot_id,
                contest_external_id=arguments.contest_external_id,
                artifact_root=arguments.artifact_root,
                report_directory=arguments.report_directory,
                config_path=arguments.config,
                draws=arguments.draws,
                seed=arguments.seed,
                independent=arguments.independent,
            )
        sys.stdout.buffer.write(simulation_result.report_bytes)
        return 0
    except (
        ContestSimulationError,
        FieldGenerationError,
        MigrationError,
        OSError,
        OutcomeSimulationError,
        SimulationCalibrationError,
        SimulationConfigError,
        SimulationRunError,
        StoreConfigurationError,
        ValueError,
        sqlite3.Error,
    ) as error:
        print(
            json.dumps(
                {"error": {"code": "simulation_failed", "message": str(error)}},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
