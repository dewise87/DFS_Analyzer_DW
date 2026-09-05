import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from narrative_alpha.episodes_cli import main as episodes_main
from narrative_alpha.narrative import (
    PROMPT_VERSION_ID,
    EpisodeSnapshotConflictError,
    PreparedExtraction,
    ProviderBatchSubmission,
    ProviderResult,
    build_episodes,
    load_batch_pricing,
    load_episode_audits,
    normalize_item_text,
    run_extraction_batch,
)
from narrative_alpha.store import apply_migrations, connect_database

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "episode_claims.json"
PRICING_PATH = Path("config/model_pricing.toml")
BASE_TIME = datetime.now(UTC).replace(microsecond=0) - timedelta(days=10)


@dataclass(frozen=True)
class FixtureClaim:
    key: str
    source_id: str
    source_family: str
    observed_hours: int
    title: str
    body: str
    player_name: str
    team_refs: tuple[str, ...]
    outcome_direction: str
    roster_behavior_direction: str


@dataclass
class FixtureProvider:
    payload: dict[str, object]

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
        return (
            ProviderResult(
                custom_id=request.custom_id,
                provider_request_id=None,
                batch_submission_request_id=submission.batch_submission_request_id,
                provider_batch_id=submission.provider_batch_id,
                provider_message_id=f"message-{request.source_item_id}",
                actual_model_id="claude-haiku-4-5-20251001",
                output_json=json.dumps(self.payload),
                content_types=("text",),
                stop_reason="end_turn",
                input_tokens=20,
                output_tokens=10,
                latency_ms=1,
            ),
        )


def test_copied_headline_raises_reach_without_event_count(tmp_path: Path) -> None:
    database, player_id = _episode_database(tmp_path, "origin", "copy")
    as_of = BASE_TIME + timedelta(hours=2)

    with connect_database(database) as connection:
        report = build_episodes(connection, as_of=as_of, built_at=as_of + timedelta(hours=1))
        episode = connection.execute("SELECT * FROM narrative_episodes").fetchone()
        relations = connection.execute(
            "SELECT relation, similarity_score FROM episode_claims "
            "ORDER BY (SELECT observed_at FROM source_items "
            "          WHERE source_item_id = episode_claims.source_item_id), claim_id"
        ).fetchall()

    assert player_id is not None
    assert report.claims_considered == 2
    assert report.episode_count == 1
    assert episode["subject_player_id"] == player_id
    assert episode["unique_source_count"] == 2
    assert episode["unique_source_family_count"] == 2
    assert episode["reach_proxy"] == 2
    assert episode["n_events"] == 1
    assert episode["item_count"] == 2
    assert episode["source_entropy"] == pytest.approx(0.6931471805599453)
    assert [row["relation"] for row in relations] == ["origin", "derivative"]
    assert relations[1]["similarity_score"] == 1.0


def test_opposite_direction_links_as_contradicting(tmp_path: Path) -> None:
    database, _ = _episode_database(tmp_path, "origin", "contradiction")
    as_of = BASE_TIME + timedelta(hours=3)

    with connect_database(database) as connection:
        build_episodes(connection, as_of=as_of, built_at=as_of + timedelta(hours=1))
        relation = connection.execute(
            "SELECT relation, linked_claim_id, similarity_score FROM episode_claims "
            "WHERE relation <> 'origin'"
        ).fetchone()

    assert relation["relation"] == "contradicting"
    assert relation["linked_claim_id"] is not None
    assert relation["similarity_score"] >= 0.35


def test_claim_outside_rolling_window_opens_new_episode(tmp_path: Path) -> None:
    database, _ = _episode_database(tmp_path, "origin", "outside-window")
    as_of = BASE_TIME + timedelta(hours=101)

    with connect_database(database) as connection:
        report = build_episodes(connection, as_of=as_of, built_at=as_of + timedelta(hours=1))
        origins = connection.execute(
            "SELECT relation FROM episode_claims ORDER BY episode_id"
        ).fetchall()

    assert report.episode_count == 2
    assert [row["relation"] for row in origins] == ["origin", "origin"]


