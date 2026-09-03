"""The Saturday/Sunday slate lane: one command from captured files to an upload CSV.

`na-ops batch` is one command for the week; this is one command for the slate. It ingests
the week's captures, builds Stage 2 episodes and Stage 3 features at the decision instant,
freezes the decision, and writes the memo — passing that single instant to every stage so
the whole run replays from one cutoff.

The lane owns orchestration and two fail-closed gates only. Ingestion, the episode and
feature builds, the decision build, and the memo are the existing library functions,
called directly and injectable so tests need no network and no optimizer.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from narrative_alpha.build import (
    BuildDuplicateError,
    BuildError,
    BuildResult,
    build_decision,
)
from narrative_alpha.build_cli import DEFAULT_ARTIFACT_DIRECTORY
from narrative_alpha.candidate_selection import CandidateSelectionError
from narrative_alpha.identity import CrosswalkError, PlayerCrosswalk
from narrative_alpha.ingest.projections import (
    ProjectionIngestError,
    ProjectionLoadReport,
    SourceFormat,
    SourceFormatRegistry,
    load_projection_capture,
)
from narrative_alpha.ingest.slates import (
    SlateIngestError,
    SlateLoadReport,
    SlateSummary,
    list_slates,
    load_salary_capture,
    newest_salary_capture,
    normalize_site,
)
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.interface import SlateMemo, SlateMemoError, build_slate_memo
from narrative_alpha.narrative.episodes import (
    EpisodeError,
    build_episodes,
)
from narrative_alpha.narrative.features import (
    DEFAULT_HEAT_CONFIG_PATH,
    FeatureBuildReport,
    FeatureError,
    build_features,
)
from narrative_alpha.ops.config import OpsConfig
from narrative_alpha.ops.episodes import EpisodeStep, build_episode_snapshot
from narrative_alpha.ops.runs import (
    OpsStep,
    OpsStepStatus,
    StepFailure,
    StepOutcome,
    StepRecorder,
)
from narrative_alpha.portfolio import ContestArchetype, OptimizerError
from narrative_alpha.replay import ReplayError
from narrative_alpha.report_cli import (
    DEFAULT_REPORT_DIRECTORY,
    default_report_path,
    load_build_result,
    render_report_bundle,
    write_report_atomic,
)
from narrative_alpha.snapshots import MANIFEST_FILENAME, CaptureKind, load_manifest
from narrative_alpha.snapshots.core import snapshot_week_path
from narrative_alpha.snapshots.models import SnapshotManifest
from narrative_alpha.store import MigrationError, StoreConfigurationError

# The two capture kinds the slate lane loads into a slate; salaries are loaded by their
# own step, and odds/weather/news belong to other lanes.
VENDOR_KINDS = frozenset({CaptureKind.PROJECTIONS, CaptureKind.OWNERSHIP})

# How many by-hand commands a refusal prints before it says "+N more".
MAX_LISTED_ACTIONS = 10

NewestCapture = Callable[..., Path]
SalaryStep = Callable[..., SlateLoadReport]
VendorStep = Callable[..., ProjectionLoadReport]
FeatureStep = Callable[..., FeatureBuildReport]
DecisionStep = Callable[..., BuildResult]
MemoStep = Callable[..., SlateMemo]


@dataclass(frozen=True)
class SlateDependencies:
    """The library calls the lane makes, injectable so tests need no network.

    Defaults are the production functions; nothing here re-implements them.
    ``source_formats`` is the set of registered vendor adapters — empty today, because no
    vendor adapter has landed yet. A capture from an unregistered vendor is a recorded
    failure naming the vendor, never a guessed schema.
    """

    newest_salary_capture: NewestCapture = newest_salary_capture
    load_salary_capture: SalaryStep = load_salary_capture
    load_projection_capture: VendorStep = load_projection_capture
    build_episodes: EpisodeStep = build_episodes
    build_features: FeatureStep = build_features
    build_decision: DecisionStep = build_decision
    build_slate_memo: MemoStep = build_slate_memo
    source_formats: tuple[SourceFormat, ...] = ()


DEFAULT_SLATE_DEPENDENCIES = SlateDependencies()

# Errors a step may raise that describe a bad slate, not a broken program.
SLATE_STEP_ERRORS: tuple[type[BaseException], ...] = (
    BuildError,
    CandidateSelectionError,
    CrosswalkError,
    EpisodeError,
    FeatureError,
    MigrationError,
    OSError,
    OptimizerError,
    ProjectionIngestError,
    ReplayError,
    SlateIngestError,
    SlateMemoError,
    StoreConfigurationError,
    ValueError,
    sqlite3.Error,
)


@dataclass(frozen=True)
class SlateReport:
    """Everything one `na-ops slate` invocation did, and where it put the artifacts."""

    slate_run_id: str
    season: int
    week: int
    site: str
    decision_at: datetime
    started_at: datetime
    finished_at: datetime
    steps: tuple[StepOutcome, ...] = field(default_factory=tuple)
    slate_id: int | None = None
    decision_snapshot_id: str | None = None
    upload_csv_path: Path | None = None
    memo_path: Path | None = None
    replay_command: str | None = None

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)

    def step(self, name: OpsStep) -> StepOutcome | None:
        return next((outcome for outcome in self.steps if outcome.step == name), None)


def run_slate(
    connection: sqlite3.Connection,
    *,
    config: OpsConfig,
    database: Path,
    season: int,
    week: int,
    site: str,
    decision_at: datetime | None = None,
    number_of_lineups: int = 1,
    contest_archetype: ContestArchetype | str = ContestArchetype.CASH,
    slate_id: int | None = None,
    capture: Path | None = None,
    slate_name: str | None = None,
    starts_at: datetime | None = None,
    artifact_directory: Path = DEFAULT_ARTIFACT_DIRECTORY,
    report_directory: Path = DEFAULT_REPORT_DIRECTORY,
    heat_config_path: Path = DEFAULT_HEAT_CONFIG_PATH,
    dependencies: SlateDependencies = DEFAULT_SLATE_DEPENDENCIES,
    now: datetime | None = None,
) -> SlateReport:
    """Run the slate lane in order, isolating each step, and record every outcome.

    ``decision_at`` defaults to now, is written into every step's summary, and is the one
    cutoff handed to the episode build, the feature build, and the decision build. One
    instant, one replay.
    """

    started_at = ensure_utc(now or datetime.now(UTC))
    cutoff = ensure_utc(decision_at) if decision_at is not None else started_at
    if cutoff < started_at:
        # Everything this lane ingests is stamped `ingested_at` = now, and a
        # point-in-time read at an earlier cutoff cannot see it. Rather than let four
        # steps fail with "not eligible at ...", refuse the request once and say so.
        raise ValueError(
            f"--decision-at {utc_timestamp(cutoff)} is before this run began at "
            f"{utc_timestamp(started_at)}; the lane ingests as of now, so nothing it "
            "loads would be visible at that cutoff. To rebuild an earlier decision use "
            "`na-build --decision-at`, and to reproduce a frozen one use `na-replay`"
        )
    # An episode or feature snapshot may never claim to have been built before its own
    # cutoff, so a decision instant in the future moves the build time with it.
    built_at = max(started_at, cutoff)
    canonical_site = normalize_site(site).value
    slate_run_id = f"slate-{uuid4().hex}"
    registry = _registry(dependencies.source_formats)
    recorder = StepRecorder(
        connection,
        run_id=slate_run_id,
        step_errors=SLATE_STEP_ERRORS,
        base_summary={
            "decision_at": utc_timestamp(cutoff),
            "season": season,
            "week": week,
            "site": canonical_site,
        },
    )

    recorder.run(
        "slate_salaries",
        lambda: _ingest_salaries(
            dependencies,
            connection,
            config=config,
            season=season,
            week=week,
            site=canonical_site,
            capture=capture,
            slate_name=slate_name,
            starts_at=starts_at,
            ingested_at=started_at,
        ),
    )

    target, target_problem = _target_slate(
        connection,
        season=season,
        week=week,
        site=canonical_site,
        slate_id=slate_id,
    )
    # _target_slate always says why when it returns nothing; the fallback is belt and braces.
    no_slate = target_problem or "no slate could be resolved for this week and site"
    if target is None:
        recorder.skip("slate_projections", no_slate)
    else:
        recorder.run(
            "slate_projections",
            lambda: _ingest_vendor_captures(
                dependencies,
                connection,
                config=config,
                season=season,
                week=week,
                site=canonical_site,
                slate_id=target.slate_id,
                registry=registry,
                ingested_at=started_at,
            ),
        )

    # Episodes are slate-independent: they cluster the week's claims at the cutoff. They
    # run even when no slate resolved, so a bad salary export costs only the slate steps.
    recorder.run(
        "slate_episodes",
        lambda: _build_episodes(dependencies, connection, as_of=cutoff, built_at=built_at),
    )

    if target is None:
        recorder.skip("slate_features", no_slate)
        recorder.skip("slate_build", no_slate)
        recorder.skip("slate_memo", no_slate)
        return _report(
            slate_run_id,
            season=season,
            week=week,
            site=canonical_site,
            cutoff=cutoff,
            started_at=started_at,
            recorder=recorder,
            slate_id=None,
            build_result=None,
            memo_path=None,
            database=database,
            artifact_directory=artifact_directory,
        )

    recorder.run(
        "slate_features",
        lambda: _build_features(
            dependencies,
            connection,
            slate_id=target.slate_id,
            site=canonical_site,
            as_of=cutoff,
            built_at=built_at,
            config_path=heat_config_path,
        ),
    )

    built: list[BuildResult] = []
    recorder.run(
        "slate_build",
        lambda: _build_decision(
            dependencies,
            connection,
            config=config,
            database=database,
            season=season,
            week=week,
            slate=target,
            site=canonical_site,
            decision_at=cutoff,
            artifact_directory=artifact_directory,
            number_of_lineups=number_of_lineups,
            contest_archetype=contest_archetype,
            into=built,
        ),
    )

    memo_paths: list[Path] = []
    if not built:
        recorder.skip(
            "slate_memo",
            "the build step produced no decision snapshot, so there is nothing to write a "
            "memo about; fix the build step and rerun `na-ops slate`",
        )
    else:
        recorder.run(
            "slate_memo",
            lambda: _write_memo(
                dependencies,
                connection,
                build_result=built[0],
                report_directory=report_directory,
                into=memo_paths,
            ),
        )

    return _report(
        slate_run_id,
        season=season,
        week=week,
        site=canonical_site,
        cutoff=cutoff,
        started_at=started_at,
        recorder=recorder,
        slate_id=target.slate_id,
        build_result=built[0] if built else None,
        memo_path=memo_paths[0] if memo_paths else None,
        database=database,
        artifact_directory=artifact_directory,
    )


def _report(
    slate_run_id: str,
    *,
    season: int,
    week: int,
    site: str,
    cutoff: datetime,
    started_at: datetime,
    recorder: StepRecorder,
    slate_id: int | None,
    build_result: BuildResult | None,
    memo_path: Path | None,
    database: Path,
    artifact_directory: Path,
) -> SlateReport:
    snapshot_id = None if build_result is None else build_result.snapshot.decision_snapshot_id
    return SlateReport(
        slate_run_id=slate_run_id,
        season=season,
        week=week,
        site=site,
        decision_at=cutoff,
        started_at=started_at,
        finished_at=ensure_utc(datetime.now(UTC)),
        steps=tuple(recorder.outcomes),
        slate_id=slate_id,
        decision_snapshot_id=snapshot_id,
        upload_csv_path=None if build_result is None else build_result.generated_lineups_path,
        memo_path=memo_path,
        replay_command=None
        if snapshot_id is None
        else replay_command(
            database=database,
            decision_snapshot_id=snapshot_id,
            decision_at=cutoff,
            artifact_root=artifact_directory,
        ),
    )


def replay_command(
    *,
    database: Path,
    decision_snapshot_id: str,
    decision_at: datetime,
    artifact_root: Path,
) -> str:
    """The one line that reproduces this decision's bytes from the frozen artifacts."""

    return (
        f"na-replay --database {database} --decision-snapshot {decision_snapshot_id} "
        f"--decision-at {utc_timestamp(decision_at)} --artifact-root {artifact_root}"
    )


