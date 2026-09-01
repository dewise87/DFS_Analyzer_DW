import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from narrative_alpha.narrative import (
    PolicyGateError,
    clean_markup,
    collect_source,
    purge_expired_content,
    tombstone_removed_item,
)
from narrative_alpha.store import (
    ContentTombstoneRow,
    SourceItemRow,
    SourcePolicyRow,
    SourceRow,
    apply_migrations,
    connect_database,
)
from narrative_alpha.store.models import StoreRow

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "public_feed.xml"
CAPTURE_TIME = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def test_policy_gate_refuses_missing_policy_before_fetch(tmp_path: Path) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=FIXTURE_PATH.read_bytes(), request=request)

    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        _seed_source(connection, "team-a")
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(PolicyGateError, match="no reviewed current policy"):
            collect_source(connection, "team-a", observed_at=CAPTURE_TIME, client=client)

    assert requests == 0


def test_policy_gate_refuses_stale_review_before_fetch(tmp_path: Path) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=FIXTURE_PATH.read_bytes(), request=request)

    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        _seed_source(connection, "team-a")
        _seed_policy(
            connection,
            "team-a",
            terms_reviewed_at=CAPTURE_TIME - timedelta(days=366),
        )
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(PolicyGateError, match="policy review is stale"):
            collect_source(connection, "team-a", observed_at=CAPTURE_TIME, client=client)

    assert requests == 0


def test_capture_time_cleaning_and_source_scoped_dedup(tmp_path: Path) -> None:
    payload = FIXTURE_PATH.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"etag": "fixture-v1"}, request=request)

    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        for source_id in ("team-a", "team-b"):
            _seed_source(connection, source_id)
            _seed_policy(connection, source_id)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        first = collect_source(
            connection, "team-a", observed_at=CAPTURE_TIME, client=client, sleep=lambda _: None
        )
        repeat = collect_source(
            connection, "team-a", observed_at=CAPTURE_TIME, client=client, sleep=lambda _: None
        )
        cross_source = collect_source(
            connection, "team-b", observed_at=CAPTURE_TIME, client=client, sleep=lambda _: None
        )
        rows = connection.execute("SELECT * FROM source_items ORDER BY source_item_id").fetchall()

    assert (first.fetched_items, first.inserted_items, first.duplicate_items) == (2, 1, 1)
    assert (repeat.inserted_items, repeat.duplicate_items) == (0, 2)
    assert (cross_source.inserted_items, cross_source.duplicate_items) == (1, 1)
    assert len(rows) == 2
    assert rows[0]["source_id"] == "team-a"
    assert rows[1]["source_id"] == "team-b"
    assert rows[0]["content_sha256"] == rows[1]["content_sha256"]
    assert rows[0]["published_at"] == "2026-09-01T12:00:00.000000Z"
    assert rows[0]["observed_at"] == "2026-09-03T12:00:00.000000Z"
    assert b"ignore previous instructions" in rows[0]["raw_content"]
    assert rows[0]["cleaned_text"] == "Player remains the starter."
    assert "ignore previous instructions" not in rows[0]["cleaned_text"]
    assert "hidden claim" not in rows[0]["cleaned_text"]


