from __future__ import annotations

import hashlib
import json
import shutil
import socket
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from narrative_alpha.identity.nflverse import PinnedRosterRelease, roster_archive_path
from narrative_alpha.identity.pins import pin_archive_path
from narrative_alpha.ingest.nflverse_stats import PinnedStatsRelease
from narrative_alpha.ops.backup import BackupError, create_backup, restore_backup
from narrative_alpha.ops.cli import main as ops_main
from narrative_alpha.ops.config import OpsConfig, load_ops_config
from narrative_alpha.ops.doctor import (
    CONFIG_FILENAMES,
    DoctorReport,
    collect_doctor,
    render_doctor,
)
from narrative_alpha.ops.schedule import build_jobs, install_schedule
from narrative_alpha.ops.status import collect_ops_status, render_status, status_payload
from narrative_alpha.store import DEFAULT_MIGRATIONS_PATH, apply_migrations, connect_database

NOW = datetime(2026, 9, 4, 14, 0, tzinfo=UTC)


@dataclass(frozen=True)
class DoctorFixture:
    root: Path
    config: OpsConfig
    artifacts: Path
    reports: Path
    snapshots: Path
    backups: Path
    home: Path
    executable: Path
    config_paths: dict[str, Path]
    roster_releases: dict[int, tuple[PinnedRosterRelease, ...]]
    stats_releases: dict[int, tuple[PinnedStatsRelease, ...]]

    def run(self, **overrides: object) -> DoctorReport:
        arguments: dict[str, object] = {
            "config": self.config,
            "database": self.config.database,
            "repository": self.root,
            "home": self.home,
            "artifact_directory": self.artifacts,
            "report_directory": self.reports,
            "snapshot_root": self.snapshots,
            "backup_directory": self.backups,
            "now": NOW,
            "config_paths": self.config_paths,
            "launchctl": lambda command: (0, ""),
            "secret_reader": lambda config: "fixture-secret",
            "roster_releases": self.roster_releases,
            "stats_releases": self.stats_releases,
            "na_ops_executable": self.executable,
        }
        arguments.update(overrides)
        return collect_doctor(**arguments)  # type: ignore[arg-type]