def _registry(formats: tuple[SourceFormat, ...]) -> SourceFormatRegistry:
    registry = SourceFormatRegistry()
    for source_format in formats:
        registry.register(source_format)
    return registry


def _ingest_salaries(
    dependencies: SlateDependencies,
    connection: sqlite3.Connection,
    *,
    config: OpsConfig,
    season: int,
    week: int,
    site: str,
    capture: Path | None,
    slate_name: str | None,
    starts_at: datetime | None,
    ingested_at: datetime,
) -> tuple[OpsStepStatus, dict[str, object], str | None]:
    # No ``run_id``: it is a foreign key into ``model_runs`` and the lane opens no model
    # run of its own. One invocation is traced through its ``ops_runs`` rows, and each
    # ingested row through its own ``source_file_sha256`` — the same as `na-slate ingest`.
    capture_path = capture or dependencies.newest_salary_capture(config.snapshot_root, season, week)
    report = dependencies.load_salary_capture(
        connection,
        capture_path,
        season=season,
        week=week,
        site=site,
        slate_name=slate_name,
        starts_at=starts_at,
        ingested_at=ingested_at,
    )
    connection.commit()
    summary: dict[str, object] = {
        "capture_path": str(capture_path),
        "observed_at": utc_timestamp(report.observed_at),
        "files_seen": report.files_seen,
        "rows_seen": report.rows_seen,
        "rows_rejected": report.rows_rejected,
        "salary_rows_inserted": report.salary_rows_inserted,
        "duplicate_rows": report.duplicate_rows,
        "unresolved_rows": report.unresolved_rows,
        "slate_ids": [slate.slate_id for slate in report.slates],
        "external_slate_ids": [slate.external_slate_id for slate in report.slates],
        "salary_changes": sum(len(slate.salary_changes) for slate in report.slates),
    }
    if report.ok:
        return "succeeded", summary, None

    reasons: list[str] = []
    if report.unresolved_rows:
        commands = [
            f"na-crosswalk resolve --unresolved-id {unresolved.unresolved_id} "
            f"--player-id <player_id>  # {unresolved.name_raw} "
            f"{unresolved.position} {unresolved.team}"
            for slate in report.slates
            for unresolved in slate.unresolved
        ]
        reasons.append(
            f"{report.unresolved_rows} salary row(s) did not resolve to a canonical "
            f"player; the slate was written but a build refuses until they are cleared:"
            + _listed(commands)
        )
    if report.rows_rejected:
        reasons.append(f"{report.rows_rejected} row(s) were rejected by the parser")
    if report.errors:
        reasons.append("; ".join(report.errors[:5]))
    raise StepFailure(" | ".join(reasons), summary)


