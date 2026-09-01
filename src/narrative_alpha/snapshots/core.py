"""Filesystem operations for append-only Phase -1 snapshots."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from narrative_alpha.snapshots.models import (
    CaptureKind,
    SnapshotError,
    SnapshotFile,
    SnapshotManifest,
    SnapshotRequest,
)

DEFAULT_SNAPSHOT_ROOT = Path("data/snapshots")
MANIFEST_FILENAME = "manifest.json"
_HASH_CHUNK_SIZE = 1024 * 1024
_WEEK_DIRECTORY_PATTERN = re.compile(r"week_(\d+)$")


@dataclass(frozen=True)
class VerificationReport:
    """Result of checking all captures for one season and week."""

    week_path: Path
    manifests_checked: int
    files_checked: int
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class WeekStatus:
    """Most recent observed time for each kind present in a week."""

    season: int
    week: int
    last_captured: Mapping[CaptureKind, datetime]


@dataclass(frozen=True)
class StatusReport:
    """Status across every initialized snapshot week."""

    weeks: tuple[WeekStatus, ...]
    problems: tuple[str, ...]


@dataclass(frozen=True)
class CapturePayload:
    """Raw bytes and provenance to persist through the shared capture writer."""

    filename: str
    content: bytes
    observed_at: datetime
    source: str


@dataclass(frozen=True)
class _CaptureInput:
    filename: str
    observed_at: datetime
    source: str
    source_path: Path | None = None
    content: bytes | None = None


def snapshot_week_path(snapshot_root: Path, season: int, week: int) -> Path:
    """Return the canonical directory for a season/week pair."""

    _validate_season_week(season, week)
    return snapshot_root / str(season) / f"week_{week:02d}"


def initialize_week(snapshot_root: Path, season: int, week: int) -> Path:
    """Create the snapshot directory hierarchy for a week."""

    week_path = snapshot_week_path(snapshot_root, season, week)
    week_path.mkdir(parents=True, exist_ok=True)
    return week_path


def capture_files(
    snapshot_root: Path,
    season: int,
    week: int,
    kind: CaptureKind | str,
    source: str,
    files: Sequence[Path],
    *,
    observed_at: datetime | None = None,
) -> Path:
    """Copy files into a newly allocated capture and write its manifest.

    An existing capture directory is never opened for writing. If two captures receive the
    same clock timestamp, the later directory name advances by one microsecond until an unused
    ISO timestamp is found.
    """

    capture_kind = CaptureKind(kind)
    source_label = source.strip()
    if not source_label:
        raise ValueError("source must not be empty")

    source_files = tuple(Path(file) for file in files)
    _validate_source_files(source_files)

    capture_time = _normalize_capture_time(observed_at or datetime.now(UTC))
    inputs = tuple(
        _CaptureInput(
            filename=source_file.name,
            observed_at=capture_time,
            source=source_label,
            source_path=source_file,
        )
        for source_file in source_files
    )
    return _write_capture(
        snapshot_root,
        season,
        week,
        capture_kind,
        inputs,
        captured_at=capture_time,
    )


def capture_payloads(
    snapshot_root: Path,
    season: int,
    week: int,
    kind: CaptureKind | str,
    payloads: Sequence[CapturePayload],
    *,
    requests: Sequence[SnapshotRequest] = (),
    errors: Sequence[SnapshotError] = (),
    captured_at: datetime | None = None,
) -> Path:
    """Persist fetched response bytes with request and degraded-mode provenance."""

    capture_kind = CaptureKind(kind)
    capture_time = _normalize_capture_time(captured_at or datetime.now(UTC))
    inputs = tuple(
        _CaptureInput(
            filename=payload.filename,
            content=payload.content,
            observed_at=_normalize_capture_time(payload.observed_at),
            source=payload.source.strip(),
        )
        for payload in payloads
    )
    _validate_capture_inputs(inputs)
    return _write_capture(
        snapshot_root,
        season,
        week,
        capture_kind,
        inputs,
        requests=requests,
        errors=errors,
        captured_at=capture_time,
    )


def load_manifest(manifest_path: Path) -> SnapshotManifest:
    """Load and validate a snapshot manifest from disk."""

    return SnapshotManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    """Calculate a file's SHA-256 digest without loading it all into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        while chunk := source_file.read(_HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def verify_week(snapshot_root: Path, season: int, week: int) -> VerificationReport:
    """Verify every manifest and captured file for a week."""

    week_path = snapshot_week_path(snapshot_root, season, week)
    if not week_path.is_dir():
        return VerificationReport(
            week_path=week_path,
            manifests_checked=0,
            files_checked=0,
            problems=(f"snapshot week does not exist: {week_path}",),
        )

    problems: list[str] = []
    manifests_checked = 0
    files_checked = 0

    capture_directories: list[Path] = []
    for entry in sorted(week_path.iterdir()):
        if entry.is_symlink():
            problems.append(f"unsupported symlink in week directory: {entry}")
        elif entry.is_dir():
            capture_directories.append(entry)
        elif entry.is_file():
            problems.append(f"unmanifested file outside a capture: {entry}")
        else:
            problems.append(f"unsupported filesystem entry: {entry}")

    for capture_path in capture_directories:
        manifest_path = capture_path / MANIFEST_FILENAME
        actual_files, scan_problems = _scan_capture_files(capture_path)
        problems.extend(scan_problems)

        if not manifest_path.exists():
            problems.append(f"missing manifest: {manifest_path}")
            problems.extend(
                f"unmanifested file: {capture_path / relative_path}"
                for relative_path in actual_files
            )
            continue
        if manifest_path.is_symlink() or not manifest_path.is_file():
            problems.append(f"manifest is not a regular file: {manifest_path}")
            problems.extend(
                f"unmanifested file: {capture_path / relative_path}"
                for relative_path in actual_files
            )
            continue

        try:
            manifest = load_manifest(manifest_path)
        except (OSError, ValidationError) as error:
            problems.append(f"invalid manifest {manifest_path}: {error}")
            problems.extend(
                f"unmanifested file: {capture_path / relative_path}"
                for relative_path in actual_files
            )
            continue

        manifests_checked += 1
        if manifest.season != season or manifest.week != week:
            problems.append(
                f"manifest season/week mismatch in {manifest_path}: "
                f"expected {season}/week_{week:02d}, got "
                f"{manifest.season}/week_{manifest.week:02d}"
            )

        expected_paths = {record.path for record in manifest.files}
        for record in manifest.files:
            captured_file = capture_path.joinpath(*PurePosixPath(record.path).parts)
            if _path_contains_symlink(capture_path, record.path):
                problems.append(f"manifested path contains a symlink: {captured_file}")
                continue
            if not captured_file.exists():
                problems.append(f"manifested file is missing: {captured_file}")
                continue
            if not captured_file.is_file():
                problems.append(f"manifested path is not a regular file: {captured_file}")
                continue

            files_checked += 1
            actual_size = captured_file.stat().st_size
            if actual_size != record.size_bytes:
                problems.append(
                    f"byte-size mismatch for {captured_file}: "
                    f"expected {record.size_bytes}, got {actual_size}"
                )

            actual_hash = sha256_file(captured_file)
            if actual_hash != record.sha256:
                problems.append(
                    f"sha256 mismatch for {captured_file}: "
                    f"expected {record.sha256}, got {actual_hash}"
                )

        problems.extend(
            f"unmanifested file: {capture_path / relative_path}"
            for relative_path in sorted(actual_files - expected_paths)
        )

    return VerificationReport(
        week_path=week_path,
        manifests_checked=manifests_checked,
        files_checked=files_checked,
        problems=tuple(problems),
    )


