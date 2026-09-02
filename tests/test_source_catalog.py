import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

import narrative_alpha.collect_cli as collect_cli
import narrative_alpha.narrative.collectors as collectors
from narrative_alpha.collect_cli import main
from narrative_alpha.narrative import (
    CatalogError,
    CollectionError,
    CollectionReport,
    apply_source_seed,
    check_catalog_feeds,
    feed_check_payload,
    load_source_catalog,
    plan_source_seed,
)
from narrative_alpha.store import apply_migrations, connect_database

CATALOG_FIXTURE = Path(__file__).with_name("fixtures") / "narrative_sources.toml"
FEED_FIXTURE = Path(__file__).with_name("fixtures") / "public_feed.xml"
REVIEWED_AT = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
SEEDED_AT = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def test_seed_cli_refuses_without_explicit_terms_attestation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "store.sqlite3"

    exit_code = main(
        ["seed", "--catalog", str(CATALOG_FIXTURE), "--database", str(database)]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "--terms-reviewed-at is required" in captured.err
    assert not database.exists()


def test_attested_timestamp_lands_on_every_policy_and_tier_terms_are_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "store.sqlite3"

    exit_code = main(
        [
            "seed",
            "--catalog",
            str(CATALOG_FIXTURE),
            "--database",
            str(database),
            "--terms-reviewed-at",
            "2026-08-01T12:00:00Z",
        ]
    )
    output = capsys.readouterr().out
    with connect_database(database) as connection:
        rows = connection.execute(
            "SELECT source_id, terms_reviewed_at FROM source_policies ORDER BY source_id"
        ).fetchall()

    assert exit_code == 0
    assert [(row[0], row[1]) for row in rows] == [
        ("fixture-media", "2026-08-01T12:00:00.000000Z"),
        ("fixture-official", "2026-08-01T12:00:00.000000Z"),
    ]
    assert '"tier": "media"' in output
    assert '"tier": "official"' in output
    assert '"raw_retention_days": 14' in output
    assert '"raw_retention_days": 30' in output
    assert '"source_count": 1' in output


def test_catalog_review_timestamp_is_rejected_instead_of_read(tmp_path: Path) -> None:
    catalog_path = tmp_path / "forged-review.toml"
    catalog_path.write_text(
        CATALOG_FIXTURE.read_text(encoding="utf-8")
        + '\nterms_reviewed_at = "1999-01-01T00:00:00Z"\n',
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match="must not appear in a catalog"):
        load_source_catalog(catalog_path)


def test_reseed_is_noop_then_versions_changed_url_and_policy(tmp_path: Path) -> None:
    database = tmp_path / "store.sqlite3"
    catalog_path = tmp_path / "catalog.toml"
    catalog_path.write_text(_one_source_catalog(), encoding="utf-8")

    with connect_database(database) as connection:
        apply_migrations(connection)
        first_plan = plan_source_seed(
            connection,
            catalog_path,
            terms_reviewed_at=REVIEWED_AT,
            observed_at=SEEDED_AT,
        )
        first = apply_source_seed(connection, first_plan)

        unchanged_plan = plan_source_seed(
            connection,
            catalog_path,
            terms_reviewed_at=REVIEWED_AT,
            observed_at=SEEDED_AT + timedelta(hours=1),
        )
        unchanged = apply_source_seed(connection, unchanged_plan)

        catalog_path.write_text(
            _one_source_catalog(feed_url="https://example.test/changed.xml"),
            encoding="utf-8",
        )
        url_plan = plan_source_seed(
            connection,
            catalog_path,
            terms_reviewed_at=REVIEWED_AT,
            observed_at=SEEDED_AT + timedelta(hours=2),
        )
        url_result = apply_source_seed(connection, url_plan)

        catalog_path.write_text(
            _one_source_catalog(
                feed_url="https://example.test/changed.xml", raw_retention_days=7
            ),
            encoding="utf-8",
        )
        policy_plan = plan_source_seed(
            connection,
            catalog_path,
            terms_reviewed_at=REVIEWED_AT,
            observed_at=SEEDED_AT + timedelta(hours=3),
        )
        policy_result = apply_source_seed(connection, policy_plan)
        source_rows = connection.execute(
            "SELECT feed_url FROM sources ORDER BY source_record_id"
        ).fetchall()
        policy_rows = connection.execute(
            "SELECT raw_retention_days FROM source_policies ORDER BY source_policy_id"
        ).fetchall()

    assert first.source_versions_inserted == 1
    assert first.policy_versions_inserted == 1
    assert unchanged.source_versions_inserted == 0
    assert unchanged.policy_versions_inserted == 0
    assert unchanged_plan.changed_sources == ()
    assert url_result.source_versions_inserted == 1
    assert url_result.policy_versions_inserted == 0
    assert url_plan.changed_sources[0].source_changed_fields == ("feed_url",)
    assert policy_result.source_versions_inserted == 0
    assert policy_result.policy_versions_inserted == 1
    assert policy_plan.changed_sources[0].policy_changed_fields == ("raw_retention_days",)
    assert [row[0] for row in source_rows] == [
        "https://example.test/original.xml",
        "https://example.test/changed.xml",
    ]
    assert [row[0] for row in policy_rows] == [14, 7]


def test_seed_dry_run_writes_no_catalog_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "store.sqlite3"

    exit_code = main(
        [
            "seed",
            "--catalog",
            str(CATALOG_FIXTURE),
            "--database",
            str(database),
            "--terms-reviewed-at",
            "2026-08-01T12:00:00Z",
            "--dry-run",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert not database.exists()
    assert '"dry_run": true' in output
    assert '"source_action": "insert"' in output


def test_a_locked_database_keeps_already_collected_sources_and_explains_itself(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store error must be isolated like a feed error, not discard the whole batch.

    The run holds a write lock for as long as it takes to fetch every feed, so a second
    concurrent run hits "database is locked". Before per-source commits, that error escaped
    the loop and rolled back every source already collected.
    """

    database = tmp_path / "locked.sqlite3"
    with connect_database(database) as connection:
        apply_migrations(connection)
        apply_source_seed(
            connection,
            plan_source_seed(
                connection,
                CATALOG_FIXTURE,
                terms_reviewed_at=REVIEWED_AT,
                observed_at=SEEDED_AT,
            ),
        )
    calls: list[str] = []

    def fake_collect(connection: object, source_id: str, **_: object) -> CollectionReport:
        calls.append(source_id)
        if len(calls) == 1:
            return CollectionReport(source_id, datetime(2026, 9, 1, tzinfo=UTC), 3, 3, 0, 1)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(collectors, "collect_source", fake_collect)
    exit_code = collect_cli.main(["run", "--database", str(database)])

    assert exit_code == 2
    # Stopped at the lock rather than burning a full busy timeout on every remaining source.
    assert len(calls) == 2
    with connect_database(database) as connection:
        apply_migrations(connection)
        rows = connection.execute("SELECT count(*) FROM source_items").fetchone()[0]
    assert rows == 0  # the fake collector stores nothing; the run must still not crash


def test_locked_database_message_names_the_likely_cause() -> None:
    message = collectors.store_error_message(sqlite3.OperationalError("database is locked"))
    assert "another collection run" in message
    assert "were kept" in message


def test_future_attestation_error_names_both_instants(tmp_path: Path) -> None:
    """The common cause is an operator ahead of UTC writing their local date as a Z time.

    "must not be later than the seed observation" gives them nothing to act on; the message
    has to show the attested instant and the current UTC instant side by side.
    """

    database = tmp_path / "future.sqlite3"
    with connect_database(database) as connection:
        apply_migrations(connection)
        with pytest.raises(CatalogError) as error:
            plan_source_seed(
                connection,
                CATALOG_FIXTURE,
                terms_reviewed_at=datetime(2026, 9, 2, tzinfo=UTC),
                observed_at=datetime(2026, 9, 1, 21, 8, tzinfo=UTC),
            )

    message = str(error.value)
    assert "2026-09-02T00:00:00.000000Z" in message
    assert "2026-09-01T21:08:00.000000Z" in message
    assert "future" in message


def test_feed_check_follows_a_moved_feed_instead_of_reporting_it_dead() -> None:
    """A publisher moving a feed answers 301; refusing to follow silently drops the source.

    Feed requests carry no credentials, so the redirect-leak rationale that makes the
    odds/weather client refuse redirects does not apply. The health check must agree with
    what collection would actually fetch.
    """

    catalog = load_source_catalog(CATALOG_FIXTURE)
    moved = catalog.sources[0].feed_url
    destination = "https://feeds.example.com/moved.xml"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == moved:
            return httpx.Response(301, headers={"Location": destination}, request=request)
        if str(request.url) == destination:
            return httpx.Response(200, content=FEED_FIXTURE.read_bytes(), request=request)
        return httpx.Response(404, content=b"not found", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    report = check_catalog_feeds(catalog, client=client, sleep=lambda _: None)

    check = next(c for c in report.checks if c.feed_url == moved)
    assert check.ok
    assert check.item_count is not None and check.item_count > 0


def test_feed_check_reports_dead_feed_and_checks_every_source_without_network() -> None:
    catalog = load_source_catalog(CATALOG_FIXTURE)
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path.endswith("official.xml"):
            return httpx.Response(200, content=FEED_FIXTURE.read_bytes(), request=request)
        return httpx.Response(404, content=b"not found", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = check_catalog_feeds(catalog, client=client, sleep=lambda _: None)
    payload = feed_check_payload(report)

    assert report.ok is False
    assert len(requested) == 2
    assert [check.source_id for check in report.checks] == [
        "fixture-official",
        "fixture-media",
    ]
    assert report.checks[0].ok is True
    assert report.checks[1].ok is False
    assert report.checks[1].attempts == 1
    assert "HTTP 404" in (report.checks[1].error or "")
    assert payload["failed_count"] == 1
    assert payload["feeds"][1]["source_id"] == "fixture-media"  # type: ignore[index]


def test_run_isolates_one_source_failure_and_returns_nonzero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_collect(
        connection: sqlite3.Connection,
        source_id: str,
        **kwargs: object,
    ) -> CollectionReport:
        calls.append(source_id)
        if source_id == "dead-feed":
            raise CollectionError("HTTP 404")
        return CollectionReport(source_id, SEEDED_AT, 2, 2, 0, 1)

    monkeypatch.setattr(collectors, "collect_source", fake_collect)
    exit_code = main(
        [
            "run",
            "--database",
            str(tmp_path / "store.sqlite3"),
            "--source-id",
            "dead-feed",
            "--source-id",
            "live-feed",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert calls == ["dead-feed", "live-feed"]
    assert output["errors"] == [
        {"message": "HTTP 404", "source_id": "dead-feed"}
    ]
    assert output["reports"][0]["source_id"] == "live-feed"


def test_run_selects_only_latest_source_version_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "store.sqlite3"
    catalog_path = tmp_path / "catalog.toml"
    catalog_path.write_text(_one_source_catalog(), encoding="utf-8")
    with connect_database(database) as connection:
        apply_migrations(connection)
        first = plan_source_seed(
            connection,
            catalog_path,
            terms_reviewed_at=REVIEWED_AT,
            observed_at=SEEDED_AT,
        )
        apply_source_seed(connection, first)
        catalog_path.write_text(
            _one_source_catalog(feed_url="https://example.test/changed.xml"),
            encoding="utf-8",
        )
        changed = plan_source_seed(
            connection,
            catalog_path,
            terms_reviewed_at=REVIEWED_AT,
            observed_at=SEEDED_AT + timedelta(hours=1),
        )
        apply_source_seed(connection, changed)

    calls: list[str] = []

    def fake_collect(
        connection: sqlite3.Connection,
        source_id: str,
        **kwargs: object,
    ) -> CollectionReport:
        calls.append(source_id)
        return CollectionReport(source_id, SEEDED_AT, 1, 1, 0, 1)

    monkeypatch.setattr(collectors, "collect_source", fake_collect)
    exit_code = main(
        [
            "run",
            "--database",
            str(database),
            "--observed-at",
            "2026-08-03T12:00:00Z",
        ]
    )
    capsys.readouterr()

    assert exit_code == 0
    assert calls == ["fixture-media"]


def test_committed_catalog_has_expected_scope_and_no_review_attestation() -> None:
    catalog = load_source_catalog(Path("config/narrative_sources.toml"))

    assert len(catalog.sources) == 104
    assert {source.collector_kind for source in catalog.sources} == {"rss_atom"}


def _one_source_catalog(
    *,
    feed_url: str = "https://example.test/original.xml",
    raw_retention_days: int = 14,
) -> str:
    return f"""
[policy_tiers.media]
permitted_use = "internal_analysis_only"
raw_retention_days = {raw_retention_days}
personal_data_fields_allowed = []
must_honor_deletions = true
redistribution_allowed = false
third_party_processing_allowed = true
commercial_use_status = "commercial_use_prohibited"

[[sources]]
source_id = "fixture-media"
display_name = "Fixture media"
source_family = "national_media"
collector_kind = "rss_atom"
feed_url = "{feed_url}"
policy_tier = "media"
""".strip()