def _target_slate(
    connection: sqlite3.Connection,
    *,
    season: int,
    week: int,
    site: str,
    slate_id: int | None,
) -> tuple[SlateSummary | None, str | None]:
    """Pick the one slate the rest of the lane works on, or say why it cannot."""

    summaries = list_slates(connection, season=season, week=week, site=site)
    if slate_id is not None:
        chosen = next((slate for slate in summaries if slate.slate_id == slate_id), None)
        if chosen is None:
            known = ", ".join(str(slate.slate_id) for slate in summaries) or "none"
            return None, (
                f"slate {slate_id} is not a {site} slate for {season} week {week:02d} "
                f"(ingested slate ids: {known}); check `na-slate list`"
            )
        return chosen, None
    if not summaries:
        return None, (
            f"no {site} slate exists for {season} week {week:02d}; the salaries step wrote "
            "none, so there is nothing to build against"
        )
    if len(summaries) > 1:
        detail = ", ".join(
            f"{slate.slate_id} ({slate.slate_type}, locks {utc_timestamp(slate.locks_at)})"
            for slate in summaries
        )
        return None, (
            f"{len(summaries)} {site} slates exist for {season} week {week:02d} — "
            f"{detail}; rerun with `--slate-id` naming the one to play"
        )
    return summaries[0], None


