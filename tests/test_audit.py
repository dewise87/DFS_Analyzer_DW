"""Slice 31 — the signal and evidence audit view: the model, the two renderers, as-of."""

from __future__ import annotations

import http.client
import threading
from collections.abc import Iterator
from datetime import timedelta
from html import escape
from pathlib import Path

import pytest
from test_ownership_routing import (
    FIRST_DECISION_AT,
    NARRATIVE_PLAYER_NAME,
    OWNERSHIP_CONFIG_PATH,
    SCENARIOS_AT,
    SECOND_DECISION_AT,
    RoutingFixture,
    _baselines,
    _fixture,
    _insert_evaluation,
    _insert_scenario_set,
    _ops_config,
    _persist_synthetic_fit,
    _seed_narrative_claim,
)

from narrative_alpha.build import build_decision
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.narrative import build_episodes, build_features
from narrative_alpha.narrative.audit import (
    AuditError,
    player_audit,
    render_player_audit,
    resolve_audit_player,
)
from narrative_alpha.ops.dashboard import DashboardServer, build_dashboard
from narrative_alpha.ownership import load_ownership_config
from narrative_alpha.ownership_routing import material_delta
from narrative_alpha.report_cli import main as report_main
from narrative_alpha.store import connect_database

HEAT_CONFIG_PATH = Path("config/heat.toml")
LATE_ITEM_AT = FIRST_DECISION_AT + timedelta(minutes=2)
LATE_EXTRACTED_AT = FIRST_DECISION_AT + timedelta(minutes=3)


