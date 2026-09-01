"""Canonical UTC timestamp formatting shared by every ingest database write.

All ingest writes and lexicographic ``observed_at`` comparisons must use this
single chokepoint so same-instant timestamps always serialize to the identical
string and therefore sort correctly under SQLite TEXT comparison.
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["ensure_utc", "optional_utc_timestamp", "utc_timestamp"]


def ensure_utc(value: datetime) -> datetime:
    """Return the timezone-aware ``value`` converted to UTC; reject naive input."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def utc_timestamp(value: datetime) -> str:
    """Format an aware datetime as the canonical sortable ``...ssssssZ`` string."""

    return ensure_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def optional_utc_timestamp(value: datetime | None) -> str | None:
    """Canonical formatting for nullable timestamp columns."""

    return None if value is None else utc_timestamp(value)
