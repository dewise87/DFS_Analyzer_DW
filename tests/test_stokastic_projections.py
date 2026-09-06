from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from narrative_alpha.ingest.projections import (
    SourceFormatError,
    load_projection_capture,
    render_projection_load,
)
from narrative_alpha.ingest.slates import load_salary_capture
from narrative_alpha.ingest.stokastic_projections import (
    parse_stokastic_ownership,
    parse_stokastic_projections,
)
from narrative_alpha.ingest.stokastic_stats import (
    StokasticSourceFormat,
    default_stokastic_registry,
)
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.slate_cli import main as slate_main
from narrative_alpha.snapshots import CaptureKind, capture_files
from narrative_alpha.store import apply_migrations, connect_database

GOLDEN_PATH = Path(__file__).with_name("golden")
OBSERVED = datetime(2026, 9, 12, 22, 0, tzinfo=UTC)


ROSTER = (
    ("Avery Archer", "GB", "QB"),
    ("Blake Bishop", "CHI", "QB"),
    ("Casey Crane", "GB", "RB"),
    ("Dana Drury", "CHI", "RB"),
    ("Emery Ellis", "GB", "WR"),
    ("Frankie Fox", "CHI", "WR"),
    ("Gale Grimm", "GB", "TE"),
    ("Harper Hale", "JAX", "WR"),
    ("Indigo Iles", "JAX", "QB"),
    ("Jules Jansen", "CLE", "RB"),
)


def _seed_players(connection: sqlite3.Connection) -> None:
    """The golden roster, so the crosswalk resolves instead of queueing every row."""

    stamp = utc_timestamp(OBSERVED - timedelta(days=7))
    for name, team, position in ROSTER:
        cursor = connection.execute(
            """
            INSERT INTO players(
                player_key, canonical_name, position, birth_date, source,
                published_at, observed_at, ingested_at, effective_at, valid_from,
                valid_to, source_version, run_id
            ) VALUES (?, ?, ?, NULL, 'fixture', NULL, ?, ?, NULL, ?, NULL,
                      'fixture-v1', NULL)
            """,
            (name.lower().replace(" ", "-"), name, position, stamp, stamp, stamp),
        )
        assert cursor.lastrowid is not None
        connection.execute(
            """
            INSERT INTO player_team_history(
                player_id, team, position, roster_status, season, week, source,
                published_at, observed_at, ingested_at, effective_at, valid_from,
                valid_to, source_version, run_id
            ) VALUES (?, ?, ?, 'ACT', 2026, 1, 'fixture', NULL, ?, ?, NULL, ?, NULL,
                      'fixture-v1', NULL)
            """,
            (int(cursor.lastrowid), team, position, stamp, stamp, stamp),
        )


def _seed_slate(connection: sqlite3.Connection, tmp_path: Path, *, site: str) -> int:
    """Build a real slate from the golden salary export the projections are priced on."""

    _seed_players(connection)
    golden = "dk_salaries_status.csv" if site == "dk" else "fd_salaries_classic_2026.csv"
    source = "draftkings" if site == "dk" else "fanduel"
    staged = tmp_path / "staged_salaries" / golden
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes((GOLDEN_PATH / golden).read_bytes())
    capture = capture_files(
        tmp_path / "snapshots",
        2026,
        1,
        CaptureKind.SALARIES,
        source,
        [staged],
        observed_at=OBSERVED - timedelta(hours=1),
    )
    report = load_salary_capture(
        connection,
        capture,
        season=2026,
        week=1,
        site=site,
        starts_at=None if site == "dk" else datetime(2026, 9, 13, 17, 0, tzinfo=UTC),
    )
    assert len(report.slates) == 1
    return report.slates[0].slate_id


def _capture_projections(
    tmp_path: Path,
    *goldens: str,
    kind: CaptureKind = CaptureKind.PROJECTIONS,
    observed_at: datetime = OBSERVED,
    text: dict[str, str] | None = None,
) -> Path:
    staged_directory = tmp_path / f"staged_{kind.value}"
    staged_directory.mkdir(parents=True, exist_ok=True)
    staged = []
    for golden in goldens:
        path = staged_directory / golden
        if text is not None and golden in text:
            path.write_text(text[golden], encoding="utf-8")
        else:
            path.write_bytes((GOLDEN_PATH / golden).read_bytes())
        staged.append(path)
    return capture_files(
        tmp_path / "snapshots",
        2026,
        1,
        kind,
        "stokastic",
        staged,
        observed_at=observed_at,
    )


# --- parsing -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("golden", "expected_site", "defense_position"),
    [
        ("stokastic_projections_dk.csv", "draftkings", "DST"),
        ("stokastic_projections_fd.csv", "fanduel", "D"),
    ],
)
def test_site_is_attributed_from_the_defense_token(
    golden: str, expected_site: str, defense_position: str
) -> None:
    result = parse_stokastic_projections(GOLDEN_PATH / golden)

    assert result.site == expected_site
    assert defense_position in (GOLDEN_PATH / golden).read_text(encoding="utf-8")
    # Whatever the vendor calls it, the parsed position is the canonical DST.
    assert {row.position for row in result.rows if row.name_raw == "Packers"} == {"DST"}