def _doctor_fixture(tmp_path: Path) -> DoctorFixture:
    root = tmp_path / "repo"
    config_directory = root / "config"
    shutil.copytree(Path("config"), config_directory)
    database = root / "data" / "db" / "fixture.sqlite3"
    snapshots = root / "data" / "snapshots"
    pin_archive = root / "data" / "archive" / "nflverse"
    logs = root / "data" / "logs"
    artifacts = root / "data" / "decisions"
    reports = root / "data" / "reports"
    backups = root / "data" / "backups"
    for directory in (snapshots, pin_archive, logs, artifacts, reports):
        directory.mkdir(parents=True, exist_ok=True)

    ops_path = config_directory / "ops.toml"
    ops_path.write_text(
        f"""
timezone = "America/New_York"
season = 2026
monthly_llm_budget_usd = "50.00"
keychain_service = "fixture-service"

[batch]
weekdays = ["wed", "thu", "fri"]
local_time = "09:30"
max_items_per_run = 20

[backup]
keep_newest = 14
local_time = "02:00"

[paths]
database = "{database}"
snapshot_root = "{snapshots}"
nflverse_archive = "{pin_archive}"
log_directory = "{logs}"
""".lstrip(),
        encoding="utf-8",
    )
    config = load_ops_config(ops_path)

    roster_bytes = b"fixture roster\n"
    weekly_bytes = b"fixture weekly stats\n"
    snaps_bytes = b"fixture snap counts\n"
    roster_hash = hashlib.sha256(roster_bytes).hexdigest()
    weekly_hash = hashlib.sha256(weekly_bytes).hexdigest()
    snaps_hash = hashlib.sha256(snaps_bytes).hexdigest()
    reviewed = NOW.date()
    roster_release = PinnedRosterRelease(2026, "https://example.test/roster", roster_hash, reviewed)
    stats_release = PinnedStatsRelease(
        season=2026,
        reviewed_at=reviewed,
        weekly_url="https://example.test/weekly",
        weekly_sha256=weekly_hash,
        snaps_url="https://example.test/snaps",
        snaps_sha256=snaps_hash,
    )
    for path, content in (
        (roster_archive_path(pin_archive, roster_hash), roster_bytes),
        (
            pin_archive_path(pin_archive, weekly_hash, label="nflverse weekly player stats"),
            weekly_bytes,
        ),
        (
            pin_archive_path(pin_archive, snaps_hash, label="nflverse snap counts"),
            snaps_bytes,
        ),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    stamp = "2026-09-04T12:00:00.000000Z"
    with connect_database(database) as connection:
        apply_migrations(connection)
        connection.execute(
            """
            INSERT INTO players(
                player_key, canonical_name, position, birth_date, source, published_at,
                observed_at, ingested_at, effective_at, valid_from, valid_to, source_version,
                run_id
            ) VALUES ('fixture', 'Fixture Player', 'QB', NULL, 'nflverse', NULL, ?, ?, NULL,
                      ?, NULL, ?, NULL)
            """,
            (stamp, stamp, stamp, f"fixture:sha256:{roster_hash}"),
        )

    (artifacts / "decision.txt").write_text("decision\n", encoding="utf-8")
    (reports / "report.txt").write_text("report\n", encoding="utf-8")
    create_backup(
        database=database,
        artifact_directory=artifacts,
        report_directory=reports,
        pin_archive=pin_archive,
        snapshot_root=snapshots,
        backup_directory=backups,
        now=NOW - timedelta(hours=1),
    )

    executable = root / ".venv" / "bin" / "na-ops"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    home = tmp_path / "home"
    install_schedule(
        build_jobs(config, home=home, repository=root, na_ops_executable=executable),
        launchctl=None,
    )
    return DoctorFixture(
        root=root,
        config=config,
        artifacts=artifacts,
        reports=reports,
        snapshots=snapshots,
        backups=backups,
        home=home,
        executable=executable,
        config_paths={name: config_directory / name for name in CONFIG_FILENAMES},
        roster_releases={2026: (roster_release,)},
        stats_releases={2026: (stats_release,)},
    )


def _check(report: DoctorReport, name: str):
    return next(check for check in report.checks if check.name == name)


def test_doctor_all_checks_have_a_healthy_fixture(tmp_path: Path) -> None:
    fixture = _doctor_fixture(tmp_path)
    secret = "fixture-secret-that-must-never-print"
    report = fixture.run(secret_reader=lambda config: secret)
    assert report.ok, [
        (check.name, check.detail) for check in report.checks if check.level == "FAIL"
    ]
    assert all(check.level == "OK" for check in report.checks)
    for filename in CONFIG_FILENAMES:
        check = _check(report, f"config {filename}")
        assert len(check.detail.split("sha256 ", 1)[1].split(" ", 1)[0]) == 64
    assert secret not in render_doctor(report)
    assert len(render_doctor(report).splitlines()) == len(report.checks) + 3


def test_status_shows_newest_backup_age(tmp_path: Path) -> None:
    fixture = _doctor_fixture(tmp_path)
    with connect_database(fixture.config.database) as connection:
        status = collect_ops_status(
            connection,
            config=fixture.config,
            database=fixture.config.database,
            now=NOW,
            backup_directory=fixture.backups,
            workload_stats_releases=fixture.stats_releases,
        )
    assert status.newest_backup is not None
    assert status.newest_backup.stamp == "20260904T130000Z"
    assert "20260904T130000Z (1h00m ago)" in render_status(status)
    assert status_payload(status)["newest_backup"]["age_seconds"] == 3600  # type: ignore[index]


def test_doctor_cli_reports_invalid_ops_config_and_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invalid = tmp_path / "ops.toml"
    invalid.write_text("not valid = [", encoding="utf-8")
    assert ops_main(["--config", str(invalid), "doctor"]) == 1
    output = capsys.readouterr().out
    assert "DOCTOR (read-only)" in output
    assert "FAIL  config ops.toml" in output


@pytest.mark.parametrize("filename", CONFIG_FILENAMES)
def test_doctor_refuses_each_unparseable_config(tmp_path: Path, filename: str) -> None:
    fixture = _doctor_fixture(tmp_path)
    broken = tmp_path / f"broken-{filename}"
    broken.write_text("this is not valid = [", encoding="utf-8")
    paths = dict(fixture.config_paths)
    paths[filename] = broken
    report = fixture.run(config_paths=paths)
    assert _check(report, f"config {filename}").level == "FAIL"


def test_doctor_failure_fixtures_name_their_remedies(tmp_path: Path) -> None:
    fixture = _doctor_fixture(tmp_path)

    missing_key = fixture.run(secret_reader=lambda config: None)
    key = _check(missing_key, "Keychain / Anthropic")
    assert key.level == "FAIL" and "security add-generic-password" in key.detail

    no_roster = fixture.run(roster_releases={})
    roster = _check(no_roster, "nflverse roster pin")
    assert roster.level == "FAIL" and "nflverse-refresh" in roster.detail

    no_stats = fixture.run(stats_releases={})
    stats = _check(no_stats, "nflverse stats pin")
    assert stats.level == "FAIL" and "nflverse-stats-refresh" in stats.detail

    no_backup = fixture.run(backup_directory=tmp_path / "no-backups")
    backup = _check(no_backup, "newest backup")
    assert backup.level == "FAIL" and "na-ops backup" in backup.detail

    no_agents = fixture.run(home=tmp_path / "empty-home", launchctl=lambda command: (3, "absent"))
    launchd = [check for check in no_agents.checks if check.name.startswith("launchd ")]
    assert launchd and all(check.level == "FAIL" for check in launchd)
    assert all("schedule install" in check.detail for check in launchd)


def test_schedule_contains_a_nightly_backup_wrapper(tmp_path: Path) -> None:
    fixture = _doctor_fixture(tmp_path)
    jobs = build_jobs(
        fixture.config,
        home=fixture.home,
        repository=fixture.root,
        na_ops_executable=fixture.executable,
    )
    backup = next(job for job in jobs if job.label.endswith(".backup"))
    assert backup.weekday_numbers == tuple(range(7))
    assert backup.local_time == fixture.config.backup_local_time
    assert "--config" in backup.script and " backup " in backup.script
    assert "security" not in backup.script


def test_doctor_pending_migration_and_expired_rules_are_failures(tmp_path: Path) -> None:
    fixture = _doctor_fixture(tmp_path)
    migrations = tmp_path / "migrations"
    shutil.copytree(DEFAULT_MIGRATIONS_PATH, migrations)
    (migrations / "0025_fixture_pending.sql").write_text(
        "CREATE TABLE fixture_pending(value TEXT) STRICT;\n", encoding="utf-8"
    )
    pending = fixture.run(migrations_path=migrations)
    migration = _check(pending, "database migrations")
    assert migration.level == "FAIL" and "0025_fixture_pending.sql" in migration.detail

    expired_path = tmp_path / "expired-rules.yaml"
    expired = fixture.config_paths["fast_lane_rules.yaml"].read_text(encoding="utf-8")
    expired_path.write_text(
        expired.replace("2026-09-30T23:59:59Z", "2026-09-03T23:59:59Z"),
        encoding="utf-8",
    )
    paths = dict(fixture.config_paths)
    paths["fast_lane_rules.yaml"] = expired_path
    expired_report = fixture.run(config_paths=paths)
    rules = _check(expired_report, "fast-lane signature")
    assert rules.level == "FAIL" and "re-sign" in rules.detail


def test_doctor_missing_directory_fails_and_occupied_port_warns(tmp_path: Path) -> None:
    fixture = _doctor_fixture(tmp_path)
    missing = fixture.run(artifact_directory=tmp_path / "missing-artifacts")
    assert _check(missing, "decision artifact directory").level == "FAIL"

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    try:
        port = int(listener.getsockname()[1])
        occupied = fixture.run(dashboard_port=port)
    finally:
        listener.close()
    # A listener is usually the dashboard itself: a warning, not a failed preflight.
    assert _check(occupied, "dashboard port").level == "WARN"
    assert occupied.ok


def _backup_fixture(tmp_path: Path) -> dict[str, Path]:
    database = tmp_path / "db" / "fixture.sqlite3"
    with connect_database(database) as connection:
        apply_migrations(connection)
        connection.execute("INSERT INTO source_keys(source_id) VALUES ('backup-fixture')")
    paths = {
        "database": database,
        "artifact_directory": tmp_path / "decisions",
        "report_directory": tmp_path / "reports",
        "pin_archive": tmp_path / "pins",
        "snapshot_root": tmp_path / "snapshots",
        "backup_directory": tmp_path / "backups",
    }
    for name, path in paths.items():
        if name not in {"database", "backup_directory"}:
            path.mkdir(parents=True)
    (paths["artifact_directory"] / "decision.bin").write_bytes(b"decision bytes")
    (paths["report_directory"] / "report.txt").write_text("report\n", encoding="utf-8")
    (paths["pin_archive"] / "pin.csv").write_bytes(b"pin bytes")
    (paths["snapshot_root"] / "capture.bin").write_bytes(b"large immutable capture")
    return paths


def test_backup_manifest_hashes_and_default_snapshot_exclusion(tmp_path: Path) -> None:
    paths = _backup_fixture(tmp_path)
    report = create_backup(**paths, now=NOW)
    manifest = json.loads(report.manifest_path.read_text(encoding="utf-8"))
    assert manifest["included_snapshots"] is False
    assert not any(record["path"].startswith("snapshots/") for record in manifest["files"])
    for record in manifest["files"]:
        payload = report.path / record["path"]
        assert payload.stat().st_size == record["size"]
        assert hashlib.sha256(payload.read_bytes()).hexdigest() == record["sha256"]
    assert manifest["database"]["row_counts"]["source_keys"] == 1


def test_backup_can_include_snapshot_captures_explicitly(tmp_path: Path) -> None:
    paths = _backup_fixture(tmp_path)
    report = create_backup(**paths, include_snapshots=True, now=NOW)
    manifest = json.loads(report.manifest_path.read_text(encoding="utf-8"))
    assert manifest["included_snapshots"] is True
    assert (report.path / "snapshots" / "capture.bin").read_bytes() == (b"large immutable capture")


def test_backup_pruning_keeps_newest_n(tmp_path: Path) -> None:
    paths = _backup_fixture(tmp_path)
    reports = [
        create_backup(**paths, keep_newest=2, now=NOW + timedelta(seconds=offset))
        for offset in range(3)
    ]
    assert sorted(path.name for path in paths["backup_directory"].iterdir()) == [
        reports[1].stamp,
        reports[2].stamp,
    ]
    assert reports[2].pruned == (reports[0].path,)


def test_restore_refuses_manifest_mismatch_without_creating_destination(tmp_path: Path) -> None:
    paths = _backup_fixture(tmp_path)
    report = create_backup(**paths, now=NOW)
    payload = report.path / "reports" / "report.txt"
    payload.write_text("tampered\n", encoding="utf-8")
    destination = tmp_path / "restored"
    with pytest.raises(BackupError, match="manifest mismatch"):
        restore_backup(
            backup=report.stamp,
            into=destination,
            backup_directory=paths["backup_directory"],
        )
    assert not destination.exists()


def test_restore_verifies_rows_and_printable_flags(tmp_path: Path) -> None:
    paths = _backup_fixture(tmp_path)
    report = create_backup(**paths, now=NOW)
    restored = restore_backup(
        backup=report.stamp,
        into=tmp_path / "restored copy",
        backup_directory=paths["backup_directory"],
    )
    assert restored.row_counts["source_keys"] == 1
    assert restored.database.is_file()
    assert restored.artifact_directory.joinpath("decision.bin").read_bytes() == b"decision bytes"
    assert "--database '" in restored.flags
    assert "--artifact-directory '" in restored.flags


def test_online_backup_captures_committed_wal_rows(tmp_path: Path) -> None:
    paths = _backup_fixture(tmp_path)
    writer = sqlite3.connect(paths["database"])
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("INSERT INTO source_keys(source_id) VALUES ('still-in-wal')")
        writer.commit()
        report = create_backup(**paths, now=NOW)
    finally:
        writer.close()
    restored = restore_backup(
        backup=report.stamp,
        into=tmp_path / "wal-restored",
        backup_directory=paths["backup_directory"],
    )
    with sqlite3.connect(restored.database) as connection:
        count = connection.execute("SELECT count(*) FROM source_keys").fetchone()[0]
    assert count == 2


def test_backup_and_restore_cli_round_trip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _backup_fixture(tmp_path)
    config_path = tmp_path / "ops.toml"
    config_path.write_text(
        f"""
timezone = "UTC"
season = 2026
monthly_llm_budget_usd = "1.00"
keychain_service = "fixture"

[batch]
weekdays = ["wed"]
local_time = "09:30"

[backup]
keep_newest = 14
local_time = "02:00"

[paths]
database = "{paths["database"]}"
snapshot_root = "{paths["snapshot_root"]}"
nflverse_archive = "{paths["pin_archive"]}"
log_directory = "{tmp_path / "logs"}"
""".lstrip(),
        encoding="utf-8",
    )
    common = ["--config", str(config_path)]
    assert (
        ops_main(
            [
                *common,
                "backup",
                "--artifact-directory",
                str(paths["artifact_directory"]),
                "--report-directory",
                str(paths["report_directory"]),
                "--backup-directory",
                str(paths["backup_directory"]),
            ]
        )
        == 0
    )
    backup_output = capsys.readouterr().out
    stamp = backup_output.splitlines()[0].split()[1]
    destination = tmp_path / "cli restored"
    assert (
        ops_main(
            [
                *common,
                "restore",
                "--backup",
                stamp,
                "--into",
                str(destination),
                "--backup-directory",
                str(paths["backup_directory"]),
            ]
        )
        == 0
    )
    restored_output = capsys.readouterr().out
    assert "files" in restored_output and "row counts verified" in restored_output
    assert f"--database '{destination}/store/fixture.sqlite3'" in restored_output
    assert f"--artifact-directory '{destination}/artifacts'" in restored_output
