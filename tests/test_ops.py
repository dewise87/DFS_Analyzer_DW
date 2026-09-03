import hashlib
import json
import os
import shlex
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from narrative_alpha.identity.nflverse import RosterHashError
from narrative_alpha.narrative import (
    CollectionError,
    CollectionReport,
    CollectionRunReport,
    EpisodeError,
    PreparedExtraction,
    ProviderBatchSubmission,
    ProviderResult,
    PurgeReport,
    SourceCollectionError,
    build_episodes,
    load_batch_pricing,
    normalize_item_text,
    run_extraction_batch,
)
from narrative_alpha.ops import (
    BatchDependencies,
    DashboardContext,
    LaneRunner,
    ScheduleError,
    build_jobs,
    collect_ops_status,
    eastern_to_local,
    extraction_window_start,
    inspect_schedule,
    install_schedule,
    last_run,
    load_ops_config,
    month_start_utc,
    record_ops_run,
    render_status,
    run_batch,
    status_payload,
    uninstall_schedule,
)
from narrative_alpha.ops.cli import main as ops_main
from narrative_alpha.ops.config import OpsConfigError
from narrative_alpha.ops.dashboard import _memo_page, _queues_page, _runs_page, _status_page
from narrative_alpha.ops.schedule import (
    LABEL_PREFIX,
    WRAPPER_MARKER,
    default_na_ops_executable,
)
from narrative_alpha.ops.secrets import anthropic_api_key
from narrative_alpha.store import apply_migrations, connect_database

NOW = datetime(2026, 9, 2, 13, 30, tzinfo=UTC)
CAPTURE_TIME = NOW - timedelta(hours=6)
GOLDEN = Path(__file__).with_name("golden")
FIXTURE_HOME = Path("/Users/fixture")
FIXTURE_REPOSITORY = Path("/opt/narrative-alpha")
FIXTURE_NA_OPS = FIXTURE_REPOSITORY / ".venv" / "bin" / "na-ops"
SECRET = "sk-ant-fixture-never-written-anywhere"


class _ClaimProvider:
    """One deterministic terminal Stage 1 claim for batch/status integration tests."""

    def submit_batch(
        self,
        requests: tuple[PreparedExtraction, ...],
    ) -> ProviderBatchSubmission:
        assert len(requests) == 1
        return ProviderBatchSubmission(
            provider_batch_id=f"batch-{requests[0].source_item_id}",
            batch_submission_request_id=f"request-{requests[0].source_item_id}",
        )

    def retrieve_batch(
        self,
        requests: tuple[PreparedExtraction, ...],
        submission: ProviderBatchSubmission,
    ) -> tuple[ProviderResult, ...]:
        request = requests[0]
        text = normalize_item_text(
            "WAS role update", "Jordan Reed will start and see expanded routes for WAS."
        )
        evidence_start = text.index("Jordan Reed")
        payload = {
            "schema_version": "stage1-extraction-v1",
            "prompt_injection_detected": False,
            "claims": [
                {
                    "player_refs": [{"name_raw": "Jordan Reed"}],
                    "team_refs": ["WAS"],
                    "claim_type": "usage",
                    "claim_dimension": "role",
                    "outcome_direction": "increase",
                    "roster_behavior_direction": "increase",
                    "evidence_class": "B",
                    "evidence_basis": "beat_report",
                    "falsifiable": True,
                    "specificity": 0.8,
                    "actionability": 0.8,
                    "novelty": "new",
                    "model_confidence": "high",
                    "uncertainty_flags": ["none"],
                    "ambiguity_flags": ["none"],
                    "suggested_channels": ["mean", "ownership"],
                    "disconfirming_context": None,
                    "evidence_refs": [
                        {
                            "source_item_id": request.source_item_id,
                            "extract_start": evidence_start,
                            "extract_end": len(text),
                            "verbatim_extract": text[evidence_start:],
                        }
                    ],
                }
            ],
        }
        return (
            ProviderResult(
                custom_id=request.custom_id,
                provider_request_id=None,
                batch_submission_request_id=submission.batch_submission_request_id,
                provider_batch_id=submission.provider_batch_id,
                provider_message_id=f"message-{request.source_item_id}",
                actual_model_id="claude-haiku-4-5-20251001",
                output_json=json.dumps(payload),
                content_types=("text",),
                stop_reason="end_turn",
                input_tokens=20,
                output_tokens=10,
                latency_ms=1,
            ),
        )


def _write_config(tmp_path: Path, **overrides: object) -> Path:
    settings: dict[str, object] = {
        "timezone": "America/New_York",
        "season": 2026,
        "monthly_llm_budget_usd": "50.00",
        "keychain_service": "narrative-alpha-anthropic",
        "batch_weekdays": '["wed", "thu", "fri"]',
        "batch_local_time": "09:30",
        "database": str(tmp_path / "store.sqlite3"),
        "snapshot_root": str(tmp_path / "snapshots"),
        "nflverse_archive": str(tmp_path / "archive"),
        "log_directory": str(tmp_path / "logs"),
    }
    settings |= overrides
    max_items = settings.get("max_items_per_run")
    max_items_line = "" if max_items is None else f"max_items_per_run = {max_items}"
    path = tmp_path / "ops.toml"
    path.write_text(
        f"""
timezone = "{settings["timezone"]}"
season = {settings["season"]}
monthly_llm_budget_usd = "{settings["monthly_llm_budget_usd"]}"
keychain_service = "{settings["keychain_service"]}"

[batch]
weekdays = {settings["batch_weekdays"]}
local_time = "{settings["batch_local_time"]}"
{max_items_line}

[paths]
database = "{settings["database"]}"
snapshot_root = "{settings["snapshot_root"]}"
nflverse_archive = "{settings["nflverse_archive"]}"
log_directory = "{settings["log_directory"]}"
""".lstrip(),
        encoding="utf-8",
    )
    return path


