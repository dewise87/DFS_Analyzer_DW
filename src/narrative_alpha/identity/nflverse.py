"""Pinned nflverse roster fetch/cache and canonical-player seeding."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType

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
    reviewed_at: date


@dataclass(frozen=True)
class RosterPlayerChange:
    """Fields that changed for one GSIS player between two reviewed roster files."""

    gsis_id: str
    full_name: str
    fields: tuple[tuple[str, str | None, str | None], ...]


@dataclass(frozen=True)
class RosterRefreshReport:
    """Non-mutating comparison of the rolling roster asset with the newest pin."""

    season: int
    url: str
    reviewed_at: date
    sha256: str
    compared_with: PinnedRosterRelease
    added: tuple[tuple[str, str], ...]
    removed: tuple[tuple[str, str], ...]
    changed: tuple[RosterPlayerChange, ...]
    issues: tuple[RosterSeedIssue, ...] = ()

    def render(self) -> str:
        """Render the review summary and a syntactically valid pin entry."""

        lines = [
            f"season={self.season}",
            f"url={self.url}",
            f"sha256={self.sha256}",
            f"compared_pin_reviewed_at={self.compared_with.reviewed_at.isoformat()}",
            f"players_added={len(self.added)}",
        ]
        lines.extend(f"  + {gsis_id} {name}" for gsis_id, name in self.added)
        lines.append(f"players_removed={len(self.removed)}")
        lines.extend(f"  - {gsis_id} {name}" for gsis_id, name in self.removed)
        lines.append(f"players_changed={len(self.changed)}")
        for change in self.changed:
            details = "; ".join(
                f"{field}: {before or '-'} -> {after or '-'}"
                for field, before, after in change.fields
            )
            lines.append(f"  ~ {change.gsis_id} {change.full_name}: {details}")
        lines.append(f"rows_rejected={len(self.issues)}")
        lines.extend(f"  ! row {issue.row_number}: {issue.reason}" for issue in self.issues)
        lines.extend(
            (
                "paste_entry:",
                "PinnedRosterRelease(",
                f"    season={self.season},",
                f"    url={self.url!r},",
                f"    sha256={self.sha256!r},",
                "    reviewed_at="
                f"date({self.reviewed_at.year}, {self.reviewed_at.month}, {self.reviewed_at.day}),",
                ")",
            )
        )
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class _RosterPlayer:
    gsis_id: str
    full_name: str
    team: str
    position: str | None
    status: str | None
    birth_date: str | None


ROLLING_ROSTER_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/rosters/roster_{season}.csv"
)

# A refresh appends a reviewed entry; it never edits an older pin. Historical selection plus
# the content-addressed byte archive makes an earlier decision reproducible after the rolling
# upstream URL has moved on.
PINNED_ROSTER_RELEASES: Mapping[int, tuple[PinnedRosterRelease, ...]] = MappingProxyType(
    {
        2026: (
            PinnedRosterRelease(
                season=2026,
                url=ROLLING_ROSTER_URL.format(season=2026),
                sha256=(
                    "fa89e8c9766c6ea02b943a7a50465370b50dad5cfb76ee6ea6e287d13840ec63"
                ),
                reviewed_at=date(2026, 9, 1),
            ),
        )
    }
)


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


def pinned_roster_release(
    season: int,
    as_of: date | datetime,
    *,
    releases: Mapping[int, tuple[PinnedRosterRelease, ...]] = PINNED_ROSTER_RELEASES,
) -> PinnedRosterRelease:
    """Return the newest reviewed release available on ``as_of``; never look ahead."""

    cutoff = as_of.date() if isinstance(as_of, datetime) else as_of
    eligible = tuple(
        release
        for release in releases.get(season, ())
        if release.season == season and release.reviewed_at <= cutoff
    )
    if not eligible:
        raise NflverseRosterError(
            f"no nflverse roster release is pinned for season {season} at or before "
            f"{cutoff.isoformat()}; review and add its hash"
        )
    return _newest_pin(eligible)


def _newest_pin(pins: tuple[PinnedRosterRelease, ...]) -> PinnedRosterRelease:
    """Newest ``reviewed_at`` wins; a same-day re-pin later in the table beats an earlier one."""

    return max(enumerate(pins), key=lambda item: (item[1].reviewed_at, item[0]))[1]


def roster_archive_path(archive_dir: Path, sha256: str) -> Path:
    """Return the content-addressed path for exact roster bytes."""

    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise NflverseRosterError("nflverse roster sha256 must be 64 lowercase hexadecimal chars")
    return archive_dir / "sha256" / sha256[:2] / f"{sha256}.csv"


def fetch_pinned_roster(
    release: PinnedRosterRelease,
    archive_dir: Path,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Return verified bytes from the local archive, fetching only on an archive miss."""

    archive_dir.mkdir(parents=True, exist_ok=True)
    target = roster_archive_path(archive_dir, release.sha256)
    if target.exists():
        archived_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        if archived_sha256 != release.sha256:
            raise RosterHashError(
                f"archived nflverse roster bytes at {target} do not match the reviewed hash "
                f"(expected {release.sha256}, got {archived_sha256}); the local archive file is "
                "corrupt — delete it to refetch"
            )
        return target

    owns_client = client is None
    http_client = client or httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True)
    try:
        content = _fetch_roster_bytes(http_client, release.url, sleep=sleep)
        _verify_hash(content, release)
        return _archive_bytes(archive_dir, content, release.sha256)
    finally:
        if owns_client:
            http_client.close()


