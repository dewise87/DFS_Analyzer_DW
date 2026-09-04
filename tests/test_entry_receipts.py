from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from narrative_alpha.entries import build_entry_receipt_report, record_contest_entries
from narrative_alpha.ingest.results import (
    ContestArchetype,
    ContestMetadata,
    ContestStandingsError,
    load_contest_standings,
)
from narrative_alpha.ingest.salaries import SalarySite, SalarySlateType
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.portfolio import DfsSite, Lineup, OptimizationRequest, UploadEntry
from narrative_alpha.store import apply_migrations, connect_database

PRELOCK = datetime(2026, 9, 13, 16, tzinfo=UTC)
OBSERVED = datetime(2026, 9, 15, 12, tzinfo=UTC)


def test_frozen_entry_settles_at_payout_and_rerun_is_a_noop(tmp_path: Path) -> None:
    database = tmp_path / "receipts.sqlite3"
    standings = _standings(tmp_path, ledger_entry=True)
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed(connection, entry_id="mine", with_payout=True)
        first = load_contest_standings(connection, standings, _metadata())
        second = load_contest_standings(connection, standings, _metadata())
        row = connection.execute("SELECT * FROM contest_entry_results").fetchone()
        report = build_entry_receipt_report(connection, season=2026, week=1)

    assert first.entry_result_rows_inserted == 1
    assert second.entry_result_rows_inserted == 0
    assert second.entry_result_duplicate_rows == 1
    assert (row["settlement_status"], row["rank"], row["points"], row["payout_cents"]) == (
        "settled", 1, 25.5, 500
    )
    receipt = report.rows[0]
    assert (receipt.entries, receipt.fees_cents, receipt.winnings_cents, receipt.net_cents) == (
        1, 100, 500, 400
    )
    assert receipt.realized_roi == 4.0
    assert (receipt.best_rank, receipt.worst_rank, receipt.label_rows) == (1, 1, 1)


def test_frozen_upload_assignment_is_written_to_the_ledger(tmp_path: Path) -> None:
    database = tmp_path / "assignment.sqlite3"
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed(connection, entry_id="mine", with_payout=True, ledger=False)
        request = OptimizationRequest.model_construct(
            site=DfsSite.DRAFTKINGS,
            slate_id=1,
            upload_entries=(
                UploadEntry(
                    entry_id="mine",
                    contest_id="contest-1",
                    contest_name="Fixture",
                    entry_fee="$1.00",
                ),
            ),
        )
        lineup = Lineup.model_construct(lineup_id="d" * 64)
        inserted = record_contest_entries(
            connection,
            decision_snapshot_id="decision",
            decision_at=PRELOCK,
            request=request,
            lineups=(lineup,),
            source="slate_build",
        )
        row = connection.execute("SELECT * FROM contest_entries").fetchone()

    assert inserted == 1
    assert (row["entry_id"], row["entry_fee_cents"], row["lineup_id"]) == (
        "mine", 100, "d" * 64
    )


