"""CLI for restart-safe slate memos and baseline evaluation reports."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal

from narrative_alpha.build import BuildResult
from narrative_alpha.build_cli import DEFAULT_ARTIFACT_DIRECTORY, DEFAULT_DATABASE_PATH
from narrative_alpha.evaluation import (
    BaselineEvaluationReport,
    BaselineReportError,
    BaselineThresholds,
    build_baseline_report,
    render_baseline_report,
)
from narrative_alpha.interface import (
    SlateMemo,
    SlateMemoError,
    build_slate_memo,
    render_slate_memo,
)
from narrative_alpha.portfolio import OptimizerError, PydfsAdapter
from narrative_alpha.replay import (
    PointInTimeSession,
    ReplayArtifactError,
    ReplayError,
    ReplayResult,
    replay_decision,
)
from narrative_alpha.store import (
    DecisionManifestHash,
    DecisionSnapshotRow,
    MigrationError,
    StoreConfigurationError,
    apply_migrations,
    canonical_manifest_hashes,
    connect_database,
)

DEFAULT_REPORT_DIRECTORY = Path("data/reports")
BASELINE_NOT_REQUESTED_NOTICE = (
    "baseline evaluation not requested — no --evaluation-as-of result-label cutoff "
    "was supplied, so no projection error was measured"
)


class ReportCliError(RuntimeError):
    """Raised when persisted decision state cannot produce a report bundle."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="na-report",
        description="Render a verified slate memo and point-in-time baseline report.",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument(
        "--decision-snapshot-id",
        "--decision-snapshot",
        dest="decision_snapshot_id",
        required=True,
    )
    parser.add_argument("--decision-at", type=_timestamp, required=True)
    parser.add_argument(
        "--evaluation-as-of",
        type=_timestamp,
        help=(
            "explicit result-label cutoff; omit before kickoff to render the "
            "memo alone, since no result labels exist yet"
        ),
    )
    parser.add_argument(
        "--artifact-directory",
        "--artifact-dir",
        "--artifact-root",
        dest="artifact_root",
        type=Path,
        default=DEFAULT_ARTIFACT_DIRECTORY,
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--contest-id", type=int)
    parser.add_argument("--minimum-sample-size", type=int, default=5)
    parser.add_argument("--pit-bins", type=int, default=10)
    parser.add_argument("--pit-random-seed", type=int, default=20260901)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    output_path = arguments.output or _default_output(arguments.decision_snapshot_id)
    try:
        thresholds = BaselineThresholds(
            minimum_sample_size=arguments.minimum_sample_size,
            pit_bins=arguments.pit_bins,
            pit_random_seed=arguments.pit_random_seed,
        )
        with connect_database(arguments.database) as connection:
            apply_migrations(connection)
            build_result = _load_build_result(
                connection,
                decision_snapshot_id=arguments.decision_snapshot_id,
                decision_at=arguments.decision_at,
                artifact_root=arguments.artifact_root,
            )
            memo = build_slate_memo(
                build_result,
                connection,
                contest_id=arguments.contest_id,
            )
            baseline = (
                None
                if arguments.evaluation_as_of is None
                else build_baseline_report(
                    connection,
                    decision_snapshot_id=arguments.decision_snapshot_id,
                    decision_at=arguments.decision_at,
                    evaluation_as_of=arguments.evaluation_as_of,
                    thresholds=thresholds,
                )
            )
        bundle = render_report_bundle(memo, baseline)
        _write_atomic(output_path, bundle)
    except (
        BaselineReportError,
        MigrationError,
        OSError,
        OptimizerError,
        ReplayError,
        ReportCliError,
        SlateMemoError,
        StoreConfigurationError,
        ValueError,
        sqlite3.Error,
    ) as error:
        print(
            json.dumps(
                {"error": {"code": "report_failed", "message": str(error)}},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    sys.stdout.write(bundle)
    return 0


def render_report_bundle(
    memo: SlateMemo,
    baseline: BaselineEvaluationReport | None,
) -> str:
    """Combine the two independently renderable artifacts with a stable delimiter.

    A ``None`` baseline means no result-label cutoff was supplied — the pre-kickoff case.
    That is stated in the bundle rather than left as a silent absence (rule 1.5.7); it is
    never a claim that the baseline was computed and found empty.
    """

    body = (
        BASELINE_NOT_REQUESTED_NOTICE + "\n"
        if baseline is None
        else render_baseline_report(baseline)
    )
    return (
        "NARRATIVE ALPHA REPORT BUNDLE\n\n"
        + render_slate_memo(memo)
        + "\nBASELINE EVALUATION ARTIFACT\n"
        + body
    )


def _load_build_result(
    connection: sqlite3.Connection,
    *,
    decision_snapshot_id: str,
    decision_at: datetime,
    artifact_root: Path,
) -> BuildResult:
    root = artifact_root.resolve()
    session = PointInTimeSession(connection)
    snapshot = session.decision_snapshot(decision_snapshot_id, as_of=decision_at)
    replay = replay_decision(
        connection,
        decision_snapshot_id=decision_snapshot_id,
        decision_at=decision_at,
        artifact_root=root,
        adapter=PydfsAdapter(),
    )
    if not replay.report.output_matches:
        raise ReportCliError(
            "verified replay output does not match the frozen generated lineup artifact"
        )
    request_artifact = _single_artifact(snapshot, "optimizer_request")
    lineups_artifact = _single_artifact(snapshot, "generated_lineups")
    request_path = _safe_artifact_path(root, request_artifact)
    lineups_path = _safe_artifact_path(root, lineups_artifact)
    if replay.output_bytes != lineups_path.read_bytes():
        raise ReportCliError("verified replay bytes differ from generated_lineups.csv")
    artifact_directory = request_path.parent
    if lineups_path.parent != artifact_directory:
        raise ReportCliError("decision request and lineup artifacts are in different directories")
    manifest_path = artifact_directory / "manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as error:
        raise ReportCliError(f"cannot read decision manifest: {error}") from error
    expected_manifest = canonical_manifest_hashes(snapshot.manifest_hashes_json).encode("utf-8")
    if manifest_bytes != expected_manifest:
        raise ReportCliError("decision manifest file differs from the stored hash-set")
    return _reconstructed_build_result(
        snapshot=snapshot,
        replay=replay,
        artifact_root=root,
        artifact_directory=artifact_directory,
        request_path=request_path,
        lineups_path=lineups_path,
        manifest_path=manifest_path,
    )


def _reconstructed_build_result(
    *,
    snapshot: DecisionSnapshotRow,
    replay: ReplayResult,
    artifact_root: Path,
    artifact_directory: Path,
    request_path: Path,
    lineups_path: Path,
    manifest_path: Path,
) -> BuildResult:
    return BuildResult(
        snapshot=snapshot,
        request=replay.request,
        lineups=replay.lineups,
        replay=replay,
        artifact_root=artifact_root,
        artifact_directory=artifact_directory,
        optimizer_request_path=request_path,
        generated_lineups_path=lineups_path,
        manifest_path=manifest_path,
    )


def _single_artifact(
    snapshot: DecisionSnapshotRow,
    artifact_kind: Literal["optimizer_request", "generated_lineups"],
) -> DecisionManifestHash:
    values = tuple(
        item for item in snapshot.manifest_hashes_json if item.artifact_kind == artifact_kind
    )
    if len(values) != 1:
        raise ReplayArtifactError(
            f"decision manifest must contain exactly one {artifact_kind} artifact"
        )
    return values[0]


def _safe_artifact_path(root: Path, artifact: DecisionManifestHash) -> Path:
    candidate = (root / artifact.path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ReplayArtifactError(
            f"artifact path escapes artifact root: {artifact.path}"
        ) from error
    return candidate


def _write_atomic(path: Path, content: str) -> None:
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _default_output(decision_snapshot_id: str) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", decision_snapshot_id).strip("._")
    if not safe_name:
        safe_name = "decision-report"
    return DEFAULT_REPORT_DIRECTORY / f"{safe_name}.txt"


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
