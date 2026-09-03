"""The Tuesday results lane: freeze standings, ingest labels, replay, and evaluate."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from narrative_alpha.evaluation import (
    BaselineEvaluationReport,
    BaselineReportError,
    build_baseline_report,
    render_baseline_report,
)
from narrative_alpha.ingest.results import (
    ContestArchetype,
    ContestLoadReport,
    ContestMetadata,
    ContestStandingsError,
    load_contest_standings,
)
from narrative_alpha.ingest.salaries import SalarySite, SalarySlateType
from narrative_alpha.ingest.slates import SlateIngestError, normalize_site
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.ops.config import OpsConfig
from narrative_alpha.ops.runs import (
    OpsStep,
    OpsStepStatus,
    StepFailure,
    StepOutcome,
    StepRecorder,
)
from narrative_alpha.portfolio import OptimizerError, PydfsAdapter
from narrative_alpha.replay import ReplayError, ReplayResult, replay_decision
from narrative_alpha.report_cli import (
    DEFAULT_REPORT_DIRECTORY,
    write_report_atomic,
)
from narrative_alpha.snapshots import (
    MANIFEST_FILENAME,
    CaptureKind,
    capture_files,
    load_manifest,
    sha256_file,
)
from narrative_alpha.snapshots.core import snapshot_week_path
from narrative_alpha.store import MigrationError, StoreConfigurationError

CaptureFiles = Callable[..., Path]
LoadStandings = Callable[..., ContestLoadReport]
ReplayDecision = Callable[..., ReplayResult]
BuildBaseline = Callable[..., BaselineEvaluationReport]
RenderBaseline = Callable[[BaselineEvaluationReport], str]
WriteReport = Callable[[Path, str], None]


@dataclass(frozen=True)
class ResultsDependencies:
    """Existing library seams used by the lane, injectable for deterministic tests."""

    capture_files: CaptureFiles = capture_files
    load_contest_standings: LoadStandings = load_contest_standings
    replay_decision: ReplayDecision = replay_decision
    build_baseline_report: BuildBaseline = build_baseline_report
    render_baseline_report: RenderBaseline = render_baseline_report
    write_report: WriteReport = write_report_atomic


DEFAULT_RESULTS_DEPENDENCIES = ResultsDependencies()

RESULTS_STEP_ERRORS: tuple[type[BaseException], ...] = (
    BaselineReportError,
    ContestStandingsError,
    MigrationError,
    OSError,
    OptimizerError,
    ReplayError,
    SlateIngestError,
    StoreConfigurationError,
    ValueError,
    sqlite3.Error,
)


@dataclass(frozen=True)
class ResultsReport:
    """Everything one ``na-ops results`` invocation did."""

    results_run_id: str
    season: int
    week: int
    site: str
    evaluation_as_of: datetime
    started_at: datetime
    finished_at: datetime
    steps: tuple[StepOutcome, ...] = field(default_factory=tuple)
    report_path: Path | None = None

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)

    def step(self, name: OpsStep) -> StepOutcome | None:
        return next((outcome for outcome in self.steps if outcome.step == name), None)


@dataclass(frozen=True)
class _CapturedStanding:
    requested_path: Path
    captured_path: Path
    sha256: str
    observed_at: datetime
    newly_captured: bool


@dataclass(frozen=True)
class _Decision:
    decision_snapshot_id: str
    decision_at: datetime


def run_results(
    connection: sqlite3.Connection,
    *,
    config: OpsConfig,
    season: int,
    week: int,
    site: str,
    standings_files: Sequence[Path],
    artifact_directory: Path,
    report_directory: Path = DEFAULT_REPORT_DIRECTORY,
    dependencies: ResultsDependencies = DEFAULT_RESULTS_DEPENDENCIES,
    now: datetime | None = None,
) -> ResultsReport:
    """Run all five Tuesday steps while preserving every completed step in history."""

    started_at = ensure_utc(now or datetime.now(UTC))
    canonical_site = normalize_site(site).value
    inputs = tuple(Path(path) for path in standings_files)
    if not inputs:
        raise ValueError("at least one standings file is required")
    results_run_id = f"results-{uuid4().hex}"
    recorder = StepRecorder(
        connection,
        run_id=results_run_id,
        step_errors=RESULTS_STEP_ERRORS,
        base_summary={
            "evaluation_as_of": utc_timestamp(started_at),
            "season": season,
            "week": week,
            "site": canonical_site,
        },
    )

    captured: list[_CapturedStanding] = []
    capture = recorder.run(
        "results_capture",
        lambda: _capture(
            dependencies,
            config=config,
            season=season,
            week=week,
            site=canonical_site,
            files=inputs,
            observed_at=started_at,
            into=captured,
        ),
    )

    if capture.status == "failed":
        ingest = recorder.skip(
            "results_ingest",
            "the standings capture failed, so no mutable source file will be parsed; fix "
            "results_capture and rerun `na-ops results`",
        )
    else:
        ingest = recorder.run(
            "results_ingest",
            lambda: _ingest(
                dependencies,
                connection,
                season=season,
                week=week,
                site=canonical_site,
                captured=captured,
                ingested_at=started_at,
            ),
        )

    decisions = _decisions(connection, season=season, week=week, site=canonical_site)
    replay = recorder.run(
        "results_replay",
        lambda: _replay(
            dependencies,
            connection,
            decisions=decisions,
            artifact_directory=artifact_directory,
        ),
    )

    report_paths: list[Path] = []
    if ingest.status != "succeeded":
        recorder.skip(
            "results_report",
            "the label ingest did not succeed, so a baseline report would describe an "
            "incomplete result set; fix results_ingest and rerun `na-ops results`",
        )
    elif not decisions:
        recorder.skip(
            "results_report",
            f"no {canonical_site} decision snapshot is frozen for {season} week {week:02d}; "
            "run `na-ops slate` before evaluating results",
        )
    elif replay.status != "succeeded":
        recorder.skip(
            "results_report",
            "the frozen decision did not replay successfully, so no evaluation report "
            "will be written; fix results_replay and rerun `na-ops results`",
        )
    else:
        recorder.run(
            "results_report",
            lambda: _report(
                dependencies,
                connection,
                decision=decisions[-1],
                evaluation_as_of=started_at,
                season=season,
                week=week,
                site=canonical_site,
                report_directory=report_directory,
                into=report_paths,
            ),
        )

    recorder.run("results_labels", lambda: _labels(connection))
    return ResultsReport(
        results_run_id=results_run_id,
        season=season,
        week=week,
        site=canonical_site,
        evaluation_as_of=started_at,
        started_at=started_at,
        finished_at=ensure_utc(datetime.now(UTC)),
        steps=tuple(recorder.outcomes),
        report_path=report_paths[0] if report_paths else None,
    )


def _capture(
    dependencies: ResultsDependencies,
    *,
    config: OpsConfig,
    season: int,
    week: int,
    site: str,
    files: tuple[Path, ...],
    observed_at: datetime,
    into: list[_CapturedStanding],
) -> tuple[OpsStepStatus, dict[str, object], str | None]:
    existing = _existing_standings(config.snapshot_root, season, week)
    hashes: list[tuple[Path, str]] = [(path, sha256_file(path)) for path in files]
    new_by_hash: dict[str, Path] = {}
    for path, digest in hashes:
        if digest not in existing:
            new_by_hash.setdefault(digest, path)

    new_capture: Path | None = None
    if new_by_hash:
        new_capture = dependencies.capture_files(
            config.snapshot_root,
            season,
            week,
            CaptureKind.STANDINGS,
            site,
            tuple(new_by_hash.values()),
            observed_at=observed_at,
        )
        manifest = load_manifest(new_capture / MANIFEST_FILENAME)
        for record in manifest.files:
            original = new_by_hash.get(record.sha256)
            if original is None:
                raise ValueError(
                    "a standings file changed while it was being captured; discard this "
                    "capture, restore the settled export, and rerun `na-ops results`"
                )
            existing[record.sha256] = _CapturedStanding(
                requested_path=original,
                captured_path=new_capture / record.path,
                sha256=record.sha256,
                observed_at=record.observed_at,
                newly_captured=True,
            )

    seen: set[str] = set()
    for requested, digest in hashes:
        seen.add(digest)
        frozen = existing[digest]
        into.append(
            _CapturedStanding(
                requested_path=requested,
                captured_path=frozen.captured_path,
                sha256=digest,
                observed_at=frozen.observed_at,
                newly_captured=digest in new_by_hash,
            )
        )
    return (
        "succeeded",
        {
            "files_seen": len(files),
            "unique_files": len(seen),
            "files_new": len(new_by_hash),
            "files_already_captured": len(files) - len(new_by_hash),
            "new_capture_path": None if new_capture is None else str(new_capture),
            "files": [
                {
                    "requested_path": str(item.requested_path),
                    "captured_path": str(item.captured_path),
                    "sha256": item.sha256,
                    "observed_at": utc_timestamp(item.observed_at),
                    "new": item.newly_captured,
                }
                for item in into
            ],
        },
        None,
    )


def _existing_standings(
    snapshot_root: Path, season: int, week: int
) -> dict[str, _CapturedStanding]:
    found: dict[str, _CapturedStanding] = {}
    week_path = snapshot_week_path(snapshot_root, season, week)
    if not week_path.is_dir():
        return found
    for capture_path in sorted(path for path in week_path.iterdir() if path.is_dir()):
        manifest_path = capture_path / MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        manifest = load_manifest(manifest_path)
        for record in manifest.files:
            if record.kind is not CaptureKind.STANDINGS or record.sha256 in found:
                continue
            captured_path = capture_path / record.path
            if sha256_file(captured_path) != record.sha256:
                raise ValueError(
                    f"captured standings file no longer matches its manifest: "
                    f"{captured_path}; restore the immutable capture before running results"
                )
            found[record.sha256] = _CapturedStanding(
                requested_path=capture_path / record.path,
                captured_path=captured_path,
                sha256=record.sha256,
                observed_at=record.observed_at,
                newly_captured=False,
            )
    return found


def _ingest(
    dependencies: ResultsDependencies,
    connection: sqlite3.Connection,
    *,
    season: int,
    week: int,
    site: str,
    captured: list[_CapturedStanding],
    ingested_at: datetime,
) -> tuple[OpsStepStatus, dict[str, object], str | None]:
    metadata = [
        _contest_metadata(
            connection,
            item=item,
            season=season,
            week=week,
            site=site,
            as_of=ingested_at,
        )
        for item in captured
    ]
    contest_ids = [contest.contest_id for contest in metadata]
    duplicates = sorted(
        contest_id for contest_id in set(contest_ids) if contest_ids.count(contest_id) > 1
    )
    if duplicates:
        raise ContestStandingsError(
            "more than one standings file names the same contest population: "
            + ", ".join(duplicates)
            + "; pass exactly one settled export per contest"
        )
    reports: list[tuple[_CapturedStanding, ContestMetadata, ContestLoadReport]] = []
    for item, contest in zip(captured, metadata, strict=True):
        report = dependencies.load_contest_standings(
            connection,
            item.captured_path,
            contest,
            ingested_at=ingested_at,
        )
        connection.commit()
        reports.append((item, contest, report))

    unresolved = sum(r.unresolved_rows for _, _, r in reports)
    rejected = sum(r.rejected_rows for _, _, r in reports)
    summary: dict[str, object] = {
        "files_loaded": len(reports),
        "contests": [contest.contest_id for _, contest, _ in reports],
        "ownership_rows_inserted": sum(r.ownership_rows_inserted for _, _, r in reports),
        "result_rows_inserted": sum(r.result_rows_inserted for _, _, r in reports),
        "duplicate_rows": sum(r.duplicate_rows for _, _, r in reports),
        "unresolved_rows": unresolved,
        "rejected_rows": rejected,
        "source_observed_at": [utc_timestamp(item.observed_at) for item, _, _ in reports],
    }
    reasons: list[str] = []
    errors = [error for _, _, report in reports for error in report.errors]
    if unresolved:
        reasons.append(f"{unresolved} standings row(s) did not resolve: `na-crosswalk resolve`")
    if rejected:
        reasons.append(f"{rejected} standings row(s) were rejected by the parser")
    if errors:
        reasons.append("; ".join(errors[:5]))
    if reasons:
        raise StepFailure(" | ".join(reasons), summary)
    return "succeeded", summary, None


def _contest_metadata(
    connection: sqlite3.Connection,
    *,
    item: _CapturedStanding,
    season: int,
    week: int,
    site: str,
    as_of: datetime,
) -> ContestMetadata:
    stamp = utc_timestamp(as_of)
    rows = connection.execute(
        """
        WITH current_contests AS (
            SELECT c.*, s.slate_type,
                   row_number() OVER (
                       PARTITION BY c.site, c.external_contest_id
                       ORDER BY c.observed_at DESC, c.contest_id DESC
                   ) AS version_rank
            FROM contests AS c
            JOIN slates AS s ON s.slate_id = c.slate_id
            WHERE s.season = ? AND s.week = ? AND c.site = ?
              AND rtrim(c.observed_at, 'Z') <= rtrim(?, 'Z')
              AND rtrim(c.valid_from, 'Z') <= rtrim(?, 'Z')
              AND (c.valid_to IS NULL OR rtrim(c.valid_to, 'Z') > rtrim(?, 'Z'))
        )
        SELECT * FROM current_contests WHERE version_rank = 1
        ORDER BY external_contest_id
        """,
        (season, week, site, stamp, stamp, stamp),
    ).fetchall()
    matches = [
        row
        for row in rows
        if _filename_names(item.requested_path.name, str(row["external_contest_id"]))
    ]
    if not matches:
        known = ", ".join(str(row["external_contest_id"]) for row in rows) or "none"
        raise ContestStandingsError(
            f"standings file {item.requested_path.name!r} does not name a stored {site} "
            f"contest for {season} week {week:02d} (known external contest ids: {known}). "
            "Keep the site-exported contest id in the filename, then add missing metadata "
            "with `na-contest add` and rerun `na-ops results`"
        )
    if len(matches) > 1:
        names = ", ".join(str(row["external_contest_id"]) for row in matches)
        raise ContestStandingsError(
            f"standings filename {item.requested_path.name!r} matches more than one stored "
            f"contest ({names}); rename it so exactly one external contest id is present"
        )
    row = matches[0]
    return ContestMetadata(
        contest_id=str(row["external_contest_id"]),
        site=SalarySite(site),
        slate_id=int(row["slate_id"]),
        slate_type=SalarySlateType(str(row["slate_type"])),
        contest_archetype=ContestArchetype(str(row["archetype"])),
        entry_limit=int(row["entry_limit"]),
        entry_fee_cents=int(row["entry_fee_cents"]),
        observed_at=item.observed_at,
        expected_field_size=int(row["field_size"]),
        payout_curve_id=None if row["payout_curve_id"] is None else str(row["payout_curve_id"]),
    )


def _filename_names(filename: str, external_contest_id: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9]){re.escape(external_contest_id)}(?![A-Za-z0-9])"
    return re.search(pattern, filename, flags=re.IGNORECASE) is not None


def _decisions(
    connection: sqlite3.Connection, *, season: int, week: int, site: str
) -> tuple[_Decision, ...]:
    rows = connection.execute(
        """
        SELECT d.decision_snapshot_id, d.decision_at
        FROM decision_snapshots AS d
        JOIN slates AS s ON s.slate_id = d.slate_id
        WHERE s.season = ? AND s.week = ? AND s.site = ?
        ORDER BY rtrim(d.decision_at, 'Z'), d.decision_snapshot_id
        """,
        (season, week, site),
    ).fetchall()
    return tuple(
        _Decision(
            decision_snapshot_id=str(row["decision_snapshot_id"]),
            decision_at=ensure_utc(
                datetime.fromisoformat(str(row["decision_at"]).replace("Z", "+00:00"))
            ),
        )
        for row in rows
    )


def _replay(
    dependencies: ResultsDependencies,
    connection: sqlite3.Connection,
    *,
    decisions: tuple[_Decision, ...],
    artifact_directory: Path,
) -> tuple[OpsStepStatus, dict[str, object], str | None]:
    if not decisions:
        return (
            "skipped",
            {"decisions_seen": 0, "replays": []},
            "no decision snapshot exists for this week and site; run `na-ops slate` first",
        )
    replays: list[dict[str, object]] = []
    try:
        for decision in decisions:
            replay = dependencies.replay_decision(
                connection,
                decision_snapshot_id=decision.decision_snapshot_id,
                decision_at=decision.decision_at,
                artifact_root=artifact_directory,
                adapter=PydfsAdapter(),
            )
            replays.append(
                {
                    "decision_snapshot_id": decision.decision_snapshot_id,
                    "decision_at": utc_timestamp(decision.decision_at),
                    "expected_output_sha256": replay.report.expected_output_sha256,
                    "actual_output_sha256": replay.report.actual_output_sha256,
                    "output_matches": replay.report.output_matches,
                }
            )
    except (OptimizerError, ReplayError, OSError, ValueError) as error:
        raise StepFailure(
            f"decision replay failed: {type(error).__name__}: {error}",
            {"decisions_seen": len(decisions), "replays": replays},
        ) from error
    summary: dict[str, object] = {
        "decisions_seen": len(decisions),
        "decisions_verified": sum(bool(item["output_matches"]) for item in replays),
        "replays": replays,
    }
    mismatches = [item for item in replays if not item["output_matches"]]
    if mismatches:
        detail = "; ".join(
            f"{item['decision_snapshot_id']}: expected {item['expected_output_sha256']}, "
            f"rebuilt {item['actual_output_sha256']}"
            for item in mismatches
        )
        raise StepFailure(
            f"{len(mismatches)} frozen decision replay(s) were not byte-identical — {detail}",
            summary,
        )
    return "succeeded", summary, None


def _report(
    dependencies: ResultsDependencies,
    connection: sqlite3.Connection,
    *,
    decision: _Decision,
    evaluation_as_of: datetime,
    season: int,
    week: int,
    site: str,
    report_directory: Path,
    into: list[Path],
) -> tuple[OpsStepStatus, dict[str, object], str | None]:
    report = dependencies.build_baseline_report(
        connection,
        decision_snapshot_id=decision.decision_snapshot_id,
        decision_at=decision.decision_at,
        evaluation_as_of=evaluation_as_of,
    )
    rendered = dependencies.render_baseline_report(report)
    stamp = evaluation_as_of.strftime("%Y%m%dT%H%M%S%fZ")
    site_slug = "dk" if site == SalarySite.DRAFTKINGS.value else "fd"
    path = report_directory / str(season) / f"week_{week:02d}" / f"results-{site_slug}-{stamp}.txt"
    dependencies.write_report(path, rendered)
    into.append(path)
    return (
        "succeeded",
        {
            "report_path": str(path),
            "decision_snapshot_id": decision.decision_snapshot_id,
            "decision_at": utc_timestamp(decision.decision_at),
            "evaluation_as_of": utc_timestamp(evaluation_as_of),
        },
        None,
    )


@dataclass(frozen=True)
class LabelCohort:
    """One season/week/site/archetype label population, as the store holds it now."""

    season: int
    week: int
    site: str
    contest_archetype: str
    label_rows: int
    distinct_contests: int


def label_cohorts(connection: sqlite3.Connection) -> tuple[LabelCohort, ...]:
    """The label gate's rows — the one query the lane records and the screen shows."""

    rows = connection.execute(
        """
        SELECT s.season, s.week, ao.site, ao.contest_archetype,
               count(*) AS label_rows,
               count(DISTINCT ao.site || ':' || ao.external_contest_id) AS distinct_contests
        FROM actual_ownership AS ao
        JOIN slates AS s ON s.slate_id = ao.slate_id
        GROUP BY s.season, s.week, ao.site, ao.contest_archetype
        ORDER BY s.season, s.week, ao.site, ao.contest_archetype
        """
    ).fetchall()
    return tuple(
        LabelCohort(
            season=int(row["season"]),
            week=int(row["week"]),
            site=str(row["site"]),
            contest_archetype=str(row["contest_archetype"]),
            label_rows=int(row["label_rows"]),
            distinct_contests=int(row["distinct_contests"]),
        )
        for row in rows
    )


def weeks_with_labels(cohorts: tuple[LabelCohort, ...]) -> int:
    return len({(cohort.season, cohort.week) for cohort in cohorts})


def label_summary(connection: sqlite3.Connection) -> dict[str, object]:
    """Return the status/report shape shared by the lane and the operator screen."""

    cohorts = label_cohorts(connection)
    return {
        "weeks_with_labels": weeks_with_labels(cohorts),
        "by_week_and_archetype": [
            {
                "season": cohort.season,
                "week": cohort.week,
                "site": cohort.site,
                "contest_archetype": cohort.contest_archetype,
                "label_rows": cohort.label_rows,
                "distinct_contests": cohort.distinct_contests,
            }
            for cohort in cohorts
        ],
    }


def _labels(
    connection: sqlite3.Connection,
) -> tuple[OpsStepStatus, dict[str, object], str | None]:
    return "succeeded", label_summary(connection), None