def _ingest_vendor_captures(
    dependencies: SlateDependencies,
    connection: sqlite3.Connection,
    *,
    config: OpsConfig,
    season: int,
    week: int,
    site: str,
    slate_id: int,
    registry: SourceFormatRegistry,
    ingested_at: datetime,
) -> tuple[OpsStepStatus, dict[str, object], str | None]:
    """Load every projection/ownership capture of the week that has a registered adapter.


    Loading is keyed on (source, site, slate, player, observed_at), so a capture already
    loaded inserts nothing and is reported as duplicates rather than skipped by guesswork.
    """

    captures = tuple(_vendor_captures(config.snapshot_root, season, week))
    summary: dict[str, object] = {"slate_id": slate_id, "captures_seen": len(captures)}
    if not captures:
        return (
            "skipped",
            summary,
            f"no capture under {snapshot_week_path(config.snapshot_root, season, week)} "
            f"manifests a projections or ownership file; capture the purchased downloads "
            f"with `na-snapshot capture --kind projections --source <vendor>` first",
        )

    loaded: list[str] = []
    skipped: list[str] = []
    missing_adapters: set[str] = set()
    projection_rows = 0
    ownership_rows = 0
    duplicate_rows = 0
    unresolved_rows = 0
    rejected_rows = 0
    errors: list[str] = []

    for capture_path, manifest in captures:
        vendors = sorted(
            {record.source for record in manifest.files if record.kind in VENDOR_KINDS}
        )
        unregistered = [vendor for vendor in vendors if not _registered(registry, vendor)]
        if unregistered:
            # Nothing from this capture is loaded: a partial load would leave the slate
            # holding one vendor's view while claiming the capture was ingested.
            missing_adapters.update(unregistered)
            skipped.append(capture_path.name)
            continue
        report = dependencies.load_projection_capture(
            connection,
            capture_path,
            site=site,
            slate_id=slate_id,
            registry=registry,
            ingested_at=ingested_at,
        )
        connection.commit()
        loaded.append(capture_path.name)
        projection_rows += report.projection_rows_inserted
        ownership_rows += report.ownership_rows_inserted
        duplicate_rows += report.duplicate_rows
        unresolved_rows += report.unresolved_rows
        rejected_rows += report.rejected_rows
        errors.extend(f"{capture_path.name}: {error}" for error in report.errors)

    summary |= {
        "captures_loaded": len(loaded),
        "captures_skipped": len(skipped),
        "skipped_captures": skipped,
        "missing_adapter_vendors": sorted(missing_adapters),
        "projection_rows_inserted": projection_rows,
        "ownership_rows_inserted": ownership_rows,
        "duplicate_rows": duplicate_rows,
        "unresolved_rows": unresolved_rows,
        "rejected_rows": rejected_rows,
    }

    reasons: list[str] = []
    if missing_adapters:
        registered = ", ".join(registry.names) or "none"
        reasons.append(
            f"no SourceFormat adapter is registered for vendor(s) "
            f"{', '.join(sorted(missing_adapters))}; their capture(s) "
            f"{', '.join(skipped)} were not loaded and nothing was guessed "
            f"(registered vendors: {registered})"
        )
    if unresolved_rows:
        reasons.append(
            f"{unresolved_rows} vendor row(s) did not resolve to a canonical player: "
            "`na-crosswalk resolve`"
        )
    if rejected_rows:
        reasons.append(f"{rejected_rows} vendor row(s) were rejected by their adapter")
    if errors:
        reasons.append("; ".join(errors[:5]))
    if reasons:
        raise StepFailure(" | ".join(reasons), summary)
    return "succeeded", summary, None


