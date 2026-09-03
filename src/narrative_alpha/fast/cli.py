"""Command-line entry point for the intentionally small Sunday fast lane."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

import anthropic

from narrative_alpha.build import BuildError
from narrative_alpha.build_cli import DEFAULT_ARTIFACT_DIRECTORY
from narrative_alpha.fast.inactives import FastInactivesError, process_official_inactives
from narrative_alpha.fast.item import (
    DEFAULT_SOURCE_CATALOG_PATH,
    FastItemError,
    extract_fast_item,
)
from narrative_alpha.fast.rules import DEFAULT_FAST_LANE_RULES_PATH, FastLaneRuleError
from narrative_alpha.narrative import DEFAULT_PRICING_PATH, ExtractionError
from narrative_alpha.ops.config import DEFAULT_OPS_CONFIG_PATH, OpsConfigError, load_ops_config
from narrative_alpha.ops.schedule import KEYCHAIN_ACCOUNT_HINT
from narrative_alpha.ops.secrets import anthropic_api_key
from narrative_alpha.portfolio import OptimizerError
from narrative_alpha.replay import ReplayError
from narrative_alpha.store import MigrationError, StoreConfigurationError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="na-fast",
        description="Pre-approved Sunday actions: official inactives or one A-grade item.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_OPS_CONFIG_PATH)
    parser.add_argument("--database", type=Path, help="override the operator-config database")
    commands = parser.add_subparsers(dest="command", required=True)

    inactives = commands.add_parser(
        "inactives",
        help="record an official list and rebuild only affected frozen lineups",
    )
    inactives.add_argument("--season", type=_positive_int, required=True)
    inactives.add_argument("--week", type=_positive_int, required=True)
    inactives.add_argument("--site", choices=("dk", "fd"), required=True)
    source = inactives.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="UTF-8 text file, one inactive per line")
    source.add_argument(
        "--paste",
        action="store_true",
        help="read the official inactive list from standard input",
    )
    inactives.add_argument("--rules", type=Path, default=DEFAULT_FAST_LANE_RULES_PATH)
    inactives.add_argument(
        "--artifact-directory",
        "--artifact-root",
        dest="artifact_directory",
        type=Path,
        default=DEFAULT_ARTIFACT_DIRECTORY,
    )

    item = commands.add_parser(
        "item",
        help="synchronously extract one already-collected A-graded source item",
    )
    target = item.add_mutually_exclusive_group(required=True)
    target.add_argument("--url")
    target.add_argument("--source-item-id", type=_positive_int)
    item.add_argument("--source-catalog", type=Path, default=DEFAULT_SOURCE_CATALOG_PATH)
    item.add_argument("--pricing-config", type=Path, default=DEFAULT_PRICING_PATH)
    item.add_argument("--rules", type=Path, default=DEFAULT_FAST_LANE_RULES_PATH)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        config = load_ops_config(arguments.config)
        database = arguments.database or config.database
        if arguments.command == "inactives":
            text = sys.stdin.read() if arguments.paste else arguments.file.read_text("utf-8")
            inactive_report = process_official_inactives(
                database,
                season=arguments.season,
                week=arguments.week,
                site=arguments.site,
                text=text,
                snapshot_root=config.snapshot_root,
                artifact_directory=arguments.artifact_directory,
                rules_path=arguments.rules,
            )
            _print_inactives(inactive_report)
            return 0

        key = anthropic_api_key(config)
        if key is None:
            hint = KEYCHAIN_ACCOUNT_HINT.format(service=config.keychain_service)
            raise FastItemError(
                "ANTHROPIC_API_KEY is not set for this process and no Keychain item was "
                f"found. Add it with `{hint}`"
            )
        item_report = extract_fast_item(
            database,
            url=arguments.url,
            source_item_id=arguments.source_item_id,
            api_key=key,
            catalog_path=arguments.source_catalog,
            pricing_path=arguments.pricing_config,
            rules_path=arguments.rules,
            monthly_budget_nanos=config.monthly_llm_budget_nanos,
            budget_timezone=config.timezone,
        )
        _print_item(item_report)
        return 0
    except (
        anthropic.AnthropicError,
        BuildError,
        ExtractionError,
        FastInactivesError,
        FastItemError,
        FastLaneRuleError,
        MigrationError,
        OSError,
        OpsConfigError,
        OptimizerError,
        ReplayError,
        StoreConfigurationError,
        ValueError,
        sqlite3.Error,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _print_inactives(report: object) -> None:
    from narrative_alpha.fast.inactives import FastInactivesReport

    assert isinstance(report, FastInactivesReport)
    print(
        f"FAST INACTIVES — {report.season} week {report.week:02d} {report.site}\n"
        f"  rule       {report.rules_version} / {report.rule_id} permitted it\n"
        f"  inactives  {', '.join(player.name for player in report.inactive_players)}\n"
        f"  affected   {report.affected_lineups} of {report.portfolio_lineups} lineup(s); "
        "the rest are pinned unchanged in the new decision\n"
        f"  mean delta {report.mean_change:.3f} fantasy points\n"
    )
    for index, diff in enumerate(report.diffs, start=1):
        print(f"  diff {index}")
        print(f"    out  {', '.join(diff.out) or 'none'}")
        print(f"    in   {', '.join(diff.in_) or 'none'}")
    print(f"  decision   {report.decision_snapshot_id}")
    print(f"  upload CSV {report.upload_csv_path}")
    print(f"  elapsed    {report.elapsed_seconds:.3f}s")


def _print_item(report: object) -> None:
    from narrative_alpha.fast.item import FastItemReport

    assert isinstance(report, FastItemReport)
    print(
        f"FAST ITEM ALERT — item {report.source_item_id}, source {report.source_id} "
        f"(grade {report.source_grade})"
    )
    for claim in report.claims:
        players = ", ".join(claim.players) or "none resolved"
        print(f"  {claim.claim_id}: {claim.claim_type}/{claim.claim_dimension} — {players}")
    if not report.claims:
        print("  no claims stored" + ("; review flag pending" if report.review_flagged else ""))
    print(f"  players touched: {', '.join(report.players) or 'none'}")
    print(f"  elapsed: {report.elapsed_seconds:.3f}s")
    print("  no lineup was changed")


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
