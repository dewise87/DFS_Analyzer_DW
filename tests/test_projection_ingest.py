import csv
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from narrative_alpha.ingest import (
    OwnershipParseResult,
    ParsedOwnership,
    ParsedProjection,
    ProjectionIngestError,
    ProjectionParseResult,
    SourceFormatRegistry,
    load_projection_capture,
)
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.snapshots import (
    CaptureKind,
    SnapshotError,
    capture_files,
    capture_payloads,
    load_manifest,
)
from narrative_alpha.store import (
    OwnershipBaselineRow,
    ProjectionSnapshotRow,
    apply_migrations,
    connect_database,
)

OBSERVED = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)


class FixtureSourceFormat:
    name = "fixture-vendor"

    def parse_projections(self, path: Path) -> ProjectionParseResult:
        with path.open(newline="", encoding="utf-8") as source:
            rows = list(csv.DictReader(source))
        return ProjectionParseResult(
            rows_seen=len(rows),
            rows=tuple(
                ParsedProjection(
                    name_raw=row["name"],
                    team=row["team"],
                    position=row["position"],
                    external_player_id=row["player_id"] or None,
                    projection_mean=float(row["mean"]),
                    projection_floor=float(row["floor"]),
                    projection_ceiling=float(row["ceiling"]),
                    ownership_projection=float(row["ownership"]),
                    source_version="fixture-csv-v1",
                )
                for row in rows
            ),
        )

    def parse_ownership(self, path: Path) -> OwnershipParseResult:
        with path.open(newline="", encoding="utf-8") as source:
            rows = list(csv.DictReader(source))
        return OwnershipParseResult(
            rows_seen=len(rows),
            rows=tuple(
                ParsedOwnership(
                    name_raw=row["name"],
                    team=row["team"],
                    position=row["position"],
                    external_player_id=row["player_id"] or None,
                    role=row["role"],
                    ownership=float(row["ownership"]),
                    source_version="fixture-csv-v1",
                )
                for row in rows
            ),
        )


def _timestamp(value: datetime) -> str:
    return utc_timestamp(value)


