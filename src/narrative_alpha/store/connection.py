"""Thin SQLite connection management for the operational store."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class StoreConfigurationError(RuntimeError):
    """Raised when SQLite cannot enable a required safety setting."""


@contextmanager
def connect_database(database_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Open a WAL-mode SQLite connection with foreign keys enforced.

    The context commits successful work, rolls back on exceptions, and always closes the
    connection. A file-backed database is required because SQLite cannot use WAL for an
    in-memory database.
    """

    path = Path(database_path)
    if str(database_path) == ":memory:":
        raise StoreConfigurationError("WAL mode requires a file-backed SQLite database")
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
        if foreign_keys != 1:
            raise StoreConfigurationError("could not enable SQLite foreign keys")

        journal_mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
        if journal_mode.casefold() != "wal":
            raise StoreConfigurationError(
                f"could not enable SQLite WAL mode; SQLite reported {journal_mode!r}"
            )
        connection.execute("PRAGMA busy_timeout = 10000")

        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
    finally:
        connection.close()