def test_a_refreeze_supersedes_the_base_assignment_for_the_entries_it_replaced(
    tmp_path: Path,
) -> None:
    """The fast lane records only the entries it rebuilt; the ledger's current assignment
    for those entries is the re-freeze, and the untouched entry stays with the base."""

    database = tmp_path / "refreeze.sqlite3"
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed(connection, entry_id="mine", with_payout=True, ledger=False)
        entries = (
            UploadEntry(
                entry_id="mine", contest_id="contest-1", contest_name="Fixture", entry_fee="$1.00"
            ),
            UploadEntry(
                entry_id="other", contest_id="contest-1", contest_name="Fixture", entry_fee="$1.00"
            ),
        )
        base_request = OptimizationRequest.model_construct(
            site=DfsSite.DRAFTKINGS, slate_id=1, upload_entries=entries
        )
        base_lineups = (
            Lineup.model_construct(lineup_id="a" * 64),
            Lineup.model_construct(lineup_id="b" * 64),
        )
        record_contest_entries(
            connection,
            decision_snapshot_id="decision",
            decision_at=PRELOCK,
            request=base_request,
            lineups=base_lineups,
            source="slate_build",
        )
        connection.execute(
            """
            INSERT INTO decision_snapshots(
                decision_snapshot_id, slate_id, decision_at, created_at,
                manifest_schema_version, manifest_hashes_json,
                manifest_hash_set_sha256, run_id, note
            ) VALUES ('decision-fast', 1, ?, ?, 'decision-v1', '[]', ?, NULL, 'refreeze')
            """,
            (
                utc_timestamp(PRELOCK + timedelta(minutes=30)),
                utc_timestamp(PRELOCK + timedelta(minutes=30)),
                "f" * 64,
            ),
        )
        # Pinned first, replacements after: the re-freeze rebuilt only the second entry.
        refreeze_request = OptimizationRequest.model_construct(
            site=DfsSite.DRAFTKINGS, slate_id=1, upload_entries=(entries[0], entries[1])
        )
        refreeze_lineups = (base_lineups[0], Lineup.model_construct(lineup_id="c" * 64))
        inserted = record_contest_entries(
            connection,
            decision_snapshot_id="decision-fast",
            decision_at=PRELOCK + timedelta(minutes=30),
            request=refreeze_request,
            lineups=refreeze_lineups,
            source="fast_refreeze",
            indexes=(1,),
        )
        rows = connection.execute(
            "SELECT entry_id, lineup_id, source FROM contest_entries ORDER BY contest_entry_id"
        ).fetchall()
        report = build_entry_receipt_report(connection, season=2026, week=1)

    assert inserted == 1
    assert [tuple(row) for row in rows] == [
        ("mine", "a" * 64, "slate_build"),
        ("other", "b" * 64, "slate_build"),
        ("other", "c" * 64, "fast_refreeze"),
    ]
    # Two entries, not three: the ledger counts the current assignment per entry id.
    assert report.rows[0].entries == 2


def test_ledger_entry_absent_from_export_is_unsettled(tmp_path: Path) -> None:
    database = tmp_path / "missing.sqlite3"
    standings = _standings(tmp_path, ledger_entry=False)
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed(connection, entry_id="missing", with_payout=True)
        report = load_contest_standings(connection, standings, _metadata())
        row = connection.execute("SELECT * FROM contest_entry_results").fetchone()

    assert report.unsettled_entries == 1
    assert row["settlement_status"] == "unsettled"
    assert row["rank"] is None and row["points"] is None and row["payout_cents"] is None
    assert "absent" in row["unsettled_reason"]


def test_standings_reports_same_name_entry_missing_from_ledger(tmp_path: Path) -> None:
    database = tmp_path / "outside.sqlite3"
    standings = _standings(tmp_path, ledger_entry=True)
    standings.write_text(
        standings.read_text(encoding="utf-8").replace("Field Name", "My Name"),
        encoding="utf-8",
    )
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed(connection, entry_id="mine", with_payout=True)
        report = load_contest_standings(connection, standings, _metadata())

    assert report.unledgered_entries == 1


def test_ledger_contest_without_payout_table_refuses(tmp_path: Path) -> None:
    database = tmp_path / "no-payout.sqlite3"
    standings = _standings(tmp_path, ledger_entry=True)
    with connect_database(database) as connection:
        apply_migrations(connection)
        _seed(connection, entry_id="mine", with_payout=False)
        with pytest.raises(ContestStandingsError, match=r"na-contest add"):
            load_contest_standings(connection, standings, _metadata(payout_curve_id=None))
        assert connection.execute("SELECT count(*) FROM contest_entry_results").fetchone()[0] == 0


def _standings(tmp_path: Path, *, ledger_entry: bool) -> Path:
    first_id = "mine" if ledger_entry else "someone-else"
    path = tmp_path / f"standings-{first_id}.csv"
    path.write_text(
        "Rank,EntryId,EntryName,TimeRemaining,Points,Lineup\n"
        f"1,{first_id},My Name,0,25.5,QB Example Passer\n"
        "2,field-entry,Field Name,0,10.0,QB Example Passer\n\n"
        "Player,Roster Position,%Drafted,FPTS\nExample Passer,QB,100%,25.5\n",
        encoding="utf-8",
    )
    return path