def _registered(registry: SourceFormatRegistry, vendor: str) -> bool:
    try:
        registry.get(vendor)
    except ProjectionIngestError:
        return False
    return True


def _vendor_captures(
    snapshot_root: Path,
    season: int,
    week: int,
) -> Iterator[tuple[Path, SnapshotManifest]]:
    """Yield the week's captures that manifest a projections or ownership file, oldest first.

    Oldest first so a Sunday re-download lands after Saturday's, leaving the newest
    observation newest in the store as well.
    """

    week_path = snapshot_week_path(snapshot_root, season, week)
    if not week_path.is_dir():
        return
    for capture_path in sorted(path for path in week_path.iterdir() if path.is_dir()):
        manifest_path = capture_path / MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        manifest = load_manifest(manifest_path)
        if any(record.kind in VENDOR_KINDS for record in manifest.files):
            yield capture_path, manifest


def _build_episodes(
    dependencies: SlateDependencies,
    connection: sqlite3.Connection,
    *,
    as_of: datetime,
    built_at: datetime,
) -> tuple[OpsStepStatus, dict[str, object], str | None]:
    return build_episode_snapshot(
        dependencies.build_episodes,
        connection,
        as_of=as_of,
        built_at=built_at,
    )


def _build_features(
    dependencies: SlateDependencies,
    connection: sqlite3.Connection,
    *,
    slate_id: int,
    site: str,
    as_of: datetime,
    built_at: datetime,
    config_path: Path,
) -> tuple[OpsStepStatus, dict[str, object], str | None]:
    report = dependencies.build_features(
        connection,
        slate_id=slate_id,
        site=site,
        as_of=as_of,
        built_at=built_at,
        config_path=config_path,
    )
    connection.commit()
    return (
        "succeeded",
        {
            "slate_id": report.slate_id,
            "feature_version": report.feature_version,
            "player_count": report.player_count,
            "episode_count": report.episode_count,
            "features_inserted": report.features_inserted,
            "reused_existing": report.reused_existing,
        },
        None,
    )


