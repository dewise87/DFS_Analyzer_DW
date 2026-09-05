from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from narrative_alpha.snapshots.cli import main
from narrative_alpha.snapshots.core import (
    capture_files,
    load_manifest,
    verify_week,
)
from narrative_alpha.snapshots.fetch import fetch_odds, fetch_weather
from narrative_alpha.snapshots.models import CaptureKind, SnapshotFile, SnapshotManifest
from narrative_alpha.snapshots.stadiums import (
    STADIUM_TABLE_VERSION,
    STADIUMS,
    RoofType,
    SurfaceType,
)


def test_manifest_round_trip(tmp_path: Path) -> None:
    observed_at = datetime(2026, 9, 5, 22, 0, tzinfo=UTC)
    manifest = SnapshotManifest(
        season=2026,
        week=1,
        captured_at=observed_at,
        files=(
            SnapshotFile(
                path="salaries/dk.csv",
                sha256="a" * 64,
                size_bytes=123,
                original_filename="dk.csv",
                observed_at=observed_at,
                source="draftkings",
                kind=CaptureKind.SALARIES,
            ),
        ),
    )

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

    restored = load_manifest(manifest_path)

    assert restored == manifest
    assert restored.schema_version == "1.1"


def test_verify_catches_corrupted_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    snapshot_root = tmp_path / "snapshots"
    source_file = tmp_path / "salaries.csv"
    source_file.write_text("name,salary\nQuarterback,6000\n", encoding="utf-8")
    capture_path = capture_files(
        snapshot_root,
        2026,
        1,
        CaptureKind.SALARIES,
        "draftkings",
        [source_file],
    )
    manifest = load_manifest(capture_path / "manifest.json")
    captured_file = capture_path / manifest.files[0].path
    captured_file.write_text("corrupted\n", encoding="utf-8")

    report = verify_week(snapshot_root, 2026, 1)
    exit_code = main(
        [
            "verify",
            "--season",
            "2026",
            "--week",
            "1",
            "--root",
            str(snapshot_root),
        ]
    )
    output = capsys.readouterr().out

    assert not report.ok
    assert any("sha256 mismatch" in problem for problem in report.problems)
    assert exit_code == 1
    assert "sha256 mismatch" in output