def _config(tmp_path: Path, **overrides: object):
    return load_ops_config(_write_config(tmp_path, **overrides))


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _seed_source_item(
    connection: sqlite3.Connection,
    *,
    title: str = "WAS role update",
    body: str = "Jordan Reed will start and see expanded routes for WAS.",
    observed_at: datetime = CAPTURE_TIME,
    external_item_id: str = "item-fixture",
) -> int:
    configured = _timestamp(CAPTURE_TIME - timedelta(days=10))
    captured = _timestamp(observed_at)
    connection.execute("INSERT OR IGNORE INTO source_keys(source_id) VALUES ('source-a')")
    connection.execute(
        """
        INSERT OR IGNORE INTO sources(
            source_id, display_name, source_family, collector_kind, feed_url, enabled,
            source, published_at, observed_at, ingested_at, effective_at, valid_from,
            valid_to, source_version, run_id
        ) VALUES (
            'source-a', 'Fixture source', 'official_team', 'rss_atom',
            'https://example.test/feed.xml', 1, 'fixture', NULL, ?, ?, NULL, ?,
            NULL, 'fixture-v1', NULL
        )
        """,
        (configured, configured, configured),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO source_policies(
            source_id, permitted_use, raw_retention_days, personal_data_fields_allowed,
            must_honor_deletions, redistribution_allowed, third_party_processing_allowed,
            commercial_use_status, terms_reviewed_at, source, published_at, observed_at,
            ingested_at, effective_at, valid_from, valid_to, source_version, run_id
        ) VALUES (
            'source-a', 'internal analysis', 30, '[]', 1, 0, 1, 'prohibited', ?,
            'fixture', NULL, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL
        )
        """,
        (configured, configured, configured, configured),
    )
    canonical = normalize_item_text(title, body)
    cursor = connection.execute(
        """
        INSERT INTO source_items(
            source_id, external_item_id, canonical_url, title, raw_content, cleaned_text,
            content_sha256, source, published_at, observed_at, ingested_at, effective_at,
            valid_from, valid_to, source_version, run_id
        ) VALUES (
            'source-a', ?, 'https://example.test/item', ?, X'3c6974656d2f3e', ?,
            ?, 'source-a', ?, ?, ?, ?, ?, NULL, 'fixture-v1', NULL
        )
        """,
        (
            external_item_id,
            title,
            body,
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            captured,
            captured,
            captured,
            captured,
            captured,
        ),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _seed_player(connection: sqlite3.Connection) -> None:
    """One canonical player: extraction refuses to run against an empty roster."""

    stamp = _timestamp(CAPTURE_TIME - timedelta(days=10))
    connection.execute(
        """
        INSERT OR IGNORE INTO players(
            player_key, canonical_name, position, birth_date, source, published_at,
            observed_at, ingested_at, effective_at, valid_from, valid_to, source_version,
            run_id
        ) VALUES (
            'jordan-reed', 'Jordan Reed', 'TE', NULL, 'fixture', NULL, ?, ?, NULL, ?, NULL,
            'fixture-v1', NULL
        )
        """,
        (stamp, stamp, stamp),
    )


def _collection(
    *,
    reports: tuple[CollectionReport, ...] = (),
    errors: tuple[SourceCollectionError, ...] = (),
    attempted: tuple[str, ...] = ("source-a",),
):
    def collect(connection: sqlite3.Connection, *, observed_at: datetime) -> CollectionRunReport:
        return CollectionRunReport(
            observed_at=observed_at,
            reports=reports,
            errors=errors,
            attempted_source_ids=attempted,
        )

    return collect


def _refresh_ok(season: int, archive: Path, *, reviewed_at: date) -> Any:
    return SimpleNamespace(
        season=season,
        sha256="a" * 64,
        compared_with=SimpleNamespace(sha256="a" * 64),
        added=(),
        removed=(),
        changed=(),
    )


def _never_called(*args: object, **kwargs: object) -> Any:
    raise AssertionError("this step must not run")


def _dependencies(**overrides: object) -> BatchDependencies:
    base = BatchDependencies(
        collect=_collection(reports=(_report(),)),
        refresh_roster=_refresh_ok,
        run_extraction=_never_called,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _report(source_id: str = "source-a") -> CollectionReport:
    return CollectionReport(
        source_id=source_id,
        observed_at=CAPTURE_TIME,
        fetched_items=3,
        inserted_items=2,
        duplicate_items=1,
        attempts=1,
    )


def test_failed_collection_still_purges_records_history_and_exits_nonzero(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    purge_calls: list[datetime] = []

    def purge(connection: sqlite3.Connection, *, as_of: datetime) -> PurgeReport:
        purge_calls.append(as_of)
        return PurgeReport(as_of, 0, ())

    def collect(connection: sqlite3.Connection, *, observed_at: datetime) -> CollectionRunReport:
        raise CollectionError("every feed refused the connection")

    with connect_database(config.database) as connection:
        apply_migrations(connection)
        report = run_batch(
            connection,
            config=config,
            now=NOW,
            dependencies=_dependencies(
                collect=collect,
                purge=purge,
                plan_extraction=_never_called,
                run_extraction=_never_called,
            ),
        )
        recorded = {
            str(row["step"]): (str(row["status"]), row["error_text"])
            for row in connection.execute("SELECT step, status, error_text FROM ops_runs")
        }

    assert not report.ok
    assert len(purge_calls) == 1
    assert recorded["collect"][0] == "failed"
    assert "every feed refused the connection" in str(recorded["collect"][1])
    # Purge is a retention obligation; a bad fetch never excuses skipping it.
    assert recorded["purge"][0] == "succeeded"
    # Nothing was collected, so extraction has no new input and says so rather than
    # silently submitting an empty window.
    assert recorded["extract"][0] == "skipped"
    assert "collection failed entirely" in str(recorded["extract"][1])
    assert recorded["nflverse_refresh"][0] == "succeeded"


def test_partial_collection_failure_still_extracts(tmp_path: Path) -> None:
    config = _config(tmp_path)
    extract_windows: list[tuple[datetime, datetime]] = []

    def plan(connection: sqlite3.Connection, **kwargs: Any) -> Any:
        extract_windows.append((kwargs["window_start"], kwargs["window_end"]))
        return SimpleNamespace(
            ready=(),
            resumable=(),
            submission_unknown=(),
            injection_blocked=(),
            ineligible=(),
            estimated_cost_nanos_usd=0,
        )

    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed_player(connection)
        report = run_batch(
            connection,
            config=config,
            now=NOW,
            dependencies=_dependencies(
                collect=_collection(
                    reports=(_report("source-a"),),
                    errors=(SourceCollectionError("source-b", "404 Not Found"),),
                    attempted=("source-a", "source-b"),
                ),
                plan_extraction=plan,
            ),
        )

    assert not report.ok
    collect_step = report.step("collect")
    assert collect_step is not None and collect_step.status == "failed"
    assert collect_step.summary["dead_source_ids"] == ["source-b"]
    extract_step = report.step("extract")
    assert extract_step is not None and extract_step.status == "succeeded"
    assert len(extract_windows) == 1


def test_extraction_window_derives_from_last_success_and_is_overridable(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    watermark = NOW - timedelta(days=3)
    override = NOW - timedelta(days=10)
    windows: list[datetime] = []

    def plan(connection: sqlite3.Connection, **kwargs: Any) -> Any:
        windows.append(kwargs["window_start"])
        return SimpleNamespace(
            ready=(),
            resumable=(),
            submission_unknown=(),
            injection_blocked=(),
            ineligible=(),
            estimated_cost_nanos_usd=0,
        )

    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed_player(connection)
        _seed_source_item(connection)

        # With no recorded run, the window opens at the earliest retained item.
        assert extraction_window_start(connection, now=NOW) == CAPTURE_TIME

        record_ops_run(
            connection,
            batch_run_id="ops-earlier",
            step="extract",
            status="succeeded",
            started_at=watermark - timedelta(minutes=5),
            finished_at=watermark,
            summary={"window_end": _timestamp(watermark)},
        )
        # A later *failed* run must not advance the watermark.
        record_ops_run(
            connection,
            batch_run_id="ops-later",
            step="extract",
            status="failed",
            started_at=NOW - timedelta(hours=2),
            finished_at=NOW - timedelta(hours=1),
            summary={"window_end": _timestamp(NOW - timedelta(hours=1))},
            error_text="provider refused",
        )
        connection.commit()

        assert extraction_window_start(connection, now=NOW) == watermark

        run_batch(
            connection,
            config=config,
            now=NOW,
            dependencies=_dependencies(plan_extraction=plan),
        )
        run_batch(
            connection,
            config=config,
            now=NOW,
            window_start=override,
            dependencies=_dependencies(plan_extraction=plan),
        )

    assert windows == [watermark, override]


def test_budget_guard_refuses_the_whole_batch_and_records_the_numbers(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, monthly_llm_budget_usd="0.00001")
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed_player(connection)
        _seed_source_item(connection)
        connection.commit()
        report = run_batch(
            connection,
            config=config,
            now=NOW,
            dependencies=_dependencies(run_extraction=_never_called),
        )
        stored = connection.execute(
            "SELECT status, error_text, summary_json FROM ops_runs WHERE step = 'extract'"
        ).fetchone()

    step = report.step("extract")
    assert step is not None and step.status == "failed"
    assert "budget guard refused the batch" in str(step.error_text)
    # The sentence rounds to cents; the stored summary keeps full precision.
    assert "$0.00 budget" in str(step.error_text)
    assert stored["status"] == "failed"
    summary = json.loads(stored["summary_json"])
    assert summary["budget_usd"] == "0.00001"
    assert summary["month_to_date_spend_usd"] == "0"
    # The guard is all-or-nothing: it prices the batch and submits none of it.
    assert summary["ready_items"] == 1
    assert "submitted_items" not in summary


def test_budget_guard_allows_a_batch_inside_the_budget(tmp_path: Path) -> None:
    config = _config(tmp_path, monthly_llm_budget_usd="50.00")
    submitted: list[int] = []

    def run_extraction(connection: sqlite3.Connection, **kwargs: Any) -> Any:
        submitted.append(len(kwargs))
        return SimpleNamespace(
            run_id="stage1-fixture",
            selected_items=1,
            submitted_items=1,
            succeeded_items=1,
            claims_stored=2,
            flagged_item_ids=(),
            errors=(),
            ineligible=(),
            ok=True,
            pending=False,
        )

    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed_player(connection)
        _seed_source_item(connection)
        connection.commit()
        report = run_batch(
            connection,
            config=config,
            now=NOW,
            dependencies=_dependencies(
                run_extraction=run_extraction,
                provider_factory=lambda: SimpleNamespace(),
            ),
        )

    step = report.step("extract")
    assert step is not None and step.status == "succeeded", step
    assert submitted == [6]
    assert step.summary["claims_stored"] == 2


def test_missing_credential_fails_the_step_without_reserving_anything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        "narrative_alpha.ops.secrets.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=44, stdout=""),
    )
    config = _config(tmp_path)
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed_player(connection)
        _seed_source_item(connection)
        connection.commit()
        report = run_batch(
            connection,
            config=config,
            now=NOW,
            dependencies=_dependencies(run_extraction=_never_called),
        )
        attempts = connection.execute("SELECT count(*) FROM source_item_extractions").fetchone()[0]

    step = report.step("extract")
    assert step is not None and step.status == "failed"
    assert "ANTHROPIC_API_KEY is not set" in str(step.error_text)
    assert "security add-generic-password" in str(step.error_text)
    assert attempts == 0


def test_batch_builds_the_shared_episode_snapshot_at_its_start_timestamp(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    seen: dict[str, datetime] = {}

    def episodes(connection: sqlite3.Connection, **kwargs: Any) -> Any:
        seen["as_of"] = kwargs["as_of"]
        seen["built_at"] = kwargs["built_at"]
        return build_episodes(connection, **kwargs)

    def extract(connection: sqlite3.Connection, **kwargs: Any) -> Any:
        return run_extraction_batch(connection, clock=lambda: NOW, **kwargs)

    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed_player(connection)
        _seed_source_item(connection)
        connection.commit()
        report = run_batch(
            connection,
            config=config,
            now=NOW,
            dependencies=_dependencies(
                provider_factory=_ClaimProvider,
                run_extraction=extract,
                load_pricing=load_batch_pricing,
                build_episodes=episodes,
            ),
        )
        steps = [
            str(row["step"])
            for row in connection.execute("SELECT step FROM ops_runs ORDER BY ops_run_id")
        ]
        status = collect_ops_status(connection, config=config, database=config.database, now=NOW)

    episode_step = report.step("episodes")
    assert episode_step is not None and episode_step.status == "succeeded"
    assert seen == {"as_of": NOW, "built_at": NOW}
    assert episode_step.summary["episodes_inserted"] == 1
    assert steps[-2:] == ["nflverse_refresh", "episodes"]
    assert status.narrative.items_collected_last_7_days == 1
    assert status.narrative.items_extracted == 1
    assert status.narrative.items_awaiting_extraction == 0
    assert status.narrative.claims_recorded == 1
    snapshot = status.narrative.newest_episode_snapshot
    assert snapshot is not None
    assert snapshot.as_of == NOW
    assert snapshot.prompt_version_id == "stage1-extraction-v1"
    assert snapshot.episode_count == 1
    payload = status_payload(status)
    assert payload["narrative"] == {
        "items_collected_last_7_days": 1,
        "items_extracted": 1,
        "items_awaiting_extraction": 0,
        "claims_recorded": 1,
        "newest_episode_snapshot": {
            "as_of": _timestamp(NOW),
            "prompt_version_id": "stage1-extraction-v1",
            "method_version": "deterministic-token-set-jaccard-v1",
            "episode_count": 1,
        },
        "pending_review_flags": 0,
    }
    rendered = render_status(status)
    assert "NARRATIVE" in rendered
    assert "items extracted/awaiting 1 / 0" in rendered
    assert "stage1-extraction-v1" in rendered


def test_episode_step_skips_without_a_successful_extraction_and_records_builder_failure(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    def refusing_episodes(connection: sqlite3.Connection, **kwargs: Any) -> Any:
        raise EpisodeError("Stage 2 fixture refusal")

    with connect_database(config.database) as connection:
        apply_migrations(connection)
        skipped = run_batch(connection, config=config, now=NOW, dependencies=_dependencies())
        record_ops_run(
            connection,
            batch_run_id="prior-stage1",
            step="extract",
            status="succeeded",
            started_at=NOW - timedelta(hours=2),
            finished_at=NOW - timedelta(hours=1),
            summary={"window_end": _timestamp(NOW - timedelta(hours=1))},
        )
        connection.commit()
        failed = run_batch(
            connection,
            config=config,
            now=NOW,
            dependencies=_dependencies(build_episodes=refusing_episodes),
        )

    skipped_step = skipped.step("episodes")
    assert skipped_step is not None and skipped_step.status == "skipped"
    assert "no extraction has ever succeeded" in str(skipped_step.error_text)
    failed_step = failed.step("episodes")
    assert failed_step is not None and failed_step.status == "failed"
    assert "Stage 2 fixture refusal" in str(failed_step.error_text)


def test_keychain_credential_is_ephemeral_and_never_reaches_operator_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    config = _config(tmp_path)
    security_calls: list[tuple[object, ...]] = []
    seen: dict[str, str | None] = {}

    def security(command: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
        security_calls.append(command)
        assert kwargs["check"] is False
        return SimpleNamespace(returncode=0, stdout=f"{SECRET}\n")

    def provider(*, timeout_seconds: float) -> object:
        assert timeout_seconds > 0
        seen["during_provider_construction"] = os.environ.get("ANTHROPIC_API_KEY")
        return SimpleNamespace()

    def extract(connection: sqlite3.Connection, **kwargs: Any) -> Any:
        seen["during_extraction"] = os.environ.get("ANTHROPIC_API_KEY")
        return SimpleNamespace(
            run_id="stage1-keychain-fixture",
            selected_items=1,
            submitted_items=1,
            succeeded_items=1,
            claims_stored=0,
            flagged_item_ids=(),
            errors=(),
            ineligible=(),
            ok=True,
            pending=False,
        )

    monkeypatch.setattr("narrative_alpha.ops.secrets.subprocess.run", security)
    monkeypatch.setattr("narrative_alpha.ops.batch.AnthropicBatchProvider", provider)
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed_player(connection)
        _seed_source_item(connection)
        connection.commit()
        report = run_batch(
            connection,
            config=config,
            now=NOW,
            dependencies=_dependencies(run_extraction=extract),
        )
        rows = connection.execute("SELECT summary_json, error_text FROM ops_runs").fetchall()
        status = collect_ops_status(connection, config=config, database=config.database, now=NOW)

    context = DashboardContext(
        config=config,
        database=config.database,
        runner=LaneRunner(),
        clock=lambda: NOW,
    )
    pages = (_status_page(context), _queues_page(context), _runs_page(context), _memo_page(context))
    extract_step = report.step("extract")
    assert extract_step is not None and extract_step.status == "succeeded"
    assert security_calls == [
        (
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            config.keychain_service,
            "-w",
        )
    ]
    assert seen == {"during_provider_construction": SECRET, "during_extraction": None}
    assert os.environ.get("ANTHROPIC_API_KEY") is None
    assert all(SECRET not in str(row["summary_json"]) for row in rows)
    assert all(SECRET not in str(row["error_text"] or "") for row in rows)
    assert SECRET not in json.dumps(status_payload(status))
    assert all(SECRET not in page for page in pages)


def test_anthropic_api_key_prefers_the_process_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "environment-api-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "environment-auth-token")

    def unexpected_security(*args: object, **kwargs: object) -> object:
        raise AssertionError("the Keychain must not be read while an environment key exists")

    monkeypatch.setattr("narrative_alpha.ops.secrets.subprocess.run", unexpected_security)
    assert anthropic_api_key(config) == "environment-api-key"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert anthropic_api_key(config) == "environment-auth-token"


def test_anthropic_api_key_gives_up_when_the_keychain_prompt_is_never_answered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A locked Keychain can raise a dialog; the lookup must time out, not hang the lane."""

    import subprocess

    config = _config(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    seen: dict[str, object] = {}

    def hanging_security(*args: object, **kwargs: object) -> object:
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(cmd="security", timeout=float(kwargs["timeout"]))

    monkeypatch.setattr("narrative_alpha.ops.secrets.subprocess.run", hanging_security)
    assert anthropic_api_key(config) is None
    assert isinstance(seen["timeout"], float) and seen["timeout"] > 0


def test_cli_batch_exits_nonzero_when_a_step_failed(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    dependencies = _dependencies(
        collect=_collection(
            errors=(SourceCollectionError("source-a", "connection reset"),),
        ),
        plan_extraction=_never_called,
    )
    exit_code = ops_main(["--config", str(config_path), "batch"], dependencies=dependencies)
    assert exit_code == 1

    ok_code = ops_main(
        ["--config", str(config_path), "batch"],
        dependencies=_dependencies(
            plan_extraction=lambda connection, **kwargs: SimpleNamespace(
                ready=(),
                resumable=(),
                submission_unknown=(),
                injection_blocked=(),
                ineligible=(),
                estimated_cost_nanos_usd=0,
            )
        ),
    )
    assert ok_code == 0


def test_status_renders_on_an_empty_database(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        status = collect_ops_status(connection, config=config, database=config.database, now=NOW)

    screen = render_status(status)
    assert "last success never" in screen
    assert "NOT SEEDED" in screen
    assert status.extraction_backlog == 0
    assert status.snapshot_week is None
    assert status.player_rows == 0
    # A missing roster is a sentence, not a zero the operator has to interpret.
    assert any("ROSTER NOT SEEDED" in warning for warning in status.warnings)
    assert status_payload(status)["identity"] == {
        "player_rows": 0,
        "roster_seeded": False,
        "unresolved_identities": 0,
    }


def test_status_reports_a_seeded_store_with_a_pending_receipt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    receipts = config.database.parent / (config.database.name + ".stage1-receipts")
    receipts.mkdir(parents=True)
    (receipts / f"accepted-{'b' * 64}.json").write_text("{}", encoding="utf-8")

    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed_source_item(connection)
        record_ops_run(
            connection,
            batch_run_id="ops-fixture",
            step="collect",
            status="failed",
            started_at=NOW - timedelta(hours=4),
            finished_at=NOW - timedelta(hours=4) + timedelta(seconds=40),
            summary={
                "attempted_sources": 3,
                "collected_sources": 2,
                "dead_sources": 1,
                "dead_source_ids": ["espn-was"],
            },
            error_text="1 of 3 sources failed — espn-was: 404 Not Found",
        )
        connection.commit()
        status = collect_ops_status(connection, config=config, database=config.database, now=NOW)

    screen = render_status(status)
    assert status.pending_accepted_receipts == 1
    assert status.dead_feed_count == 1
    assert status.dead_feed_source_ids == ("espn-was",)
    assert status.items_collected_last_7_days == 1
    assert status.extraction_backlog == 1
    assert status.extraction_backlog_cost_usd is not None
    assert "espn-was" in screen
    assert "accepted-batch receipts  1" in screen
    payload = status_payload(status)
    assert payload["extraction"]["pending_accepted_receipts"] == 1  # type: ignore[index]
    assert payload["collection"]["dead_feed_count"] == 1  # type: ignore[index]


def test_status_counts_month_to_date_spend_in_the_configured_zone(tmp_path: Path) -> None:
    config = _config(tmp_path)
    # 2026-09-01T02:00Z is still August 31 in New York, so it belongs to last month.
    assert month_start_utc(
        datetime(2026, 9, 1, 2, 0, tzinfo=UTC), timezone=config.timezone
    ) == datetime(2026, 8, 1, 4, 0, tzinfo=UTC)
    assert month_start_utc(NOW, timezone=config.timezone) == datetime(2026, 9, 1, 4, 0, tzinfo=UTC)


def _fake_na_ops(tmp_path: Path) -> Path:
    """A stand-in binary, so install-time validation sees a real executable."""

    binary = tmp_path / "bin" / "na-ops"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


def _golden_config(tmp_path: Path):
    """A config with fixed paths, so the rendered agents are byte-comparable."""

    config = _config(
        tmp_path,
        timezone="America/Los_Angeles",
        database="data/db/narrative_alpha.sqlite3",
        snapshot_root="data/snapshots",
        nflverse_archive="data/archive/nflverse",
        log_directory="data/logs",
    )
    return replace(config, path=Path("config/ops.toml"))


def _golden_jobs(tmp_path: Path):
    return build_jobs(
        _golden_config(tmp_path),
        home=FIXTURE_HOME,
        repository=FIXTURE_REPOSITORY,
        na_ops_executable=FIXTURE_NA_OPS,
    )


@pytest.mark.parametrize(
    ("label", "golden_name", "attribute"),
    [
        (f"{LABEL_PREFIX}.batch", "ops_batch_wrapper.sh", "script"),
        (f"{LABEL_PREFIX}.batch", "ops_batch_agent.plist", "plist"),
        (
            f"{LABEL_PREFIX}.reminder-sunday-final",
            "ops_reminder_sunday_final_wrapper.sh",
            "script",
        ),
        (
            f"{LABEL_PREFIX}.reminder-sunday-final",
            "ops_reminder_sunday_final_agent.plist",
            "plist",
        ),
    ],
)
def test_agent_rendering_matches_golden(
    tmp_path: Path,
    label: str,
    golden_name: str,
    attribute: str,
) -> None:
    job = next(job for job in _golden_jobs(tmp_path) if job.label == label)
    rendered = getattr(job, attribute)
    expected = (GOLDEN / golden_name).read_bytes()
    if isinstance(rendered, str):
        rendered = rendered.encode("utf-8")
    assert rendered == expected


def test_no_agent_carries_key_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    for job in _golden_jobs(tmp_path):
        plist = job.plist.decode("utf-8")
        assert SECRET not in job.script
        assert SECRET not in plist
        # The key must not even be *named* in a plist: launchd stores plists in plain
        # text and copies them into its own state.
        assert "ANTHROPIC" not in plist
    batch = next(job for job in _golden_jobs(tmp_path) if job.label == f"{LABEL_PREFIX}.batch")
    # It is fetched, never embedded.
    assert "security find-generic-password" in batch.script
    assert batch.script.count("ANTHROPIC_API_KEY") == 2


def test_reminder_jobs_do_no_data_work_and_carry_the_manual_commands(
    tmp_path: Path,
) -> None:
    reminders = [job for job in _golden_jobs(tmp_path) if ".reminder-" in job.label]
    assert len(reminders) == 3
    for job in reminders:
        assert "osascript" in job.script
        # A reminder never runs the lane and never reaches for a credential.
        assert str(FIXTURE_NA_OPS) not in job.script
        assert "security" not in job.script
        # It does carry the exact commands the operator must type.
        assert "na-snapshot capture --season 2026" in job.script
        assert "na-snapshot fetch --season 2026" in job.script


def test_manual_capture_times_convert_from_eastern_to_local(tmp_path: Path) -> None:
    config = _golden_config(tmp_path)
    # §9.0 fixes Sat 6:00 p.m., Sun 9:00 a.m., Sun 11:00 a.m. Eastern.
    assert eastern_to_local(time(18, 0), timezone=config.timezone, season=2026) == time(15, 0)
    assert eastern_to_local(time(9, 0), timezone=config.timezone, season=2026) == time(6, 0)
    assert eastern_to_local(time(11, 0), timezone=config.timezone, season=2026) == time(8, 0)
    scheduled = {
        job.label.rsplit(".", 1)[-1]: (job.weekday_numbers, job.local_time)
        for job in _golden_jobs(tmp_path)
    }
    assert scheduled["reminder-saturday-projections"] == ((6,), time(15, 0))
    assert scheduled["reminder-sunday-early"] == ((0,), time(6, 0))
    assert scheduled["reminder-sunday-final"] == ((0,), time(8, 0))
    assert scheduled["batch"] == ((3, 4, 5), time(9, 30))


def test_install_writes_agents_under_the_monkeypatched_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repository = tmp_path / "repo"
    monkeypatch.setenv("HOME", str(home))
    config = _config(tmp_path)
    jobs = build_jobs(
        config, home=home, repository=repository, na_ops_executable=_fake_na_ops(tmp_path)
    )
    calls: list[tuple[str, ...]] = []

    def launchctl(command: object) -> tuple[int, str]:
        calls.append(tuple(str(part) for part in command))  # type: ignore[arg-type]
        return 0, ""

    install_schedule(jobs, launchctl=launchctl)

    for job in jobs:
        assert job.plist_path.exists()
        assert job.wrapper_path.exists()
        assert job.plist_path.is_relative_to(home / "Library" / "LaunchAgents")
        assert oct(job.wrapper_path.stat().st_mode)[-3:] == "700"
    assert [command[0] for command in calls] == ["bootout", "bootstrap"] * len(jobs)
    states = inspect_schedule(jobs)
    assert all(state.plist_managed and state.wrapper_managed for state in states)


def test_uninstall_removes_only_the_files_it_wrote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repository = tmp_path / "repo"
    monkeypatch.setenv("HOME", str(home))
    jobs = build_jobs(
        _config(tmp_path),
        home=home,
        repository=repository,
        na_ops_executable=_fake_na_ops(tmp_path),
    )
    install_schedule(jobs, launchctl=None)

    # Someone else's agent occupying one of our labels, and a hand-edited wrapper.
    foreign = next(job for job in jobs if job.label.endswith("reminder-sunday-early"))
    foreign.plist_path.write_text("<plist>not ours</plist>", encoding="utf-8")
    hand_edited = next(job for job in jobs if job.label.endswith("reminder-sunday-final"))
    hand_edited.wrapper_path.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")

    changes = uninstall_schedule(jobs, launchctl=None)
    left_alone = {change.path for change in changes if change.action == "left alone"}

    assert left_alone == {foreign.plist_path, hand_edited.wrapper_path}
    assert foreign.plist_path.exists()
    assert hand_edited.wrapper_path.exists()
    for job in jobs:
        if job.plist_path not in left_alone:
            assert not job.plist_path.exists()
        if job.wrapper_path not in left_alone:
            assert not job.wrapper_path.exists()
    assert hand_edited.wrapper_path.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"
    assert WRAPPER_MARKER not in hand_edited.wrapper_path.read_text(encoding="utf-8")


def test_uninstall_reports_absent_files_without_failing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    jobs = build_jobs(
        _config(tmp_path),
        home=home,
        repository=tmp_path / "repo",
        na_ops_executable=_fake_na_ops(tmp_path),
    )
    changes = uninstall_schedule(jobs, launchctl=None)
    assert {change.action for change in changes} == {"absent"}


def test_schedule_cli_show_and_install_round_trip(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _write_config(tmp_path)
    home = tmp_path / "home"
    repository = tmp_path / "repo"
    arguments = [
        "--config",
        str(config_path),
        "schedule",
        "--home",
        str(home),
        "--repository",
        str(repository),
        "--executable",
        str(_fake_na_ops(tmp_path)),
        "--no-launchctl",
    ]
    assert ops_main([*arguments, "install"]) == 0
    installed = capsys.readouterr().out
    assert "security add-generic-password" in installed

    assert ops_main([*arguments, "show"]) == 0
    shown = capsys.readouterr().out
    assert "(installed)" in shown
    assert "missed while the Mac was asleep" in shown

    assert ops_main([*arguments, "uninstall"]) == 0
    removed = capsys.readouterr().out
    assert "removed" in removed
    assert not list((home / "Library" / "LaunchAgents").glob("*.plist"))


def test_invalid_operator_config_is_refused_with_the_field_named(tmp_path: Path) -> None:
    path = tmp_path / "ops.toml"
    path.write_text('timezone = "Mars/Olympus"\n', encoding="utf-8")
    with pytest.raises(OpsConfigError) as error:
        load_ops_config(path)
    assert "Mars/Olympus" in str(error.value)


def test_ops_runs_history_is_append_only(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        record_ops_run(
            connection,
            batch_run_id="ops-fixture",
            step="purge",
            status="succeeded",
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
            summary={"tombstones_written": 0},
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE ops_runs SET status = 'failed'")
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM ops_runs")
        connection.rollback()
        assert last_run(connection, step="purge", status="succeeded") is not None


def test_install_refuses_an_agent_that_names_a_missing_binary(tmp_path: Path) -> None:
    """A wrapper pointing at nothing would fail silently at 09:30 every Wednesday."""

    jobs = build_jobs(
        _config(tmp_path),
        home=tmp_path / "home",
        repository=tmp_path / "repo",
        na_ops_executable=tmp_path / "bin" / "does-not-exist",
    )
    with pytest.raises(ScheduleError) as error:
        install_schedule(jobs, launchctl=None)
    assert "not an executable file" in str(error.value)
    assert "--executable" in str(error.value)
    assert not (tmp_path / "home").exists()


def test_default_executable_prefers_the_installed_na_ops_on_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = _fake_na_ops(tmp_path)
    monkeypatch.setenv("PATH", str(installed.parent))
    assert default_na_ops_executable() == installed.resolve()


def test_manual_action_list_stays_one_line_per_item(tmp_path: Path) -> None:
    """The step block carries the full failure text; the action list must stay scannable."""

    config = _config(tmp_path)
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        record_ops_run(
            connection,
            batch_run_id="ops-fixture",
            step="extract",
            status="failed",
            started_at=NOW - timedelta(hours=1),
            finished_at=NOW - timedelta(minutes=59),
            summary={},
            error_text="budget guard refused the batch: " + "x" * 500,
        )
        connection.commit()
        status = collect_ops_status(connection, config=config, database=config.database, now=NOW)

    action = next(item for item in status.manual_actions if item.startswith("the extract"))
    assert len(action) < 200
    assert action.endswith("…")
    # The detail view keeps every character.
    assert "x" * 500 in render_status(status)


# --- Review fixes (2026-09-02) -------------------------------------------------------------


def _empty_plan(connection: sqlite3.Connection, **kwargs: Any) -> Any:
    return SimpleNamespace(
        ready=(),
        resumable=(),
        submission_unknown=(),
        injection_blocked=(),
        ineligible=(),
        estimated_cost_nanos_usd=0,
    )


def test_extraction_is_skipped_until_a_roster_is_seeded(tmp_path: Path) -> None:
    # Extracting against an empty roster would send every name to the unresolved queue,
    # each one a by-hand resolution; the lane waits, says why, and keeps the watermark.
    config = _config(tmp_path)
    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed_source_item(connection)
        connection.commit()
        unseeded = run_batch(
            connection,
            config=config,
            now=NOW,
            dependencies=_dependencies(plan_extraction=_never_called, run_extraction=_never_called),
        )
        watermark = extraction_window_start(connection, now=NOW)
        _seed_player(connection)
        connection.commit()
        seeded = run_batch(
            connection,
            config=config,
            now=NOW,
            dependencies=_dependencies(plan_extraction=_empty_plan),
        )

    step = unseeded.step("extract")
    assert step is not None and step.status == "skipped"
    assert "roster not seeded" in str(step.error_text)
    assert unseeded.ok  # a stated skip is not a failure
    assert watermark == CAPTURE_TIME
    seeded_step = seeded.step("extract")
    assert seeded_step is not None and seeded_step.status == "succeeded"


def test_extraction_window_closes_after_collection_in_production_clock_mode(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    observed: list[datetime] = []
    windows: list[tuple[datetime, datetime]] = []

    def collect(connection: sqlite3.Connection, *, observed_at: datetime) -> CollectionRunReport:
        observed.append(observed_at)
        _seed_source_item(connection, observed_at=observed_at, external_item_id="fresh")
        connection.commit()
        return CollectionRunReport(observed_at, (_report(),), (), ("source-a",))

    def plan(connection: sqlite3.Connection, **kwargs: Any) -> Any:
        windows.append((kwargs["window_start"], kwargs["window_end"]))
        return _empty_plan(connection)

    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed_player(connection)
        connection.commit()
        run_batch(
            connection,
            config=config,
            dependencies=_dependencies(collect=collect, plan_extraction=plan),
            clock=lambda: datetime.now(UTC) + timedelta(seconds=1),
        )

    # The item this run just collected sits inside the window it extracts, not the next one.
    assert len(observed) == 1 and len(windows) == 1
    start, end = windows[0]
    assert start <= observed[0] < end


def test_nflverse_step_failures_tell_the_operator_how_to_re_pin(tmp_path: Path) -> None:
    config = _config(tmp_path)

    def gone(season: int, archive: Path, *, reviewed_at: date) -> Any:
        raise RosterHashError("nflverse roster hash mismatch for 2026")

    def moved(season: int, archive: Path, *, reviewed_at: date) -> Any:
        return SimpleNamespace(
            season=season,
            sha256="b" * 64,
            compared_with=SimpleNamespace(sha256="a" * 64),
            added=(("00-0001", "New Player"),),
            removed=(),
            changed=(),
            prior_available=True,
        )

    with connect_database(config.database) as connection:
        apply_migrations(connection)
        failed = run_batch(
            connection, config=config, now=NOW, dependencies=_dependencies(refresh_roster=gone)
        )
        drifted = run_batch(
            connection, config=config, now=NOW, dependencies=_dependencies(refresh_roster=moved)
        )

    gone_step = failed.step("nflverse_refresh")
    assert gone_step is not None and gone_step.status == "failed"
    assert "--allow-missing-prior" in str(gone_step.error_text)
    assert "na-crosswalk nflverse-refresh --season 2026" in str(gone_step.error_text)
    moved_step = drifted.step("nflverse_refresh")
    assert moved_step is not None and moved_step.status == "failed"
    assert "no longer matches the newest pin" in str(moved_step.error_text)
    assert moved_step.summary["matches_pin"] is False
    assert moved_step.summary["players_added"] == 1


def test_reminder_notification_is_one_shell_word_even_with_quotes(tmp_path: Path) -> None:
    from narrative_alpha.ops.schedule import ReminderSpec, _reminder_script

    spec = ReminderSpec(
        slug="quoted",
        weekday="sun",
        eastern_time=time(9, 0),
        title='Don\'t "miss" this',
        notification='It\'s 9 a.m.; capture "now"',
        instructions=("one line",),
    )
    script = _reminder_script(
        spec,
        config=_config(tmp_path),
        log_path=tmp_path / "quoted.log",
        label="com.narrative-alpha.reminder-quoted",
        local_time=time(9, 0),
    )
    line = next(entry for entry in script.splitlines() if entry.startswith("/usr/bin/osascript"))
    words = shlex.split(line.removesuffix(" || true"))
    assert words[:2] == ["/usr/bin/osascript", "-e"]
    assert len(words) == 3
    assert words[2] == (
        'display notification "It\'s 9 a.m.; capture \\"now\\"" '
        'with title "Narrative Alpha" subtitle "Don\'t \\"miss\\" this"'
    )


def test_max_items_defers_without_losing_items_and_defaults_from_config(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    first_at = CAPTURE_TIME
    second_at = CAPTURE_TIME + timedelta(minutes=1)
    windows: list[tuple[datetime, datetime]] = []

    def plan(connection: sqlite3.Connection, **kwargs: Any) -> Any:
        windows.append((kwargs["window_start"], kwargs["window_end"]))
        if kwargs.get("max_items") == 1 and len(windows) == 1:
            return SimpleNamespace(
                ready=(SimpleNamespace(source_item_id=1),),
                resumable=(),
                submission_unknown=(),
                injection_blocked=(),
                ineligible=(),
                estimated_cost_nanos_usd=0,
                deferred_items=1,
                deferred_from=second_at,
            )
        return _empty_plan(connection)

    def run_extraction(connection: sqlite3.Connection, **kwargs: Any) -> Any:
        return SimpleNamespace(
            run_id="stage1-fixture",
            selected_items=1,
            submitted_items=1,
            succeeded_items=1,
            claims_stored=0,
            flagged_item_ids=(),
            errors=(),
            ineligible=(),
            ok=True,
            pending=False,
        )

    with connect_database(config.database) as connection:
        apply_migrations(connection)
        _seed_player(connection)
        _seed_source_item(connection, observed_at=first_at, external_item_id="first")
        _seed_source_item(
            connection,
            body="Jordan Reed practiced in full for WAS.",
            observed_at=second_at,
            external_item_id="second",
        )
        connection.commit()
        first = run_batch(
            connection,
            config=config,
            now=NOW,
            max_items=1,
            dependencies=_dependencies(
                plan_extraction=plan,
                run_extraction=run_extraction,
                provider_factory=lambda: SimpleNamespace(),
            ),
        )
        # The next window reopens at the first deferred item, not at the end of the run.
        assert extraction_window_start(connection, now=NOW) == second_at
        run_batch(
            connection,
            config=config,
            now=NOW,
            dependencies=_dependencies(plan_extraction=plan),
        )

    step = first.step("extract")
    assert step is not None and step.status == "succeeded"
    assert step.summary["deferred_items"] == 1
    assert step.summary["next_window_start"] == _timestamp(second_at)
    assert windows == [(first_at, NOW), (second_at, NOW)]


def test_batch_max_items_per_run_comes_from_config(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, max_items_per_run=7)
    seen: list[object] = []

    def plan(connection: sqlite3.Connection, **kwargs: Any) -> Any:
        seen.append(kwargs.get("max_items"))
        return _empty_plan(connection)

    # Seed through the same config file; `_config` would rewrite it without the override.
    with connect_database(load_ops_config(config_path).database) as connection:
        apply_migrations(connection)
        _seed_player(connection)
        _seed_source_item(connection)
        connection.commit()

    default_code = ops_main(
        ["--config", str(config_path), "batch"],
        dependencies=_dependencies(plan_extraction=plan),
    )
    assert default_code == 0
    assert (
        ops_main(
            ["--config", str(config_path), "batch", "--max-items", "3"],
            dependencies=_dependencies(plan_extraction=plan),
        )
        == 0
    )
    assert seen == [7, 3]