def _build_decision(
    dependencies: SlateDependencies,
    connection: sqlite3.Connection,
    *,
    config: OpsConfig,
    database: Path,
    season: int,
    week: int,
    slate: SlateSummary,
    site: str,
    decision_at: datetime,
    artifact_directory: Path,
    number_of_lineups: int,
    contest_archetype: ContestArchetype | str,
    into: list[BuildResult],
) -> tuple[OpsStepStatus, dict[str, object], str | None]:
    summary: dict[str, object] = {
        "slate_id": slate.slate_id,
        "external_slate_id": slate.external_slate_id,
        "number_of_lineups": number_of_lineups,
        "contest_archetype": str(
            contest_archetype.value
            if isinstance(contest_archetype, ContestArchetype)
            else contest_archetype
        ),
    }
    _require_resolved_identities(connection, site=site, summary=summary)
    _require_projections(
        connection,
        config=config,
        season=season,
        week=week,
        slate_id=slate.slate_id,
        site=site,
        as_of=decision_at,
        summary=summary,
    )
    try:
        result = dependencies.build_decision(
            database,
            slate_id=slate.slate_id,
            site=site,
            decision_at=decision_at,
            artifact_directory=artifact_directory,
            number_of_lineups=number_of_lineups,
            contest_archetype=contest_archetype,
        )
        reused = False
    except BuildDuplicateError as duplicate:
        # The id is a hash of the request bytes and the cutoff, so an identical id is the
        # identical decision. Reload and re-verify it rather than refusing a rerun.
        result = load_build_result(
            connection,
            decision_snapshot_id=duplicate.decision_snapshot_id,
            decision_at=decision_at,
            artifact_root=artifact_directory,
        )
        reused = True
    into.append(result)
    routing = result.ownership_routing
    return (
        "succeeded",
        summary
        | {
            "ownership_source": "scenario_model" if routing.applied else "vendor_baseline",
            "ownership_routing_reason": routing.reason,
            "ownership_scenario_run_id": routing.scenario_run_id,
            "ownership_governance_status": routing.governance_status,
            "ownership_status_multiplier": routing.status_multiplier,
            "ownership_material_deltas": len(routing.material_deltas),
            "decision_snapshot_id": result.snapshot.decision_snapshot_id,
            "manifest_hash_set_sha256": result.snapshot.manifest_hash_set_sha256,
            "lineup_count": len(result.lineups),
            "upload_csv": str(result.generated_lineups_path),
            "artifact_directory": str(result.artifact_directory),
            "replay_verified": result.replay.report.output_matches,
            "reused_existing": reused,
        },
        None,
    )