def test_the_audit_shows_the_episode_claim_and_excerpt_behind_the_number(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    decision = _first_decision(fixture)
    with connect_database(fixture.database) as connection:
        audit = player_audit(
            connection,
            player_id=fixture.narrative_player_id,
            decision_snapshot_id=decision,
        )

    assert audit.player_name == NARRATIVE_PLAYER_NAME
    assert audit.decision_at == FIRST_DECISION_AT
    assert audit.features is not None
    assert audit.features.episode_ids
    assert {channel.name for channel in audit.features.channels} >= {"h_signed", "h_dfs"}
    assert len(audit.episodes) == 1

    episode = audit.episodes[0]
    assert episode.claims
    claim = episode.claims[0]
    assert claim.relation == "origin"
    assert claim.source_family == "national_media"
    # The fixture source is not in the reviewed catalog, so the grade falls back to the
    # family default and says so rather than implying a review that never happened.
    assert claim.source_grade == "B"
    assert claim.source_grade_basis == "source_family_default"
    assert claim.evidence
    assert NARRATIVE_PLAYER_NAME in str(claim.evidence[0].verbatim_extract)

    rendered = render_player_audit(audit)
    assert f"player_name={NARRATIVE_PLAYER_NAME}" in rendered
    assert f"episode_id={episode.episode_id}" in rendered
    assert f"claim {claim.claim_id}" in rendered
    assert "grade=B (source_family_default)" in rendered
    assert str(claim.evidence[0].verbatim_extract) in rendered


def test_a_claim_observed_after_the_decision_does_not_appear(tmp_path: Path) -> None:
    """The whole point of the view: it shows what was knowable, not what is known now."""

    fixture = _fixture(tmp_path)
    first = _first_decision(fixture)
    with connect_database(fixture.database) as connection:
        _seed_narrative_claim(
            connection,
            item_key="late-item-1",
            observed_at=LATE_ITEM_AT,
            extracted_at=LATE_EXTRACTED_AT,
            seed_source=False,
        )
        connection.commit()
        build_episodes(
            connection, as_of=SECOND_DECISION_AT, built_at=SECOND_DECISION_AT
        )
        connection.commit()
        build_features(
            connection,
            slate_id=fixture.slate_id,
            site="draftkings",
            as_of=SECOND_DECISION_AT,
            built_at=SECOND_DECISION_AT,
            config_path=HEAT_CONFIG_PATH,
        )
        connection.commit()
    later = build_decision(
        fixture.database,
        slate_id=fixture.slate_id,
        site="draftkings",
        decision_at=SECOND_DECISION_AT,
        artifact_directory=fixture.artifacts,
    ).snapshot.decision_snapshot_id

    with connect_database(fixture.database) as connection:
        early = player_audit(
            connection,
            player_id=fixture.narrative_player_id,
            decision_snapshot_id=first,
        )
        late = player_audit(
            connection,
            player_id=fixture.narrative_player_id,
            decision_snapshot_id=later,
        )

    early_claims = {claim.claim_id for episode in early.episodes for claim in episode.claims}
    late_claims = {claim.claim_id for episode in late.episodes for claim in episode.claims}
    assert len(early_claims) == 1
    assert len(late_claims) == 2
    assert early_claims < late_claims
    for episode in early.episodes:
        for claim in episode.claims:
            assert claim.item_observed_at <= FIRST_DECISION_AT
            for evidence in claim.evidence:
                assert evidence.observed_at <= FIRST_DECISION_AT
    assert (late_claims - early_claims).pop() not in render_player_audit(early)


def test_a_quiet_player_says_so_and_shows_the_vendor_baseline(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    decision = _first_decision(fixture)
    quiet = next(
        player_id for player_id in fixture.player_ids if player_id != fixture.narrative_player_id
    )
    with connect_database(fixture.database) as connection:
        audit = player_audit(
            connection, player_id=quiet, decision_snapshot_id=decision
        )

    assert not audit.episodes
    assert any("no narrative episode was behind this player" in note for note in audit.notes)
    assert audit.ownership.applied is False
    assert audit.ownership.vendor_baseline is not None
    assert "no ownership scenario set existed" in audit.ownership.reason
    rendered = render_player_audit(audit)
    assert "ownership_source=vendor_baseline" in rendered
    assert "episode_status=none" in rendered
    assert "no narrative episode was behind this player" in rendered


def test_an_applied_scenario_set_is_reported_with_its_governance_status(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    with connect_database(fixture.database) as connection:
        baselines = _baselines(connection, fixture)
        applied = dict(baselines)
        applied[fixture.narrative_player_id] = (
            baselines[fixture.narrative_player_id] + material_delta() + 0.01
        )
        run_id = _insert_scenario_set(
            connection, fixture, applied=applied, status="TESTING", at=SCENARIOS_AT
        )
        _insert_evaluation(connection, fixture, beat_baseline=True, at=SCENARIOS_AT)
        connection.commit()
    routed = build_decision(
        fixture.database,
        slate_id=fixture.slate_id,
        site="draftkings",
        decision_at=SECOND_DECISION_AT,
        artifact_directory=fixture.artifacts,
    )
    assert routed.ownership_routing.applied

    with connect_database(fixture.database) as connection:
        audit = player_audit(
            connection,
            player_id=fixture.narrative_player_id,
            decision_snapshot_id=routed.snapshot.decision_snapshot_id,
        )

    ownership = audit.ownership
    assert ownership.applied
    assert ownership.scenario_run_id == run_id
    assert ownership.governance_status == "TESTING"
    assert ownership.status_multiplier == pytest.approx(0.50)
    assert ownership.applied_ownership == pytest.approx(
        applied[fixture.narrative_player_id]
    )
    assert ownership.delta_points == pytest.approx((material_delta() + 0.01) * 100)
    assert ownership.evaluation_beat_baseline is True
    rendered = render_player_audit(audit)
    assert "ownership_source=scenario_model" in rendered
    assert f"scenario_run_id={run_id}" in rendered
    assert "governance_status=TESTING" in rendered


def test_the_available_scenario_set_is_scoped_to_the_decisions_own_archetype(
    tmp_path: Path,
) -> None:
    """A set built for another contest archetype explains nothing about this decision.

    Stage 4 looked for a set of the archetype the request named. The audit says why the
    vendor baseline reached the optimizer, so it must look for the same one — and the
    decision's archetype is frozen in its optimizer request, not in a column, which is why
    the artifact root is what turns the scoping on.
    """

    fixture = _fixture(tmp_path)
    with connect_database(fixture.database) as connection:
        tournament_fit = _persist_synthetic_fit(
            connection,
            load_ownership_config(OWNERSHIP_CONFIG_PATH),
            contest_archetype="3max",
        )
        baselines = _baselines(connection, fixture)
        applied = dict(baselines)
        applied[fixture.narrative_player_id] = (
            baselines[fixture.narrative_player_id] + material_delta() + 0.01
        )
        other_archetype_run_id = _insert_scenario_set(
            connection,
            fixture,
            applied=applied,
            status="TESTING",
            at=SCENARIOS_AT,
            contest_archetype="3max",
            model_run_id=tournament_fit,
        )
        connection.commit()

    cash = build_decision(
        fixture.database,
        slate_id=fixture.slate_id,
        site="draftkings",
        decision_at=SECOND_DECISION_AT,
        artifact_directory=fixture.artifacts,
        contest_archetype="cash",
    )
    assert not cash.ownership_routing.applied
    decision = cash.snapshot.decision_snapshot_id

    with connect_database(fixture.database) as connection:
        unscoped = player_audit(
            connection,
            player_id=fixture.narrative_player_id,
            decision_snapshot_id=decision,
        )
        scoped = player_audit(
            connection,
            player_id=fixture.narrative_player_id,
            decision_snapshot_id=decision,
            artifact_root=fixture.artifacts,
        )
        missing = player_audit(
            connection,
            player_id=fixture.narrative_player_id,
            decision_snapshot_id=decision,
            artifact_root=tmp_path / "artifacts-that-moved",
        )

    # Without the frozen request there is nothing sound to scope by, so the newest set on
    # the slate is described whichever archetype it was built for.
    assert unscoped.ownership.available_scenario_run_id == other_archetype_run_id
    assert scoped.ownership.scenario_set_available is False
    assert scoped.ownership.available_scenario_run_id is None
    assert "no ownership scenario set existed" in scoped.ownership.reason
    # An artifact root that holds no request degrades loudly rather than pretending.
    assert missing.ownership.available_scenario_run_id == other_archetype_run_id
    assert any(
        "frozen optimizer request is not readable" in note for note in missing.notes
    )


def test_the_cli_and_the_page_render_the_same_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = _fixture(tmp_path)
    decision = _first_decision(fixture)
    with connect_database(fixture.database) as connection:
        audit = player_audit(
            connection,
            player_id=fixture.narrative_player_id,
            decision_snapshot_id=decision,
        )

    exit_code = report_main(
        [
            "signals",
            "--database",
            str(fixture.database),
            "--decision-snapshot",
            decision,
            "--player",
            NARRATIVE_PLAYER_NAME,
        ]
    )
    assert exit_code == 0
    assert capsys.readouterr().out == render_player_audit(audit)

    config = _ops_config(tmp_path / "ops")
    for client in _serve(config, database=fixture.database):
        status, body = client.get(
            f"/audit?decision={decision}&player={fixture.narrative_player_id}"
        )
        index_status, index_body = client.get(f"/audit?decision={decision}")
        misdirected, _ = client.get_with_host(
            f"/audit?decision={decision}", f"attacker.example:{client.port}"
        )

    assert status == 200
    assert body.startswith("<!doctype html>")
    assert NARRATIVE_PLAYER_NAME in body
    assert audit.episodes[0].episode_id in body
    assert escape(str(audit.episodes[0].claims[0].evidence[0].verbatim_extract)) in body
    assert utc_timestamp(audit.decision_at) in body
    assert index_status == 200
    assert f"player={fixture.narrative_player_id}" in index_body
    # The new page inherits the existing rebinding defence rather than opting out of it.
    assert misdirected == 421


def test_the_dashboard_answers_a_bad_audit_request_with_400(tmp_path: Path) -> None:
    """An unknown decision or player is the caller's error, not a page that says 200."""

    import http.client
    import threading

    from narrative_alpha.ops import build_dashboard, load_ops_config

    config_path = tmp_path / "ops.toml"
    config_path.write_text(
        f'''timezone = "America/New_York"
season = 2026
monthly_llm_budget_usd = "50.00"
keychain_service = "narrative-alpha-anthropic"
[batch]
weekdays = ["wed"]
local_time = "09:30"
[paths]
database = "{tmp_path / "store.sqlite3"}"
snapshot_root = "{tmp_path / "snapshots"}"
nflverse_archive = "{tmp_path / "archive"}"
log_directory = "{tmp_path / "logs"}"
''',
        encoding="utf-8",
    )
    config = load_ops_config(config_path)
    server = build_dashboard(config=config, database=config.database, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection(server.bound_host, server.port, timeout=10)
        connection.request("GET", "/audit?decision=decision-does-not-exist&player=1")
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        connection.close()
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()

    assert response.status == 400
    assert "decision-does-not-exist" in body
    assert "Traceback" not in body


def test_a_name_that_is_not_unique_or_not_known_is_refused(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    decision = _first_decision(fixture)
    with connect_database(fixture.database) as connection:
        assert (
            resolve_audit_player(
                connection,
                selector=NARRATIVE_PLAYER_NAME,
                decision_snapshot_id=decision,
            )
            == fixture.narrative_player_id
        )
        with pytest.raises(AuditError, match="no player is named"):
            resolve_audit_player(
                connection, selector="Nobody Here", decision_snapshot_id=decision
            )
        with pytest.raises(AuditError, match="unknown decision snapshot"):
            player_audit(
                connection,
                player_id=fixture.narrative_player_id,
                decision_snapshot_id="decision-does-not-exist",
            )


def _first_decision(fixture: RoutingFixture) -> str:
    with connect_database(fixture.database) as connection:
        row = connection.execute(
            """
            SELECT decision_snapshot_id FROM decision_snapshots
            WHERE slate_id = ? ORDER BY rtrim(decision_at, 'Z') LIMIT 1
            """,
            (fixture.slate_id,),
        ).fetchone()
    return str(row[0])


class _Client:
    """The same tiny loopback client the dashboard tests use."""

    def __init__(self, server: DashboardServer) -> None:
        self.host = server.bound_host
        self.port = server.port

    def get(self, path: str) -> tuple[int, str]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            connection.close()

    def get_with_host(self, path: str, host: str) -> tuple[int, str]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            connection.putrequest("GET", path, skip_host=True)
            connection.putheader("Host", host)
            connection.endheaders()
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            connection.close()


def _serve(config: object, *, database: Path) -> Iterator[_Client]:
    server = build_dashboard(
        config=config,  # type: ignore[arg-type]
        database=database,
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Client(server)
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()