def _metadata(*, payout_curve_id: str | None = "curve-1") -> ContestMetadata:
    return ContestMetadata(
        contest_id="contest-1", site=SalarySite.DRAFTKINGS, slate_id=1,
        slate_type=SalarySlateType.CLASSIC,
        contest_archetype=ContestArchetype.SINGLE_ENTRY, entry_limit=1,
        entry_fee_cents=100, observed_at=OBSERVED, expected_field_size=2,
        payout_curve_id=payout_curve_id,
    )


def _seed(
    connection: sqlite3.Connection,
    *,
    entry_id: str,
    with_payout: bool,
    ledger: bool = True,
) -> None:
    stamp = utc_timestamp(PRELOCK)
    pit_columns = (
        "source,published_at,observed_at,ingested_at,effective_at,valid_from,valid_to,"
        "source_version,run_id"
    )
    pit = "'fixture',NULL,:at,:at,NULL,:at,NULL,'v1',NULL"
    connection.execute(
        f"INSERT INTO teams(team_id,team_key,abbreviation,canonical_name,league,{pit_columns}) "
        f"VALUES (1,'aaa','AAA','AAA','NFL',{pit})", {"at": stamp}
    )
    connection.execute(
        f"INSERT INTO teams(team_id,team_key,abbreviation,canonical_name,league,{pit_columns}) "
        f"VALUES (2,'bbb','BBB','BBB','NFL',{pit})", {"at": stamp}
    )
    connection.execute(
        f"INSERT INTO games(game_id,external_game_id,season,week,kickoff_at,home_team_id,"
        f"away_team_id,stadium_name,game_status,{pit_columns}) VALUES "
        f"(1,'game',2026,1,:at,1,2,'x','final',{pit})", {"at": stamp}
    )
    connection.execute(
        f"INSERT INTO slates(slate_id,external_slate_id,site,slate_type,season,week,name,"
        f"starts_at,locks_at,{pit_columns}) VALUES "
        f"(1,'slate','draftkings','classic',2026,1,'Main',:at,:at,{pit})", {"at": stamp}
    )
    connection.execute(
        f"INSERT INTO players(player_id,player_key,canonical_name,position,birth_date,"
        f"{pit_columns}) "
        f"VALUES (1,'p1','Example Passer','QB',NULL,{pit})", {"at": stamp}
    )
    connection.execute(
        f"INSERT INTO salaries(slate_id,player_id,game_id,team_id,opponent_team_id,"
        f"site_player_id,roster_positions_json,salary,player_status,source_file_sha256,"
        f"{pit_columns}) "
        f"VALUES (1,1,1,1,2,'1',:slots,5000,NULL,:hash,{pit})",
        {"at": stamp, "slots": json.dumps(["QB"]), "hash": "a" * 64},
    )
    cursor = connection.execute(
        f"INSERT INTO contests(external_contest_id,site,slate_id,archetype,field_size,"
        f"entry_limit,entry_fee_cents,total_prizes_cents,payout_curve_id,{pit_columns}) VALUES "
        f"('contest-1','draftkings',1,'single_entry',2,1,100,NULL,:curve,{pit})",
        {"at": stamp, "curve": "curve-1" if with_payout else None},
    )
    if with_payout:
        connection.execute(
            f"INSERT INTO contest_payouts(payout_curve_id,rank_from,rank_to,prize_cents,"
            f"{pit_columns}) VALUES ('curve-1',1,1,500,{pit})", {"at": stamp}
        )
    connection.execute(
        "INSERT INTO decision_snapshots(decision_snapshot_id,slate_id,decision_at,created_at,"
        "manifest_schema_version,manifest_hashes_json,manifest_hash_set_sha256,run_id,note) "
        "VALUES ('decision',1,?,?,'1','[]',?,NULL,NULL)", (stamp, stamp, "b" * 64)
    )
    if ledger:
        connection.execute(
            "INSERT INTO contest_entries(decision_snapshot_id,contest_id,entry_id,entry_fee_cents,"
            "lineup_id,recorded_at,source) VALUES ('decision',?,?,100,?,?,'slate_build')",
            (cursor.lastrowid, entry_id, "c" * 64, stamp),
        )
    connection.commit()