def test_a_file_with_no_defense_row_is_refused_by_name(tmp_path: Path) -> None:
    lines = (GOLDEN_PATH / "stokastic_projections_dk.csv").read_text(encoding="utf-8").splitlines()
    path = tmp_path / "no_defense.csv"
    path.write_text(
        "\n".join(line for line in lines if ",DST," not in line) + "\n", encoding="utf-8"
    )

    with pytest.raises(SourceFormatError) as error:
        parse_stokastic_projections(path)

    assert "no_defense.csv" in str(error.value)
    assert "no team-defense row" in str(error.value)


def test_mixed_defense_tokens_are_refused(tmp_path: Path) -> None:
    text = (GOLDEN_PATH / "stokastic_projections_dk.csv").read_text(encoding="utf-8")
    path = tmp_path / "mixed_defense.csv"
    path.write_text(text.replace("Jaguars,2900,DST", "Jaguars,2900,D"), encoding="utf-8")

    with pytest.raises(SourceFormatError, match="mixes team-defense position tokens"):
        parse_stokastic_projections(path)


def test_header_drift_names_the_missing_and_unexpected_columns(tmp_path: Path) -> None:
    lines = (GOLDEN_PATH / "stokastic_projections_dk.csv").read_text(encoding="utf-8").splitlines()
    path = tmp_path / "drifted.csv"
    path.write_text(
        "\n".join([lines[0].replace("Ownership %", "Ownership"), *lines[1:]]) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SourceFormatError) as error:
        parse_stokastic_projections(path)

    assert "drifted.csv" in str(error.value)
    assert "missing columns: Ownership %" in str(error.value)
    assert "unexpected columns: Ownership" in str(error.value)


def test_ownership_percent_becomes_a_fraction() -> None:
    result = parse_stokastic_projections(GOLDEN_PATH / "stokastic_projections_dk.csv")

    ownership = {row.name_raw: row.ownership_projection for row in result.rows}
    # The export writes 21.2 under a header that ends in "%", so the row is 0.212.
    assert ownership["Emery Ellis"] == pytest.approx(0.212)
    total = sum(row.ownership_projection or 0.0 for row in result.rows)
    assert total == pytest.approx(1.0)


def test_zero_projections_are_kept_and_counted() -> None:
    result = parse_stokastic_projections(GOLDEN_PATH / "stokastic_projections_dk.csv")

    zero_rows = [row for row in result.rows if row.projection_mean == 0]
    assert result.zero_projection_rows == len(zero_rows) == 2
    assert not result.rejected
    assert all(row.projection_floor is None and row.projection_ceiling is None for row in zero_rows)


def test_bounds_that_contradict_the_mean_are_dropped_and_counted() -> None:
    result = parse_stokastic_projections(GOLDEN_PATH / "stokastic_projections_fd.csv")

    assert result.range_dropped == ("Jaguars",)
    jaguars = next(row for row in result.rows if row.name_raw == "Jaguars")
    assert jaguars.projection_mean == pytest.approx(8.94)
    assert jaguars.projection_floor is None
    assert jaguars.projection_ceiling is None


def test_a_blank_or_non_numeric_projection_or_ownership_rejects_the_row(tmp_path: Path) -> None:
    lines = (GOLDEN_PATH / "stokastic_projections_dk.csv").read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace(",20.14,", ",,")
    lines[2] = lines[2].replace(",6.1,", ",n/a,")
    path = tmp_path / "bad_numbers.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = parse_stokastic_projections(path)

    assert result.rows_seen == 12
    assert len(result.rows) == 10
    assert [rejected.row_number for rejected in result.rejected] == [2, 3]
    assert result.rejected[0].reasons == ("Projection is blank",)
    assert "Ownership % is not numeric" in result.rejected[1].reasons[0]


def test_the_vendor_salary_is_carried_onto_the_parsed_row() -> None:
    result = parse_stokastic_projections(GOLDEN_PATH / "stokastic_projections_dk.csv")

    assert {row.name_raw: row.vendor_salary for row in result.rows}["Avery Archer"] == 7200


def test_ownership_reads_the_same_bytes_with_the_classic_role() -> None:
    result = parse_stokastic_ownership(GOLDEN_PATH / "stokastic_projections_fd.csv")

    assert result.site == "fanduel"
    assert {row.role for row in result.rows} == {"classic"}
    assert len(result.rows) == 12


def test_each_export_shape_refuses_the_other_by_name() -> None:
    source_format = StokasticSourceFormat()

    with pytest.raises(SourceFormatError, match=r"stokastic_stats_passing\.csv"):
        source_format.parse_projections(GOLDEN_PATH / "stokastic_stats_passing.csv")
    with pytest.raises(SourceFormatError, match=r"stokastic_projections_dk\.csv"):
        source_format.parse_stats(GOLDEN_PATH / "stokastic_projections_dk.csv")


# --- loading -----------------------------------------------------------------------


