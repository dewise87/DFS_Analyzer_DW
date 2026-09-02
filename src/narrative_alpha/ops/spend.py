"""Stage 1 spend, read from the cost columns the extraction attempts already store."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp


def month_start_utc(now: datetime, *, timezone: ZoneInfo) -> datetime:
    """The instant the operator's current calendar month began, in UTC.

    A monthly budget is a calendar-month promise where the operator lives, not where the
    database writes, so the boundary is computed in the configured zone and converted.
    """

    local = ensure_utc(now).astimezone(timezone)
    return local.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).astimezone(UTC)


def month_to_date_spend_nanos(connection: sqlite3.Connection, *, since: datetime) -> int:
    """Integer USD-nanos billed by Stage 1 attempts recorded at or after ``since``.

    ``ingested_at`` is when the attempt was recorded, which is what a bill is keyed to;
    ``observed_at`` carries the source item's capture time and would credit this month's
    spend to the week the news broke.
    """

    row = connection.execute(
        """
        SELECT coalesce(sum(cost_nanos_usd), 0)
        FROM source_item_extractions
        WHERE cost_nanos_usd IS NOT NULL
          AND rtrim(ingested_at, 'Z') >= rtrim(?, 'Z')
        """,
        (utc_timestamp(since),),
    ).fetchone()
    return int(row[0])
