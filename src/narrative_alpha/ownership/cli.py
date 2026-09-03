"""Operator CLI for fitting, evaluating, and materializing ownership scenarios."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from narrative_alpha.ownership.config import (
    DEFAULT_OWNERSHIP_CONFIG_PATH,
    GovernanceStatus,
    OwnershipConfigError,
    OwnershipModelConfig,
    SlateKind,
    load_ownership_config,
)
from narrative_alpha.ownership.data import (
    OwnershipDataError,
    available_fit_archetypes,
    canonical_site,
    load_latest_fit,
    load_scenario_inputs,
    load_training_data,
    persist_fit,
    validate_label_gate,
)
from narrative_alpha.ownership.evaluation import (
    OwnershipEvaluationError,
    OwnershipEvaluationReport,
    evaluate_forward_chaining,
    latest_evaluation_status,
    persist_evaluation,
    render_evaluation_report,
)
from narrative_alpha.ownership.model import OwnershipModelError, fit_ownership_model
from narrative_alpha.ownership.scenarios import (
    OwnershipScenarioError,
    build_scenarios,
    persist_scenarios,
)
from narrative_alpha.store import (
    MigrationError,
    StoreConfigurationError,
    apply_migrations,
    connect_database,
)

DEFAULT_DATABASE_PATH = Path("data/db/narrative_alpha.sqlite3")
DEFAULT_REPORT_DIRECTORY = Path("data/reports")
_ARCHETYPES = ("cash", "single_entry", "3max", "20max", "mass_multi_entry", "showdown")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="na-ownership",
        description="Fit and evaluate the bounded ownership-offset model.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    fit_parser = subparsers.add_parser("fit", help="fit one site/archetype cohort")
    _shared_model_arguments(fit_parser)

    evaluate_parser = subparsers.add_parser(
        "evaluate", help="run expanding-window weekly evaluation"
    )
    _shared_model_arguments(evaluate_parser)
    evaluate_parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIRECTORY)

    scenarios_parser = subparsers.add_parser(
        "scenarios", help="materialize capped and roster-calibrated player scenarios"
    )
    scenarios_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    scenarios_parser.add_argument(
        "--config", type=Path, default=DEFAULT_OWNERSHIP_CONFIG_PATH
    )
    scenarios_parser.add_argument("--decision-snapshot", required=True)
    scenarios_parser.add_argument("--archetype", choices=_ARCHETYPES)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        config = load_ownership_config(arguments.config)
        now = datetime.now(UTC)
        with connect_database(arguments.database) as connection:
            apply_migrations(connection)
            if arguments.command == "fit":
                print(
                    json.dumps(
                        _fit(connection, arguments, config, now), indent=2, sort_keys=True
                    )
                )
            elif arguments.command == "evaluate":
                report = _evaluate(connection, arguments, config, now)
                print(render_evaluation_report(report), end="")
                print(f"report_path={report.report_path}")
            elif arguments.command == "scenarios":
                print(
                    json.dumps(
                        _scenarios(connection, arguments, config, now),
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:  # pragma: no cover - argparse constrains this
                raise AssertionError(f"unhandled command {arguments.command!r}")
    except (
        MigrationError,
        OSError,
        OwnershipConfigError,
        OwnershipDataError,
        OwnershipEvaluationError,
        OwnershipModelError,
        OwnershipScenarioError,
        sqlite3.Error,
        StoreConfigurationError,
        ValueError,
    ) as error:
        print(
            json.dumps(
                {"error": {"code": "ownership_failed", "message": str(error)}},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    return 0


def _fit(
    connection: sqlite3.Connection,
    arguments: argparse.Namespace,
    config: OwnershipModelConfig,
    now: datetime,
) -> dict[str, object]:
    site = canonical_site(str(arguments.site))
    gate = validate_label_gate(
        connection,
        site=site,
        contest_archetype=str(arguments.archetype),
        minimum_weeks=config.evaluation.minimum_weeks,
        as_of=now,
    )
    data = load_training_data(
        connection,
        site=site,
        contest_archetype=str(arguments.archetype),
        feature_version=config.feature_version,
        as_of=now,
    )
    complete_weeks = {(row.season, row.week) for row in data.rows}
    if len(complete_weeks) < config.evaluation.minimum_weeks:
        raise OwnershipDataError(
            f"found {gate.distinct_weeks} labeled week(s), but only {len(complete_weeks)} "
            "have complete point-in-time feature and baseline rows; "
            f"missing features={data.missing_feature_rows}, baselines={data.missing_baseline_rows}"
        )
    roles = ("captain", "flex") if arguments.archetype == "showdown" else ("classic",)
    model = fit_ownership_model(
        data.rows,
        config=config,
        contest_archetype=str(arguments.archetype),
        site=site,
        roles=roles,
    )
    stored = persist_fit(connection, model, data, config=config, fitted_at=now)
    return {
        "run_id": stored.run_id,
        "site": site,
        "contest_archetype": stored.contest_archetype,
        "model_version": stored.model_version,
        "feature_version": stored.feature_version,
        "config_sha256": stored.config_sha256,
        "training_rows": stored.training_rows,
        "training_weeks": len(stored.training_weeks),
        "missing_feature_rows": data.missing_feature_rows,
        "missing_baseline_rows": data.missing_baseline_rows,
        "missing_rows": [
            {
                "season": row.season,
                "week": row.week,
                "slate_id": row.slate_id,
                "player_id": row.player_id,
                "role": row.role,
                "external_contest_id": row.external_contest_id,
                "missing_feature": row.missing_feature,
                "missing_baseline": row.missing_baseline,
            }
            for row in data.missing
        ],
        "coefficients": stored.coefficients,
    }


def _evaluate(
    connection: sqlite3.Connection,
    arguments: argparse.Namespace,
    config: OwnershipModelConfig,
    now: datetime,
) -> OwnershipEvaluationReport:
    site = canonical_site(str(arguments.site))
    gate = validate_label_gate(
        connection,
        site=site,
        contest_archetype=str(arguments.archetype),
        minimum_weeks=config.evaluation.minimum_weeks,
        as_of=now,
    )
    data = load_training_data(
        connection,
        site=site,
        contest_archetype=str(arguments.archetype),
        feature_version=config.feature_version,
        as_of=now,
    )
    complete_weeks = {(row.season, row.week) for row in data.rows}
    if len(complete_weeks) < config.evaluation.minimum_weeks:
        raise OwnershipDataError(
            f"found {gate.distinct_weeks} labeled week(s), but only {len(complete_weeks)} "
            "have complete point-in-time feature and baseline rows; "
            f"missing features={data.missing_feature_rows}, baselines={data.missing_baseline_rows}"
        )
    report = evaluate_forward_chaining(
        data,
        config=config,
        site=site,
        contest_archetype=str(arguments.archetype),
        evaluated_at=now,
    )
    return persist_evaluation(connection, report, report_directory=Path(arguments.report_dir))


def _scenarios(
    connection: sqlite3.Connection,
    arguments: argparse.Namespace,
    config: OwnershipModelConfig,
    now: datetime,
) -> dict[str, object]:
    identity = connection.execute(
        """
        SELECT s.site
        FROM decision_snapshots AS ds
        JOIN slates AS s ON s.slate_id = ds.slate_id
        WHERE ds.decision_snapshot_id = ?
        """,
        (arguments.decision_snapshot,),
    ).fetchone()
    if identity is None:
        raise OwnershipDataError(f"unknown decision snapshot {arguments.decision_snapshot!r}")
    site = str(identity["site"])
    archetype = arguments.archetype
    if archetype is None:
        available = available_fit_archetypes(connection, site=site, config=config, as_of=now)
        if len(available) != 1:
            choices = ", ".join(available) or "none"
            raise OwnershipDataError(
                "--archetype is required unless exactly one matching fit exists; "
                f"matching archetypes: {choices}"
            )
        archetype = available[0]
    decision_at, snapshot_site, slate_type, inputs = load_scenario_inputs(
        connection,
        decision_snapshot_id=str(arguments.decision_snapshot),
        contest_archetype=str(archetype),
        feature_version=config.feature_version,
    )
    if snapshot_site != site:
        raise OwnershipDataError("decision snapshot site changed during bounded selection")
    model = load_latest_fit(
        connection,
        site=site,
        contest_archetype=str(archetype),
        config=config,
        as_of=now,
    )
    evaluation = latest_evaluation_status(
        connection,
        site=site,
        contest_archetype=str(archetype),
        feature_version=config.feature_version,
        config_sha256=config.config_sha256,
        as_of=now,
    )
    if evaluation is not None and not evaluation[1]:
        raise OwnershipScenarioError(
            "latest out-of-week evaluation did not beat the untouched vendor baseline; "
            "scenario application refused"
        )
    status: GovernanceStatus = "UNVALIDATED" if evaluation is None else "TESTING"
    scenarios = build_scenarios(
        model,
        inputs,
        config=config,
        slate_kind=cast(SlateKind, slate_type),
        status=status,
    )
    stored = persist_scenarios(connection, scenarios, generated_at=now)
    return {
        "run_id": stored.run_id,
        "decision_snapshot_id": stored.decision_snapshot_id,
        "decision_at": decision_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "site": stored.site,
        "contest_archetype": stored.contest_archetype,
        "governance_status": stored.governance_status,
        "scenario_rows": len(stored.scenarios),
        "model_run_id": model.run_id,
        "config_sha256": config.config_sha256,
        "feature_version": config.feature_version,
        "scenarios": [
            {
                "ownership_scenario_id": row.ownership_scenario_id,
                "player_id": row.player_id,
                "role": row.role,
                "position": row.position,
                "baseline_ownership": row.baseline_ownership,
                "ownership_p10": row.ownership_p10,
                "ownership_p50": row.ownership_p50,
                "ownership_p90": row.ownership_p90,
                "delta_p50": row.delta_p50,
                "prob_delta_positive": row.prob_delta_positive,
                "status_multiplier": row.status_multiplier,
                "applied_ownership": row.applied_ownership,
                "calibrated_to_roster_totals": row.calibrated_to_roster_totals,
            }
            for row in stored.scenarios
        ],
    }


def _shared_model_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--config", type=Path, default=DEFAULT_OWNERSHIP_CONFIG_PATH)
    parser.add_argument("--archetype", choices=_ARCHETYPES, required=True)
    parser.add_argument("--site", choices=("dk", "fd"), required=True)


if __name__ == "__main__":
    raise SystemExit(main())