def _seed_store(connection: sqlite3.Connection) -> int:
    base = _timestamp(OBSERVED - timedelta(days=1))
    cursor = connection.execute(
        """
        INSERT INTO players(
            player_key, canonical_name, position, birth_date, source,
            published_at, observed_at, ingested_at, effective_at, valid_from,
            valid_to, source_version, run_id
        ) VALUES ('known-player', 'Known Player', 'WR', NULL, 'fixture', NULL,
                  ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (base, base, base),
    )
    assert cursor.lastrowid is not None
    player_id = int(cursor.lastrowid)
    connection.execute(
        """
        INSERT INTO player_team_history(
            player_id, team, position, roster_status, season, week, source,
            published_at, observed_at, ingested_at, effective_at, valid_from,
            valid_to, source_version, run_id
        ) VALUES (?, 'GB', 'WR', 'ACT', 2026, 1, 'fixture', NULL, ?, ?, NULL,
                  ?, NULL, 'fixture-v1', NULL)
        """,
        (player_id, base, base, base),
    )
    connection.execute(
        """
        INSERT INTO slates(
            external_slate_id, site, slate_type, season, week, name,
            starts_at, locks_at, source, published_at, observed_at, ingested_at,
            effective_at, valid_from, valid_to, source_version, run_id
        ) VALUES ('main-1', 'draftkings', 'classic', 2026, 1, 'Main', ?, ?,
                  'fixture', NULL, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (
            _timestamp(OBSERVED + timedelta(days=1)),
            _timestamp(OBSERVED + timedelta(days=1)),
            base,
            base,
            base,
        ),
    )
    return player_id


def _registry() -> SourceFormatRegistry:
    registry = SourceFormatRegistry()
    registry.register(FixtureSourceFormat())
    return registry


def _write_projection(path: Path, *, mean: float, unknown: bool = False) -> None:
    name = "Unknown Person" if unknown else "Known Player"
    player_id = "unknown-9" if unknown else "known-1"
    path.write_text(
        "name,player_id,team,position,mean,floor,ceiling,ownership\n"
        f"{name},{player_id},GB,WR,{mean},5.0,25.0,0.12\n",
        encoding="utf-8",
    )


def test_source_format_registry_is_explicit_and_rejects_duplicates() -> None:
    registry = _registry()
    assert registry.names == ("fixture-vendor",)
    with pytest.raises(ProjectionIngestError, match="already registered"):
        registry.register(FixtureSourceFormat())
    with pytest.raises(ProjectionIngestError, match="no SourceFormat"):
        registry.get("invented-vendor")


def test_projection_load_is_idempotent_and_preserves_distinct_observations(
    tmp_path: Path,
) -> None:
    snapshots = tmp_path / "snapshots"
    source = tmp_path / "projection.csv"
    _write_projection(source, mean=14.5)
    first_capture = capture_files(
        snapshots,
        2026,
        1,
        CaptureKind.PROJECTIONS,
        "fixture-vendor",
        [source],
        observed_at=OBSERVED,
    )

    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        player_id = _seed_store(connection)
        first = load_projection_capture(
            connection,
            first_capture,
            site="draftkings",
            slate_id=1,
            registry=_registry(),
            ingested_at=OBSERVED + timedelta(minutes=5),
        )
        duplicate = load_projection_capture(
            connection,
            first_capture,
            site="draftkings",
            slate_id=1,
            registry=_registry(),
            ingested_at=OBSERVED + timedelta(minutes=6),
        )

        _write_projection(source, mean=16.0)
        second_capture = capture_files(
            snapshots,
            2026,
            1,
            CaptureKind.PROJECTIONS,
            "fixture-vendor",
            [source],
            observed_at=OBSERVED + timedelta(hours=2),
        )
        later = load_projection_capture(
            connection,
            second_capture,
            site="draftkings",
            slate_id=1,
            registry=_registry(),
            ingested_at=OBSERVED + timedelta(hours=2, minutes=5),
        )
        rows = tuple(
            ProjectionSnapshotRow.from_db(row)
            for row in connection.execute(
                "SELECT * FROM projection_snapshots ORDER BY observed_at"
            ).fetchall()
        )

    first_hash = load_manifest(first_capture / "manifest.json").files[0].sha256
    second_hash = load_manifest(second_capture / "manifest.json").files[0].sha256
    assert player_id == rows[0].player_id
    assert first.projection_rows_inserted == 1
    assert duplicate.projection_rows_inserted == 0
    assert duplicate.duplicate_rows == 1
    assert later.projection_rows_inserted == 1
    assert [row.projection_mean for row in rows] == [14.5, 16.0]
    assert [row.source_file_sha256 for row in rows] == [first_hash, second_hash]


def test_ownership_load_inserts_known_and_reports_unresolved_player(tmp_path: Path) -> None:
    source = tmp_path / "ownership.csv"
    source.write_text(
        "name,player_id,team,position,role,ownership\n"
        "Known Player,known-1,GB,WR,classic,0.18\n"
        "Mystery Player,mystery-2,GB,WR,classic,0.03\n",
        encoding="utf-8",
    )
    capture = capture_files(
        tmp_path / "snapshots",
        2026,
        1,
        CaptureKind.OWNERSHIP,
        "fixture-vendor",
        [source],
        observed_at=OBSERVED,
    )

    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        _seed_store(connection)
        report = load_projection_capture(
            connection,
            capture,
            site="draftkings",
            slate_id=1,
            registry=_registry(),
            ingested_at=OBSERVED + timedelta(minutes=2),
        )
        ownership = OwnershipBaselineRow.from_db(
            connection.execute("SELECT * FROM ownership_baselines").fetchone()
        )
        unresolved = connection.execute(
            "SELECT status, source_file_sha256 FROM unresolved_player_matches"
        ).fetchone()

    assert report.rows_seen == 2
    assert report.ownership_rows_inserted == 1
    assert report.unresolved_rows == 1
    assert ownership.ownership == 0.18
    assert tuple(unresolved) == ("pending", ownership.source_file_sha256)


def test_loader_rejects_file_changed_after_manifest(tmp_path: Path) -> None:
    source = tmp_path / "projection.csv"
    _write_projection(source, mean=14.5)
    capture = capture_files(
        tmp_path / "snapshots",
        2026,
        1,
        CaptureKind.PROJECTIONS,
        "fixture-vendor",
        [source],
        observed_at=OBSERVED,
    )
    captured_file = capture / "projections" / source.name
    captured_file.write_text("tampered\n", encoding="utf-8")

    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        _seed_store(connection)
        with pytest.raises(ProjectionIngestError, match="hash mismatch"):
            load_projection_capture(
                connection,
                capture,
                site="draftkings",
                slate_id=1,
                registry=_registry(),
            )


def test_partial_capture_errors_are_visible_in_load_report(tmp_path: Path) -> None:
    capture = capture_payloads(
        tmp_path / "snapshots",
        2026,
        1,
        CaptureKind.PROJECTIONS,
        (),
        errors=(
            SnapshotError(
                source="fixture-vendor",
                occurred_at=OBSERVED,
                attempts=3,
                error_type="timeout",
                message="source request exhausted retries",
            ),
        ),
        captured_at=OBSERVED,
    )

    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        _seed_store(connection)
        report = load_projection_capture(
            connection,
            capture,
            site="draftkings",
            slate_id=1,
            registry=_registry(),
        )

    assert report.files_seen == 0
    assert report.ok is False
    assert report.errors == (
        "capture error [timeout] fixture-vendor: source request exhausted retries",
    )


@pytest.mark.parametrize("bad_value", (float("nan"), float("inf"), float("-inf")))
def test_non_finite_projection_values_fail_validation_loudly(bad_value: float) -> None:
    with pytest.raises(ValidationError):
        ParsedProjection(name_raw="Known Player", team="GB", projection_mean=bad_value)
    with pytest.raises(ValidationError):
        ParsedProjection(
            name_raw="Known Player",
            team="GB",
            projection_mean=10.0,
            projection_ceiling=bad_value,
        )
    with pytest.raises(ValidationError):
        ParsedOwnership(name_raw="Known Player", team="GB", role="classic", ownership=bad_value)


def test_nan_projection_mean_is_a_loud_error_not_a_silent_duplicate(tmp_path: Path) -> None:
    source = tmp_path / "projection.csv"
    _write_projection(source, mean=float("nan"))
    capture = capture_files(
        tmp_path / "snapshots",
        2026,
        1,
        CaptureKind.PROJECTIONS,
        "fixture-vendor",
        [source],
        observed_at=OBSERVED,
    )

    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        _seed_store(connection)
        with pytest.raises(ValidationError):
            load_projection_capture(
                connection,
                capture,
                site="draftkings",
                slate_id=1,
                registry=_registry(),
                ingested_at=OBSERVED + timedelta(minutes=5),
            )
        stored = connection.execute("SELECT COUNT(*) FROM projection_snapshots").fetchone()

    assert tuple(stored) == (0,)


def test_same_key_different_content_is_a_load_error_not_a_duplicate(tmp_path: Path) -> None:
    source = tmp_path / "projection.csv"
    _write_projection(source, mean=14.5)
    first_capture = capture_files(
        tmp_path / "snapshots-a",
        2026,
        1,
        CaptureKind.PROJECTIONS,
        "fixture-vendor",
        [source],
        observed_at=OBSERVED,
    )
    _write_projection(source, mean=16.0)
    conflicting_capture = capture_files(
        tmp_path / "snapshots-b",
        2026,
        1,
        CaptureKind.PROJECTIONS,
        "fixture-vendor",
        [source],
        observed_at=OBSERVED,
    )

    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        _seed_store(connection)
        first = load_projection_capture(
            connection,
            first_capture,
            site="draftkings",
            slate_id=1,
            registry=_registry(),
            ingested_at=OBSERVED + timedelta(minutes=5),
        )
        conflicting = load_projection_capture(
            connection,
            conflicting_capture,
            site="draftkings",
            slate_id=1,
            registry=_registry(),
            ingested_at=OBSERVED + timedelta(minutes=6),
        )
        stored = connection.execute(
            "SELECT projection_mean FROM projection_snapshots"
        ).fetchall()

    assert first.ok
    assert conflicting.ok is False
    assert conflicting.projection_rows_inserted == 0
    assert conflicting.duplicate_rows == 0
    assert len(conflicting.errors) == 1
    assert "projection_snapshots key conflict" in conflicting.errors[0]
    assert "different content" in conflicting.errors[0]
    assert [row["projection_mean"] for row in stored] == [14.5]