def _require_resolved_identities(
    connection: sqlite3.Connection,
    *,
    site: str,
    summary: dict[str, object],
) -> None:
    """Refuse the build while any of the site's identities is still pending (§1.5.7)."""

    pending = PlayerCrosswalk(connection).list_unresolved()
    relevant = [row for row in pending if row.site == site]
    if not relevant:
        return
    summary["unresolved_identities"] = len(relevant)
    commands = [
        f"na-crosswalk resolve --unresolved-id {row.unresolved_id} --player-id <player_id>"
        f"  # {row.name_raw} {row.position or '-'} {row.team}"
        for row in relevant
    ]
    raise StepFailure(
        f"{len(relevant)} unresolved {site} identity/identities remain; lineup generation "
        "must stop until each is decided:" + _listed(commands),
        summary,
    )


def _require_projections(
    connection: sqlite3.Connection,
    *,
    config: OpsConfig,
    season: int,
    week: int,
    slate_id: int,
    site: str,
    as_of: datetime,
    summary: dict[str, object],
) -> None:
    """Refuse the build when no projection is point-in-time eligible at the cutoff."""

    stamp = utc_timestamp(as_of)
    available = int(
        connection.execute(
            """
            SELECT count(*) FROM projection_snapshots
            WHERE slate_id = ? AND site = ?
              AND rtrim(observed_at, 'Z') <= rtrim(?, 'Z')
              AND rtrim(valid_from, 'Z') <= rtrim(?, 'Z')
              AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(?, 'Z'))
            """,
            (slate_id, site, stamp, stamp, stamp),
        ).fetchone()[0]
    )
    summary["projection_rows_available"] = available
    if available:
        return
    captured = _captured_kinds(config.snapshot_root, season, week)
    summary["captured_kinds"] = sorted(kind.value for kind in captured)
    detail = [
        f"{kind.value}: NOT CAPTURED for this week"
        if kind not in captured
        else f"{kind.value}: captured but not ingested (see the slate_projections step)"
        for kind in (CaptureKind.PROJECTIONS, CaptureKind.OWNERSHIP)
    ]
    raise StepFailure(
        f"slate {slate_id} has no {site} projection row eligible at {stamp}, so no "
        "candidate player can be priced — " + "; ".join(detail),
        summary,
    )


def _captured_kinds(snapshot_root: Path, season: int, week: int) -> frozenset[CaptureKind]:
    week_path = snapshot_week_path(snapshot_root, season, week)
    if not week_path.is_dir():
        return frozenset()
    kinds: set[CaptureKind] = set()
    for capture_path in sorted(path for path in week_path.iterdir() if path.is_dir()):
        manifest_path = capture_path / MANIFEST_FILENAME
        if manifest_path.is_file():
            kinds.update(record.kind for record in load_manifest(manifest_path).files)
    return frozenset(kinds)


def _write_memo(
    dependencies: SlateDependencies,
    connection: sqlite3.Connection,
    *,
    build_result: BuildResult,
    report_directory: Path,
    into: list[Path],
) -> tuple[OpsStepStatus, dict[str, object], str | None]:
    memo = dependencies.build_slate_memo(build_result, connection)
    # Pre-lock there are no result labels, so the bundle carries the memo and states in
    # words that no baseline was measured, exactly as `na-report` renders it.
    bundle = render_report_bundle(memo, None)
    path = default_report_path(
        build_result.snapshot.decision_snapshot_id, directory=report_directory
    )
    write_report_atomic(path, bundle)
    into.append(path)
    return (
        "succeeded",
        {
            "memo_path": str(path),
            "decision_snapshot_id": memo.decision_snapshot_id,
            "lineup_count": len(memo.lineups),
            "input_artifacts": len(memo.input_artifacts),
        },
        None,
    )


def _listed(commands: list[str]) -> str:
    shown = commands[:MAX_LISTED_ACTIONS]
    more = (
        ""
        if len(commands) <= MAX_LISTED_ACTIONS
        else (f"\n  (+{len(commands) - MAX_LISTED_ACTIONS} more)")
    )
    return "\n  " + "\n  ".join(shown) + more