def test_item_after_as_of_is_excluded_even_when_build_runs_later(tmp_path: Path) -> None:
    database, _ = _episode_database(tmp_path, "origin", "copy")
    as_of = BASE_TIME + timedelta(minutes=30)

    with connect_database(database) as connection:
        report = build_episodes(connection, as_of=as_of, built_at=BASE_TIME + timedelta(hours=3))
        member_count = connection.execute("SELECT count(*) FROM episode_claims").fetchone()[0]

    assert report.claims_considered == 1
    assert report.episode_count == 1
    assert member_count == 1


def test_repeated_build_is_identical_and_changed_parameters_conflict(tmp_path: Path) -> None:
    database, _ = _episode_database(tmp_path, "origin", "copy")
    as_of = BASE_TIME + timedelta(hours=2)

    with connect_database(database) as connection:
        first = build_episodes(connection, as_of=as_of, built_at=as_of + timedelta(hours=1))
        before = _stored_graph(connection)
        second = build_episodes(connection, as_of=as_of, built_at=as_of + timedelta(hours=2))
        after = _stored_graph(connection)
        with pytest.raises(EpisodeSnapshotConflictError, match="differs"):
            build_episodes(
                connection,
                as_of=as_of,
                built_at=as_of + timedelta(hours=2),
                window=timedelta(minutes=30),
            )

    assert first.run_id is not None and not first.reused_existing
    assert second.run_id is None and second.reused_existing
    assert second.episodes_inserted == second.memberships_inserted == 0
    assert before == after


def test_unresolved_claims_are_team_scoped_or_reported_unclustered(tmp_path: Path) -> None:
    database, _ = _episode_database(
        tmp_path,
        "unresolved-team",
        "unresolved-unclustered",
        seed_player=False,
    )
    as_of = BASE_TIME + timedelta(hours=5)

    with connect_database(database) as connection:
        report = build_episodes(connection, as_of=as_of, built_at=as_of + timedelta(hours=1))
        subjects = connection.execute(
            "SELECT subject_type, subject_team_code, unclustered_key "
            "FROM narrative_episodes ORDER BY subject_type"
        ).fetchall()

    assert report.unresolved_player_claims == 2
    assert report.unresolved_player_refs == 2
    assert report.team_scoped_claims == 1
    assert report.unclustered_claims == 1
    assert [(row["subject_type"], row["subject_team_code"]) for row in subjects] == [
        ("team", "CAR"),
        ("unclustered", None),
    ]
    assert subjects[1]["unclustered_key"].startswith("claim:")


