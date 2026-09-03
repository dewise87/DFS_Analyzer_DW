"""Production decision build path with immediate byte-stable self-verification."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

from narrative_alpha import __version__
from narrative_alpha.candidate_selection import (
    CandidateSelection,
    CandidateSelectionError,
    SelectedSourceArtifact,
    select_candidate_scenario,
)
from narrative_alpha.identity import PlayerCrosswalk
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.portfolio import (
    CLASSIC_SITE_RULES,
    CandidatePlayerScenario,
    ContestArchetype,
    DfsSite,
    Lineup,
    OptimizationRequest,
    OptimizerAdapter,
    PydfsAdapter,
    SlateType,
    UploadEntry,
)
from narrative_alpha.replay import (
    PointInTimeSession,
    ReplayError,
    ReplayResult,
    replay_decision,
)
from narrative_alpha.store import (
    DecisionManifestHash,
    DecisionSnapshotRow,
    ModelRunRow,
    apply_migrations,
    canonical_manifest_hashes,
    connect_database,
    manifest_hash_set_sha256,
)

MANIFEST_SCHEMA_VERSION = "1.0"


class BuildError(RuntimeError):
    """Base decision-build failure carrying a stable machine-readable code."""

    code = "build_failed"

    def structured(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


class BuildInputError(BuildError):
    """Raised for an invalid or unavailable requested decision scope."""

    code = "invalid_build_input"


class BuildDuplicateError(BuildInputError):
    """Raised when this exact decision — same request bytes, same ``decision_at`` — exists.

    It carries its own code and the existing id so a caller such as the slate lane can
    tell "you already froze this decision" apart from "your request was invalid", and
    reuse the frozen decision instead of re-deriving one that would hash identically.
    """

    code = "decision_already_exists"

    def __init__(self, message: str, decision_snapshot_id: str) -> None:
        super().__init__(message)
        self.decision_snapshot_id = decision_snapshot_id


class BuildDataError(BuildError):
    """Raised when bounded store rows cannot form a candidate scenario."""

    code = "candidate_selection_failed"


class BuildArtifactError(BuildError):
    """Raised when immutable decision artifacts cannot be written."""

    code = "artifact_write_failed"


class BuildSelfVerificationError(BuildError):
    """Raised when immediate replay does not reproduce the just-built bytes."""

    code = "self_verification_failed"


@dataclass(frozen=True)
class BuildResult:
    """A committed, self-verified production decision and its artifact locations."""

    snapshot: DecisionSnapshotRow
    request: OptimizationRequest
    lineups: tuple[Lineup, ...]
    replay: ReplayResult
    artifact_root: Path
    artifact_directory: Path
    optimizer_request_path: Path
    generated_lineups_path: Path
    manifest_path: Path


@dataclass(frozen=True)
class _WrittenArtifacts:
    directory: Path
    request_path: Path
    lineups_path: Path
    manifest_path: Path


def build_decision(
    database_path: Path | str,
    *,
    slate_id: int,
    site: DfsSite | str,
    decision_at: datetime,
    artifact_directory: Path,
    number_of_lineups: int = 1,
    contest_archetype: ContestArchetype | str = ContestArchetype.CASH,
    excluded_lineup_player_ids: tuple[tuple[int, ...], ...] = (),
    pinned_lineups: tuple[Lineup, ...] = (),
    upload_entries: tuple[UploadEntry, ...] = (),
    lineup_uniqueness: int = 1,
    run_type: str = "decision_build",
    note: str = "na-build immediate replay verified",
    adapter: OptimizerAdapter | None = None,
    connection: sqlite3.Connection | None = None,
) -> BuildResult:
    """Build, freeze, replay, and atomically commit one DFS decision.

    ``decision_at`` is deliberately required here. The CLI may resolve its optional
    argument to the current time before entering this deterministic path, but no code in
    this function reads the wall clock.

    With ``connection`` the caller owns the transaction: the build runs inside whatever
    the caller has begun and nothing here commits or rolls back, so a caller can make
    its own rows and this decision one atomic fact (the fast lane's availability rows
    live or die with the snapshot they justify). On-disk artifacts are still removed
    when the build raises.
    """

    try:
        cutoff = ensure_utc(decision_at)
    except ValueError as error:
        raise BuildInputError("decision_at must include a timezone") from error
    try:
        requested_site = DfsSite(site)
        requested_archetype = ContestArchetype(contest_archetype)
    except ValueError as error:
        raise BuildInputError(str(error)) from error
    if slate_id < 1:
        raise BuildInputError("slate_id must be positive")
    if number_of_lineups < 1 or number_of_lineups > 150:
        raise BuildInputError("number_of_lineups must be between 1 and 150")

    selected_adapter = adapter or PydfsAdapter()
    if connection is not None:
        if not connection.in_transaction:
            raise BuildInputError("a caller-owned build must run inside an open transaction")
        return _build_in_transaction(
            connection,
            slate_id=slate_id,
            site=requested_site,
            decision_at=cutoff,
            artifact_root=artifact_directory.resolve(),
            number_of_lineups=number_of_lineups,
            contest_archetype=requested_archetype,
            excluded_lineup_player_ids=excluded_lineup_player_ids,
            pinned_lineups=pinned_lineups,
            upload_entries=upload_entries,
            lineup_uniqueness=lineup_uniqueness,
            run_type=run_type,
            note=note,
            adapter=selected_adapter,
        )
    with connect_database(database_path) as owned:
        apply_migrations(owned)
        try:
            owned.execute("BEGIN IMMEDIATE")
            result = _build_in_transaction(
                owned,
                slate_id=slate_id,
                site=requested_site,
                decision_at=cutoff,
                artifact_root=artifact_directory.resolve(),
                number_of_lineups=number_of_lineups,
                contest_archetype=requested_archetype,
                excluded_lineup_player_ids=excluded_lineup_player_ids,
                pinned_lineups=pinned_lineups,
                upload_entries=upload_entries,
                lineup_uniqueness=lineup_uniqueness,
                run_type=run_type,
                note=note,
                adapter=selected_adapter,
            )
        except Exception:
            owned.rollback()
            raise
        else:
            owned.commit()
            return result


def canonical_json_bytes(value: BaseModel | Mapping[str, object]) -> bytes:
    """Return compact, key-sorted canonical JSON with normalized UTC and floats."""

    raw: object = value.model_dump(mode="python") if isinstance(value, BaseModel) else value
    normalized = _canonical_value(raw)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _build_in_transaction(
    connection: sqlite3.Connection,
    *,
    slate_id: int,
    site: DfsSite,
    decision_at: datetime,
    artifact_root: Path,
    number_of_lineups: int,
    contest_archetype: ContestArchetype,
    excluded_lineup_player_ids: tuple[tuple[int, ...], ...],
    pinned_lineups: tuple[Lineup, ...],
    upload_entries: tuple[UploadEntry, ...],
    lineup_uniqueness: int,
    run_type: str,
    note: str,
    adapter: OptimizerAdapter,
) -> BuildResult:
    session = PointInTimeSession(connection)
    try:
        slate = session.slate(slate_id, as_of=decision_at)
    except ReplayError as error:
        raise BuildInputError(str(error)) from error
    if slate.site != site.value:
        raise BuildInputError(
            f"slate {slate_id} belongs to {slate.site!r}, not requested site {site.value!r}"
        )

    # The existing crosswalk guard is intentionally fail-closed. Until unresolved rows
    # carry a slate key, its site scope is stricter than the active slate and cannot allow
    # an unresolved active player to slip through.
    PlayerCrosswalk(connection).require_all_resolved(site=site.value)
    try:
        selected = select_candidate_scenario(
            session,
            slate_id=slate_id,
            site=site,
            as_of=decision_at,
        )
    except CandidateSelectionError as error:
        raise BuildDataError(str(error)) from error

    scenario = CandidatePlayerScenario(
        scenario_id=_scenario_id(selected, slate_id, site, decision_at),
        players=selected.players,
        projection_source_versions=selected.projection_source_versions,
    )
    request = OptimizationRequest(
        site=site,
        slate_id=slate_id,
        slate_type=SlateType(slate.slate_type),
        contest_archetype=contest_archetype,
        salary_cap=CLASSIC_SITE_RULES[site].default_salary_cap,
        candidate_player_scenario=scenario,
        number_of_lineups=number_of_lineups,
        excluded_lineup_player_ids=excluded_lineup_player_ids,
        pinned_lineups=pinned_lineups,
        upload_entries=upload_entries,
        lineup_uniqueness=lineup_uniqueness,
    )
    request_bytes = canonical_json_bytes(request)
    request_sha256 = _sha256(request_bytes)
    decision_digest = _sha256(request_bytes + b"\x00" + utc_timestamp(decision_at).encode("ascii"))
    decision_snapshot_id = f"decision-{decision_digest}"
    run_id = f"na-build-{decision_digest}"
    if (
        connection.execute(
            "SELECT 1 FROM decision_snapshots WHERE decision_snapshot_id = ?",
            (decision_snapshot_id,),
        ).fetchone()
        is not None
    ):
        raise BuildDuplicateError(
            f"decision {decision_snapshot_id!r} already exists for this exact "
            "request and decision_at; a different decision needs different inputs",
            decision_snapshot_id,
        )

    lineups = adapter.build_lineups(request)
    upload_bytes = adapter.export_upload_csv(lineups, request.site, request.upload_entries)
    upload_sha256 = _sha256(upload_bytes)

    request_relative_path = f"{decision_snapshot_id}/optimizer_request.json"
    lineups_relative_path = f"{decision_snapshot_id}/generated_lineups.csv"
    manifest = _decision_manifest(
        selected,
        request_sha256=request_sha256,
        request_path=request_relative_path,
        lineups_sha256=upload_sha256,
        lineups_path=lineups_relative_path,
    )
    manifest_bytes = canonical_manifest_hashes(manifest).encode("utf-8")
    written = _write_artifacts(
        artifact_root,
        decision_snapshot_id=decision_snapshot_id,
        request_bytes=request_bytes,
        lineups_bytes=upload_bytes,
        manifest_bytes=manifest_bytes,
    )
    try:
        return _commit_and_verify(
            connection,
            written=written,
            artifact_root=artifact_root,
            decision_snapshot_id=decision_snapshot_id,
            run_id=run_id,
            slate_id=slate_id,
            decision_at=decision_at,
            request=request,
            request_sha256=request_sha256,
            manifest=manifest,
            lineups=lineups,
            upload_bytes=upload_bytes,
            run_type=run_type,
            note=note,
            adapter=adapter,
        )
    except Exception:
        # The DB transaction rolls back in build_decision; the on-disk artifacts must
        # roll back too, or a retry of the identical decision hits the mkdir guard.
        shutil.rmtree(written.directory, ignore_errors=True)
        raise


def _commit_and_verify(
    connection: sqlite3.Connection,
    *,
    written: _WrittenArtifacts,
    artifact_root: Path,
    decision_snapshot_id: str,
    run_id: str,
    slate_id: int,
    decision_at: datetime,
    request: OptimizationRequest,
    request_sha256: str,
    manifest: tuple[DecisionManifestHash, ...],
    lineups: tuple[Lineup, ...],
    upload_bytes: bytes,
    run_type: str,
    note: str,
    adapter: OptimizerAdapter,
) -> BuildResult:
    run = ModelRunRow(
        run_id=run_id,
        run_type=run_type,
        started_at=decision_at,
        completed_at=None,
        status="running",
        code_version=__version__,
        config_sha256=request_sha256,
        parent_run_id=None,
        error_message=None,
        created_at=decision_at,
    )
    snapshot = DecisionSnapshotRow(
        decision_snapshot_id=decision_snapshot_id,
        slate_id=slate_id,
        decision_at=decision_at,
        created_at=decision_at,
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        manifest_hashes_json=manifest,
        manifest_hash_set_sha256=manifest_hash_set_sha256(manifest),
        run_id=run_id,
        note=note,
    )
    _insert_row(connection, "model_runs", run)
    _insert_row(connection, "decision_snapshots", snapshot)

    try:
        replay = replay_decision(
            connection,
            decision_snapshot_id=decision_snapshot_id,
            decision_at=decision_at,
            artifact_root=artifact_root,
            adapter=adapter,
        )
    except Exception as error:
        raise BuildSelfVerificationError(f"immediate replay failed: {error}") from error
    if not replay.report.output_matches or replay.output_bytes != upload_bytes:
        raise BuildSelfVerificationError(
            "immediate replay output differs byte-for-byte from generated_lineups.csv"
        )

    cursor = connection.execute(
        """
        UPDATE model_runs
        SET completed_at = ?, status = 'succeeded'
        WHERE run_id = ? AND status = 'running'
        """,
        (utc_timestamp(decision_at), run_id),
    )
    if cursor.rowcount != 1:
        raise BuildError(f"could not mark model run {run_id!r} succeeded")
    return BuildResult(
        snapshot=snapshot,
        request=request,
        lineups=lineups,
        replay=replay,
        artifact_root=artifact_root,
        artifact_directory=written.directory,
        optimizer_request_path=written.request_path,
        generated_lineups_path=written.lineups_path,
        manifest_path=written.manifest_path,
    )


def _scenario_id(
    selected: CandidateSelection,
    slate_id: int,
    site: DfsSite,
    decision_at: datetime,
) -> str:
    payload: dict[str, object] = {
        "decision_at": utc_timestamp(decision_at),
        "players": [player.model_dump(mode="python") for player in selected.players],
        "projection_artifacts": [
            {"sha256": artifact.sha256, "source": artifact.source}
            for artifact in selected.projection_artifacts
        ],
        "projection_source_versions": list(selected.projection_source_versions),
        "salary_artifacts": [
            {"sha256": artifact.sha256, "source": artifact.source}
            for artifact in selected.salary_artifacts
        ],
        "site": site.value,
        "slate_id": slate_id,
    }
    if selected.availability_artifacts:
        payload["availability_artifacts"] = [
            {"sha256": artifact.sha256, "source": artifact.source}
            for artifact in selected.availability_artifacts
        ]
    return f"scenario-{_sha256(canonical_json_bytes(payload))}"


def _decision_manifest(
    selected: CandidateSelection,
    *,
    request_sha256: str,
    request_path: str,
    lineups_sha256: str,
    lineups_path: str,
) -> tuple[DecisionManifestHash, ...]:
    salary = tuple(
        _source_manifest_item("salary", artifact) for artifact in selected.salary_artifacts
    )
    projections = tuple(
        _source_manifest_item("projection", artifact) for artifact in selected.projection_artifacts
    )
    availability = tuple(
        _source_manifest_item("availability", artifact)
        for artifact in selected.availability_artifacts
    )
    generated = (
        DecisionManifestHash(
            artifact_kind="optimizer_request",
            sha256=request_sha256,
            path=request_path,
            source="narrative-alpha",
        ),
        DecisionManifestHash(
            artifact_kind="generated_lineups",
            sha256=lineups_sha256,
            path=lineups_path,
            source="narrative-alpha",
        ),
    )
    return (*salary, *projections, *availability, *generated)


def _source_manifest_item(
    artifact_kind: str,
    artifact: SelectedSourceArtifact,
) -> DecisionManifestHash:
    return DecisionManifestHash.model_validate(
        {
            "artifact_kind": artifact_kind,
            "sha256": artifact.sha256,
            # The operational schema retains the immutable content hash but not the
            # capture-relative filename, so this is an explicit logical store path.
            "path": f"store/{artifact_kind}/{artifact.sha256}",
            "source": artifact.source,
        }
    )


def _write_artifacts(
    artifact_root: Path,
    *,
    decision_snapshot_id: str,
    request_bytes: bytes,
    lineups_bytes: bytes,
    manifest_bytes: bytes,
) -> _WrittenArtifacts:
    directory = artifact_root / decision_snapshot_id
    request_path = directory / "optimizer_request.json"
    lineups_path = directory / "generated_lineups.csv"
    manifest_path = directory / "manifest.json"
    try:
        artifact_root.mkdir(parents=True, exist_ok=True)
        directory.mkdir()
        request_path.write_bytes(request_bytes)
        lineups_path.write_bytes(lineups_bytes)
        manifest_path.write_bytes(manifest_bytes)
    except OSError as error:
        raise BuildArtifactError(f"cannot write decision artifacts: {error}") from error
    return _WrittenArtifacts(
        directory=directory,
        request_path=request_path,
        lineups_path=lineups_path,
        manifest_path=manifest_path,
    )


def _insert_row(
    connection: sqlite3.Connection,
    table: str,
    row: ModelRunRow | DecisionSnapshotRow,
) -> None:
    values = row.db_values()
    for key, value in row.model_dump(mode="python").items():
        if isinstance(value, datetime):
            values[key] = utc_timestamp(value)
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )


def _canonical_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"))
    if isinstance(value, datetime):
        return utc_timestamp(value)
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise BuildInputError("canonical JSON object keys must be strings")
            normalized[key] = _canonical_value(item)
        return normalized
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BuildInputError("canonical JSON does not permit non-finite floats")
        return 0.0 if value == 0 else value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise BuildInputError(f"unsupported canonical JSON value: {type(value).__name__}")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
