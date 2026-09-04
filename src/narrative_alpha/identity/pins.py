"""One reviewed-pin mechanism: dated selection, hash verification, byte archive.

Slice 16 built this for the nflverse roster. Slice 37 needed the same discipline for the
weekly workload files and generalized it here rather than adding a second downloader, so
there is exactly one place that decides which bytes a run is allowed to read.

Three rules hold for every pinned artifact:

* **Dated selection never looks ahead.** :func:`pinned_release` returns the newest entry
  reviewed at or before the caller's as-of date, so replaying an old decision reads the
  file that decision could actually have seen.
* **A hash is the identity.** Bytes that do not match the manually reviewed hash are
  refused; nothing is archived under a hash it does not match.
* **The archive outlives the URL.** Verified bytes are stored content-addressed, so a pin
  stays fetchable after the rolling upstream asset has been overwritten.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import ClassVar

import httpx

HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_ATTEMPTS = 3
INITIAL_BACKOFF_SECONDS = 0.25


class NflversePinError(RuntimeError):
    """Base error for an unpinned, untrusted, or unreadable upstream artifact."""


class PinHashError(NflversePinError):
    """Raised when bytes do not match the manually reviewed release hash."""


@dataclass(frozen=True)
class PinnedRelease:
    """A manually reviewed upstream artifact, addressed by the bytes that were reviewed.

    ``label`` is a class variable rather than a field so every subclass keeps the plain
    ``(season, url, sha256, reviewed_at)`` constructor a pin-table entry is pasted as.
    """

    season: int
    url: str
    sha256: str
    reviewed_at: date

    label: ClassVar[str] = "pinned artifact"


def newest_pin[ReleaseT: PinnedRelease](pins: Sequence[ReleaseT]) -> ReleaseT:
    """Newest ``reviewed_at`` wins; a same-day re-pin later in the table beats an earlier one."""

    if not pins:
        raise NflversePinError("no pinned release was supplied")
    return max(enumerate(pins), key=lambda item: (item[1].reviewed_at, item[0]))[1]


def pinned_release[ReleaseT: PinnedRelease](
    season: int,
    as_of: date | datetime,
    *,
    releases: Mapping[int, tuple[ReleaseT, ...]],
    label: str,
) -> ReleaseT:
    """Return the newest reviewed release available on ``as_of``; never look ahead."""

    cutoff = as_of.date() if isinstance(as_of, datetime) else as_of
    eligible = tuple(
        release
        for release in releases.get(season, ())
        if release.season == season and release.reviewed_at <= cutoff
    )
    if not eligible:
        raise NflversePinError(
            f"no {label} release is pinned for season {season} at or before "
            f"{cutoff.isoformat()}; review and add its hash"
        )
    return newest_pin(eligible)


def pin_archive_path(archive_dir: Path, sha256: str, *, label: str) -> Path:
    """Return the content-addressed path for exact reviewed bytes."""

    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise NflversePinError(f"{label} sha256 must be 64 lowercase hexadecimal chars")
    return archive_dir / "sha256" / sha256[:2] / f"{sha256}.csv"


def fetch_pinned(
    release: PinnedRelease,
    archive_dir: Path,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Return verified bytes from the local archive, fetching only on an archive miss."""

    label = release.label
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = pin_archive_path(archive_dir, release.sha256, label=label)
    if target.exists():
        archived_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        if archived_sha256 != release.sha256:
            raise PinHashError(
                f"archived {label} bytes at {target} do not match the reviewed hash "
                f"(expected {release.sha256}, got {archived_sha256}); the local archive file is "
                "corrupt — delete it to refetch"
            )
        return target

    owns_client = client is None
    http_client = client or httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True)
    try:
        content = fetch_bytes(http_client, release.url, label=label, sleep=sleep)
        verify_hash(content, release)
        return archive_bytes(archive_dir, content, release.sha256, label=label)
    finally:
        if owns_client:
            http_client.close()


def archive_bytes(archive_dir: Path, content: bytes, sha256: str, *, label: str) -> Path:
    """Write ``content`` at its content-addressed path atomically; the hash must already hold."""

    if hashlib.sha256(content).hexdigest() != sha256:
        raise PinHashError(f"refusing to archive {label} bytes under a hash they do not match")
    target = pin_archive_path(archive_dir, sha256, label=label)
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


def verify_hash(content: bytes, release: PinnedRelease) -> None:
    """Refuse bytes that are not the ones a person reviewed."""

    actual = hashlib.sha256(content).hexdigest()
    if actual != release.sha256:
        raise PinHashError(
            f"{release.label} hash mismatch for {release.season}: "
            f"expected {release.sha256}, got {actual}"
        )


def fetch_bytes(
    client: httpx.Client,
    url: str,
    *,
    label: str,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """Fetch one artifact with bounded exponential backoff, or say how it failed."""

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
    raise NflversePinError(
        f"failed to fetch {label} after {MAX_ATTEMPTS} attempts: {type(last_error).__name__}"
    ) from last_error