def _archive_bytes(archive_dir: Path, content: bytes, sha256: str) -> Path:
    """Write ``content`` at its content-addressed path atomically; the hash must already hold."""

    if hashlib.sha256(content).hexdigest() != sha256:
        raise RosterHashError("refusing to archive roster bytes under a hash they do not match")
    target = roster_archive_path(archive_dir, sha256)
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".csv.partial")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(target)
    return target


def refresh_roster_release(
    season: int,
    archive_dir: Path,
    *,
    reviewed_at: date,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    releases: Mapping[int, tuple[PinnedRosterRelease, ...]] = PINNED_ROSTER_RELEASES,
    today: date | None = None,
) -> RosterRefreshReport:
    """Fetch and compare the rolling asset without changing the reviewed pin table.

    The downloaded bytes are archived under their own sha256 so the entry this report
    prints is fetchable offline once pasted, even after upstream overwrites the rolling
    asset again. Archiving under a self-computed hash trusts nothing: the pin table stays
    the only authority on which hash seeding may use.
    """

    pins = tuple(release for release in releases.get(season, ()) if release.season == season)
    if not pins:
        raise NflverseRosterError(
            f"no nflverse roster release is pinned for season {season}; "
            "a first pin requires manual review"
        )
    current_day = today or datetime.now(UTC).date()
    if reviewed_at > current_day:
        raise NflverseRosterError(
            f"reviewed_at {reviewed_at.isoformat()} is in the future (today is "
            f"{current_day.isoformat()}); as-of selection could never choose that pin"
        )
    newest = _newest_pin(pins)
    if reviewed_at < newest.reviewed_at:
        raise NflverseRosterError(
            f"reviewed_at {reviewed_at.isoformat()} precedes newest pin "
            f"{newest.reviewed_at.isoformat()}"
        )

    owns_client = client is None
    http_client = client or httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True)
    try:
        # Resolve this before fetching the rolling URL. If upstream already moved and the old
        # bytes were never archived, a truthful player diff is impossible and must fail closed.
        prior_path = fetch_pinned_roster(
            newest,
            archive_dir,
            client=http_client,
            sleep=sleep,
        )
        url = ROLLING_ROSTER_URL.format(season=season)
        current_bytes = _fetch_roster_bytes(http_client, url, sleep=sleep)
    finally:
        if owns_client:
            http_client.close()

    current_sha256 = hashlib.sha256(current_bytes).hexdigest()
    _archive_bytes(archive_dir, current_bytes, current_sha256)

    prior, _ = _roster_players(prior_path.read_bytes())
    current, issues = _roster_players(current_bytes)
    added = tuple(
        (gsis_id, current[gsis_id].full_name) for gsis_id in sorted(current.keys() - prior.keys())
    )
    removed = tuple(
        (gsis_id, prior[gsis_id].full_name) for gsis_id in sorted(prior.keys() - current.keys())
    )
    changed: list[RosterPlayerChange] = []
    compared_fields = ("full_name", "team", "position", "status", "birth_date")
    for gsis_id in sorted(prior.keys() & current.keys()):
        before = prior[gsis_id]
        after = current[gsis_id]
        fields = tuple(
            (field, getattr(before, field), getattr(after, field))
            for field in compared_fields
            if getattr(before, field) != getattr(after, field)
        )
        if fields:
            changed.append(
                RosterPlayerChange(
                    gsis_id=gsis_id,
                    full_name=after.full_name,
                    fields=fields,
                )
            )
    return RosterRefreshReport(
        season=season,
        url=url,
        reviewed_at=reviewed_at,
        sha256=current_sha256,
        compared_with=newest,
        added=added,
        removed=removed,
        changed=tuple(changed),
        issues=issues,
    )


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

    source_version = (
        f"nflverse-roster-{release.season}:reviewed:{release.reviewed_at.isoformat()}:"
        f"sha256:{release.sha256}"
    )
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


