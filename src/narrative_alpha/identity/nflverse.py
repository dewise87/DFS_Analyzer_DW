"""Pinned nflverse roster fetch/cache and canonical-player seeding."""

from __future__ import annotations

import csv
import hashlib
import io
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

NFLVERSE_SOURCE = "nflverse"
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_ATTEMPTS = 3
INITIAL_BACKOFF_SECONDS = 0.25
_REQUIRED_COLUMNS = frozenset(
    {"season", "team", "position", "status", "full_name", "birth_date", "gsis_id", "week"}
)


@dataclass(frozen=True)
class PinnedRosterRelease:
    """A manually versioned roster artifact accepted by the seed process."""

    season: int
    url: str
    sha256: str


# Manual updates are intentional: a changed upstream roster is rejected until its hash is reviewed.
PINNED_ROSTER_RELEASES: Mapping[int, PinnedRosterRelease] = {
    2026: PinnedRosterRelease(
        season=2026,
        url="https://github.com/nflverse/nflverse-data/releases/download/rosters/roster_2026.csv",
        sha256="fa89e8c9766c6ea02b943a7a50465370b50dad5cfb76ee6ea6e287d13840ec63",
    )
}


class NflverseRosterError(RuntimeError):
    """Base error for untrusted or unreadable nflverse roster artifacts."""


class RosterHashError(NflverseRosterError):
    """Raised when bytes do not match the manually reviewed release hash."""


class RosterSchemaError(NflverseRosterError):
    """Raised when the pinned file's required columns have drifted."""


class RosterSeedIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row_number: int = Field(ge=2)
    reason: str


class RosterSeedReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rows_seen: int = Field(ge=0)
    players_seeded: int = Field(ge=0)
    players_existing: int = Field(ge=0)
    rows_rejected: int = Field(ge=0)
    issues: tuple[RosterSeedIssue, ...] = ()
    source_version: str


def pinned_roster_release(season: int) -> PinnedRosterRelease:
    """Return a reviewed roster release or fail until a maintainer pins one."""

    try:
        return PINNED_ROSTER_RELEASES[season]
    except KeyError as error:
        raise NflverseRosterError(
            f"no nflverse roster release is pinned for season {season}; review and add its hash"
        ) from error