def test_the_loader_skips_the_other_sites_file_by_name(tmp_path: Path) -> None:
    capture = _capture_projections(
        tmp_path, "stokastic_projections_dk.csv", "stokastic_projections_fd.csv"
    )

    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        slate_id = _seed_slate(connection, tmp_path, site="dk")
        report = load_projection_capture(
            connection,
            capture,
            site="draftkings",
            slate_id=slate_id,
            registry=default_stokastic_registry(),
        )

    assert report.files_seen == 2
    assert [skipped.path for skipped in report.skipped_files] == [
        "projections/stokastic_projections_fd.csv"
    ]
    assert "fanduel" in report.skipped_files[0].reason
    assert report.rows_seen == 12
    assert report.projection_rows_inserted == 12
    assert report.zero_projection_rows == 2
    assert report.unresolved_rows == 0
    assert report.ok


def test_a_salary_that_moved_since_the_vendor_priced_it_is_reported_not_refused(
    tmp_path: Path,
) -> None:
    text = (GOLDEN_PATH / "stokastic_projections_dk.csv").read_text(encoding="utf-8")
    moved = text.replace("Avery Archer,7200", "Avery Archer,6900")
    capture = _capture_projections(
        tmp_path,
        "stokastic_projections_dk.csv",
        text={"stokastic_projections_dk.csv": moved},
    )

    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        slate_id = _seed_slate(connection, tmp_path, site="dk")
        report = load_projection_capture(
            connection,
            capture,
            site="draftkings",
            slate_id=slate_id,
            registry=default_stokastic_registry(),
        )

    assert report.salary_mismatches == 1
    assert report.salary_mismatch_names == ("Avery Archer: 6900 vs 7200",)
    assert report.projection_rows_inserted == 12
    assert report.ok
    assert "Avery Archer: 6900 vs 7200" in render_projection_load(report)


def test_reloading_the_same_capture_inserts_nothing_new(tmp_path: Path) -> None:
    projections = _capture_projections(tmp_path, "stokastic_projections_dk.csv")
    ownership = _capture_projections(
        tmp_path, "stokastic_projections_dk.csv", kind=CaptureKind.OWNERSHIP
    )

    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        slate_id = _seed_slate(connection, tmp_path, site="dk")
        registry = default_stokastic_registry()
        first = [
            load_projection_capture(
                connection, path, site="draftkings", slate_id=slate_id, registry=registry
            )
            for path in (projections, ownership)
        ]
        again = [
            load_projection_capture(
                connection, path, site="draftkings", slate_id=slate_id, registry=registry
            )
            for path in (projections, ownership)
        ]

    assert first[0].projection_rows_inserted == 12
    assert first[1].ownership_rows_inserted == 12
    inserted_again = [
        report.projection_rows_inserted + report.ownership_rows_inserted for report in again
    ]
    assert inserted_again == [0, 0]
    assert [report.duplicate_rows for report in again] == [12, 12]


def test_the_fanduel_export_loads_against_a_fanduel_slate(tmp_path: Path) -> None:
    capture = _capture_projections(
        tmp_path, "stokastic_projections_dk.csv", "stokastic_projections_fd.csv"
    )

    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        slate_id = _seed_slate(connection, tmp_path, site="fd")
        report = load_projection_capture(
            connection,
            capture,
            site="fanduel",
            slate_id=slate_id,
            registry=default_stokastic_registry(),
        )

    assert [skipped.path for skipped in report.skipped_files] == [
        "projections/stokastic_projections_dk.csv"
    ]
    # The vendor writes JAX where the FanDuel export writes JAC; the crosswalk's own
    # team handling resolves both onto one franchise, so nothing is queued.
    assert report.unresolved_rows == 0
    assert report.projection_rows_inserted == 12
    assert report.range_dropped == ("Jaguars",)
    assert "Jaguars" in render_projection_load(report)


def test_the_cli_loads_the_weeks_captures_and_prints_what_it_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _capture_projections(tmp_path, "stokastic_projections_dk.csv", "stokastic_projections_fd.csv")
    _capture_projections(
        tmp_path,
        "stokastic_projections_dk.csv",
        "stokastic_projections_fd.csv",
        kind=CaptureKind.OWNERSHIP,
        observed_at=OBSERVED + timedelta(minutes=1),
    )
    database = tmp_path / "store.sqlite3"
    with connect_database(database) as connection:
        apply_migrations(connection)
        slate_id = _seed_slate(connection, tmp_path, site="dk")

    exit_code = slate_main(
        [
            "load-projections",
            "--database",
            str(database),
            "--season",
            "2026",
            "--week",
            "1",
            "--site",
            "dk",
            "--slate-id",
            str(slate_id),
            "--root",
            str(tmp_path / "snapshots"),
        ]
    )

    printed = capsys.readouterr().out
    assert exit_code == 0
    assert "projections 12 inserted" in printed
    assert "ownership   12 inserted" in printed
    assert "zero means  2 row(s)" in printed
    assert "stokastic_projections_fd.csv — the adapter attributed it to fanduel" in printed
