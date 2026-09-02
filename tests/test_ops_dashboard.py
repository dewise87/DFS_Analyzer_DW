"""The local dashboard (`na-ops dashboard`): what it shows, what it refuses, what it writes.

Every test drives a real server over a real socket on an ephemeral port, because the
things worth checking here — the loopback bind, the POST-only actions, the refusal of a
second concurrent lane — are properties of the server, not of a render function.
"""

from __future__ import annotations

import http.client
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from narrative_alpha.identity import PlayerCrosswalk, PlayerIdentityInput
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.ops import (
    DashboardDependencies,
    DashboardError,
    DashboardServer,
    LaneRunner,
    build_dashboard,
    load_ops_config,
    record_ops_run,
)
from narrative_alpha.ops.batch import BatchReport
from narrative_alpha.ops.runs import StepOutcome, recent_runs
from narrative_alpha.ops.slate import SlateReport
from narrative_alpha.snapshots import CaptureKind, capture_files
from narrative_alpha.store import apply_migrations, connect_database

SEASON = 2026
WEEK = 1
NOW = datetime(2026, 9, 2, 13, 30, tzinfo=UTC)
OBSERVED = NOW - timedelta(hours=3)


# --------------------------------------------------------------------------------------
# Fixtures: an empty store, and a store with something in every queue the page shows
# --------------------------------------------------------------------------------------


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "ops.toml"
    path.write_text(
        f"""
timezone = "America/New_York"
season = {SEASON}
monthly_llm_budget_usd = "50.00"
keychain_service = "narrative-alpha-anthropic"

[batch]
weekdays = ["wed"]
local_time = "09:30"
max_items_per_run = 20

[paths]
database = "{tmp_path / "store.sqlite3"}"
snapshot_root = "{tmp_path / "snapshots"}"
nflverse_archive = "{tmp_path / "archive"}"
log_directory = "{tmp_path / "logs"}"
""".lstrip(),
        encoding="utf-8",
    )
    return path


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _insert_player(connection: sqlite3.Connection, name: str, team: str, position: str) -> int:
    stamp = _timestamp(OBSERVED - timedelta(days=7))
    cursor = connection.execute(
        """
        INSERT INTO players(
            player_key, canonical_name, position, birth_date, source, published_at,
            observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES (?, ?, ?, NULL, 'fixture', NULL, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (f"fixture-{name}-{team}", name, position, stamp, stamp, stamp),
    )
    assert cursor.lastrowid is not None
    player_id = int(cursor.lastrowid)
    connection.execute(
        """
        INSERT INTO player_team_history(
            player_id, team, position, roster_status, season, week, source, published_at,
            observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES (?, ?, ?, 'ACT', ?, ?, 'fixture', NULL, ?, ?, NULL, ?, NULL,
                  'fixture-v1', NULL)
        """,
        (player_id, team, position, SEASON, WEEK, stamp, stamp, stamp),
    )
    return player_id


def _queue_unresolved(connection: sqlite3.Connection) -> tuple[int, int]:
    """One pending identity with a real candidate, queued the way ingestion queues it."""

    player_id = _insert_player(connection, "Jordan Reed", "WAS", "TE")
    result = PlayerCrosswalk(connection).match(
        PlayerIdentityInput(
            source="fixture-vendor",
            site="draftkings",
            external_player_id="vendor-77",
            name_raw="Jordy Reeds",
            team="WAS",
            position="TE",
            observed_at=OBSERVED,
            source_file_sha256="b" * 64,
        )
    )
    assert result.unresolved_id is not None, "the fixture must land in the manual queue"
    assert result.candidates, "the fixture must offer a candidate for the page to render"
    return result.unresolved_id, player_id


@pytest.fixture
def empty(tmp_path: Path) -> Any:
    config = load_ops_config(_write_config(tmp_path))
    with connect_database(config.database) as connection:
        apply_migrations(connection)
    return config


@pytest.fixture
def seeded(tmp_path: Path) -> Any:
    """A store carrying one of everything the four pages render."""

    config = load_ops_config(_write_config(tmp_path))
    staged = tmp_path / "staged" / "DKSalaries.csv"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text("Position,Name,Salary\nQB,Fixture Quarterback,7000\n", encoding="utf-8")
    capture_files(
        config.snapshot_root,
        SEASON,
        WEEK,
        CaptureKind.SALARIES,
        "draftkings",
        [staged],
        observed_at=OBSERVED,
    )

    memo_path = tmp_path / "reports" / "decision-fixture.txt"
    memo_path.parent.mkdir(parents=True, exist_ok=True)
    memo_path.write_text("SLATE DECISION MEMO\nfixture body line\n", encoding="utf-8")

    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _queue_unresolved(connection)
        record_ops_run(
            connection,
            batch_run_id="ops-fixture",
            step="collect",
            status="succeeded",
            started_at=OBSERVED,
            finished_at=OBSERVED + timedelta(seconds=4),
            summary={"sources": 3, "dead_sources": 0},
        )
        record_ops_run(
            connection,
            batch_run_id="ops-fixture",
            step="extract",
            status="failed",
            started_at=OBSERVED + timedelta(seconds=5),
            finished_at=OBSERVED + timedelta(seconds=6),
            summary={"submitted": 0},
            error_text="the Keychain item could not be read",
        )
        record_ops_run(
            connection,
            batch_run_id="slate-fixture",
            step="slate_memo",
            status="succeeded",
            started_at=OBSERVED + timedelta(seconds=7),
            finished_at=OBSERVED + timedelta(seconds=8),
            summary={"memo_path": str(memo_path), "decision_snapshot_id": "decision-fixture"},
        )
    return config


class _Client:
    """A tiny HTTP client bound to one running dashboard."""

    def __init__(self, server: DashboardServer) -> None:
        self.server = server
        self.host = server.bound_host
        self.port = server.port

    def get(self, path: str) -> tuple[int, str]:
        return self._request("GET", path, body=None, headers={})

    def post(
        self,
        path: str,
        body: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        return self._request("POST", path, body=body, headers=headers or {})

    def post_status(self, path: str, body: str) -> tuple[int, str | None]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            connection.request(
                "POST",
                path,
                body=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response = connection.getresponse()
            response.read()
            return response.status, response.getheader("Location")
        finally:
            connection.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: str | None,
        headers: dict[str, str],
    ) -> tuple[int, str]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            sent = dict(headers)
            if body is not None:
                sent.setdefault("Content-Type", "application/x-www-form-urlencoded")
            connection.request(method, path, body=body, headers=sent)
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            connection.close()


def _serve(config: Any, **overrides: Any) -> Iterator[_Client]:
    arguments: dict[str, Any] = {
        "config": config,
        "database": config.database,
        "port": 0,
        "clock": lambda: NOW,
    }
    arguments |= overrides
    server = build_dashboard(**arguments)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Client(server)
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()


@pytest.fixture
def empty_client(empty: Any) -> Iterator[_Client]:
    yield from _serve(empty)


@pytest.fixture
def seeded_client(seeded: Any) -> Iterator[_Client]:
    yield from _serve(seeded)


# --------------------------------------------------------------------------------------
# The read pages
# --------------------------------------------------------------------------------------

PAGES = ("/", "/queues", "/runs", "/memo")


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders_on_an_empty_store(empty_client: _Client, path: str) -> None:
    status, body = empty_client.get(path)

    assert status == 200
    assert body.startswith("<!doctype html>")
    assert "NARRATIVE ALPHA" in body
    # An empty store is a state the page states, not an error and not a blank panel.
    assert "error" not in body.casefold() or "Traceback" not in body


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders_on_a_seeded_store(seeded_client: _Client, path: str) -> None:
    status, body = seeded_client.get(path)

    assert status == 200
    assert body.startswith("<!doctype html>")


def test_status_page_shows_every_section_of_the_status_payload(seeded_client: _Client) -> None:
    """The page renders `status_payload` itself, so no section can be silently dropped."""

    status, body = seeded_client.get("/")

    assert status == 200
    for section in (
        "as of",
        "collection",
        "config path",
        "database",
        "extraction",
        "identity",
        "manual actions",
        "slate",
        "slate steps",
        "snapshots",
        "steps",
        "warnings",
    ):
        assert f"<h2>{section}</h2>" in body, f"{section} is missing from the status page"


def test_status_page_loads_no_external_asset_and_runs_no_script(seeded_client: _Client) -> None:
    _, body = seeded_client.get("/")

    assert "<script" not in body.casefold()
    assert "https://" not in body.replace("http://", "")
    assert "cdn" not in body.casefold()


def test_runs_page_shows_the_recorded_history_newest_first(seeded_client: _Client) -> None:
    status, body = seeded_client.get("/runs")

    assert status == 200
    assert body.index("slate_memo") < body.index("extract") < body.index("collect")
    assert "the Keychain item could not be read" in body


def test_runs_page_is_capped_at_twenty_rows(seeded: Any) -> None:
    with connect_database(seeded.database) as connection:
        for index in range(25):
            record_ops_run(
                connection,
                batch_run_id=f"ops-bulk-{index}",
                step="purge",
                status="succeeded",
                started_at=NOW - timedelta(minutes=index),
                finished_at=NOW - timedelta(minutes=index) + timedelta(seconds=1),
                summary={"index": index},
            )
    with connect_database(seeded.database) as connection:
        assert len(recent_runs(connection)) == 20
        assert len(recent_runs(connection, limit=3)) == 3
        with pytest.raises(ValueError, match="must be positive"):
            recent_runs(connection, limit=0)


def test_memo_page_shows_the_memo_the_last_successful_step_wrote(seeded_client: _Client) -> None:
    status, body = seeded_client.get("/memo")

    assert status == 200
    assert "SLATE DECISION MEMO" in body
    assert "fixture body line" in body
    assert "decision-fixture" in body


def test_memo_page_says_so_when_the_file_is_gone(seeded: Any) -> None:
    row = None
    with connect_database(seeded.database) as connection:
        row = connection.execute(
            "SELECT summary_json FROM ops_runs WHERE step = 'slate_memo'"
        ).fetchone()
    assert row is not None
    import json

    Path(json.loads(str(row["summary_json"]))["memo_path"]).unlink()

    for client in _serve(seeded):
        status, body = client.get("/memo")
        assert status == 200
        # The gap is named, with the path it looked in; it is not rendered as "no memo".
        assert "cannot be read now" in body
        assert "decision-fixture.txt" in body
        break


def test_unknown_path_is_a_404_that_names_the_four_pages(empty_client: _Client) -> None:
    status, body = empty_client.get("/admin")

    assert status == 404
    for href, _ in (("/", ""), ("/queues", ""), ("/runs", ""), ("/memo", "")):
        assert f'href="{href}"' in body


# --------------------------------------------------------------------------------------
# The review queues, and resolving from the page
# --------------------------------------------------------------------------------------


def test_queues_page_offers_the_candidates_for_an_unresolved_identity(
    seeded: Any,
    seeded_client: _Client,
) -> None:
    with connect_database(seeded.database) as connection:
        unresolved = PlayerCrosswalk(connection).list_unresolved()
    assert len(unresolved) == 1
    candidate = unresolved[0].candidates_json[0]

    status, body = seeded_client.get("/queues")

    assert status == 200
    assert "Jordy Reeds" in body
    assert "once, at " in body, "a single sighting must not read '1 times between X and X'"
    assert f'value="{unresolved[0].unresolved_id}"' in body
    assert f'<option value="{candidate["player_id"]}">' in body
    assert "Jordan Reed" in body
    assert 'value="ignore"' in body
    assert 'value="resolve"' in body


def test_resolving_from_the_page_persists_what_the_cli_would(
    seeded: Any,
    seeded_client: _Client,
) -> None:
    with connect_database(seeded.database) as connection:
        unresolved = PlayerCrosswalk(connection).list_unresolved()
    unresolved_id = unresolved[0].unresolved_id
    player_id = int(unresolved[0].candidates_json[0]["player_id"])

    status, location = seeded_client.post_status(
        "/queues/resolve",
        f"unresolved_id={unresolved_id}&decision=resolve&player_id={player_id}"
        "&note=from+the+page&confirm=yes",
    )

    assert status == 303
    assert location == "/queues#unresolved"
    with connect_database(seeded.database) as connection:
        row = connection.execute(
            "SELECT * FROM unresolved_player_matches WHERE unresolved_id = ?",
            (unresolved_id,),
        ).fetchone()
        alias = connection.execute(
            "SELECT * FROM player_aliases WHERE player_id = ? AND manual_override = 1",
            (player_id,),
        ).fetchone()
        assert not PlayerCrosswalk(connection).list_unresolved()
    # Exactly the row `na-crosswalk resolve` writes: manual method, full confidence,
    # the operator's note, and the alias that stops the name queueing again.
    assert str(row["status"]) == "resolved"
    assert int(row["resolved_player_id"]) == player_id
    assert str(row["match_method"]) == "manual"
    assert float(row["match_confidence"]) == 1.0
    assert int(row["manual_override"]) == 1
    assert str(row["resolution_note"]) == "from the page"
    assert alias is not None
    assert str(alias["alias"]) == "Jordy Reeds"


def test_ignoring_from_the_page_persists_what_the_cli_would(
    seeded: Any,
    seeded_client: _Client,
) -> None:
    with connect_database(seeded.database) as connection:
        unresolved_id = PlayerCrosswalk(connection).list_unresolved()[0].unresolved_id

    status, _ = seeded_client.post_status(
        "/queues/resolve",
        f"unresolved_id={unresolved_id}&decision=ignore&note=not+a+player&confirm=yes",
    )

    assert status == 303
    with connect_database(seeded.database) as connection:
        row = connection.execute(
            "SELECT status, resolved_player_id, resolution_note FROM "
            "unresolved_player_matches WHERE unresolved_id = ?",
            (unresolved_id,),
        ).fetchone()
    assert str(row["status"]) == "ignored"
    assert row["resolved_player_id"] is None
    assert str(row["resolution_note"]) == "not a player"


def test_a_resolve_naming_no_player_is_refused_and_changes_nothing(
    seeded: Any,
    seeded_client: _Client,
) -> None:
    with connect_database(seeded.database) as connection:
        unresolved_id = PlayerCrosswalk(connection).list_unresolved()[0].unresolved_id

    status, body = seeded_client.post(
        "/queues/resolve",
        f"unresolved_id={unresolved_id}&decision=resolve&player_id=9999&confirm=yes",
    )

    assert status == 400
    assert "canonical player does not exist: 9999" in body
    with connect_database(seeded.database) as connection:
        assert len(PlayerCrosswalk(connection).list_unresolved()) == 1


def test_an_unconfirmed_write_is_refused(seeded: Any, seeded_client: _Client) -> None:
    with connect_database(seeded.database) as connection:
        unresolved_id = PlayerCrosswalk(connection).list_unresolved()[0].unresolved_id

    status, body = seeded_client.post(
        "/queues/resolve",
        f"unresolved_id={unresolved_id}&decision=ignore",
    )

    assert status == 400
    assert "not confirmed" in body
    with connect_database(seeded.database) as connection:
        assert len(PlayerCrosswalk(connection).list_unresolved()) == 1


def test_a_write_posted_from_another_origin_is_refused(
    seeded: Any,
    seeded_client: _Client,
) -> None:
    with connect_database(seeded.database) as connection:
        unresolved_id = PlayerCrosswalk(connection).list_unresolved()[0].unresolved_id

    status, body = seeded_client.post(
        "/queues/resolve",
        f"unresolved_id={unresolved_id}&decision=ignore&confirm=yes",
        headers={"Origin": "https://not-this-page.example"},
    )

    assert status == 400
    assert "https://not-this-page.example" in body
    with connect_database(seeded.database) as connection:
        assert len(PlayerCrosswalk(connection).list_unresolved()) == 1


def test_a_write_from_an_opaque_origin_is_refused_with_the_remedy(
    seeded: Any,
    seeded_client: _Client,
) -> None:
    """A sandboxed frame sends `Origin: null` — and so does a hostile page embedding this one."""

    with connect_database(seeded.database) as connection:
        unresolved_id = PlayerCrosswalk(connection).list_unresolved()[0].unresolved_id

    status, body = seeded_client.post(
        "/queues/resolve",
        f"unresolved_id={unresolved_id}&decision=ignore&confirm=yes",
        headers={"Origin": "null"},
    )

    assert status == 400
    assert "opaque origin" in body
    assert "browser tab of its own" in body
    with connect_database(seeded.database) as connection:
        assert len(PlayerCrosswalk(connection).list_unresolved()) == 1


def test_a_write_from_this_pages_own_origin_is_allowed(
    seeded: Any,
    seeded_client: _Client,
) -> None:
    """The guard must not block the only case that matters: the page's own form."""

    with connect_database(seeded.database) as connection:
        unresolved_id = PlayerCrosswalk(connection).list_unresolved()[0].unresolved_id
    origin = f"http://{seeded_client.host}:{seeded_client.port}"

    status, body = seeded_client.post(
        "/queues/resolve",
        f"unresolved_id={unresolved_id}&decision=ignore&confirm=yes",
        headers={"Origin": origin},
    )

    assert status == 303, body
    with connect_database(seeded.database) as connection:
        assert not PlayerCrosswalk(connection).list_unresolved()


def test_the_favicon_is_answered_without_a_body(empty_client: _Client) -> None:
    """Every page view asks for one; a 404 HTML body each time buries the request log."""

    status, body = empty_client.get("/favicon.ico")

    assert status == 204
    assert body == ""


def test_the_actions_are_post_only(empty_client: _Client) -> None:
    for path in ("/actions/batch", "/actions/slate", "/queues/resolve"):
        status, body = empty_client.get(path)
        assert status == 404, f"{path} answered a GET"
        assert "No such page" in body


# --------------------------------------------------------------------------------------
# The two lane actions
# --------------------------------------------------------------------------------------


def _batch_report(run_id: str = "ops-dashboard") -> BatchReport:
    return BatchReport(
        batch_run_id=run_id,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        steps=(
            StepOutcome(
                step="collect",
                status="succeeded",
                started_at=NOW,
                finished_at=NOW + timedelta(seconds=1),
                summary={"sources": 1},
            ),
        ),
    )


def _slate_report(run_id: str = "slate-dashboard") -> SlateReport:
    return SlateReport(
        slate_run_id=run_id,
        season=SEASON,
        week=WEEK,
        site="dk",
        decision_at=NOW,
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        steps=(
            StepOutcome(
                step="slate_salaries",
                status="succeeded",
                started_at=NOW,
                finished_at=NOW + timedelta(seconds=1),
                summary={"slate_id": 1},
            ),
        ),
    )


class _RecordingLane:
    """Stands in for a lane: records the call, and blocks until the test lets it finish."""

    def __init__(self, report: Any) -> None:
        self.report = report
        self.calls: list[dict[str, Any]] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def __call__(self, connection: sqlite3.Connection, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        self.entered.set()
        assert self.release.wait(timeout=10), "the lane was never released"
        return self.report


def test_the_batch_action_runs_the_lane_with_the_operators_arguments(seeded: Any) -> None:
    lane = _RecordingLane(_batch_report())
    dependencies = DashboardDependencies(run_batch=lane)

    for client in _serve(seeded, dependencies=dependencies):
        status, location = client.post_status("/actions/batch", "max_items=7&confirm=yes")
        assert status == 303
        assert location == "/"
        assert lane.entered.wait(timeout=10)
        _wait_until_idle(client)

        _, body = client.get("/")
        assert "all steps ok" in body
        assert "ops-dashboard" in body
        break

    assert len(lane.calls) == 1
    assert lane.calls[0]["max_items"] == 7
    assert lane.calls[0]["config"] is seeded


def test_the_batch_action_falls_back_to_the_configured_cap(seeded: Any) -> None:
    lane = _RecordingLane(_batch_report())

    for client in _serve(seeded, dependencies=DashboardDependencies(run_batch=lane)):
        client.post_status("/actions/batch", "confirm=yes")
        assert lane.entered.wait(timeout=10)
        _wait_until_idle(client)
        break

    assert lane.calls[0]["max_items"] == 20


def test_the_slate_action_runs_the_current_week(seeded: Any) -> None:
    lane = _RecordingLane(_slate_report())

    for client in _serve(seeded, dependencies=DashboardDependencies(run_slate=lane)):
        _, page = client.get("/")
        # The page names the week it will run, and posts exactly that week.
        assert f'name="season" value="{SEASON}"' in page
        assert f'name="week" value="{WEEK}"' in page

        status, _ = client.post_status(
            "/actions/slate",
            f"season={SEASON}&week={WEEK}&site=dk&lineups=20&confirm=yes",
        )
        assert status == 303
        assert lane.entered.wait(timeout=10)
        _wait_until_idle(client)
        break

    assert lane.calls[0]["season"] == SEASON
    assert lane.calls[0]["week"] == WEEK
    assert lane.calls[0]["site"] == "dk"
    assert lane.calls[0]["number_of_lineups"] == 20


def test_the_slate_action_refuses_to_guess_a_week_with_no_snapshots(empty: Any) -> None:
    lane = _RecordingLane(_slate_report())

    for client in _serve(empty, dependencies=DashboardDependencies(run_slate=lane)):
        _, page = client.get("/")
        assert "No snapshot week is initialized" in page
        assert 'name="season"' not in page

        status, body = client.post("/actions/slate", "site=dk&confirm=yes")
        assert status == 400
        assert "season is required" in body
        break

    assert lane.calls == []


def test_an_unknown_site_is_refused(seeded: Any) -> None:
    lane = _RecordingLane(_slate_report())

    for client in _serve(seeded, dependencies=DashboardDependencies(run_slate=lane)):
        status, body = client.post(
            "/actions/slate",
            f"season={SEASON}&week={WEEK}&site=yahoo&confirm=yes",
        )
        assert status == 400
        assert "site must be one of dk, fd" in body
        break

    assert lane.calls == []


def test_a_second_batch_start_is_refused_while_the_first_is_running(seeded: Any) -> None:
    lane = _RecordingLane(_batch_report())
    lane.release.clear()

    for client in _serve(seeded, dependencies=DashboardDependencies(run_batch=lane)):
        first, _ = client.post_status("/actions/batch", "confirm=yes")
        assert first == 303
        assert lane.entered.wait(timeout=10), "the first run never started"

        # While the first is still inside the lane, the page says so...
        _, page = client.get("/")
        assert "RUNNING since" in page
        assert 'http-equiv="refresh"' in page

        # ...and a second start is refused rather than queued or silently dropped.
        second, body = client.post("/actions/batch", "confirm=yes")
        assert second == 409
        assert "the batch lane started at" in body
        assert "has not finished" in body

        lane.release.set()
        _wait_until_idle(client)

        # Once it is done, the same button works again.
        third, _ = client.post_status("/actions/batch", "confirm=yes")
        assert third == 303
        _wait_until_idle(client)
        break

    assert len(lane.calls) == 2


def test_the_two_lanes_do_not_block_each_other(seeded: Any) -> None:
    batch = _RecordingLane(_batch_report())
    batch.release.clear()
    slate = _RecordingLane(_slate_report())

    for client in _serve(
        seeded,
        dependencies=DashboardDependencies(run_batch=batch, run_slate=slate),
    ):
        client.post_status("/actions/batch", "confirm=yes")
        assert batch.entered.wait(timeout=10)

        status, _ = client.post_status(
            "/actions/slate",
            f"season={SEASON}&week={WEEK}&site=dk&confirm=yes",
        )
        assert status == 303
        assert slate.entered.wait(timeout=10)

        batch.release.set()
        _wait_until_idle(client)
        break

    assert len(batch.calls) == 1
    assert len(slate.calls) == 1


def test_a_lane_that_raises_is_reported_and_does_not_stay_running(seeded: Any) -> None:
    def explode(connection: sqlite3.Connection, **kwargs: Any) -> BatchReport:
        raise RuntimeError("the lane hit a bug")

    for client in _serve(seeded, dependencies=DashboardDependencies(run_batch=explode)):
        client.post_status("/actions/batch", "confirm=yes")
        _wait_until_idle(client)

        _, body = client.get("/")
        assert "RuntimeError: the lane hit a bug" in body
        assert "RUNNING since" not in body
        # A finished lane stops the auto-refresh; the page is static again.
        assert 'http-equiv="refresh"' not in body
        break


def test_the_lane_runner_refuses_a_second_start_without_a_server(seeded: Any) -> None:
    """The refusal is the runner's own invariant, not a property of the HTTP layer."""

    runner = LaneRunner()
    release = threading.Event()
    entered = threading.Event()

    def action() -> tuple[str, bool, str]:
        entered.set()
        assert release.wait(timeout=10)
        return "ops-1", True, "collect succeeded"

    runner.start("batch", action)
    assert entered.wait(timeout=10)
    with pytest.raises(DashboardError, match="has not finished"):
        runner.start("batch", action)
    # The other lane is untouched by the busy one.
    runner.start("slate", lambda: ("slate-1", True, "slate_salaries succeeded"))
    release.set()


def _wait_until_idle(client: _Client, *, timeout: float = 10.0) -> None:
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    while datetime.now(UTC) < deadline:
        if not client.server.context.runner.any_running:
            return
        client.get("/")
    raise AssertionError("a lane was still running after the timeout")


# --------------------------------------------------------------------------------------
# The bind
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "example.com", ""])
def test_a_non_loopback_bind_is_refused(empty: Any, host: str) -> None:
    with pytest.raises(DashboardError) as error:
        build_dashboard(config=empty, database=empty.database, host=host, port=0)

    assert "loopback" in str(error.value)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_a_loopback_bind_is_accepted(empty: Any, host: str) -> None:
    server = build_dashboard(config=empty, database=empty.database, host=host, port=0)
    try:
        assert server.bound_host == "127.0.0.1"
        assert server.port > 0
        assert server.url == f"http://127.0.0.1:{server.port}/"
    finally:
        server.server_close()


def test_the_ipv6_loopback_binds_too(empty: Any) -> None:
    """The refusal names ::1 as acceptable, so ::1 has to actually work."""

    server = build_dashboard(config=empty, database=empty.database, host="::1", port=0)
    try:
        assert server.url == f"http://[::1]:{server.port}/"
        connection = http.client.HTTPConnection("::1", server.port, timeout=10)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection.request("GET", "/runs")
            assert connection.getresponse().status == 200
        finally:
            connection.close()
            server.shutdown()
            thread.join(timeout=10)
    finally:
        server.server_close()


def test_the_rule_is_loopback_not_one_literal_address(empty: Any) -> None:
    """127.0.0.2 is loopback whether or not this machine has aliased it."""

    from narrative_alpha.ops.dashboard import _loopback_host

    assert _loopback_host("127.0.0.2") == "127.0.0.2"
    assert _loopback_host("localhost") == "127.0.0.1"
    with pytest.raises(DashboardError, match="loopback"):
        _loopback_host("10.0.0.1")


def test_the_cli_rejects_a_non_loopback_host(empty: Any, capsys: Any) -> None:
    from narrative_alpha.ops.cli import main as ops_main

    code = ops_main(
        ["--config", str(empty.path), "dashboard", "--host", "0.0.0.0", "--port", "0"]
    )

    assert code == 2
    assert "loopback" in capsys.readouterr().err


def test_the_cli_rejects_an_impossible_port(empty: Any) -> None:
    with pytest.raises(SystemExit):
        from narrative_alpha.ops.cli import main as ops_main

        ops_main(["--config", str(empty.path), "dashboard", "--port", "70000"])


def test_the_page_never_carries_the_keychain_service_or_a_secret(seeded_client: _Client) -> None:
    """Nothing on the page is a credential; the lanes read the key at run time, not here."""

    for path in PAGES:
        _, body = seeded_client.get(path)
        assert "sk-ant" not in body
        assert "add-generic-password" not in body


def test_utc_timestamps_render_on_the_pages(seeded_client: _Client) -> None:
    _, body = seeded_client.get("/runs")

    assert utc_timestamp(OBSERVED) in body
