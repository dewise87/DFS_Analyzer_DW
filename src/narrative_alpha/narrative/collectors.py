"""Policy-gated collection of inert public RSS and Atom source material.

Source text stops at this boundary as untrusted data.  This module has no model, tool,
template, or execution path for item content; it only cleans, hashes, and stores bytes.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import ClassVar, Protocol

import httpx

from narrative_alpha.ingest.timestamps import (
    ensure_utc,
    optional_utc_timestamp,
    utc_timestamp,
)
from narrative_alpha.snapshots.fetch import (
    HTTP_TIMEOUT,
    HttpRequestFailure,
    Sleeper,
    get_with_retry,
)
from narrative_alpha.store import SourcePolicyRow, SourceRow

DEFAULT_POLICY_MAX_AGE = timedelta(days=365)


class CollectionError(RuntimeError):
    """Base error for collection that must be visible to the operator."""


class PolicyGateError(CollectionError):
    """Raised before fetching when a source has no current reviewed policy."""


class FeedParseError(CollectionError):
    """Raised when a feed cannot be safely and deterministically parsed."""


@dataclass(frozen=True)
class CollectedItem:
    external_item_id: str | None
    canonical_url: str | None
    title: str | None
    raw_content: bytes
    cleaned_text: str
    normalized_text: str
    content_sha256: str
    published_at: datetime | None


@dataclass(frozen=True)
class CollectorBatch:
    """Transport-neutral output from a collector after its source has passed policy."""

    items: tuple[CollectedItem, ...]
    attempts: int
    source_version: str | None


class SourceCollector(Protocol):
    """Collector boundary invoked only after the shared fail-closed policy gate."""

    def collect(
        self,
        source: SourceRow,
        *,
        client: httpx.Client,
        sleep: Sleeper,
    ) -> CollectorBatch: ...


@dataclass(frozen=True)
class CollectionReport:
    source_id: str
    observed_at: datetime
    fetched_items: int
    inserted_items: int
    duplicate_items: int
    attempts: int


@dataclass(frozen=True)
class PurgeReport:
    as_of: datetime
    tombstones_written: int
    source_items_purged: tuple[int, ...]


class RssAtomCollector:
    """Deterministic RSS 2.x and Atom parser with no interpretation step."""

    def collect(
        self,
        source: SourceRow,
        *,
        client: httpx.Client,
        sleep: Sleeper,
    ) -> CollectorBatch:
        response, attempts = get_with_retry(client, source.feed_url, {}, sleep=sleep)
        source_version = response.headers.get("etag") or response.headers.get("last-modified")
        return CollectorBatch(self.parse(response.content), attempts, source_version)

    def parse(self, payload: bytes) -> tuple[CollectedItem, ...]:
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as error:
            raise FeedParseError(f"feed is not well-formed XML: {error}") from error

        root_name = _local_name(root.tag)
        if root_name not in {"rss", "rdf", "feed"}:
            raise FeedParseError(f"unsupported feed root element: {root_name!r}")
        item_name = "entry" if root_name == "feed" else "item"
        elements = [element for element in root.iter() if _local_name(element.tag) == item_name]
        if not elements:
            raise FeedParseError("feed contains no item or entry elements")

        items: list[CollectedItem] = []
        for element in elements:
            title_markup = _element_payload(_first_child(element, "title"))
            content_element = _first_child(element, "content", "encoded", "description", "summary")
            content_markup = _element_payload(content_element)
            clean_title = clean_markup(title_markup) or None
            cleaned_text = clean_markup(content_markup)
            normalized_text = normalize_item_text(clean_title, cleaned_text)
            if not normalized_text:
                raise FeedParseError("feed item has no usable title or content")
            published_text = _child_text(element, "published", "updated", "pubDate", "date")
            published_at = _parse_published_at(published_text) if published_text else None
            external_item_id = _child_text(element, "id", "guid")
            canonical_url = _item_url(element)
            raw_content = ET.tostring(element, encoding="utf-8")
            items.append(
                CollectedItem(
                    external_item_id=external_item_id,
                    canonical_url=canonical_url,
                    title=clean_title,
                    raw_content=raw_content,
                    cleaned_text=cleaned_text,
                    normalized_text=normalized_text,
                    content_sha256=hashlib.sha256(normalized_text.encode("utf-8")).hexdigest(),
                    published_at=published_at,
                )
            )
        return tuple(items)


def require_current_policy(
    connection: sqlite3.Connection,
    source_id: str,
    as_of: datetime,
    *,
    max_age: timedelta = DEFAULT_POLICY_MAX_AGE,
) -> SourcePolicyRow:
    """Return the reviewed policy applicable at ``as_of`` or fail closed."""

    if max_age.total_seconds() < 0:
        raise ValueError("policy max age must not be negative")
    policy = _reviewed_policy(connection, source_id, ensure_utc(as_of))
    if policy.terms_reviewed_at > as_of:
        raise PolicyGateError(
            f"source {source_id!r} policy review is dated after the collection instant"
        )
    if policy.terms_reviewed_at < ensure_utc(as_of) - max_age:
        raise PolicyGateError(
            f"source {source_id!r} policy review is stale; reviewed "
            f"{utc_timestamp(policy.terms_reviewed_at)}"
        )
    return policy


def collect_source(
    connection: sqlite3.Connection,
    source_id: str,
    *,
    observed_at: datetime | None = None,
    policy_max_age: timedelta = DEFAULT_POLICY_MAX_AGE,
    client: httpx.Client | None = None,
    sleep: Sleeper = time.sleep,
    collector: SourceCollector | None = None,
) -> CollectionReport:
    """Fetch and store one source after its policy passes, using the shared retry path."""

    capture_time = ensure_utc(observed_at or datetime.now(UTC))
    source = _load_source(connection, source_id, capture_time)
    require_current_policy(connection, source_id, capture_time, max_age=policy_max_age)
    if not source.enabled:
        raise CollectionError(f"source {source_id!r} is disabled")

    owned_client = client is None
    # Feed requests carry no credentials, so the redirect-leak rationale that makes the
    # odds/weather client refuse redirects does not apply here. A publisher moving a feed
    # answers 301, and refusing to follow it silently drops that source from collection.
    http_client = client or httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True)
    context = http_client if owned_client else nullcontext(http_client)
    try:
        with context as active_client:
            batch = (collector or RssAtomCollector()).collect(
                source, client=active_client, sleep=sleep
            )
    except HttpRequestFailure as failure:
        status = "" if failure.status_code is None else f" HTTP {failure.status_code}"
        raise CollectionError(
            f"source {source_id!r} fetch failed after {failure.attempts} attempts{status}"
        ) from failure

    inserted = 0
    for item in batch.items:
        cursor = connection.execute(
            """
            INSERT INTO source_items(
                source_id, external_item_id, canonical_url, title, raw_content,
                cleaned_text, content_sha256, source, published_at, observed_at,
                ingested_at, effective_at, valid_from, valid_to, source_version, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL)
            ON CONFLICT(source_id, content_sha256) DO NOTHING
            """,
            (
                source.source_id,
                item.external_item_id,
                item.canonical_url,
                item.title,
                item.raw_content,
                item.cleaned_text,
                item.content_sha256,
                source.source_id,
                optional_utc_timestamp(item.published_at),
                utc_timestamp(capture_time),
                utc_timestamp(capture_time),
                optional_utc_timestamp(item.published_at),
                utc_timestamp(capture_time),
                batch.source_version,
            ),
        )
        inserted += cursor.rowcount

    return CollectionReport(
        source_id=source_id,
        observed_at=capture_time,
        fetched_items=len(batch.items),
        inserted_items=inserted,
        duplicate_items=len(batch.items) - inserted,
        attempts=batch.attempts,
    )


def purge_expired_content(
    connection: sqlite3.Connection,
    *,
    as_of: datetime | None = None,
    source_ids: Sequence[str] | None = None,
) -> PurgeReport:
    """Remove expired reconstructive text and leave one durable tombstone per item."""

    purge_time = ensure_utc(as_of or datetime.now(UTC))
    if source_ids is None:
        rows = connection.execute(
            "SELECT DISTINCT source_id FROM source_items WHERE raw_content IS NOT NULL"
        ).fetchall()
        selected_source_ids = tuple(str(row[0]) for row in rows)
    else:
        selected_source_ids = tuple(dict.fromkeys(source_ids))

    purged: list[int] = []
    for source_id in selected_source_ids:
        policy = _reviewed_policy(connection, source_id, purge_time)
        cutoff = purge_time - timedelta(days=policy.raw_retention_days)
        rows = connection.execute(
            """
            SELECT source_item_id, source_id, content_sha256
            FROM source_items
            WHERE source_id = ?
              AND raw_content IS NOT NULL
              AND rtrim(observed_at, 'Z') <= rtrim(?, 'Z')
            ORDER BY source_item_id
            """,
            (source_id, utc_timestamp(cutoff)),
        ).fetchall()
        for row in rows:
            item_id = int(row["source_item_id"])
            if _tombstone(
                connection,
                item_id=item_id,
                source_id=str(row["source_id"]),
                content_sha256=str(row["content_sha256"]),
                reason="retention_expired",
                at=purge_time,
            ):
                purged.append(item_id)

    return PurgeReport(purge_time, len(purged), tuple(purged))


def tombstone_removed_item(
    connection: sqlite3.Connection,
    source_item_id: int,
    *,
    reported_at: datetime | None = None,
) -> bool:
    """Honor a platform deletion report without erasing the evidence row itself."""

    deletion_time = ensure_utc(reported_at or datetime.now(UTC))
    row = connection.execute(
        """
        SELECT source_item_id, source_id, content_sha256
        FROM source_items WHERE source_item_id = ?
        """,
        (source_item_id,),
    ).fetchone()
    if row is None:
        raise CollectionError(f"source item {source_item_id} does not exist")
    # A missing policy is never treated as permission to retain reported-deleted content.
    _reviewed_policy(connection, str(row["source_id"]), deletion_time)
    return _tombstone(
        connection,
        item_id=int(row["source_item_id"]),
        source_id=str(row["source_id"]),
        content_sha256=str(row["content_sha256"]),
        reason="platform_deleted",
        at=deletion_time,
    )


def normalize_item_text(title: str | None, cleaned_text: str) -> str:
    """Canonical content used for source-scoped addressing and deduplication."""

    joined = "\n".join(part for part in (title, cleaned_text) if part)
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", joined)).strip()


def clean_markup(markup: str) -> str:
    """Remove executable/hidden markup and return visible text only."""

    cleaner = _VisibleTextExtractor()
    cleaner.feed(markup)
    cleaner.close()
    return re.sub(r"\s+", " ", " ".join(cleaner.parts)).strip()


def _reviewed_policy(
    connection: sqlite3.Connection, source_id: str, as_of: datetime
) -> SourcePolicyRow:
    cutoff = utc_timestamp(as_of)
    row = connection.execute(
        """
        SELECT * FROM source_policies
        WHERE source_id = ?
          AND rtrim(observed_at, 'Z') <= rtrim(?, 'Z')
          AND rtrim(valid_from, 'Z') <= rtrim(?, 'Z')
          AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(?, 'Z'))
        ORDER BY observed_at DESC, source_policy_id DESC
        LIMIT 1
        """,
        (source_id, cutoff, cutoff, cutoff),
    ).fetchone()
    if row is None:
        raise PolicyGateError(f"source {source_id!r} has no reviewed current policy")
    return SourcePolicyRow.from_db(row)


def _load_source(connection: sqlite3.Connection, source_id: str, as_of: datetime) -> SourceRow:
    cutoff = utc_timestamp(as_of)
    row = connection.execute(
        """
        SELECT * FROM sources
        WHERE source_id = ?
          AND rtrim(observed_at, 'Z') <= rtrim(?, 'Z')
          AND rtrim(valid_from, 'Z') <= rtrim(?, 'Z')
          AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(?, 'Z'))
        ORDER BY observed_at DESC, source_record_id DESC
        LIMIT 1
        """,
        (source_id, cutoff, cutoff, cutoff),
    ).fetchone()
    if row is None:
        raise CollectionError(f"source {source_id!r} is not configured at the collection instant")
    return SourceRow.from_db(row)


def _tombstone(
    connection: sqlite3.Connection,
    *,
    item_id: int,
    source_id: str,
    content_sha256: str,
    reason: str,
    at: datetime,
) -> bool:
    timestamp = utc_timestamp(at)
    cursor = connection.execute(
        """
        INSERT INTO content_tombstones(
            source_item_id, source_id, content_sha256, reason, tombstoned_at,
            source, published_at, observed_at, ingested_at, effective_at,
            valid_from, valid_to, source_version, run_id
        ) VALUES (?, ?, ?, ?, ?, 'retention-enforcer', NULL, ?, ?, ?, ?, NULL, 'v1', NULL)
        ON CONFLICT(source_item_id) DO NOTHING
        """,
        (
            item_id,
            source_id,
            content_sha256,
            reason,
            timestamp,
            timestamp,
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    connection.execute(
        """
        UPDATE source_items
        SET title = NULL, raw_content = NULL, cleaned_text = NULL
        WHERE source_item_id = ?
        """,
        (item_id,),
    )
    return cursor.rowcount == 1


class _VisibleTextExtractor(HTMLParser):
    """Collect visible text, tracking element nesting rather than a bare depth counter.

    Feed HTML is not well-formed: void elements never emit an end tag, and unclosed
    ``<p>``/``<li>`` tags are routine. A depth counter treats both as an extra open level
    that never closes, so one tracking pixel inside a hidden ad block silently swallows the
    rest of the article. Keeping the open-element stack means an unmatched end tag or a
    missing one costs at most the element it belongs to, never the remainder of the document.
    """

    _ALWAYS_HIDDEN: ClassVar[set[str]] = {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "canvas",
    }
    # HTML5 void elements: they have no children, so they can never hide following content.
    _VOID: ClassVar[set[str]] = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._open: list[tuple[str, bool]] = []
        self._hidden_count = 0

    def _is_hidden(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        attributes: Mapping[str, str] = {
            name.casefold(): value.casefold() for name, value in attrs if value is not None
        }
        style = attributes.get("style", "").replace(" ", "")
        return (
            tag in self._ALWAYS_HIDDEN
            or "hidden" in {name.casefold() for name, _ in attrs}
            or attributes.get("aria-hidden") == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.casefold()
        if name in self._VOID:
            return
        hidden = self._is_hidden(name, attrs)
        self._open.append((name, hidden))
        if hidden:
            self._hidden_count += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        return

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index][0] == name:
                for _, hidden in self._open[index:]:
                    if hidden:
                        self._hidden_count -= 1
                del self._open[index:]
                return
        # An end tag with no matching open element is discarded, not treated as an unwind.

    def handle_data(self, data: str) -> None:
        if not self._hidden_count and data.strip():
            self.parts.append(data.strip())


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _first_child(element: ET.Element, *names: str) -> ET.Element | None:
    wanted = {name.casefold() for name in names}
    return next(
        (child for child in element if _local_name(child.tag).casefold() in wanted),
        None,
    )


def _element_payload(element: ET.Element | None) -> str:
    if element is None:
        return ""
    if len(element) == 0:
        return element.text or ""
    prefix = element.text or ""
    return prefix + "".join(ET.tostring(child, encoding="unicode") for child in element)


def _child_text(element: ET.Element, *names: str) -> str | None:
    child = _first_child(element, *names)
    if child is None:
        return None
    text = "".join(child.itertext()).strip()
    return text or None


def _item_url(element: ET.Element) -> str | None:
    for child in element:
        if _local_name(child.tag).casefold() != "link":
            continue
        relation = child.attrib.get("rel", "alternate")
        href = child.attrib.get("href")
        if href and relation in {"alternate", ""}:
            return href.strip() or None
        if child.text and child.text.strip():
            return child.text.strip()
    return None


def _parse_published_at(value: str) -> datetime:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise FeedParseError(f"invalid feed item timestamp: {value!r}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FeedParseError(f"feed item timestamp has no timezone: {value!r}")
    return parsed.astimezone(UTC)


__all__ = [
    "DEFAULT_POLICY_MAX_AGE",
    "CollectedItem",
    "CollectionError",
    "CollectionReport",
    "CollectorBatch",
    "FeedParseError",
    "PolicyGateError",
    "PurgeReport",
    "RssAtomCollector",
    "SourceCollector",
    "clean_markup",
    "collect_source",
    "normalize_item_text",
    "purge_expired_content",
    "require_current_policy",
    "tombstone_removed_item",
]
