from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from narrative_alpha.contest_cli import main as contest_main
from narrative_alpha.contests import (
    ContestEntryError,
    ManualContest,
    PayoutBand,
    add_contest,
    load_contest,
    load_contest_payouts,
)
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.store import apply_migrations, connect_database

OBSERVED_AT = datetime(2026, 9, 13, 12, tzinfo=UTC)


def test_payout_band_overlap_is_refused_before_any_write(tmp_path: Path) -> None:
    database = tmp_path / "contests.sqlite3"
    _seed_slate(database)

    with connect_database(database) as connection:
        with pytest.raises(ContestEntryError, match="payout bands overlap"):
            add_contest(
                connection,
                _contest(),
                (
                    PayoutBand(rank_from=1, rank_to=5, prize_cents=1_000),
                    PayoutBand(rank_from=5, rank_to=10, prize_cents=500),
                ),
                ingested_at=OBSERVED_AT,
            )
        assert connection.execute("SELECT count(*) FROM contests").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM contest_payouts").fetchone()[0] == 0


def test_prize_total_mismatch_is_refused_before_any_write(tmp_path: Path) -> None:
    database = tmp_path / "contests.sqlite3"
    _seed_slate(database)

    with connect_database(database) as connection:
        with pytest.raises(
            ContestEntryError,
            match="payout bands total 9000 cents, but total_prizes_cents is 10000 cents",
        ):
            add_contest(
                connection,
                _contest(total_prizes_cents=10_000),
                _payouts(),
                ingested_at=OBSERVED_AT,
            )
        assert connection.execute("SELECT count(*) FROM contests").fetchone()[0] == 0


def test_cli_add_and_typed_reload(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    database = tmp_path / "contests.sqlite3"
    payout_csv = tmp_path / "payouts.csv"
    _seed_slate(database)
    payout_csv.write_text(
        "rank_from,rank_to,prize_cents\n1,1,5000\n2,3,2000\n",
        encoding="utf-8",
    )

    exit_code = contest_main(
        [
            "add",
            "--database",
            str(database),
            "--external-contest-id",
            "dk-manual-1",
            "--site",
            "draftkings",
            "--slate-id",
            "1",
            "--archetype",
            "single_entry",
            "--field-size",
            "100",
            "--entry-limit",
            "1",
            "--entry-fee-cents",
            "100",
            "--total-prizes-cents",
            "9000",
            "--payout-curve-id",
            "dk-manual-1-payouts",
            "--payouts-csv",
            str(payout_csv),
            "--observed-at",
            "2026-09-13T12:00:00Z",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "contest_id": 1,
        "external_contest_id": "dk-manual-1",
        "payout_curve_id": "dk-manual-1-payouts",
        "payout_rows_added": 2,
        "site": "draftkings",
    }
    with connect_database(database) as connection:
        contest = load_contest(connection, contest_id=payload["contest_id"])
        payouts = load_contest_payouts(
            connection, payout_curve_id="dk-manual-1-payouts"
        )

    assert contest.external_contest_id == "dk-manual-1"
    assert contest.total_prizes_cents == 9_000
    assert [(row.rank_from, row.rank_to, row.prize_cents) for row in payouts] == [
        (1, 1, 5_000),
        (2, 3, 2_000),
    ]


def _contest(*, total_prizes_cents: int = 9_000) -> ManualContest:
    return ManualContest(
        external_contest_id="dk-manual-1",
        site="draftkings",
        slate_id=1,
        archetype="single_entry",
        field_size=100,
        entry_limit=1,
        entry_fee_cents=100,
        total_prizes_cents=total_prizes_cents,
        payout_curve_id="dk-manual-1-payouts",
        observed_at=OBSERVED_AT,
    )


def _payouts() -> tuple[PayoutBand, ...]:
    return (
        PayoutBand(rank_from=1, rank_to=1, prize_cents=5_000),
        PayoutBand(rank_from=2, rank_to=3, prize_cents=2_000),
    )


def _seed_slate(database: Path) -> None:
    timestamp = utc_timestamp(OBSERVED_AT)
    with connect_database(database) as connection:
        apply_migrations(connection)
        connection.execute(
            """
            INSERT INTO slates(
                slate_id, external_slate_id, site, slate_type, season, week, name,
                starts_at, locks_at, source, published_at, observed_at, ingested_at,
                effective_at, valid_from, valid_to, source_version, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "dk-main",
                "draftkings",
                "classic",
                2026,
                1,
                "Sunday Main",
                "2026-09-13T17:00:00.000000Z",
                "2026-09-13T17:00:00.000000Z",
                "fixture",
                None,
                timestamp,
                timestamp,
                None,
                timestamp,
                None,
                "fixture-v1",
                None,
            ),
        )


def test_payout_curve_can_be_reobserved_and_reader_returns_one_version(
    tmp_path: Path,
) -> None:
    database = tmp_path / "contests.sqlite3"
    _seed_slate(database)
    later = datetime(2026, 9, 13, 15, tzinfo=UTC)

    with connect_database(database) as connection:
        add_contest(connection, _contest(), _payouts(), ingested_at=OBSERVED_AT)
        # A filling contest re-observed later: a new version, not an overlap.
        add_contest(
            connection,
            _contest(total_prizes_cents=14_000).model_copy(update={"observed_at": later}),
            (
                PayoutBand(rank_from=1, rank_to=1, prize_cents=10_000),
                PayoutBand(rank_from=2, rank_to=3, prize_cents=2_000),
            ),
            ingested_at=later,
        )

        newest = load_contest_payouts(connection, payout_curve_id="dk-manual-1-payouts")
        earlier = load_contest_payouts(
            connection, payout_curve_id="dk-manual-1-payouts", as_of=OBSERVED_AT
        )

    assert [row.prize_cents for row in newest] == [10_000, 2_000]
    assert [row.prize_cents for row in earlier] == [5_000, 2_000]


def test_overlapping_band_within_one_observation_is_still_refused(tmp_path: Path) -> None:
    database = tmp_path / "contests.sqlite3"
    _seed_slate(database)

    with connect_database(database) as connection:
        add_contest(connection, _contest(), _payouts(), ingested_at=OBSERVED_AT)
        with pytest.raises(ContestEntryError, match="overlaps existing band"):
            add_contest(
                connection,
                _contest(total_prizes_cents=3_000),
                (PayoutBand(rank_from=3, rank_to=4, prize_cents=1_500),),
                ingested_at=OBSERVED_AT,
            )


def test_contest_rows_round_trip_through_typed_models(tmp_path: Path) -> None:
    database = tmp_path / "contests.sqlite3"
    _seed_slate(database)

    with connect_database(database) as connection:
        result = add_contest(connection, _contest(), _payouts(), ingested_at=OBSERVED_AT)
        reloaded = load_contest(connection, contest_id=result.contest.contest_id)
        payouts = load_contest_payouts(connection, payout_curve_id="dk-manual-1-payouts")

    assert reloaded == result.contest
    assert reloaded.observed_at == OBSERVED_AT
    assert reloaded.valid_to is None
    assert payouts == result.payouts


def test_unknown_payout_curve_reads_as_empty(tmp_path: Path) -> None:
    database = tmp_path / "contests.sqlite3"
    _seed_slate(database)

    with connect_database(database) as connection:
        assert load_contest_payouts(connection, payout_curve_id="missing") == ()