def _fetch_roster_bytes(
    client: httpx.Client,
    url: str,
    *,
    sleep: Callable[[float], None],
) -> bytes:
    last_error: httpx.HTTPError | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as error:
            last_error = error
            if attempt < MAX_ATTEMPTS:
                sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            break
        return response.content
    assert last_error is not None
    raise NflverseRosterError(
        f"failed to fetch nflverse roster after {MAX_ATTEMPTS} attempts: "
        f"{type(last_error).__name__}"
    ) from last_error


def _roster_players(
    content: bytes,
) -> tuple[dict[str, _RosterPlayer], tuple[RosterSeedIssue, ...]]:
    """Parse the latest row per GSIS id, returning the rows that could not be used."""

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise NflverseRosterError("nflverse roster is not UTF-8") from error
    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = frozenset(reader.fieldnames or ())
    missing = tuple(sorted(_REQUIRED_COLUMNS - headers))
    if missing:
        raise RosterSchemaError(
            f"nflverse roster is missing required columns: {', '.join(missing)}"
        )

    latest: dict[str, tuple[int, _RosterPlayer]] = {}
    issues: list[RosterSeedIssue] = []
    for row_number, row in enumerate(reader, start=2):
        gsis_id = (row.get("gsis_id") or "").strip()
        full_name = (row.get("full_name") or "").strip()
        team = (row.get("team") or "").strip().upper()
        if not gsis_id or not full_name or not team:
            blank = [
                name
                for name, value in (("gsis_id", gsis_id), ("full_name", full_name), ("team", team))
                if not value
            ]
            issues.append(
                RosterSeedIssue(row_number=row_number, reason=f"blank {', '.join(blank)}")
            )
            continue
        player = _RosterPlayer(
            gsis_id=gsis_id,
            full_name=full_name,
            team=team,
            position=_optional_upper(row.get("position")),
            status=_optional_upper(row.get("status")),
            birth_date=_optional(row.get("birth_date")),
        )
        week = _week_number(row.get("week"))
        prior = latest.get(gsis_id)
        if prior is not None and week == prior[0] and prior[1] != player:
            issues.append(
                RosterSeedIssue(
                    row_number=row_number,
                    reason=f"conflicting duplicate row for {gsis_id} in week {week}",
                )
            )
        if prior is None or week >= prior[0]:
            latest[gsis_id] = (week, player)
    return {gsis_id: item[1] for gsis_id, item in latest.items()}, tuple(issues)


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
