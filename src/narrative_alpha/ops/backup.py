"""Verified, generation-based backup and out-of-place restore for operator state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.store import DEFAULT_MIGRATIONS_PATH, inspect_migrations

DEFAULT_BACKUP_DIRECTORY = Path("data/backups")
BACKUP_MANIFEST_FILENAME = "manifest.json"
BACKUP_FORMAT_VERSION = 1
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_STAMP_PATTERN = re.compile(r"^\d{8}T\d{6}Z$")


class BackupError(RuntimeError):
    """A backup or restore could not be proven complete and internally consistent."""


@dataclass(frozen=True)
class BackupFile:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class BackupReport:
    stamp: str
    path: Path
    manifest_path: Path
    files: tuple[BackupFile, ...]
    pruned: tuple[Path, ...]


@dataclass(frozen=True)
class RestoreReport:
    backup_stamp: str
    path: Path
    database: Path
    artifact_directory: Path
    files_verified: int
    row_counts: dict[str, int]

    @property
    def flags(self) -> str:
        return (
            f"--database {shlex.quote(str(self.database))} "
            f"--artifact-directory {shlex.quote(str(self.artifact_directory))}"
        )


def backup_stamp(now: datetime) -> str:
    return ensure_utc(now).strftime(_STAMP_FORMAT)


def create_backup(
    *,
    database: Path,
    artifact_directory: Path,
    report_directory: Path,
    pin_archive: Path,
    snapshot_root: Path,
    backup_directory: Path = DEFAULT_BACKUP_DIRECTORY,
    include_snapshots: bool = False,
    keep_newest: int = 14,
    now: datetime | None = None,
) -> BackupReport:
    """Create one atomic backup generation and prune older complete generations.

    SQLite is copied only through its online backup API.  Every other payload is copied
    as ordinary immutable/artifact files and is hashed after it reaches the generation.
    """

    if keep_newest < 1:
        raise BackupError("backup retention keep_newest must be at least 1")
    source_database = database.resolve()
    if not source_database.is_file():
        raise BackupError(f"database does not exist: {database}")

    sources = {
        "artifacts": artifact_directory.resolve(),
        "reports": report_directory.resolve(),
        "pins": pin_archive.resolve(),
    }
    if include_snapshots:
        sources["snapshots"] = snapshot_root.resolve()
    for label, source in sources.items():
        if not source.is_dir():
            raise BackupError(
                f"{label} directory does not exist: {source}; create it before backing up"
            )

    root = backup_directory.resolve()
    for label, source in sources.items():
        if _is_within(root, source):
            raise BackupError(
                f"backup directory {root} is inside the {label} source {source}; refusing recursion"
            )
    root.mkdir(parents=True, exist_ok=True)
    created_at = ensure_utc(now or datetime.now(UTC))
    stamp = backup_stamp(created_at)
    final = root / stamp
    if final.exists():
        raise BackupError(f"backup generation already exists: {final}")

    staging = Path(tempfile.mkdtemp(prefix=f".{stamp}-partial-", dir=root))
    try:
        database_relative = PurePosixPath("store") / source_database.name
        destination_database = staging / Path(database_relative)
        destination_database.parent.mkdir(parents=True, exist_ok=True)
        _online_backup(source_database, destination_database)
        row_counts, applied_migrations = _database_inventory(destination_database)

        receipt_source = source_database.with_name(source_database.name + ".stage1-receipts")
        if receipt_source.exists():
            if not receipt_source.is_dir():
                raise BackupError(
                    f"accepted-batch receipt path is not a directory: {receipt_source}"
                )
            _copy_tree(
                receipt_source,
                staging / "store" / receipt_source.name,
            )

        for label, source in sources.items():
            _copy_tree(source, staging / label)

        files = _inventory_files(staging)
        manifest = {
            "format_version": BACKUP_FORMAT_VERSION,
            "created_at": utc_timestamp(created_at),
            "stamp": stamp,
            "included_snapshots": include_snapshots,
            "sources": {
                "database": str(source_database),
                **{label: str(path) for label, path in sources.items()},
            },
            "database": {
                "path": database_relative.as_posix(),
                "row_counts": row_counts,
                "applied_migrations": applied_migrations,
            },
            "artifact_directory": "artifacts",
            "files": [file.__dict__ for file in files],
        }
        manifest_path = staging / BACKUP_MANIFEST_FILENAME
        _write_json(manifest_path, manifest)
        staging.rename(final)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    pruned = _prune_generations(root, keep_newest=keep_newest)
    return BackupReport(
        stamp=stamp,
        path=final,
        manifest_path=final / BACKUP_MANIFEST_FILENAME,
        files=files,
        pruned=pruned,
    )


def restore_backup(
    *,
    backup: str,
    into: Path,
    backup_directory: Path = DEFAULT_BACKUP_DIRECTORY,
    migrations_path: Path = DEFAULT_MIGRATIONS_PATH,
) -> RestoreReport:
    """Verify and restore a named generation into a new directory, never in place."""

    if _STAMP_PATTERN.fullmatch(backup) is None:
        raise BackupError("--backup must be a UTC stamp like 20260904T021500Z")
    source = backup_directory.resolve() / backup
    if not source.is_dir() or source.is_symlink():
        raise BackupError(f"backup generation does not exist or is not a real directory: {source}")
    manifest_path = source / BACKUP_MANIFEST_FILENAME
    manifest = _read_manifest(manifest_path)
    files = _manifest_files(manifest)
    _verify_generation(source, files)

    destination = into.resolve()
    if destination.exists():
        raise BackupError(
            f"restore destination already exists; refusing to overwrite: {destination}"
        )
    if destination == source or _is_within(destination, source):
        raise BackupError("restore destination must not be the backup generation or inside it")
    source_paths = _mapping(manifest.get("sources"), "sources")
    original_locations = {
        Path(value).resolve()
        for value in source_paths.values()
        if isinstance(value, str) and value
    }
    database_source = source_paths.get("database")
    if isinstance(database_source, str) and database_source:
        original_locations.add(Path(database_source).resolve().parent)
    if destination in original_locations:
        raise BackupError(f"restore destination is an original live location: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-restore-", dir=destination.parent))
    try:
        for record in files:
            target = staging / Path(record.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / Path(record.path), target)
        shutil.copy2(manifest_path, staging / BACKUP_MANIFEST_FILENAME)
        _verify_generation(staging, files)

        database_block = _mapping(manifest.get("database"), "database")
        database_relative = _safe_relative_path(database_block.get("path"), "database.path")
        restored_database = staging / Path(database_relative)
        expected_counts = _integer_mapping(database_block.get("row_counts"), "row_counts")
        actual_counts, actual_migrations = _database_inventory(
            restored_database, migrations_path=migrations_path, require_current=True
        )
        if actual_counts != expected_counts:
            raise BackupError(
                "restored database row counts do not match the manifest: "
                f"expected {expected_counts}, got {actual_counts}"
            )
        expected_migrations = database_block.get("applied_migrations")
        if actual_migrations != expected_migrations:
            raise BackupError("restored database migration ledger does not match the manifest")

        artifact_relative = _safe_relative_path(
            manifest.get("artifact_directory"), "artifact_directory"
        )
        staging.rename(destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return RestoreReport(
        backup_stamp=backup,
        path=destination,
        database=destination / Path(database_relative),
        artifact_directory=destination / Path(artifact_relative),
        files_verified=len(files),
        row_counts=actual_counts,
    )


def newest_backup(
    backup_directory: Path = DEFAULT_BACKUP_DIRECTORY,
) -> tuple[str, datetime, Path] | None:
    """Return the newest complete UTC-stamped generation without modifying anything."""

    generations = _generation_directories(backup_directory)
    if not generations:
        return None
    path = generations[-1]
    try:
        created = datetime.strptime(path.name, _STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None
    return path.name, created, path


def _online_backup(source_path: Path, destination_path: Path) -> None:
    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    try:
        source = sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True, timeout=10.0)
        destination = sqlite3.connect(destination_path)
        source.backup(destination)
    except sqlite3.Error as error:
        raise BackupError(f"SQLite online backup failed: {error}") from error
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()


def _database_inventory(
    database: Path,
    *,
    migrations_path: Path = DEFAULT_MIGRATIONS_PATH,
    require_current: bool = False,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            if quick_check != "ok":
                raise BackupError(f"SQLite quick_check failed: {quick_check}")
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_keys:
                raise BackupError(
                    f"SQLite foreign_key_check found {len(foreign_keys)} violation(s)"
                )
            migration_status = inspect_migrations(connection, migrations_path)
            if require_current and migration_status.pending:
                names = ", ".join(migration.name for migration in migration_status.pending)
                raise BackupError(f"restored database has pending migration(s): {names}")
            tables = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            )
            counts = {
                table: int(
                    connection.execute(
                        f'SELECT count(*) FROM "{table.replace(chr(34), chr(34) * 2)}"'
                    ).fetchone()[0]
                )
                for table in tables
            }
            migration_rows = (
                []
                if connection.execute(
                    "SELECT 1 FROM sqlite_schema "
                    "WHERE type = 'table' AND name = 'applied_migrations'"
                ).fetchone()
                is None
                else [
                    {
                        "version": int(row["version"]),
                        "name": str(row["name"]),
                        "sha256": str(row["sha256"]),
                        "applied_at": str(row["applied_at"]),
                    }
                    for row in connection.execute(
                        "SELECT version, name, sha256, applied_at "
                        "FROM applied_migrations ORDER BY version"
                    )
                ]
            )
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise BackupError(f"cannot inspect SQLite backup {database}: {error}") from error
    return counts, migration_rows


def _copy_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise BackupError(f"refusing symlink in backup source: {path}")
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        else:
            raise BackupError(f"refusing non-regular backup source entry: {path}")


def _inventory_files(root: Path) -> tuple[BackupFile, ...]:
    records = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        relative = path.relative_to(root).as_posix()
        records.append(BackupFile(path=relative, sha256=_sha256(path), size=path.stat().st_size))
    return tuple(records)


def _verify_generation(root: Path, files: tuple[BackupFile, ...]) -> None:
    symlinks = tuple(path for path in root.rglob("*") if path.is_symlink())
    if symlinks:
        raise BackupError(f"backup generation contains a symlink: {symlinks[0]}")
    expected = {record.path for record in files}
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).as_posix() != BACKUP_MANIFEST_FILENAME
    }
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise BackupError(
            "backup file set does not match manifest"
            + (f"; missing: {', '.join(missing)}" if missing else "")
            + (f"; unexpected: {', '.join(extra)}" if extra else "")
        )
    for record in files:
        path = root / Path(record.path)
        size = path.stat().st_size
        digest = _sha256(path)
        if size != record.size or digest != record.sha256:
            raise BackupError(
                f"manifest mismatch for {record.path}: expected {record.size} bytes / "
                f"{record.sha256}, got {size} bytes / {digest}"
            )


def _manifest_files(manifest: dict[str, Any]) -> tuple[BackupFile, ...]:
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise BackupError("backup manifest files must be a list")
    records: list[BackupFile] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_files):
        item = _mapping(raw, f"files[{index}]")
        path = _safe_relative_path(item.get("path"), f"files[{index}].path").as_posix()
        sha256 = item.get("sha256")
        size = item.get("size")
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise BackupError(f"invalid sha256 in backup manifest for {path}")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise BackupError(f"invalid size in backup manifest for {path}")
        if path in seen:
            raise BackupError(f"duplicate path in backup manifest: {path}")
        seen.add(path)
        records.append(BackupFile(path=path, sha256=sha256, size=size))
    return tuple(records)


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackupError(f"cannot read backup manifest {path}: {error}") from error
    manifest = _mapping(raw, "manifest")
    if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        raise BackupError(f"unsupported backup manifest format: {manifest.get('format_version')!r}")
    if manifest.get("stamp") != path.parent.name:
        raise BackupError("backup manifest stamp does not match its directory")
    return manifest


def _write_json(path: Path, value: dict[str, Any]) -> None:
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with path.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def _generation_directories(root: Path) -> tuple[Path, ...]:
    try:
        return tuple(
            sorted(
                path
                for path in root.iterdir()
                if path.is_dir()
                and _STAMP_PATTERN.fullmatch(path.name)
                and (path / BACKUP_MANIFEST_FILENAME).is_file()
            )
        )
    except OSError:
        return ()


def _prune_generations(root: Path, *, keep_newest: int) -> tuple[Path, ...]:
    generations = _generation_directories(root)
    old = generations[:-keep_newest]
    for path in old:
        # Only strict UTC-stamped children of the resolved backup root reach this point.
        if path.parent != root or _STAMP_PATTERN.fullmatch(path.name) is None:
            raise BackupError(f"refusing unsafe backup prune target: {path}")
        shutil.rmtree(path)
    return old


def _safe_relative_path(value: Any, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise BackupError(f"backup manifest {field} must be a relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise BackupError(f"unsafe path in backup manifest {field}: {value!r}")
    return path


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise BackupError(f"backup manifest {field} must be an object")
    return value


def _integer_mapping(value: Any, field: str) -> dict[str, int]:
    mapping = _mapping(value, field)
    if not all(
        isinstance(item, int) and not isinstance(item, bool) and item >= 0
        for item in mapping.values()
    ):
        raise BackupError(f"backup manifest {field} must contain non-negative integers")
    return {key: int(item) for key, item in mapping.items()}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


__all__ = [
    "BACKUP_MANIFEST_FILENAME",
    "DEFAULT_BACKUP_DIRECTORY",
    "BackupError",
    "BackupFile",
    "BackupReport",
    "RestoreReport",
    "backup_stamp",
    "create_backup",
    "newest_backup",
    "restore_backup",
]