def collect_status(snapshot_root: Path) -> StatusReport:
    """Collect the last valid capture time for every kind in every initialized week."""

    statuses: list[WeekStatus] = []
    problems: list[str] = []

    for season, week, week_path in _iter_week_directories(snapshot_root):
        latest: dict[CaptureKind, datetime] = {}
        for capture_path in sorted(path for path in week_path.iterdir() if path.is_dir()):
            manifest_path = capture_path / MANIFEST_FILENAME
            if not manifest_path.is_file() or manifest_path.is_symlink():
                if any(path.is_file() for path in capture_path.rglob("*")):
                    problems.append(f"missing or invalid manifest: {manifest_path}")
                continue
            try:
                manifest = load_manifest(manifest_path)
            except (OSError, ValidationError) as error:
                problems.append(f"invalid manifest {manifest_path}: {error}")
                continue
            if manifest.season != season or manifest.week != week:
                problems.append(f"manifest season/week mismatch: {manifest_path}")
                continue

            for record in manifest.files:
                previous = latest.get(record.kind)
                if previous is None or record.observed_at > previous:
                    latest[record.kind] = record.observed_at

        statuses.append(WeekStatus(season=season, week=week, last_captured=dict(latest)))

    return StatusReport(weeks=tuple(statuses), problems=tuple(problems))


def format_utc(timestamp: datetime) -> str:
    """Format a timezone-aware timestamp as UTC ISO 8601 with a trailing Z."""

    normalized = _normalize_capture_time(timestamp)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_season_week(season: int, week: int) -> None:
    if season < 1:
        raise ValueError("season must be at least 1")
    if not 1 <= week <= 99:
        raise ValueError("week must be between 1 and 99")


