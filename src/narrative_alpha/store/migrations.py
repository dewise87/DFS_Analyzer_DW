"""Small, transactional migration runner for numbered SQL files."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_MIGRATIONS_PATH = Path(__file__).with_name("migrations")
_MIGRATION_PATTERN = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_TRANSACTION_KEYWORDS = {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"}


class MigrationError(RuntimeError):
    """Base error raised for invalid, drifted, or failed migrations."""


class MigrationDriftError(MigrationError):
    """Raised when an already-applied migration file has changed."""


@dataclass(frozen=True)
class Migration:
    """A validated migration file discovered on disk."""

    version: int
    name: str
    path: Path
    sha256: str
    sql: str


@dataclass(frozen=True)
class AppliedMigration:
    """A migration applied during the current runner invocation."""

    version: int
    name: str
    sha256: str
    applied_at: datetime


def apply_migrations(
    connection: sqlite3.Connection,
    migrations_path: Path = DEFAULT_MIGRATIONS_PATH,
) -> tuple[AppliedMigration, ...]:
    """Apply pending migrations in version order, one transaction per file."""

    _ensure_migrations_table(connection)
    migrations = discover_migrations(migrations_path)
    existing = {
        int(row[0]): (str(row[1]), str(row[2]))
        for row in connection.execute(
            "SELECT version, name, sha256 FROM applied_migrations ORDER BY version"
        )
    }

    applied: list[AppliedMigration] = []
    for migration in migrations:
        prior = existing.get(migration.version)
        if prior is not None:
            prior_name, prior_sha256 = prior
            if prior_name != migration.name or prior_sha256 != migration.sha256:
                raise MigrationDriftError(
                    f"migration {migration.version:04d} differs from the applied record"
                )
            continue

        applied_at = datetime.now(UTC)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in _iter_sql_statements(migration.sql):
                keyword = _statement_keyword(statement)
                if keyword in _TRANSACTION_KEYWORDS:
                    raise MigrationError(
                        f"migration {migration.name} contains transaction control {keyword}"
                    )
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO applied_migrations(version, name, sha256, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    migration.version,
                    migration.name,
                    migration.sha256,
                    _format_utc(applied_at),
                ),
            )
        except Exception as error:
            connection.rollback()
            if isinstance(error, MigrationError):
                raise
            raise MigrationError(f"migration {migration.name} failed: {error}") from error
        else:
            connection.commit()

        applied.append(
            AppliedMigration(
                version=migration.version,
                name=migration.name,
                sha256=migration.sha256,
                applied_at=applied_at,
            )
        )
        existing[migration.version] = (migration.name, migration.sha256)

    return tuple(applied)


def discover_migrations(migrations_path: Path) -> tuple[Migration, ...]:
    """Read and validate numbered ``NNNN_name.sql`` migration files."""

    if not migrations_path.is_dir():
        raise MigrationError(f"migrations directory does not exist: {migrations_path}")

    migrations: list[Migration] = []
    seen_versions: set[int] = set()
    for path in sorted(migrations_path.iterdir()):
        if not path.is_file() or path.suffix != ".sql":
            continue
        match = _MIGRATION_PATTERN.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"invalid migration filename: {path.name}")
        version = int(match.group(1))
        if version in seen_versions:
            raise MigrationError(f"duplicate migration version: {version:04d}")
        seen_versions.add(version)
        raw_sql = path.read_bytes()
        try:
            sql = raw_sql.decode("utf-8")
        except UnicodeDecodeError as error:
            raise MigrationError(f"migration is not UTF-8: {path.name}") from error
        migrations.append(
            Migration(
                version=version,
                name=path.name,
                path=path,
                sha256=hashlib.sha256(raw_sql).hexdigest(),
                sql=sql,
            )
        )

    return tuple(sorted(migrations, key=lambda migration: migration.version))


def _ensure_migrations_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS applied_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
            applied_at TEXT NOT NULL
        ) STRICT
        """
    )
    connection.commit()


def _iter_sql_statements(sql: str) -> Iterator[str]:
    buffer = ""
    for character in sql:
        buffer += character
        if character == ";" and sqlite3.complete_statement(buffer):
            if _strip_comments(buffer).strip():
                yield buffer
            buffer = ""
    if _strip_comments(buffer).strip():
        raise MigrationError("migration ends with an incomplete SQL statement")


def _statement_keyword(statement: str) -> str:
    without_comments = _strip_comments(statement).lstrip()
    match = re.match(r"([A-Za-z]+)", without_comments)
    return "" if match is None else match.group(1).upper()


def _strip_comments(sql: str) -> str:
    without_blocks = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*(?:\n|$)", "\n", without_blocks)


def _format_utc(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
