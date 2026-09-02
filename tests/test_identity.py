import hashlib
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from narrative_alpha.identity import (
    PINNED_ROSTER_RELEASES,
    CrosswalkError,
    MatchMethod,
    NflverseRosterError,
    PinnedRosterRelease,
    PlayerCrosswalk,
    PlayerIdentityInput,
    RosterHashError,
    fetch_pinned_roster,
    normalize_name,
    pinned_roster_release,
    refresh_roster_release,
    roster_archive_path,
    seed_nflverse_roster,
)
from narrative_alpha.identity.cli import main as crosswalk_main
from narrative_alpha.identity.crosswalk import SUFFIX_MATCH_CONFIDENCE
from narrative_alpha.store import apply_migrations, connect_database

OBSERVED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _insert_player(
    connection: sqlite3.Connection,
    name: str,
    team: str,
    *,
    position: str = "WR",
    birth_date: date | None = None,
    valid_from: datetime = OBSERVED,
    valid_to: datetime | None = None,
) -> int:
    timestamp = _timestamp(valid_from)
    cursor = connection.execute(
        """
        INSERT INTO players(
            player_key, canonical_name, position, birth_date, source,
            published_at, observed_at, ingested_at, effective_at,
            valid_from, valid_to, source_version, run_id
        ) VALUES (?, ?, ?, ?, 'fixture', NULL, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (
            f"fixture-{name}-{team}-{connection.total_changes}",
            name,
            position,
            None if birth_date is None else birth_date.isoformat(),
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    assert cursor.lastrowid is not None
    player_id = int(cursor.lastrowid)
    connection.execute(
        """
        INSERT INTO player_team_history(
            player_id, team, position, roster_status, season, week, source,
            published_at, observed_at, ingested_at, effective_at, valid_from,
            valid_to, source_version, run_id
        ) VALUES (?, ?, ?, 'ACT', 2026, 1, 'fixture', NULL, ?, ?, NULL, ?, ?,
                  'fixture-v1', NULL)
        """,
        (
            player_id,
            team,
            position,
            timestamp,
            timestamp,
            timestamp,
            None if valid_to is None else _timestamp(valid_to),
        ),
    )
    return player_id


def _identity(
    name: str,
    team: str,
    *,
    external_id: str | None = None,
    position: str | None = "WR",
    birth_date: date | None = None,
    observed_at: datetime = OBSERVED + timedelta(hours=1),
) -> PlayerIdentityInput:
    return PlayerIdentityInput(
        source="test-vendor",
        site="draftkings",
        external_player_id=external_id,
        name_raw=name,
        team=team,
        position=position,
        birth_date=birth_date,
        observed_at=observed_at,
        source_file_sha256="a" * 64,
    )


def _insert_alias(
    connection: sqlite3.Connection,
    player_id: int,
    alias: str,
    *,
    team: str | None,
    valid_from: datetime = OBSERVED,
) -> None:
    timestamp = _timestamp(valid_from)
    connection.execute(
        """
        INSERT INTO player_aliases(
            player_id, team_id, team, alias, normalized_alias, match_method,
            match_confidence, manual_override, source, published_at,
            observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES (?, NULL, ?, ?, ?, 'manual', 1.0, 1, 'test-vendor', NULL,
                  ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
        """,
        (
            player_id,
            team,
            alias,
            normalize_name(alias),
            timestamp,
            timestamp,
            timestamp,
        ),
    )


@pytest.mark.parametrize(
    ("canonical", "vendor_name"),
    [
        ("John Smith Jr.", "John Smith"),
        ("D.J. Moore", "DJ Moore"),
        ("Amon-Ra St. Brown", "Amon Ra St Brown"),
        ("Ke'Shawn Vaughn", "Keshawn Vaughn"),
    ],
)
def test_deterministic_name_variants_resolve(
    tmp_path: Path, canonical: str, vendor_name: str
) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        player_id = _insert_player(connection, canonical, "CHI")
        result = PlayerCrosswalk(connection).match(_identity(vendor_name, "CHI"))

    assert result.player_id == player_id
    assert result.method in {
        MatchMethod.EXACT_NAME_TEAM,
        MatchMethod.DETERMINISTIC_ALIAS,
        MatchMethod.SUFFIX_TOLERANT_NAME,
    }


def test_nickname_uses_a_durable_source_alias(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        player_id = _insert_player(connection, "Marquise Brown", "KC")
        timestamp = _timestamp(OBSERVED)
        connection.execute(
            """
            INSERT INTO player_aliases(
                player_id, team_id, alias, normalized_alias, match_method,
                match_confidence, manual_override, source, published_at,
                observed_at, ingested_at, effective_at, valid_from, valid_to,
                source_version, run_id
            ) VALUES (?, NULL, 'Hollywood Brown', ?, 'manual', 1.0, 1,
                      'test-vendor', NULL, ?, ?, NULL, ?, NULL, 'fixture-v1', NULL)
            """,
            (player_id, normalize_name("Hollywood Brown"), timestamp, timestamp, timestamp),
        )
        result = PlayerCrosswalk(connection).match(_identity("Hollywood Brown", "KC"))

    assert result.player_id == player_id
    assert result.method is MatchMethod.DETERMINISTIC_ALIAS


def test_duplicate_names_are_gated_by_team_and_same_team_ambiguity_is_queued(
    tmp_path: Path,
) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        kc_player = _insert_player(connection, "Chris Johnson", "KC")
        _insert_player(connection, "Chris Johnson", "DET")
        _insert_player(connection, "Alex Smith", "ATL")
        _insert_player(connection, "Alex Smith", "ATL")
        crosswalk = PlayerCrosswalk(connection)

        resolved = crosswalk.match(_identity("Chris Johnson", "KC"))
        ambiguous = crosswalk.match(_identity("Alex Smith", "ATL"))

    assert resolved.player_id == kc_player
    assert ambiguous.player_id is None
    assert ambiguous.unresolved_id is not None
    assert len(ambiguous.candidates) == 2


def test_team_history_is_evaluated_as_of_the_source_observation(tmp_path: Path) -> None:
    trade_at = OBSERVED + timedelta(days=2)
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        player_id = _insert_player(
            connection,
            "Trade Player",
            "LAR",
            valid_to=trade_at,
        )
        timestamp = _timestamp(trade_at)
        connection.execute(
            """
            INSERT INTO player_team_history(
                player_id, team, position, roster_status, season, week, source,
                published_at, observed_at, ingested_at, effective_at, valid_from,
                valid_to, source_version, run_id
            ) VALUES (?, 'BUF', 'WR', 'ACT', 2026, 2, 'fixture', NULL, ?, ?, NULL,
                      ?, NULL, 'fixture-v2', NULL)
            """,
            (player_id, timestamp, timestamp, timestamp),
        )
        crosswalk = PlayerCrosswalk(connection)
        before = crosswalk.match(
            _identity("Trade Player", "LAR", observed_at=trade_at - timedelta(hours=1))
        )
        after = crosswalk.match(
            _identity("Trade Player", "BUF", observed_at=trade_at + timedelta(hours=1))
        )
        stale_team = crosswalk.match(
            _identity("Trade Player", "LAR", observed_at=trade_at + timedelta(hours=1))
        )

    assert before.player_id == player_id
    assert after.player_id == player_id
    assert stale_team.player_id is None


def test_fuzzy_matching_is_gated_and_never_silently_accepts_low_confidence(
    tmp_path: Path,
) -> None:
    birth_date = date(1999, 1, 19)
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        player_id = _insert_player(
            connection,
            "Jonathan Taylor",
            "IND",
            position="RB",
            birth_date=birth_date,
        )
        crosswalk = PlayerCrosswalk(connection)
        close = crosswalk.match(
            _identity(
                "Jonathn Taylor",
                "IND",
                position="RB",
                birth_date=birth_date,
                external_id="close",
            )
        )
        wrong_dob = crosswalk.match(
            _identity(
                "Jonathen Taylor",
                "IND",
                position="RB",
                birth_date=date(2000, 1, 1),
                external_id="wrong-dob",
            )
        )
        low = crosswalk.match(
            _identity("Entirely Different", "IND", position="RB", external_id="low")
        )
        queue = crosswalk.list_unresolved()
        with pytest.raises(CrosswalkError, match="lineup generation must stop"):
            crosswalk.require_all_resolved(site="draftkings")

    assert close.player_id == player_id
    assert close.method is MatchMethod.FUZZY
    assert wrong_dob.player_id is None
    assert low.player_id is None
    assert {row.unresolved_id for row in queue} == {wrong_dob.unresolved_id, low.unresolved_id}


def test_manual_resolution_persists_alias_external_id_and_audit_fields(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        player_id = _insert_player(connection, "William Fuller", "MIA")
        crosswalk = PlayerCrosswalk(connection)
        unresolved = crosswalk.match(_identity("Will Fuller", "MIA", external_id="vendor-42"))
        assert unresolved.unresolved_id is not None

        manual = crosswalk.resolve(
            unresolved.unresolved_id,
            player_id,
            note="known nickname",
            resolved_at=OBSERVED + timedelta(days=1),
        )
        replay = crosswalk.match(_identity("Changed Display Name", "NYJ", external_id="vendor-42"))
        stored = connection.execute(
            """
            SELECT match_method, match_confidence, manual_override
            FROM external_player_ids WHERE external_player_id = 'vendor-42'
            """
        ).fetchone()

    assert manual.method is MatchMethod.MANUAL
    assert replay.player_id == player_id
    assert replay.method is MatchMethod.EXACT_VENDOR_ID
    assert replay.manual_override is True
    assert tuple(stored) == ("manual", 1.0, 1)


def test_manual_alias_applies_when_reingesting_the_original_older_capture(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        player_id = _insert_player(connection, "William Fuller", "MIA")
        crosswalk = PlayerCrosswalk(connection)
        original = _identity("Billy Fuller", "MIA")
        unresolved = crosswalk.match(original)
        assert unresolved.unresolved_id is not None

        crosswalk.resolve(
            unresolved.unresolved_id,
            player_id,
            resolved_at=OBSERVED + timedelta(days=1),
        )
        replay = crosswalk.match(original)

    assert replay.player_id == player_id
    assert replay.method is MatchMethod.DETERMINISTIC_ALIAS
    assert replay.manual_override is True


def test_requeued_identity_reopens_resolved_status_and_fails_closed(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        player_id = _insert_player(connection, "William Fuller", "MIA")
        crosswalk = PlayerCrosswalk(connection)
        unresolved = crosswalk.match(_identity("Billy Fuller", "MIA"))
        assert unresolved.unresolved_id is not None

        crosswalk.resolve(
            unresolved.unresolved_id, player_id, resolved_at=OBSERVED + timedelta(days=1)
        )
        crosswalk.require_all_resolved()
        connection.execute(
            "UPDATE player_aliases SET valid_to = ? WHERE normalized_alias = ?",
            (_timestamp(OBSERVED + timedelta(days=2)), normalize_name("Billy Fuller")),
        )
        requeued = crosswalk.match(
            _identity("Billy Fuller", "MIA", observed_at=OBSERVED + timedelta(days=3))
        )
        with pytest.raises(CrosswalkError, match="lineup generation must stop"):
            crosswalk.require_all_resolved()

    assert requeued.player_id is None
    assert requeued.unresolved_id == unresolved.unresolved_id


def test_manual_resolution_is_team_scoped_and_preserves_other_teams_aliases(
    tmp_path: Path,
) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        kc_player = _insert_player(connection, "Marquise Brown", "KC")
        det_player = _insert_player(connection, "Devon Brown", "DET")
        crosswalk = PlayerCrosswalk(connection)
        kc_queue = crosswalk.match(_identity("Hollywood Brown", "KC"))
        det_queue = crosswalk.match(_identity("Hollywood Brown", "DET"))
        assert kc_queue.unresolved_id is not None
        assert det_queue.unresolved_id is not None

        crosswalk.resolve(
            kc_queue.unresolved_id, kc_player, resolved_at=OBSERVED + timedelta(days=1)
        )
        crosswalk.resolve(
            det_queue.unresolved_id, det_player, resolved_at=OBSERVED + timedelta(days=2)
        )
        active = int(
            connection.execute(
                """
                SELECT count(*) FROM player_aliases
                WHERE normalized_alias = ? AND valid_to IS NULL
                """,
                (normalize_name("Hollywood Brown"),),
            ).fetchone()[0]
        )
        kc_match = crosswalk.match(_identity("Hollywood Brown", "KC"))
        det_match = crosswalk.match(_identity("Hollywood Brown", "DET"))

    assert active == 2
    assert kc_match.player_id == kc_player
    assert kc_match.method is MatchMethod.DETERMINISTIC_ALIAS
    assert det_match.player_id == det_player
    assert det_match.method is MatchMethod.DETERMINISTIC_ALIAS


def test_manual_resolution_overrides_conflicting_vendor_id_mapping(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        player_a = _insert_player(connection, "Alpha Runner", "KC", position="RB")
        player_b = _insert_player(connection, "Beta Runner", "KC", position="RB")
        crosswalk = PlayerCrosswalk(connection)
        queued = crosswalk.match(
            _identity("Mystery Man", "KC", position="RB", external_id="shared-1")
        )
        assert queued.unresolved_id is not None
        auto = crosswalk.match(
            _identity(
                "Alpha Runner",
                "KC",
                position="RB",
                external_id="shared-1",
                observed_at=OBSERVED + timedelta(hours=2),
            )
        )
        manual = crosswalk.resolve(
            queued.unresolved_id, player_b, resolved_at=OBSERVED + timedelta(days=1)
        )
        replay = crosswalk.match(
            _identity(
                "Renamed Vendor Row",
                "KC",
                position="RB",
                external_id="shared-1",
                observed_at=OBSERVED + timedelta(days=2),
            )
        )

    assert auto.player_id == player_a
    assert manual.player_id == player_b
    assert replay.player_id == player_b
    assert replay.method is MatchMethod.EXACT_VENDOR_ID
    assert replay.manual_override is True


def test_different_suffixes_never_match(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        _insert_player(connection, "Kenneth Walker III", "SEA", position="RB")
        result = PlayerCrosswalk(connection).match(
            _identity("Kenneth Walker Jr.", "SEA", position="QB")
        )

    assert result.player_id is None
    assert result.unresolved_id is not None


def test_suffix_stage_requires_position_agreement(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        _insert_player(connection, "Kenneth Walker", "SEA", position="RB")
        result = PlayerCrosswalk(connection).match(
            _identity("Kenneth Walker Jr.", "SEA", position="QB")
        )

    assert result.player_id is None
    assert result.unresolved_id is not None


def test_missing_suffix_matches_with_an_honest_method_and_confidence(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        player_id = _insert_player(connection, "Odell Beckham Jr.", "BAL")
        result = PlayerCrosswalk(connection).match(_identity("Odell Beckham", "BAL"))

    assert result.player_id == player_id
    assert result.method is MatchMethod.SUFFIX_TOLERANT_NAME
    assert result.confidence == SUFFIX_MATCH_CONFIDENCE
    assert result.confidence is not None and result.confidence < 1.0


def test_fuzzy_requires_the_input_to_carry_a_position(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        _insert_player(connection, "Jonathan Taylor", "IND", position="RB")
        crosswalk = PlayerCrosswalk(connection)
        result = crosswalk.match(_identity("Jonathn Taylor", "IND", position=None))
        with pytest.raises(CrosswalkError, match="lineup generation must stop"):
            crosswalk.require_all_resolved()

    assert result.player_id is None
    assert result.unresolved_id is not None


def test_fuzzy_rejects_same_team_position_disagreement(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        _insert_player(connection, "Marquez Valdes-Scantling", "BUF", position="WR")
        result = PlayerCrosswalk(connection).match(
            _identity("Marquise Valdes-Scantling", "BUF", position="TE")
        )

    assert result.player_id is None
    assert result.unresolved_id is not None


def test_team_code_variants_normalize_across_sources(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        nflverse_rams = _insert_player(connection, "Puka Nacua", "LA")
        site_rams = _insert_player(connection, "Cooper Kupp", "LAR")
        nflverse_jax = _insert_player(connection, "Trevor Lawrence", "JAX", position="QB")
        crosswalk = PlayerCrosswalk(connection)
        dk_rams = crosswalk.match(_identity("Puka Nacua", "LAR"))
        nflverse_input = crosswalk.match(_identity("Cooper Kupp", "LA"))
        fd_jax = crosswalk.match(_identity("Trevor Lawrence", "JAC", position="QB"))

    assert dk_rams.player_id == nflverse_rams
    assert nflverse_input.player_id == site_rams
    assert fd_jax.player_id == nflverse_jax


def test_active_alias_identity_is_unique_regardless_of_player(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        player_a = _insert_player(connection, "Player One", "KC")
        player_b = _insert_player(connection, "Player Two", "KC")
        _insert_alias(connection, player_a, "Shared Label", team="KC")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_alias(connection, player_b, "Shared Label", team="KC")


def test_accept_refuses_to_shadow_a_conflicting_active_alias(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        _insert_player(connection, "John Doe", "CHI")
        player_b = _insert_player(connection, "Jonathan Doerr", "CHI")
        _insert_alias(connection, player_b, "John Doe", team="CHI")

        with pytest.raises(CrosswalkError, match="active alias"):
            PlayerCrosswalk(connection).match(_identity("John Doe", "CHI"))


def test_fuzzy_accept_confidence_is_not_laundered_into_a_certain_alias(tmp_path: Path) -> None:
    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        _insert_player(connection, "Jonathan Taylor", "IND", position="RB")
        crosswalk = PlayerCrosswalk(connection)
        first = crosswalk.match(_identity("Jonathn Taylor", "IND", position="RB"))
        again = crosswalk.match(
            _identity(
                "Jonathn Taylor",
                "IND",
                position="RB",
                observed_at=OBSERVED + timedelta(days=1),
            )
        )
        persisted = int(
            connection.execute(
                "SELECT count(*) FROM player_aliases WHERE normalized_alias = ?",
                (normalize_name("Jonathn Taylor"),),
            ).fetchone()[0]
        )

    assert first.method is MatchMethod.FUZZY
    assert first.confidence is not None and first.confidence < 1.0
    assert again.method is MatchMethod.FUZZY
    assert again.confidence == first.confidence
    assert persisted == 0


def test_crosswalk_resolve_cli_lists_and_accepts_a_decision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "store.sqlite3"
    with connect_database(database) as connection:
        apply_migrations(connection)
        player_id = _insert_player(connection, "William Fuller", "MIA")
        unresolved = PlayerCrosswalk(connection).match(
            _identity("Will Fuller", "MIA", external_id="cli-vendor-42")
        )
    assert unresolved.unresolved_id is not None

    list_code = crosswalk_main(["--database", str(database), "resolve"])
    listed = capsys.readouterr().out
    resolve_code = crosswalk_main(
        [
            "--database",
            str(database),
            "resolve",
            "--unresolved-id",
            str(unresolved.unresolved_id),
            "--player-id",
            str(player_id),
            "--note",
            "confirmed",
        ]
    )

    with connect_database(database) as connection:
        status = connection.execute(
            "SELECT status, resolved_player_id FROM unresolved_player_matches"
        ).fetchone()

    assert list_code == 0
    assert "Will Fuller" in listed
    assert resolve_code == 0
    assert tuple(status) == ("resolved", player_id)


def test_pinned_roster_fetch_retries_hash_checks_and_caches(tmp_path: Path) -> None:
    content = (
        b"season,team,position,status,full_name,birth_date,gsis_id,week\n"
        b"2026,GB,WR,ACT,Example Player,2000-01-01,00-001,1\n"
    )
    release = PinnedRosterRelease(
        season=2026,
        url="https://example.test/roster.csv",
        sha256=hashlib.sha256(content).hexdigest(),
        reviewed_at=date(2026, 9, 1),
    )
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(200, content=content, request=request)

    sleeps: list[float] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        cached = fetch_pinned_roster(
            release, tmp_path / "cache", client=client, sleep=sleeps.append
        )
        again = fetch_pinned_roster(release, tmp_path / "cache", client=client)

    assert cached == again
    assert cached == roster_archive_path(tmp_path / "cache", release.sha256)
    assert cached.read_bytes() == content
    assert attempts == 2
    assert sleeps == [0.25]


def test_pinned_roster_rejects_hash_mismatch(tmp_path: Path) -> None:
    release = PinnedRosterRelease(
        season=2026,
        url="https://example.test/roster.csv",
        sha256="0" * 64,
        reviewed_at=date(2026, 9, 1),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"changed", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(RosterHashError, match="hash mismatch"),
    ):
        fetch_pinned_roster(release, tmp_path / "cache", client=client)

    assert not tuple(path for path in (tmp_path / "cache").rglob("*") if path.is_file())


def test_corrupt_archive_entry_is_named_and_never_trusted(tmp_path: Path) -> None:
    content = (
        b"season,team,position,status,full_name,birth_date,gsis_id,week\n"
        b"2026,GB,WR,ACT,Example Player,2000-01-01,00-001,1\n"
    )
    release = PinnedRosterRelease(
        season=2026,
        url="https://example.test/roster.csv",
        sha256=hashlib.sha256(content).hexdigest(),
        reviewed_at=date(2026, 9, 1),
    )
    archived = roster_archive_path(tmp_path / "archive", release.sha256)
    archived.parent.mkdir(parents=True)
    archived.write_bytes(content[:-10])

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("a corrupt archive entry must not be silently refetched")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(RosterHashError, match="local archive file is corrupt"),
    ):
        fetch_pinned_roster(release, tmp_path / "archive", client=client)


def test_pinned_roster_seed_is_idempotent(tmp_path: Path) -> None:
    content = (
        b"season,team,position,status,full_name,birth_date,gsis_id,week\n"
        b"2026,GB,WR,ACT,Example Player,2000-01-01,00-001,1\n"
    )
    release = PinnedRosterRelease(
        season=2026,
        url="https://example.test/roster.csv",
        sha256=hashlib.sha256(content).hexdigest(),
        reviewed_at=date(2026, 9, 1),
    )
    roster = tmp_path / "roster.csv"
    roster.write_bytes(content)

    with connect_database(tmp_path / "store.sqlite3") as connection:
        apply_migrations(connection)
        first = seed_nflverse_roster(connection, roster, release, observed_at=OBSERVED)
        second = seed_nflverse_roster(connection, roster, release, observed_at=OBSERVED)
        counts = tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("players", "external_player_ids", "player_team_history")
        )

    assert first.players_seeded == 1
    assert second.players_existing == 1
    assert counts == (1, 1, 1)


def test_pinned_roster_release_selects_newest_pin_without_lookahead() -> None:
    january = PinnedRosterRelease(
        2026,
        "https://example.test/january.csv",
        "1" * 64,
        date(2026, 1, 5),
    )
    september = PinnedRosterRelease(
        2026,
        "https://example.test/september.csv",
        "2" * 64,
        date(2026, 9, 1),
    )
    releases = {2026: (september, january)}

    assert pinned_roster_release(2026, date(2026, 8, 31), releases=releases) is january
    assert pinned_roster_release(2026, date(2026, 9, 1), releases=releases) is september
    with pytest.raises(NflverseRosterError, match="at or before 2025-12-31"):
        pinned_roster_release(2026, date(2025, 12, 31), releases=releases)


def test_same_day_repin_selects_the_later_table_entry() -> None:
    morning = PinnedRosterRelease(2026, "https://example.test/am.csv", "3" * 64, date(2026, 9, 2))
    afternoon = PinnedRosterRelease(
        2026, "https://example.test/pm.csv", "4" * 64, date(2026, 9, 2)
    )
    releases = {2026: (morning, afternoon)}

    assert pinned_roster_release(2026, date(2026, 9, 2), releases=releases) is afternoon
    assert pinned_roster_release(2026, date(2026, 9, 3), releases=releases) is afternoon


def test_refresh_roster_reports_diff_and_does_not_mutate_pins(tmp_path: Path) -> None:
    prior = (
        b"season,team,position,status,full_name,birth_date,gsis_id,week\n"
        b"2026,GB,WR,ACT,Changed Player,2000-01-01,00-001,1\n"
        b"2026,NYJ,QB,ACT,Removed Player,1999-01-01,00-002,1\n"
    )
    current = (
        b"season,team,position,status,full_name,birth_date,gsis_id,week\n"
        b"2026,CHI,WR,ACT,Changed Player,2000-01-01,00-001,2\n"
        b"2026,DET,RB,ACT,Added Player,2001-01-01,00-003,2\n"
    )
    pin = PinnedRosterRelease(
        season=2026,
        url="https://example.test/prior.csv",
        sha256=hashlib.sha256(prior).hexdigest(),
        reviewed_at=date(2026, 8, 25),
    )
    releases = {2026: (pin,)}
    archive = tmp_path / "archive"
    requests: list[str] = []

    def archive_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=prior, request=request)

    with httpx.Client(transport=httpx.MockTransport(archive_handler)) as client:
        fetch_pinned_roster(pin, archive, client=client)

    before = tuple(PINNED_ROSTER_RELEASES[2026])

    def refresh_handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, content=current, request=request)

    with httpx.Client(transport=httpx.MockTransport(refresh_handler)) as client:
        report = refresh_roster_release(
            2026,
            archive,
            reviewed_at=date(2026, 9, 2),
            client=client,
            releases=releases,
        )

    assert len(requests) == 1
    assert report.sha256 == hashlib.sha256(current).hexdigest()
    assert report.added == (("00-003", "Added Player"),)
    assert report.removed == (("00-002", "Removed Player"),)
    assert report.changed[0].fields == (("team", "GB", "CHI"),)
    assert report.issues == ()
    assert "reviewed_at=date(2026, 9, 2)" in report.render()
    assert tuple(PINNED_ROSTER_RELEASES[2026]) == before

    # The reviewed entry it printed must be fetchable offline once pasted: the rolling bytes
    # are archived under their own hash, and the pin table itself is untouched.
    pasted = PinnedRosterRelease(2026, report.url, report.sha256, date(2026, 9, 2))
    assert roster_archive_path(archive, report.sha256).read_bytes() == current

    def offline(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("archived refresh bytes must not be refetched")

    with httpx.Client(transport=httpx.MockTransport(offline)) as client:
        fetched = fetch_pinned_roster(pasted, archive, client=client)
    assert fetched.read_bytes() == current


def test_refresh_rejects_a_future_review_date(tmp_path: Path) -> None:
    pin = PinnedRosterRelease(2026, "https://example.test/prior.csv", "5" * 64, date(2026, 9, 1))

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("no fetch may happen before the review date is validated")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(NflverseRosterError, match="2062-09-02 is in the future"),
    ):
        refresh_roster_release(
            2026,
            tmp_path / "archive",
            reviewed_at=date(2062, 9, 2),
            client=client,
            releases={2026: (pin,)},
            today=date(2026, 9, 2),
        )


def test_refresh_reports_malformed_and_conflicting_rows_instead_of_dropping_them(
    tmp_path: Path,
) -> None:
    prior = (
        b"season,team,position,status,full_name,birth_date,gsis_id,week\n"
        b"2026,GB,WR,ACT,Example Player,2000-01-01,00-001,1\n"
    )
    current = (
        b"season,team,position,status,full_name,birth_date,gsis_id,week\n"
        b"2026,,WR,ACT,Example Player,2000-01-01,00-001,2\n"
        b"2026,DET,RB,ACT,Twice Listed,2001-01-01,00-003,2\n"
        b"2026,CHI,RB,ACT,Twice Listed,2001-01-01,00-003,2\n"
    )
    pin = PinnedRosterRelease(
        season=2026,
        url="https://example.test/prior.csv",
        sha256=hashlib.sha256(prior).hexdigest(),
        reviewed_at=date(2026, 8, 25),
    )
    archive = tmp_path / "archive"
    roster_archive_path(archive, pin.sha256).parent.mkdir(parents=True)
    roster_archive_path(archive, pin.sha256).write_bytes(prior)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=current, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = refresh_roster_release(
            2026,
            archive,
            reviewed_at=date(2026, 9, 2),
            client=client,
            releases={2026: (pin,)},
            today=date(2026, 9, 2),
        )

    assert [issue.reason for issue in report.issues] == [
        "blank team",
        "conflicting duplicate row for 00-003 in week 2",
    ]
    assert report.removed == (("00-001", "Example Player"),)
    rendered = report.render()
    assert "rows_rejected=2" in rendered
    assert "! row 2: blank team" in rendered


def test_refresh_bootstraps_a_pin_whose_bytes_upstream_no_longer_serves(tmp_path: Path) -> None:
    prior = (
        b"season,team,position,status,full_name,birth_date,gsis_id,week\n"
        b"2026,GB,WR,ACT,Old Player,2000-01-01,00-001,1\n"
    )
    current = (
        b"season,team,position,status,full_name,birth_date,gsis_id,week\n"
        b"2026,GB,WR,ACT,New Player,2000-01-01,00-002,2\n"
    )
    pin = PinnedRosterRelease(
        season=2026,
        url="https://example.test/roster.csv",
        sha256=hashlib.sha256(prior).hexdigest(),
        reviewed_at=date(2026, 9, 1),
    )
    archive = tmp_path / "archive"

    def upstream_moved(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=current, request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(upstream_moved)) as client,
        pytest.raises(NflverseRosterError, match="--allow-missing-prior"),
    ):
        refresh_roster_release(
            2026,
            archive,
            reviewed_at=date(2026, 9, 2),
            client=client,
            releases={2026: (pin,)},
            today=date(2026, 9, 2),
        )

    with httpx.Client(transport=httpx.MockTransport(upstream_moved)) as client:
        report = refresh_roster_release(
            2026,
            archive,
            reviewed_at=date(2026, 9, 2),
            client=client,
            releases={2026: (pin,)},
            today=date(2026, 9, 2),
            allow_missing_prior=True,
        )

    assert report.prior_available is False
    assert report.sha256 == hashlib.sha256(current).hexdigest()
    assert report.added == () and report.removed == () and report.changed == ()
    rendered = report.render()
    assert "players_diff=UNAVAILABLE" in rendered
    assert f"sha256={report.sha256!r}" in rendered
    # The current bytes are archived under their own hash; the lost pin is not fabricated.
    assert roster_archive_path(archive, report.sha256).read_bytes() == current
    assert not roster_archive_path(archive, pin.sha256).exists()
