from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from narrative_alpha.ingest.salaries import (
    ParsedSalaryRow,
    SalaryCsvError,
    SalaryFormat,
    SalarySchemaError,
    SalarySite,
    SalarySlateType,
    parse_salary_csv,
)

GOLDEN_PATH = Path(__file__).with_name("golden")


@pytest.mark.parametrize(
    ("filename", "salary_format", "expected_site", "expected_slate_type", "expects_kickoff"),
    [
        (
            "draftkings_classic.csv",
            SalaryFormat.DRAFTKINGS_CLASSIC,
            SalarySite.DRAFTKINGS,
            SalarySlateType.CLASSIC,
            True,
        ),
        (
            "draftkings_showdown.csv",
            SalaryFormat.DRAFTKINGS_SHOWDOWN,
            SalarySite.DRAFTKINGS,
            SalarySlateType.SHOWDOWN,
            True,
        ),
        (
            "fanduel_classic.csv",
            SalaryFormat.FANDUEL_CLASSIC,
            SalarySite.FANDUEL,
            SalarySlateType.CLASSIC,
            False,
        ),
        (
            "fanduel_showdown.csv",
            SalaryFormat.FANDUEL_SHOWDOWN,
            SalarySite.FANDUEL,
            SalarySlateType.SHOWDOWN,
            False,
        ),
    ],
)
def test_golden_salary_exports(
    filename: str,
    salary_format: SalaryFormat,
    expected_site: SalarySite,
    expected_slate_type: SalarySlateType,
    expects_kickoff: bool,
) -> None:
    result = parse_salary_csv(
        GOLDEN_PATH / filename,
        slate_id="week-1-main",
        slate_name="Week 1 Fixture",
    )

    assert result.salary_format is salary_format
    assert result.parse_report.rows_seen == 3
    assert result.parse_report.rows_parsed == 3
    assert result.parse_report.rows_rejected == 0
    assert len(result.source_file_sha256) == 64
    assert {row.site for row in result.rows} == {expected_site}
    assert {row.slate_type for row in result.rows} == {expected_slate_type}
    assert {row.slate_id for row in result.rows} == {"week-1-main"}
    if expects_kickoff:
        assert all(row.game_time is not None and row.game_time.tzinfo == UTC for row in result.rows)
    else:
        # Real FanDuel player-list exports carry a bare AWAY@HOME Game value,
        # so kickoff-dependent fields stay None instead of erroring.
        assert all(row.game_time is None for row in result.rows)


@pytest.mark.parametrize(
    ("filename", "home_team"),
    [
        ("draftkings_classic.csv", "CHI"),
        ("draftkings_showdown.csv", "MIA"),
        ("fanduel_classic.csv", "NYG"),
        ("fanduel_showdown.csv", "LV"),
    ],
)
def test_game_field_direction_is_parsed_as_away_at_home(
    filename: str, home_team: str
) -> None:
    result = parse_salary_csv(GOLDEN_PATH / filename)

    assert all(row.is_home is not None for row in result.rows)
    assert all(row.is_home == (row.team == home_team) for row in result.rows)


def test_format_detection_ignores_filename(tmp_path: Path) -> None:
    misleading_path = tmp_path / "fanduel_showdown.csv"
    misleading_path.write_bytes((GOLDEN_PATH / "draftkings_classic.csv").read_bytes())

    result = parse_salary_csv(misleading_path)

    assert result.salary_format is SalaryFormat.DRAFTKINGS_CLASSIC


def test_header_drift_names_missing_and_unexpected_columns(tmp_path: Path) -> None:
    drifted = tmp_path / "drifted.csv"
    original = (GOLDEN_PATH / "draftkings_classic.csv").read_text(encoding="utf-8")
    drifted.write_text(original.replace("Salary", "Player Cost", 1), encoding="utf-8")

    with pytest.raises(SalarySchemaError) as raised:
        parse_salary_csv(drifted)

    assert raised.value.detected_near == "draftkings"
    assert raised.value.missing_columns == ("Salary",)
    assert raised.value.unexpected_columns == ("Player Cost",)
    assert "missing columns: Salary" in str(raised.value)
    assert "unexpected columns: Player Cost" in str(raised.value)


def test_bad_player_row_is_rejected_with_reason(tmp_path: Path) -> None:
    source = (GOLDEN_PATH / "draftkings_classic.csv").read_text(encoding="utf-8")
    bad_row = (
        "RB,Broken Runner (9999),Broken Runner,9999,RB/FLEX,-100,"
        "GB@CHI 09/13/2026 01:00PM ET,CHI,0.0\n"
    )
    csv_path = tmp_path / "one-bad-row.csv"
    csv_path.write_text(source + bad_row, encoding="utf-8")

    result = parse_salary_csv(csv_path)

    assert result.parse_report.rows_seen == 4
    assert result.parse_report.rows_parsed == 3
    assert result.parse_report.rows_rejected == 1
    rejected = result.parse_report.rejected[0]
    assert rejected.row_number == 5
    assert rejected.site_player_id == "9999"
    assert any("salary must be positive" in reason for reason in rejected.reasons)