def test_show_cli_renders_auditable_relation_graph(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database, player_id = _episode_database(tmp_path, "origin", "copy")
    as_of = BASE_TIME + timedelta(hours=2)
    with connect_database(database) as connection:
        build_episodes(connection, as_of=as_of, built_at=as_of + timedelta(hours=1))
        audits = load_episode_audits(connection, player_id=player_id)

    assert len(audits) == 1
    assert audits[0].claims[0].canonical_text is not None
    assert episodes_main(
        ["show", "--database", str(database), "--player", str(player_id)]
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["count"] == 1
    assert [claim["relation"] for claim in output["episodes"][0]["claims"]] == [
        "origin",
        "derivative",
    ]


def _episode_database(
    tmp_path: Path,
    *keys: str,
    seed_player: bool = True,
) -> tuple[Path, int | None]:
    database = tmp_path / "episodes.sqlite3"
    fixtures = {fixture.key: fixture for fixture in _load_fixtures()}
    with connect_database(database) as connection:
        apply_migrations(connection)
        player_id = _seed_player(connection) if seed_player else None
        for key in keys:
            _seed_extracted_claim(connection, fixtures[key])
    return database, player_id


def _load_fixtures() -> tuple[FixtureClaim, ...]:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return tuple(
        FixtureClaim(
            key=str(item["key"]),
            source_id=str(item["source_id"]),
            source_family=str(item["source_family"]),
            observed_hours=int(item["observed_hours"]),
            title=str(item["title"]),
            body=str(item["body"]),
            player_name=str(item["player_name"]),
            team_refs=tuple(str(team) for team in item["team_refs"]),
            outcome_direction=str(item["outcome_direction"]),
            roster_behavior_direction=str(item["roster_behavior_direction"]),
        )
        for item in payload
    )


def _seed_extracted_claim(connection: sqlite3.Connection, fixture: FixtureClaim) -> None:
    configured_at = BASE_TIME - timedelta(days=5)
    observed_at = BASE_TIME + timedelta(hours=fixture.observed_hours)
    _seed_source(connection, fixture, configured_at)
    canonical_text = normalize_item_text(fixture.title, fixture.body)
    cursor = connection.execute(
        """
        INSERT INTO source_items(
            source_id, external_item_id, canonical_url, title, raw_content, cleaned_text,
            content_sha256, source, published_at, observed_at, ingested_at, effective_at,
            valid_from, valid_to, source_version, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (
            fixture.source_id,
            fixture.key,
            f"https://example.test/{fixture.key}",
            fixture.title,
            fixture.body.encode("utf-8"),
            fixture.body,
            hashlib.sha256(canonical_text.encode("utf-8")).hexdigest(),
            fixture.source_id,
            _timestamp(observed_at),
            _timestamp(observed_at),
            _timestamp(observed_at),
            _timestamp(observed_at),
        ),
    )
    assert cursor.lastrowid is not None
    item_id = int(cursor.lastrowid)
    evidence_start = canonical_text.index(fixture.player_name)
    evidence = canonical_text[evidence_start:]
    payload: dict[str, object] = {
        "schema_version": "stage1-extraction-v1",
        "prompt_injection_detected": False,
        "claims": [
            {
                "player_refs": [{"name_raw": fixture.player_name}],
                "team_refs": list(fixture.team_refs),
                "claim_type": "usage",
                "claim_dimension": "role",
                "outcome_direction": fixture.outcome_direction,
                "roster_behavior_direction": fixture.roster_behavior_direction,
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
                        "source_item_id": item_id,
                        "extract_start": evidence_start,
                        "extract_end": evidence_start + len(evidence),
                        "verbatim_extract": evidence,
                    }
                ],
            }
        ],
    }
    report = run_extraction_batch(
        connection,
        window_start=observed_at - timedelta(seconds=1),
        window_end=observed_at + timedelta(seconds=1),
        provider=FixtureProvider(payload),
        pricing=load_batch_pricing(PRICING_PATH),
        run_at=observed_at + timedelta(minutes=1),
        clock=lambda: observed_at + timedelta(minutes=1),
    )
    assert report.claims_stored == 1


def _seed_source(
    connection: sqlite3.Connection,
    fixture: FixtureClaim,
    configured_at: datetime,
) -> None:
    timestamp = _timestamp(configured_at)
    if connection.execute(
        "SELECT 1 FROM source_keys WHERE source_id = ?", (fixture.source_id,)
    ).fetchone():
        return
    connection.execute(
        "INSERT INTO source_keys(source_id) VALUES (?)",
        (fixture.source_id,),
    )
    connection.execute(
        """
        INSERT INTO sources(
            source_id, display_name, source_family, collector_kind, feed_url, enabled,
            source, published_at, observed_at, ingested_at, effective_at, valid_from,
            valid_to, source_version, run_id
        ) VALUES (?, ?, ?, 'rss_atom', ?, 1, 'fixture', NULL, ?, ?, NULL, ?,
                  NULL, 'fixture-v1', NULL)
        """,
        (
            fixture.source_id,
            fixture.source_id,
            fixture.source_family,
            f"https://example.test/{fixture.source_id}.xml",
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    connection.execute(
        """
        INSERT INTO source_policies(
            source_id, permitted_use, raw_retention_days, personal_data_fields_allowed,
            must_honor_deletions, redistribution_allowed, third_party_processing_allowed,
            commercial_use_status, terms_reviewed_at, source, published_at, observed_at,
            ingested_at, effective_at, valid_from, valid_to, source_version, run_id
        ) VALUES (?, 'internal analysis', 30, '[]', 1, 0, 1, 'prohibited', ?,
                  'fixture', NULL, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (fixture.source_id, timestamp, timestamp, timestamp, timestamp),
    )


def _seed_player(connection: sqlite3.Connection) -> int:
    timestamp = _timestamp(BASE_TIME - timedelta(days=1))
    cursor = connection.execute(
        """
        INSERT INTO players(
            player_key, canonical_name, position, birth_date, source, published_at,
            observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES ('fixture-marcus-bell', 'Marcus Bell', 'WR', NULL, 'fixture', NULL,
                  ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (timestamp, timestamp, timestamp),
    )
    assert cursor.lastrowid is not None
    player_id = int(cursor.lastrowid)
    connection.execute(
        """
        INSERT INTO player_team_history(
            player_id, team, position, roster_status, season, week, source,
            published_at, observed_at, ingested_at, effective_at, valid_from,
            valid_to, source_version, run_id
        ) VALUES (?, 'CAR', 'WR', 'ACT', 2026, 1, 'fixture', NULL, ?, ?, NULL, ?,
                  NULL, 'fixture-v1', NULL)
        """,
        (player_id, timestamp, timestamp, timestamp),
    )
    return player_id


def _stored_graph(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    rows = connection.execute(
        """
        SELECT episode_id, claim_id, relation, similarity_score, linked_claim_id
        FROM episode_claims ORDER BY episode_id, claim_id
        """
    ).fetchall()
    return tuple(tuple(row) for row in rows)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


# --- Review fixes (2026-09-02) -------------------------------------------------------------


def test_identical_copy_is_derivative_whatever_its_directions_say(tmp_path: Path) -> None:
    # Half of the first live corpus carried unknown/neutral directions; a byte-identical
    # copy must still be a copy, and a copy never counts as a second event.
    database = tmp_path / "episodes.sqlite3"
    fixtures = {fixture.key: fixture for fixture in _load_fixtures()}
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed_player(connection)
        _seed_extracted_claim(
            connection,
            replace(
                fixtures["origin"],
                outcome_direction="unknown",
                roster_behavior_direction="unknown",
            ),
        )
        _seed_extracted_claim(
            connection,
            replace(
                fixtures["copy"],
                outcome_direction="neutral",
                roster_behavior_direction="increase",
            ),
        )
        as_of = BASE_TIME + timedelta(hours=2)
        build_episodes(connection, as_of=as_of, built_at=as_of + timedelta(hours=1))
        episode = connection.execute("SELECT * FROM narrative_episodes").fetchone()
        relations = [
            row["relation"]
            for row in connection.execute(
                "SELECT relation FROM episode_claims ORDER BY relation"
            )
        ]

    assert sorted(relations) == ["derivative", "origin"]
    assert episode["n_events"] == 1
    assert episode["unique_source_count"] == 2


def test_same_source_repost_is_not_a_second_event(tmp_path: Path) -> None:
    database = tmp_path / "episodes.sqlite3"
    fixtures = {fixture.key: fixture for fixture in _load_fixtures()}
    origin = fixtures["origin"]
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed_player(connection)
        _seed_extracted_claim(connection, origin)
        _seed_extracted_claim(
            connection,
            replace(
                fixtures["copy"],
                source_id=origin.source_id,
                source_family=origin.source_family,
                body=origin.body + " Update.",
            ),
        )
        as_of = BASE_TIME + timedelta(hours=2)
        build_episodes(connection, as_of=as_of, built_at=as_of + timedelta(hours=1))
        episode = connection.execute("SELECT * FROM narrative_episodes").fetchone()
        relations = sorted(
            row["relation"] for row in connection.execute("SELECT relation FROM episode_claims")
        )

    assert relations == ["derivative", "origin"]
    assert episode["n_events"] == 1
    assert episode["unique_source_count"] == 1


def test_build_reports_dropped_team_references_and_pins_the_prompt_version(
    tmp_path: Path,
) -> None:
    database = tmp_path / "episodes.sqlite3"
    fixtures = {fixture.key: fixture for fixture in _load_fixtures()}
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed_extracted_claim(
            connection,
            replace(
                fixtures["origin"],
                body="Marcus Bell will start in LA, no question, for CAR.",
                team_refs=("LA", "no"),
            ),
        )
        as_of = BASE_TIME + timedelta(hours=2)
        report = build_episodes(connection, as_of=as_of, built_at=as_of + timedelta(hours=1))
        other_prompt = build_episodes(
            connection,
            as_of=as_of,
            built_at=as_of + timedelta(hours=1),
            prompt_version_id="stage1-unrelated-fixture",
        )
        stored = connection.execute(
            "SELECT prompt_version_id, subject_type FROM narrative_episodes"
        ).fetchall()

    # "LA" is ambiguous and "no" is a stray word, never the Saints; both are reported.
    assert report.dropped_team_references == ("LA", "no")
    assert report.unclustered_claims == 1
    assert [tuple(row) for row in stored] == [(PROMPT_VERSION_ID, "unclustered")]
    # Claims extracted under another prompt version are a different snapshot, not a conflict.
    assert other_prompt.claims_considered == 0
    assert other_prompt.episode_count == 0