def test_capture_is_append_only_when_timestamps_collide(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    source_file = tmp_path / "projections.csv"
    source_file.write_text("player,projection\nA,20.5\n", encoding="utf-8")
    capture_time = datetime(2026, 9, 5, 22, 0, tzinfo=UTC)

    first_capture = capture_files(
        snapshot_root,
        2026,
        1,
        CaptureKind.PROJECTIONS,
        "vendor-a",
        [source_file],
        observed_at=capture_time,
    )
    first_manifest_bytes = (first_capture / "manifest.json").read_bytes()
    second_capture = capture_files(
        snapshot_root,
        2026,
        1,
        CaptureKind.PROJECTIONS,
        "vendor-a",
        [source_file],
        observed_at=capture_time,
    )

    assert first_capture != second_capture
    assert first_capture.is_dir()
    assert second_capture.is_dir()
    assert (first_capture / "manifest.json").read_bytes() == first_manifest_bytes
    assert len(list(first_capture.parent.iterdir())) == 2


def test_status_marks_a_missing_kind(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    snapshot_root = tmp_path / "snapshots"
    source_file = tmp_path / "salaries.csv"
    source_file.write_text("name,salary\nA,5000\n", encoding="utf-8")
    capture_files(
        snapshot_root,
        2026,
        1,
        CaptureKind.SALARIES,
        "draftkings",
        [source_file],
        observed_at=datetime(2026, 9, 5, 22, 0, tzinfo=UTC),
    )

    exit_code = main(["status", "--root", str(snapshot_root)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "2026 week_01" in output
    assert "salaries: 2026-09-05T22:00:00.000000Z" in output
    assert "ownership: MISSING" in output
    assert "stats: MISSING" in output


def test_odds_fetch_preserves_raw_body_quota_headers_and_redacts_key(
    tmp_path: Path,
) -> None:
    raw_body = b'{"raw": [1,  2]}\n'
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(
            200,
            content=raw_body,
            headers={
                "x-requests-remaining": "497",
                "x-requests-used": "3",
                "x-requests-last": "2",
                "server": "must-not-be-manifested",
            },
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = fetch_odds(
        tmp_path / "snapshots",
        2026,
        1,
        api_key="super-secret-key",
        client=client,
        observed_at=datetime(2026, 9, 10, 14, 0, tzinfo=UTC),
        sleep=lambda _: None,
    )
    manifest_bytes = (report.capture_path / "manifest.json").read_bytes()
    manifest = load_manifest(report.capture_path / "manifest.json")

    assert report.ok
    assert (report.capture_path / "odds" / "odds.json").read_bytes() == raw_body
    assert "super-secret-key" in seen_urls[0]
    assert b"super-secret-key" not in manifest_bytes
    assert "apiKey=REDACTED" in manifest.requests[0].url
    assert manifest.requests[0].response_headers == {
        "x-requests-remaining": "497",
        "x-requests-used": "3",
        "x-requests-last": "2",
    }
    assert "markets=spreads%2Ctotals" in manifest.requests[0].url


def test_fetch_retries_twice_before_success(tmp_path: Path) -> None:
    attempts = 0
    backoffs: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        status_code = 503 if attempts < 3 else 200
        return httpx.Response(status_code, content=b"[]", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = fetch_odds(
        tmp_path / "snapshots",
        2026,
        1,
        api_key="test-key",
        client=client,
        observed_at=datetime(2026, 9, 10, 14, 0, tzinfo=UTC),
        sleep=backoffs.append,
    )
    manifest = load_manifest(report.capture_path / "manifest.json")

    assert report.ok
    assert attempts == 3
    assert backoffs == [0.25, 0.5]
    assert manifest.requests[0].attempts == 3


def test_weather_partial_failure_writes_success_and_manifest_error(
    tmp_path: Path,
) -> None:
    games_csv = tmp_path / "games.csv"
    games_csv.write_text(
        "stadium,home_team,kickoff\n"
        "Lambeau Field,,2026-09-13T17:00:00Z\n"
        "Gillette Stadium,,2026-09-13T20:25:00Z\n"
        ",Detroit Lions,2026-09-13T17:00:00Z\n",
        encoding="utf-8",
    )
    calls_by_latitude: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        latitude = request.url.params["latitude"]
        calls_by_latitude[latitude] = calls_by_latitude.get(latitude, 0) + 1
        if latitude == "44.5013":
            return httpx.Response(200, content=b'{"forecast":"raw"}\n', request=request)
        return httpx.Response(503, content=b'{"reason":"down"}', request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = fetch_weather(
        tmp_path / "snapshots",
        2026,
        1,
        games_csv,
        client=client,
        observed_at=datetime(2026, 9, 10, 14, 23, tzinfo=UTC),
        sleep=lambda _: None,
    )
    manifest = load_manifest(report.capture_path / "manifest.json")

    assert not report.ok
    assert report.files_captured == 1
    assert len(manifest.files) == 1
    assert len(manifest.requests) == 1
    assert len(manifest.errors) == 1
    assert manifest.errors[0].attempts == 3
    assert "HTTP 503" in manifest.errors[0].message
    assert calls_by_latitude == {"44.5013": 1, "42.0909": 3}
    assert (report.capture_path / manifest.files[0].path).read_bytes() == b'{"forecast":"raw"}\n'
    weather_request = manifest.requests[0]
    assert weather_request.stadium == "Lambeau Field"
    assert weather_request.stadium_table_version == STADIUM_TABLE_VERSION
    assert weather_request.forecast_model_run_at == datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
    assert weather_request.forecast_lead_time_seconds == 298800


def test_missing_odds_key_still_writes_partial_capture(tmp_path: Path) -> None:
    report = fetch_odds(
        tmp_path / "snapshots",
        2026,
        1,
        api_key=None,
        observed_at=datetime(2026, 9, 10, 14, 0, tzinfo=UTC),
        sleep=lambda _: None,
    )
    manifest = load_manifest(report.capture_path / "manifest.json")

    assert not report.ok
    assert manifest.files == ()
    assert manifest.errors[0].error_type == "missing_api_key"
    assert "apiKey=REDACTED" in manifest.errors[0].request_url
    assert verify_week(tmp_path / "snapshots", 2026, 1).ok


def test_stadium_table_has_30_typed_unique_venues() -> None:
    assert len(STADIUMS) == 30
    assert len({stadium.name for stadium in STADIUMS}) == 30
    assert all(isinstance(stadium.roof, RoofType) for stadium in STADIUMS)
    assert all(isinstance(stadium.surface, SurfaceType) for stadium in STADIUMS)
    assert {team for stadium in STADIUMS for team in stadium.home_teams} == {
        "ARI",
        "ATL",
        "BAL",
        "BUF",
        "CAR",
        "CHI",
        "CIN",
        "CLE",
        "DAL",
        "DEN",
        "DET",
        "GB",
        "HOU",
        "IND",
        "JAX",
        "KC",
        "LV",
        "LAC",
        "LAR",
        "MIA",
        "MIN",
        "NE",
        "NO",
        "NYG",
        "NYJ",
        "PHI",
        "PIT",
        "SF",
        "SEA",
        "TB",
        "TEN",
        "WAS",
    }


def test_verify_catches_unmanifested_file(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    source_file = tmp_path / "salaries.csv"
    source_file.write_text("name,salary\nQuarterback,6000\n", encoding="utf-8")
    capture_path = capture_files(
        snapshot_root,
        2026,
        1,
        CaptureKind.SALARIES,
        "draftkings",
        [source_file],
    )
    (capture_path / "salaries" / "smuggled.csv").write_text("extra\n", encoding="utf-8")

    report = verify_week(snapshot_root, 2026, 1)

    assert not report.ok
    assert any("smuggled.csv" in problem for problem in report.problems)


def test_deterministic_4xx_is_not_retried(tmp_path: Path) -> None:
    attempts = 0
    backoffs: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, content=b"bad key", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = fetch_odds(
        tmp_path / "snapshots",
        2026,
        1,
        api_key="wrong-key",
        client=client,
        observed_at=datetime(2026, 9, 10, 14, 0, tzinfo=UTC),
        sleep=backoffs.append,
    )
    manifest = load_manifest(report.capture_path / "manifest.json")

    assert not report.ok
    assert attempts == 1
    assert backoffs == []
    assert manifest.errors[0].attempts == 1


def test_rate_limited_429_is_still_retried(tmp_path: Path) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        status_code = 429 if attempts < 2 else 200
        return httpx.Response(status_code, content=b"[]", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = fetch_odds(
        tmp_path / "snapshots",
        2026,
        1,
        api_key="test-key",
        client=client,
        observed_at=datetime(2026, 9, 10, 14, 0, tzinfo=UTC),
        sleep=lambda _: None,
    )

    assert report.ok
    assert attempts == 2


def test_weather_games_csv_with_zero_usable_rows_is_an_error(tmp_path: Path) -> None:
    games_csv = tmp_path / "games.csv"
    games_csv.write_text("home_team,kickoff\n", encoding="utf-8")

    report = fetch_weather(
        tmp_path / "snapshots",
        2026,
        1,
        games_csv,
        client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
        observed_at=datetime(2026, 9, 10, 14, 0, tzinfo=UTC),
        sleep=lambda _: None,
    )
    manifest = load_manifest(report.capture_path / "manifest.json")

    assert not report.ok
    assert manifest.errors[0].error_type == "no_games"


def test_games_csv_trailing_comma_does_not_crash_fetch(tmp_path: Path) -> None:
    games_csv = tmp_path / "games.csv"
    games_csv.write_text(
        "home_team,kickoff\nGB,2026-09-13T17:00:00Z,extra-cell\n",
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"forecast":"raw"}\n', request=request)

    report = fetch_weather(
        tmp_path / "snapshots",
        2026,
        1,
        games_csv,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        observed_at=datetime(2026, 9, 10, 14, 0, tzinfo=UTC),
        sleep=lambda _: None,
    )

    assert report.ok
    assert report.files_captured == 1