def test_fanduel_old_style_game_with_kickoff_and_no_tier_still_parses(tmp_path: Path) -> None:
    csv_path = tmp_path / "fanduel_old_style.csv"
    csv_path.write_text(
        "Id,Position,First Name,Nickname,Last Name,FPPG,Played,Salary,Game,Team,"
        "Opponent,Injury Indicator,Injury Details\n"
        "3001,QB,Example,Example Thrower,Thrower,20.8,16,8100,"
        "DAL@NYG 09/13/2026 04:25PM ET,DAL,NYG,O,Out\n",
        encoding="utf-8",
    )

    result = parse_salary_csv(csv_path)

    assert result.salary_format is SalaryFormat.FANDUEL_CLASSIC
    assert result.parse_report.rows_rejected == 0
    row = result.rows[0]
    assert row.game_time == datetime(2026, 9, 13, 20, 25, tzinfo=UTC)
    assert row.opponent == "NYG"
    assert row.player_status == "O"


def test_fanduel_tier_column_is_not_reported_as_drift(tmp_path: Path) -> None:
    drifted = tmp_path / "fanduel_drifted.csv"
    original = (GOLDEN_PATH / "fanduel_classic.csv").read_text(encoding="utf-8")
    drifted.write_text(original.replace("Salary", "Player Cost", 1), encoding="utf-8")

    with pytest.raises(SalarySchemaError) as raised:
        parse_salary_csv(drifted)

    assert raised.value.detected_near == "fanduel_classic"
    assert raised.value.missing_columns == ("Salary",)
    assert raised.value.unexpected_columns == ("Player Cost",)
    assert "Tier" not in raised.value.missing_columns
    assert "Tier" not in raised.value.unexpected_columns


def test_draftkings_game_info_still_requires_kickoff(tmp_path: Path) -> None:
    source = (GOLDEN_PATH / "draftkings_classic.csv").read_text(encoding="utf-8")
    bad_row = "RB,Timeless Runner (9998),Timeless Runner,9998,RB/FLEX,4800,GB@CHI,CHI,9.9\n"
    csv_path = tmp_path / "dk_timeless_game.csv"
    csv_path.write_text(source + bad_row, encoding="utf-8")

    result = parse_salary_csv(csv_path)

    assert result.parse_report.rows_rejected == 1
    rejected = result.parse_report.rejected[0]
    assert rejected.site_player_id == "9998"
    assert any("MM/DD/YYYY" in reason for reason in rejected.reasons)


def test_headers_only_salary_file_is_a_structured_error(tmp_path: Path) -> None:
    headers_only = tmp_path / "empty.csv"
    original = (GOLDEN_PATH / "draftkings_classic.csv").read_text(encoding="utf-8")
    headers_only.write_text(original.splitlines()[0] + "\n", encoding="utf-8")

    with pytest.raises(SalaryCsvError, match="no data rows"):
        parse_salary_csv(headers_only)


VALID_CLASSIC_COMBINATIONS = (
    (SalarySite.DRAFTKINGS, "QB", ("QB",)),
    (SalarySite.DRAFTKINGS, "RB", ("RB", "FLEX")),
    (SalarySite.DRAFTKINGS, "WR", ("WR", "FLEX")),
    (SalarySite.DRAFTKINGS, "TE", ("TE", "FLEX")),
    (SalarySite.DRAFTKINGS, "DST", ("DST",)),
    (SalarySite.FANDUEL, "QB", ("QB",)),
    (SalarySite.FANDUEL, "RB", ("RB", "FLEX")),
    (SalarySite.FANDUEL, "WR", ("WR", "FLEX")),
    (SalarySite.FANDUEL, "TE", ("TE", "FLEX")),
    (SalarySite.FANDUEL, "D", ("D",)),
)


@given(
    combination=st.sampled_from(VALID_CLASSIC_COMBINATIONS),
    salary=st.integers(min_value=1, max_value=100_000),
)
def test_salary_and_position_invariants_accept_valid_classic_rows(
    combination: tuple[SalarySite, str, tuple[str, ...]], salary: int
) -> None:
    site, position, slots = combination

    row = _salary_row(site=site, position=position, slots=slots, salary=salary)

    assert row.salary == salary
    assert row.listed_position == position


@given(salary=st.integers(max_value=0))
def test_salary_invariant_rejects_nonpositive_values(salary: int) -> None:
    with pytest.raises(ValidationError):
        _salary_row(
            site=SalarySite.DRAFTKINGS,
            position="QB",
            slots=("QB",),
            salary=salary,
        )