def _validate_source_files(files: Sequence[Path]) -> None:
    if not files:
        raise ValueError("at least one source file is required")

    seen_names: set[str] = set()
    for source_file in files:
        if not source_file.is_file():
            raise ValueError(f"source file does not exist or is not a file: {source_file}")
        normalized_name = source_file.name.casefold()
        if normalized_name in seen_names:
            raise ValueError(
                f"source filenames must be unique within a capture: {source_file.name}"
            )
        seen_names.add(normalized_name)


def _validate_capture_inputs(inputs: Sequence[_CaptureInput]) -> None:
    seen_names: set[str] = set()
    for item in inputs:
        if not item.filename or Path(item.filename).name != item.filename:
            raise ValueError("capture filenames must be base filenames")
        if not item.source:
            raise ValueError("capture source must not be empty")
        if (item.source_path is None) == (item.content is None):
            raise ValueError("capture input must have exactly one content source")
        normalized_name = item.filename.casefold()
        if normalized_name in seen_names:
            raise ValueError(f"capture filenames must be unique: {item.filename}")
        seen_names.add(normalized_name)


def _write_capture(
    snapshot_root: Path,
    season: int,
    week: int,
    kind: CaptureKind,
    inputs: Sequence[_CaptureInput],
    *,
    requests: Sequence[SnapshotRequest] = (),
    errors: Sequence[SnapshotError] = (),
    captured_at: datetime,
) -> Path:
    _validate_capture_inputs(inputs)
    week_path = initialize_week(snapshot_root, season, week)
    capture_path = _allocate_capture_directory(week_path, captured_at)

    try:
        kind_path = capture_path / kind.value
        kind_path.mkdir()
        records: list[SnapshotFile] = []
        for item in inputs:
            destination = kind_path / item.filename
            if item.source_path is not None:
                shutil.copyfile(item.source_path, destination)
            else:
                assert item.content is not None
                with destination.open("xb") as captured_file:
                    captured_file.write(item.content)
            records.append(
                SnapshotFile(
                    path=destination.relative_to(capture_path).as_posix(),
                    sha256=sha256_file(destination),
                    size_bytes=destination.stat().st_size,
                    original_filename=item.filename,
                    observed_at=item.observed_at,
                    source=item.source,
                    kind=kind,
                )
            )

        manifest = SnapshotManifest(
            season=season,
            week=week,
            captured_at=captured_at,
            files=tuple(records),
            requests=tuple(requests),
            errors=tuple(errors),
        )
        # Write-then-rename with fsync so a kill mid-capture can never leave a
        # truncated manifest that verify would have to distinguish from corruption.
        manifest_path = capture_path / MANIFEST_FILENAME
        temporary_path = capture_path / f"{MANIFEST_FILENAME}.tmp"
        with temporary_path.open("x", encoding="utf-8") as manifest_file:
            manifest_file.write(manifest.model_dump_json(indent=2))
            manifest_file.write("\n")
            manifest_file.flush()
            os.fsync(manifest_file.fileno())
        os.replace(temporary_path, manifest_path)
    except Exception:
        shutil.rmtree(capture_path)
        raise

    return capture_path


def _normalize_capture_time(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("capture timestamp must include a timezone")
    return timestamp.astimezone(UTC)


def _allocate_capture_directory(week_path: Path, capture_time: datetime) -> Path:
    candidate_time = capture_time
    while True:
        candidate = week_path / format_utc(candidate_time)
        try:
            candidate.mkdir()
        except FileExistsError:
            candidate_time += timedelta(microseconds=1)
        else:
            return candidate


def _scan_capture_files(capture_path: Path) -> tuple[set[str], list[str]]:
    files: set[str] = set()
    problems: list[str] = []
    manifest_path = capture_path / MANIFEST_FILENAME

    for path in capture_path.rglob("*"):
        if path == manifest_path:
            continue
        if path.is_symlink():
            problems.append(f"unsupported symlink in capture: {path}")
        elif path.is_file():
            files.add(path.relative_to(capture_path).as_posix())
    return files, problems


def _path_contains_symlink(capture_path: Path, relative_path: str) -> bool:
    candidate = capture_path
    for part in PurePosixPath(relative_path).parts:
        candidate /= part
        if candidate.is_symlink():
            return True
    return False


def _iter_week_directories(snapshot_root: Path) -> Iterator[tuple[int, int, Path]]:
    if not snapshot_root.is_dir():
        return

    week_directories: list[tuple[int, int, Path]] = []
    for season_path in snapshot_root.iterdir():
        if not season_path.is_dir() or not season_path.name.isdecimal():
            continue
        season = int(season_path.name)
        for week_path in season_path.iterdir():
            match = _WEEK_DIRECTORY_PATTERN.fullmatch(week_path.name)
            if week_path.is_dir() and match is not None:
                week_directories.append((season, int(match.group(1)), week_path))

    yield from sorted(week_directories, key=lambda item: (item[0], item[1]))