def test_retention_purge_tombstones_raw_text_and_is_idempotent(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        _seed_source(connection, "team-a")
        _seed_policy(connection, "team-a", raw_retention_days=1)
        item_id = _collect_fixture(connection, "team-a")

        first = purge_expired_content(connection, as_of=CAPTURE_TIME + timedelta(days=2))
        second = purge_expired_content(connection, as_of=CAPTURE_TIME + timedelta(days=3))
        item = connection.execute(
            "SELECT * FROM source_items WHERE source_item_id = ?", (item_id,)
        ).fetchone()
        tombstones = connection.execute("SELECT * FROM content_tombstones").fetchall()

    assert first.tombstones_written == 1
    assert first.source_items_purged == (item_id,)
    assert second.tombstones_written == 0
    assert item["title"] is None
    assert item["raw_content"] is None
    assert item["cleaned_text"] is None
    assert len(tombstones) == 1
    assert tombstones[0]["reason"] == "retention_expired"


def test_platform_deletion_removes_content_and_is_idempotent(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        _seed_source(connection, "team-a")
        _seed_policy(connection, "team-a", raw_retention_days=30)
        item_id = _collect_fixture(connection, "team-a")

        first = tombstone_removed_item(
            connection, item_id, reported_at=CAPTURE_TIME + timedelta(hours=1)
        )
        second = tombstone_removed_item(
            connection, item_id, reported_at=CAPTURE_TIME + timedelta(hours=2)
        )
        item = connection.execute(
            "SELECT raw_content FROM source_items WHERE source_item_id = ?", (item_id,)
        ).fetchone()
        tombstone = connection.execute("SELECT * FROM content_tombstones").fetchone()

    assert first is True
    assert second is False
    assert item["raw_content"] is None
    assert tombstone["reason"] == "platform_deleted"


@pytest.mark.parametrize(
    "markup",
    [
        # Void elements never emit an end tag.
        '<p>Starter is OUT</p><div style="display:none">ad<br>filler</div><p>Backup starts</p>',
        # A tracking pixel inside an aria-hidden wrapper is routine in real feeds.
        '<p>Starter is OUT</p><span aria-hidden="true"><img src="x.png"></span>'
        "<p>Backup starts</p>",
        # Unclosed tags inside a hidden block are equally routine.
        "<p>Starter is OUT</p><div hidden><p>pixel</div><p>Backup starts</p>",
        # A stray end tag must not unwind the visible document.
        "<p>Starter is OUT</p></div><p>Backup starts</p>",
    ],
)
def test_hidden_markup_never_swallows_the_rest_of_the_document(markup: str) -> None:
    """Malformed feed HTML must cost at most its own element, never the article body.

    A depth counter treats a void or unclosed tag as an open level that never closes, so
    one hidden tracking pixel silently truncates everything after it — invisible data loss
    in the one slice whose whole purpose is capturing evidence that cannot be re-fetched.
    """

    cleaned = clean_markup(markup)

    assert "Starter is OUT" in cleaned
    assert "Backup starts" in cleaned
    for hidden_text in ("ad", "filler", "pixel"):
        assert hidden_text not in cleaned.split()


def test_nested_hidden_blocks_stay_hidden_after_the_inner_block_closes() -> None:
    assert clean_markup("<div hidden><div hidden>inner</div>outer</div><p>visible</p>") == (
        "visible"
    )


def test_narrative_source_row_models_round_trip(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        source = _seed_source(connection, "team-a")
        policy = _seed_policy(connection, "team-a", raw_retention_days=0)
        item_id = _collect_fixture(connection, "team-a")
        purge_expired_content(connection, as_of=CAPTURE_TIME)

        stored_source = SourceRow.from_db(
            connection.execute("SELECT * FROM sources WHERE source_id = 'team-a'").fetchone()
        )
        stored_policy = SourcePolicyRow.from_db(
            connection.execute("SELECT * FROM source_policies").fetchone()
        )
        stored_item = SourceItemRow.from_db(
            connection.execute("SELECT * FROM source_items").fetchone()
        )
        stored_tombstone = ContentTombstoneRow.from_db(
            connection.execute("SELECT * FROM content_tombstones").fetchone()
        )

    assert stored_source == source
    assert stored_policy == policy
    assert stored_item.source_item_id == item_id
    assert stored_item.raw_content is None
    assert stored_tombstone.source_item_id == item_id


def _seed_source(connection: sqlite3.Connection, source_id: str) -> SourceRow:
    configured_at = CAPTURE_TIME - timedelta(days=10)
    connection.execute(
        "INSERT OR IGNORE INTO source_keys(source_id) VALUES (?)", (source_id,)
    )
    row = SourceRow(
        source_record_id=int(
            connection.execute(
                "SELECT coalesce(max(source_record_id), 0) + 1 FROM sources"
            ).fetchone()[0]
        ),
        source_id=source_id,
        display_name=source_id,
        source_family="official_team",
        collector_kind="official_team_feed",
        feed_url=f"https://example.test/{source_id}.xml",
        enabled=True,
        **_point_in_time(configured_at, source="operator-config"),
    )
    _insert_row(connection, "sources", row)
    return row


def _seed_policy(
    connection: sqlite3.Connection,
    source_id: str,
    *,
    terms_reviewed_at: datetime | None = None,
    raw_retention_days: int = 7,
) -> SourcePolicyRow:
    reviewed_at = terms_reviewed_at or CAPTURE_TIME - timedelta(days=10)
    row = SourcePolicyRow(
        source_policy_id=int(
            connection.execute(
                "SELECT coalesce(max(source_policy_id), 0) + 1 FROM source_policies"
            ).fetchone()[0]
        ),
        source_id=source_id,
        permitted_use="prospective internal analysis",
        raw_retention_days=raw_retention_days,
        personal_data_fields_allowed=(),
        must_honor_deletions=True,
        redistribution_allowed=False,
        third_party_processing_allowed=False,
        commercial_use_status="not reviewed for commercial use",
        terms_reviewed_at=reviewed_at,
        **_point_in_time(reviewed_at, source="operator-policy-review"),
    )
    _insert_row(connection, "source_policies", row)
    return row


def _collect_fixture(connection: sqlite3.Connection, source_id: str) -> int:
    payload = FIXTURE_PATH.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    collect_source(connection, source_id, observed_at=CAPTURE_TIME, client=client)
    return int(
        connection.execute(
            "SELECT source_item_id FROM source_items WHERE source_id = ?", (source_id,)
        ).fetchone()[0]
    )


def _point_in_time(at: datetime, *, source: str) -> dict[str, object]:
    return {
        "source": source,
        "published_at": None,
        "observed_at": at,
        "ingested_at": at,
        "effective_at": None,
        "valid_from": at,
        "valid_to": None,
        "source_version": "fixture-v1",
        "run_id": None,
    }


def _insert_row(connection: sqlite3.Connection, table: str, row: StoreRow) -> None:
    values = row.db_values()
    columns = ", ".join(values)
    placeholders = ", ".join(f":{column}" for column in values)
    connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", values)