@given(
    site=st.sampled_from((SalarySite.DRAFTKINGS, SalarySite.FANDUEL)),
    invalid_position=st.sampled_from(("K", "P", "CPT", "MVP")),
)
def test_position_invariant_rejects_unknown_classic_positions(
    site: SalarySite, invalid_position: str
) -> None:
    with pytest.raises(ValidationError):
        _salary_row(
            site=site,
            position=invalid_position,
            slots=(invalid_position,),
            salary=5000,
        )


def _salary_row(
    *, site: SalarySite, position: str, slots: tuple[str, ...], salary: int
) -> ParsedSalaryRow:
    return ParsedSalaryRow(
        site=site,
        slate_type=SalarySlateType.CLASSIC,
        slate_id="property-slate",
        slate_name="Property Test",
        site_player_id="fixture-id",
        name_raw="Fixture Player",
        team="AAA",
        opponent="BBB",
        listed_position=position,
        eligible_roster_slots=slots,
        salary=salary,
        game_time=datetime(2026, 9, 13, 17, 0, tzinfo=UTC),
    )


def test_draftkings_status_column_is_accepted_and_parsed() -> None:
    result = parse_salary_csv(GOLDEN_PATH / "dk_salaries_status.csv")

    assert result.salary_format is SalaryFormat.DRAFTKINGS_CLASSIC
    assert result.parse_report.rows_rejected == 0
    statuses = {row.name_raw: row.player_status for row in result.rows}
    assert statuses["Avery Archer"] is None
    assert statuses["Blake Bishop"] == "Q"
    assert statuses["Casey Crane"] == "OUT"
    assert statuses["Frankie Fox"] == "IR"


def test_draftkings_export_without_status_still_parses(tmp_path: Path) -> None:
    lines = (GOLDEN_PATH / "dk_salaries_status.csv").read_text(encoding="utf-8-sig").splitlines()
    trimmed = "\n".join(line.rsplit(",", 1)[0] for line in lines)
    path = tmp_path / "no_status.csv"
    path.write_text(trimmed + "\n", encoding="utf-8")

    result = parse_salary_csv(path)

    assert result.salary_format is SalaryFormat.DRAFTKINGS_CLASSIC
    assert all(row.player_status is None for row in result.rows)


def test_fanduel_empty_header_cells_are_dropped_and_the_slate_is_classic() -> None:
    result = parse_salary_csv(GOLDEN_PATH / "fd_salaries_classic_2026.csv")

    # `Roster Position` is present and carries FLEX, but no MVP: still a classic slate.
    assert result.salary_format is SalaryFormat.FANDUEL_CLASSIC
    assert result.parse_report.rows_rejected == 0
    archer = next(row for row in result.rows if row.name_raw == "Avery Archer")
    assert archer.eligible_roster_slots == ("QB",)
    catcher = next(row for row in result.rows if row.name_raw == "Emery Ellis")
    assert catcher.eligible_roster_slots == ("WR", "FLEX")


def test_fanduel_showdown_with_empty_header_cells_is_still_showdown(tmp_path: Path) -> None:
    lines = (GOLDEN_PATH / "fanduel_showdown.csv").read_text(encoding="utf-8").splitlines()
    header, *rows = lines
    widened = [header.replace("Tier,", "Tier,,,")]
    widened.extend(row.replace(",,,,MVP/FLEX", ",,,,,,MVP/FLEX") for row in rows)
    path = tmp_path / "fd_showdown_padded.csv"
    path.write_text("\n".join(widened) + "\n", encoding="utf-8")

    result = parse_salary_csv(path)

    assert result.salary_format is SalaryFormat.FANDUEL_SHOWDOWN
    assert result.parse_report.rows_rejected == 0


def test_a_duplicated_named_header_is_still_drift(tmp_path: Path) -> None:
    lines = (GOLDEN_PATH / "fd_salaries_classic_2026.csv").read_text(encoding="utf-8").splitlines()
    path = tmp_path / "duplicated.csv"
    path.write_text(
        "\n".join([lines[0].replace("Tier,,,", "Tier,Team,,"), *lines[1:]]) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SalarySchemaError) as error:
        parse_salary_csv(path)

    assert "duplicate:Team" in error.value.unexpected_columns


def test_data_under_an_unnamed_header_column_rejects_the_row(tmp_path: Path) -> None:
    lines = (GOLDEN_PATH / "fd_salaries_classic_2026.csv").read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace(",,,,,QB", ",,,smuggled,,QB")
    path = tmp_path / "unnamed_data.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = parse_salary_csv(path)

    assert result.parse_report.rows_rejected == 1
    assert "unnamed header column" in result.parse_report.rejected[0].reasons[0]
    assert "smuggled" in result.parse_report.rejected[0].reasons[0]