def fetch_pinned_roster(
    release: PinnedRosterRelease,
    cache_dir: Path,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Fetch once, verify the exact bytes, and cache under a hash-bearing filename."""

    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"roster_{release.season}_{release.sha256[:16]}.csv"
    if target.exists():
        _verify_hash(target.read_bytes(), release)
        return target

    owns_client = client is None
    http_client = client or httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True)
    try:
        last_error: httpx.HTTPError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = http_client.get(release.url)
                response.raise_for_status()
            except httpx.HTTPError as error:
                last_error = error
                if attempt < MAX_ATTEMPTS:
                    sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                    continue
                break
            _verify_hash(response.content, release)
            temporary = target.with_suffix(".csv.partial")
            temporary.write_bytes(response.content)
            temporary.replace(target)
            return target
        assert last_error is not None
        raise NflverseRosterError(
            f"failed to fetch pinned nflverse roster after {MAX_ATTEMPTS} attempts: "
            f"{type(last_error).__name__}"
        ) from last_error
    finally:
        if owns_client:
            http_client.close()


def seed_nflverse_roster(
    connection: sqlite3.Connection,
    roster_path: Path,
    release: PinnedRosterRelease,
    *,
    observed_at: datetime,
    run_id: str | None = None,
) -> RosterSeedReport:
    """Seed canonical players and append temporal roster membership from pinned bytes."""

    observed_at = _utc(observed_at)
    raw_bytes = roster_path.read_bytes()
    _verify_hash(raw_bytes, release)
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise NflverseRosterError("pinned nflverse roster is not UTF-8") from error
    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = frozenset(reader.fieldnames or ())
    missing = tuple(sorted(_REQUIRED_COLUMNS - headers))
    if missing:
        raise RosterSchemaError(
            f"nflverse roster is missing required columns: {', '.join(missing)}"
        )

    rows = list(reader)
    latest: dict[str, tuple[int, dict[str, str | None]]] = {}
    issues: list[RosterSeedIssue] = []
    for row_number, row in enumerate(rows, start=2):
        gsis_id = (row.get("gsis_id") or "").strip()
        full_name = (row.get("full_name") or "").strip()
        team = (row.get("team") or "").strip().upper()
        if not gsis_id or not full_name or not team:
            issues.append(
                RosterSeedIssue(
                    row_number=row_number,
                    reason="missing gsis_id, full_name, or team",
                )
            )
            continue
        prior = latest.get(gsis_id)
        if prior is None or _week_number(row.get("week")) >= _week_number(prior[1].get("week")):
            latest[gsis_id] = (row_number, row)

    source_version = f"nflverse-roster-{release.season}:sha256:{release.sha256}"
    timestamp = _timestamp(observed_at)
    players_seeded = 0
    players_existing = 0
    for _, row in sorted(latest.values(), key=lambda item: item[0]):
        gsis_id = (row.get("gsis_id") or "").strip()
        existing = connection.execute(
            """
            SELECT player_id FROM external_player_ids
            WHERE source = ? AND site IS NULL AND external_player_id = ?
              AND valid_to IS NULL
            ORDER BY observed_at DESC LIMIT 1
            """,
            (NFLVERSE_SOURCE, gsis_id),
        ).fetchone()
        if existing is None:
            existing = connection.execute(
                """
                SELECT player_id FROM players
                WHERE player_key = ? AND valid_to IS NULL
                ORDER BY observed_at DESC LIMIT 1
                """,
                (gsis_id,),
            ).fetchone()

        if existing is None:
            cursor = connection.execute(
                """
                INSERT INTO players(
                    player_key, canonical_name, position, birth_date, source,
                    published_at, observed_at, ingested_at, effective_at,
                    valid_from, valid_to, source_version, run_id
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?, NULL, ?, ?)
                """,
                (
                    gsis_id,
                    (row.get("full_name") or "").strip(),
                    _optional_upper(row.get("position")),
                    _optional(row.get("birth_date")),
                    NFLVERSE_SOURCE,
                    timestamp,
                    timestamp,
                    timestamp,
                    source_version,
                    run_id,
                ),
            )
            if cursor.lastrowid is None:  # pragma: no cover - sqlite INSERT contract
                raise NflverseRosterError("SQLite did not return a canonical player ID")
            player_id = int(cursor.lastrowid)
            players_seeded += 1
        else:
            player_id = int(existing["player_id"])
            players_existing += 1

        connection.execute(
            """
            INSERT OR IGNORE INTO external_player_ids(
                player_id, source, site, external_player_id, published_at,
                observed_at, ingested_at, effective_at, valid_from, valid_to,
                source_version, run_id, match_method, match_confidence, manual_override
            ) VALUES (?, ?, NULL, ?, NULL, ?, ?, NULL, ?, NULL, ?, ?, 'seed', 1.0, 0)
            """,
            (
                player_id,
                NFLVERSE_SOURCE,
                gsis_id,
                timestamp,
                timestamp,
                timestamp,
                source_version,
                run_id,
            ),
        )
        _append_team_membership(
            connection,
            player_id,
            row,
            timestamp=timestamp,
            source_version=source_version,
            run_id=run_id,
        )

    return RosterSeedReport(
        rows_seen=len(rows),
        players_seeded=players_seeded,
        players_existing=players_existing,
        rows_rejected=len(issues),
        issues=tuple(issues),
        source_version=source_version,
    )


def _append_team_membership(
    connection: sqlite3.Connection,
    player_id: int,
    row: dict[str, str | None],
    *,
    timestamp: str,
    source_version: str,
    run_id: str | None,
) -> None:
    team = (row.get("team") or "").strip().upper()
    position = _optional_upper(row.get("position"))
    status = _optional_upper(row.get("status"))
    current = connection.execute(
        """
        SELECT player_team_history_id, team, position, roster_status
        FROM player_team_history
        WHERE player_id = ? AND valid_to IS NULL
        ORDER BY observed_at DESC LIMIT 1
        """,
        (player_id,),
    ).fetchone()
    state = (team, position, status)
    if current is not None and state == (
        str(current["team"]),
        current["position"],
        current["roster_status"],
    ):
        return
    if current is not None:
        connection.execute(
            "UPDATE player_team_history SET valid_to = ? WHERE player_team_history_id = ?",
            (timestamp, int(current["player_team_history_id"])),
        )
    connection.execute(
        """
        INSERT INTO player_team_history(
            player_id, team, position, roster_status, season, week, source,
            published_at, observed_at, ingested_at, effective_at, valid_from,
            valid_to, source_version, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?, NULL, ?, ?)
        """,
        (
            player_id,
            team,
            position,
            status,
            int((row.get("season") or "0").strip()),
            _week_number(row.get("week")) or None,
            NFLVERSE_SOURCE,
            timestamp,
            timestamp,
            timestamp,
            source_version,
            run_id,
        ),
    )


def _verify_hash(content: bytes, release: PinnedRosterRelease) -> None:
    actual = hashlib.sha256(content).hexdigest()
    if actual != release.sha256:
        raise RosterHashError(
            f"nflverse roster hash mismatch for {release.season}: "
            f"expected {release.sha256}, got {actual}"
        )


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _optional_upper(value: str | None) -> str | None:
    normalized = _optional(value)
    return None if normalized is None else normalized.upper()


def _week_number(value: str | None) -> int:
    try:
        return int((value or "0").strip())
    except ValueError:
        return 0


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must include a timezone")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
