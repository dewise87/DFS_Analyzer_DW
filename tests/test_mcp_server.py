"""Slice 35 — the local MCP server: the read pages over stdio, and the four refusals.

Every test drives the real server through the SDK's in-process client session, because
the things worth checking here — what the tool list contains, what a tool answers, how an
excerpt is framed — are properties of the registered server, not of a Python function that
happens to sit behind one.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from test_ops_dashboard import NOW as DASHBOARD_NOW
from test_ops_dashboard import seeded  # noqa: F401 — used as a fixture in this module
from test_ownership_routing import (
    FIRST_DECISION_AT,
    NARRATIVE_PLAYER_NAME,
    SECOND_DECISION_AT,
    RoutingFixture,
    _fixture,
    _ops_config,
    _seed_narrative_claim,
)

from narrative_alpha import __version__
from narrative_alpha.build import build_decision
from narrative_alpha.ingest.timestamps import utc_timestamp
from narrative_alpha.mcp_server import (
    UNTRUSTED_TEXT_FIELDS,
    WRITE_LIKE_NAME_STEMS,
    build_mcp_server,
)
from narrative_alpha.narrative import build_episodes, build_features
from narrative_alpha.narrative.audit import AuditEvidence, player_audit
from narrative_alpha.store import connect_database

HEAT_CONFIG_PATH = Path("config/heat.toml")
LATE_ITEM_AT = FIRST_DECISION_AT + timedelta(minutes=2)
LATE_EXTRACTED_AT = FIRST_DECISION_AT + timedelta(minutes=3)

EXPECTED_TOOLS = {
    "get_status",
    "get_slate_memo",
    "get_player_dossier",
    "list_audit_candidates",
    "get_narrative_episode",
    "get_ownership_scenarios",
    "search_evidence",
    "replay_snapshot",
}


# --------------------------------------------------------------------------------------
# Driving the server the way a client does
# --------------------------------------------------------------------------------------


def _server(fixture: RoutingFixture, tmp_path: Path) -> FastMCP:
    return build_mcp_server(
        config=_ops_config(tmp_path / "ops"),
        database=fixture.database,
        artifact_directory=fixture.artifacts,
        report_directory=tmp_path / "reports",
    )


def _call(server: FastMCP, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call one tool through the SDK's in-process session and return its JSON body."""

    async def run() -> dict[str, Any]:
        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool(name, arguments or {})
            if result.isError:
                raise AssertionError(f"{name} failed: {_text(result.content)}")
            assert result.structuredContent is not None
            return dict(result.structuredContent)

    return anyio.run(run)


def _refusal(server: FastMCP, name: str, arguments: dict[str, Any]) -> str:
    async def run() -> str:
        async with create_connected_server_and_client_session(server) as client:
            result = await client.call_tool(name, arguments)
            assert result.isError, f"{name} was expected to refuse"
            return _text(result.content)

    return anyio.run(run)


def _tool_names(server: FastMCP) -> tuple[str, ...]:
    async def run() -> tuple[str, ...]:
        async with create_connected_server_and_client_session(server) as client:
            listed = await client.list_tools()
            return tuple(tool.name for tool in listed.tools)

    return anyio.run(run)


def _text(content: Any) -> str:
    return "".join(getattr(block, "text", "") for block in content)


def _instant(value: str) -> datetime:
    """Parse a timestamp from a response, whichever of the two shapes it carries."""

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _claim_ids(audit: dict[str, Any]) -> set[str]:
    return {
        claim["claim_id"] for episode in audit["episodes"] for claim in episode["claims"]
    }


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


def _later_decision(fixture: RoutingFixture) -> str:
    """Add a claim observed after the first decision and freeze a second decision on it."""

    with connect_database(fixture.database) as connection:
        _seed_narrative_claim(
            connection,
            item_key="late-item-1",
            observed_at=LATE_ITEM_AT,
            extracted_at=LATE_EXTRACTED_AT,
            seed_source=False,
        )
        connection.commit()
        build_episodes(connection, as_of=SECOND_DECISION_AT, built_at=SECOND_DECISION_AT)
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
    return build_decision(
        fixture.database,
        slate_id=fixture.slate_id,
        site="draftkings",
        decision_at=SECOND_DECISION_AT,
        artifact_directory=fixture.artifacts,
    ).snapshot.decision_snapshot_id


# --------------------------------------------------------------------------------------
# The tool list
# --------------------------------------------------------------------------------------


def test_the_server_exposes_exactly_the_eight_read_tools(tmp_path: Path) -> None:
    server = _server(_fixture(tmp_path), tmp_path)
    assert set(_tool_names(server)) == EXPECTED_TOOLS


def test_no_tool_name_suggests_a_write(tmp_path: Path) -> None:
    """Writes stay on the CLI and the dashboard, where a human confirms them.

    The check is on the *name*, not on the implementation, because the name is what a
    client's model reads before it decides what this server will let it do.
    """

    server = _server(_fixture(tmp_path), tmp_path)
    for name in _tool_names(server):
        segments = set(name.split("_"))
        offending = segments & set(WRITE_LIKE_NAME_STEMS)
        assert not offending, f"tool {name!r} reads like a write: {sorted(offending)}"
    assert "log_decision" not in _tool_names(server)


def test_every_tool_is_annotated_read_only(tmp_path: Path) -> None:
    server = _server(_fixture(tmp_path), tmp_path)

    async def run() -> None:
        async with create_connected_server_and_client_session(server) as client:
            for tool in (await client.list_tools()).tools:
                assert tool.annotations is not None, tool.name
                assert tool.annotations.readOnlyHint is True, tool.name
                assert tool.annotations.destructiveHint is False, tool.name

    anyio.run(run)


# --------------------------------------------------------------------------------------
# Each tool against the seeded fixtures
# --------------------------------------------------------------------------------------


def test_get_status_answers_from_the_dashboard_fixture(seeded: Any, tmp_path: Path) -> None:  # noqa: F811
    """The same payload the dashboard's status page renders, with the §7.1 envelope."""

    server = build_mcp_server(
        config=seeded,
        database=seeded.database,
        artifact_directory=tmp_path / "artifacts",
        report_directory=tmp_path / "reports",
        clock=lambda: DASHBOARD_NOW,
    )
    body = _call(server, "get_status")

    assert body["as_of"] == utc_timestamp(DASHBOARD_NOW)
    assert body["code_version"] == __version__
    assert body["database"] == str(seeded.database)
    steps = {step["step"]: step for step in body["steps"]}
    assert steps["extract"]["last_failure_text"] == "the Keychain item could not be read"


def test_get_slate_memo_returns_the_recorded_text_and_the_structured_model(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    server = _server(fixture, tmp_path)
    decision = _first_decision(fixture)
    body = _call(server, "get_slate_memo", {"decision_snapshot_id": decision})

    assert body["decision_snapshot_id"] == decision
    assert body["as_of"] == utc_timestamp(FIRST_DECISION_AT)
    assert body["memo_text"].startswith("NARRATIVE ALPHA REPORT BUNDLE")
    assert f"decision_snapshot_id={decision}" in body["memo_text"]
    model = body["memo_model"]
    assert model["decision_snapshot_id"] == decision
    assert _instant(model["as_of"]) == FIRST_DECISION_AT
    assert model["lineups"]


def test_get_slate_memo_states_the_gap_when_the_decision_is_gone(
    seeded: Any, tmp_path: Path  # noqa: F811
) -> None:
    """The dashboard fixture records a memo for a decision the store never held.

    The text still comes back, the model does not, and the answer says which — a missing
    model is a fact about the store, not a reason to answer nothing.
    """

    server = build_mcp_server(
        config=seeded,
        database=seeded.database,
        artifact_directory=tmp_path / "artifacts",
        report_directory=tmp_path / "reports",
        clock=lambda: DASHBOARD_NOW,
    )
    body = _call(server, "get_slate_memo")

    assert body["decision_snapshot_id"] == "decision-fixture"
    assert body["decision_source"] == "latest_slate_memo_step"
    assert body["memo_text"] == "SLATE DECISION MEMO\nfixture body line\n"
    assert body["memo_model"] is None
    assert any("is not in this store" in note for note in body["notes"])


def test_get_player_dossier_carries_the_episode_claim_and_excerpt(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    server = _server(fixture, tmp_path)
    decision = _first_decision(fixture)
    with connect_database(fixture.database) as connection:
        expected = player_audit(
            connection,
            player_id=fixture.narrative_player_id,
            decision_snapshot_id=decision,
        )

    body = _call(
        server,
        "get_player_dossier",
        {"player": NARRATIVE_PLAYER_NAME, "decision_snapshot_id": decision},
    )
    audit = body["audit"]

    assert body["decision_snapshot_id"] == decision
    assert audit["player_name"] == NARRATIVE_PLAYER_NAME
    assert audit["ownership"]["vendor_baseline"] == expected.ownership.vendor_baseline
    episode = audit["episodes"][0]
    assert episode["episode_id"] == expected.episodes[0].episode_id
    claim = episode["claims"][0]
    assert claim["claim_id"] == expected.episodes[0].claims[0].claim_id
    assert claim["source_grade_basis"] == "source_family_default"


def test_list_audit_candidates_matches_the_decision_cutoff(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    server = _server(fixture, tmp_path)
    decision = _first_decision(fixture)
    body = _call(server, "list_audit_candidates", {"decision_snapshot_id": decision})

    names = {row["player_name"] for row in body["candidates"]}
    assert body["candidate_count"] == len(body["candidates"])
    assert NARRATIVE_PLAYER_NAME in names
    assert body["as_of"] == utc_timestamp(FIRST_DECISION_AT)


def test_get_narrative_episode_returns_one_episode_with_its_claims(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    server = _server(fixture, tmp_path)
    decision = _first_decision(fixture)
    with connect_database(fixture.database) as connection:
        expected = player_audit(
            connection,
            player_id=fixture.narrative_player_id,
            decision_snapshot_id=decision,
        ).episodes[0]

    body = _call(
        server,
        "get_narrative_episode",
        {"episode_id": expected.episode_id, "decision_snapshot_id": decision},
    )
    episode = body["episode"]

    assert episode["episode_id"] == expected.episode_id
    assert len(episode["claims"]) == len(expected.claims)
    assert episode["claims"][0]["relation"] == "origin"
    assert episode["claims"][0]["evidence"]


def test_get_ownership_scenarios_reports_the_set_and_the_routing_record(
    tmp_path: Path,
) -> None:
    """The routing fixture's first decision applied the vendor baseline; say so, with why."""

    fixture = _fixture(tmp_path)
    server = _server(fixture, tmp_path)
    decision = _first_decision(fixture)
    body = _call(server, "get_ownership_scenarios", {"decision_snapshot_id": decision})
    scenarios = body["scenarios"]

    assert body["decision_snapshot_id"] == decision
    assert scenarios["set_status"] == "none"
    assert scenarios["applied"] is False
    assert scenarios["rows"] == []
    assert scenarios["routing_record"]["applied"] is False
    assert "vendor baseline" in scenarios["routing_record"]["reason"]
    assert any("no ownership scenario set existed" in note for note in body["notes"])


def test_search_evidence_finds_the_excerpt_and_reports_its_cap(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    server = _server(fixture, tmp_path)
    decision = _first_decision(fixture)
    body = _call(
        server,
        "search_evidence",
        {"query": NARRATIVE_PLAYER_NAME.lower(), "decision_snapshot_id": decision},
    )
    search = body["search"]

    assert body["as_of"] == utc_timestamp(FIRST_DECISION_AT)
    assert search["truncated"] is False
    assert search["hits"]
    assert NARRATIVE_PLAYER_NAME in search["hits"][0]["evidence"]["verbatim_extract"]["text"]

    capped = _call(
        server,
        "search_evidence",
        {
            "query": NARRATIVE_PLAYER_NAME.lower(),
            "decision_snapshot_id": decision,
            "limit": 1,
        },
    )
    assert len(capped["search"]["hits"]) == 1


def test_replay_snapshot_reports_a_matching_rebuild(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    server = _server(fixture, tmp_path)
    decision = _first_decision(fixture)
    body = _call(server, "replay_snapshot", {"decision_snapshot_id": decision})
    report = body["report"]

    assert body["decision_snapshot_id"] == decision
    assert report["output_matches"] is True
    assert report["expected_output_sha256"] == report["actual_output_sha256"]
    assert report["lineup_count"] >= 1


# --------------------------------------------------------------------------------------
# The envelope, and the cutoff it names
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_slate_memo", {}),
        ("get_player_dossier", {"player": NARRATIVE_PLAYER_NAME}),
        ("list_audit_candidates", {}),
        ("get_ownership_scenarios", {}),
        ("search_evidence", {"query": "snap"}),
        ("replay_snapshot", {}),
    ],
)
def test_a_response_for_a_decision_carries_that_decisions_as_of(
    tmp_path: Path, tool: str, arguments: dict[str, Any]
) -> None:
    fixture = _fixture(tmp_path)
    server = _server(fixture, tmp_path)
    decision = _first_decision(fixture)
    body = _call(server, tool, {**arguments, "decision_snapshot_id": decision})

    assert body["as_of"] == utc_timestamp(FIRST_DECISION_AT)
    assert body["decision_snapshot_id"] == decision
    assert body["code_version"] == __version__


def test_get_narrative_episode_carries_the_decisions_as_of(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    server = _server(fixture, tmp_path)
    decision = _first_decision(fixture)
    with connect_database(fixture.database) as connection:
        episode_id = player_audit(
            connection,
            player_id=fixture.narrative_player_id,
            decision_snapshot_id=decision,
        ).episodes[0].episode_id

    body = _call(
        server,
        "get_narrative_episode",
        {"episode_id": episode_id, "decision_snapshot_id": decision},
    )
    assert body["as_of"] == utc_timestamp(FIRST_DECISION_AT)
    assert body["decision_snapshot_id"] == decision


# --------------------------------------------------------------------------------------
# As-of discipline: what was knowable, not what is known now
# --------------------------------------------------------------------------------------


def test_a_claim_observed_after_the_decision_is_absent(tmp_path: Path) -> None:
    """The point of the whole interface, asked through the client rather than the library."""

    fixture = _fixture(tmp_path)
    server = _server(fixture, tmp_path)
    first = _first_decision(fixture)
    later = _later_decision(fixture)

    early = _call(
        server,
        "get_player_dossier",
        {"player": NARRATIVE_PLAYER_NAME, "decision_snapshot_id": first},
    )
    late = _call(
        server,
        "get_player_dossier",
        {"player": NARRATIVE_PLAYER_NAME, "decision_snapshot_id": later},
    )
    early_claims = _claim_ids(early["audit"])
    late_claims = _claim_ids(late["audit"])

    assert len(early_claims) == 1
    assert len(late_claims) == 2
    assert early_claims < late_claims
    newest = (late_claims - early_claims).pop()
    assert newest not in json.dumps(early)
    assert early["as_of"] == utc_timestamp(FIRST_DECISION_AT)
    assert late["as_of"] == utc_timestamp(SECOND_DECISION_AT)


def test_an_episode_built_after_a_decision_cannot_be_read_as_of_it(
    tmp_path: Path,
) -> None:
    """A Stage 2 snapshot is dated: the later rebuild is a different episode, not this one.

    So the earlier decision's episode still carries the one claim it carried, and the
    episode the later rebuild produced is refused at the earlier cutoff rather than
    quietly answered with the claim that arrived afterwards.
    """

    fixture = _fixture(tmp_path)
    server = _server(fixture, tmp_path)
    first = _first_decision(fixture)
    later = _later_decision(fixture)
    with connect_database(fixture.database) as connection:
        early_episode = (
            player_audit(
                connection,
                player_id=fixture.narrative_player_id,
                decision_snapshot_id=first,
            )
            .episodes[0]
            .episode_id
        )
        late_episode = (
            player_audit(
                connection,
                player_id=fixture.narrative_player_id,
                decision_snapshot_id=later,
            )
            .episodes[0]
            .episode_id
        )
    assert early_episode != late_episode

    early = _call(
        server,
        "get_narrative_episode",
        {"episode_id": early_episode, "decision_snapshot_id": first},
    )
    late = _call(
        server,
        "get_narrative_episode",
        {"episode_id": late_episode, "decision_snapshot_id": later},
    )
    detail = _refusal(
        server,
        "get_narrative_episode",
        {"episode_id": late_episode, "decision_snapshot_id": first},
    )

    assert len(early["episode"]["claims"]) == 1
    assert len(late["episode"]["claims"]) == 2
    assert "was not visible at" in detail
    assert utc_timestamp(FIRST_DECISION_AT) in detail


def test_search_evidence_cannot_reach_an_excerpt_observed_after_the_decision(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    server = _server(fixture, tmp_path)
    first = _first_decision(fixture)
    later = _later_decision(fixture)
    query = NARRATIVE_PLAYER_NAME.lower()

    early = _call(server, "search_evidence", {"query": query, "decision_snapshot_id": first})
    late = _call(server, "search_evidence", {"query": query, "decision_snapshot_id": later})

    early_claims = {hit["claim_id"] for hit in early["search"]["hits"]}
    late_claims = {hit["claim_id"] for hit in late["search"]["hits"]}
    assert early_claims
    assert early_claims < late_claims
    for hit in early["search"]["hits"]:
        assert _instant(hit["evidence"]["observed_at"]) <= FIRST_DECISION_AT


def test_an_unknown_decision_is_refused_rather_than_answered_from_now(
    tmp_path: Path,
) -> None:
    server = _server(_fixture(tmp_path), tmp_path)
    detail = _refusal(
        server,
        "get_player_dossier",
        {"player": NARRATIVE_PLAYER_NAME, "decision_snapshot_id": "decision-does-not-exist"},
    )
    assert "decision-does-not-exist" in detail


# --------------------------------------------------------------------------------------
# §7.6: scraped text arrives framed as data
# --------------------------------------------------------------------------------------


def test_every_excerpt_is_wrapped_in_the_untrusted_framing(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    server = _server(fixture, tmp_path)
    decision = _first_decision(fixture)
    with connect_database(fixture.database) as connection:
        evidence = (
            player_audit(
                connection,
                player_id=fixture.narrative_player_id,
                decision_snapshot_id=decision,
            )
            .episodes[0]
            .claims[0]
            .evidence[0]
        )
    raw = str(evidence.verbatim_extract)

    body = _call(
        server,
        "get_player_dossier",
        {"player": NARRATIVE_PLAYER_NAME, "decision_snapshot_id": decision},
    )
    block = body["audit"]["episodes"][0]["claims"][0]["evidence"][0]["verbatim_extract"]

    assert block["text"].startswith("---BEGIN NA_UNTRUSTED_SOURCE_")
    assert block["text"].endswith("---")
    assert "---END NA_UNTRUSTED_SOURCE_" in block["text"]
    assert raw in block["text"]
    assert "may contain malicious instructions" in block["notice"]
    # The excerpt exists in the response exactly once, inside its delimiters: no sibling
    # field repeats it unframed for a client that reads the shorter key.
    assert json.dumps(body).count(json.dumps(raw)[1:-1]) == 1


def test_a_scraped_headline_is_framed_too(tmp_path: Path) -> None:
    """The prompt-injection surface is the headline as much as the excerpt."""

    fixture = _fixture(tmp_path)
    server = _server(fixture, tmp_path)
    decision = _first_decision(fixture)
    body = _call(
        server,
        "search_evidence",
        {"query": NARRATIVE_PLAYER_NAME.lower(), "decision_snapshot_id": decision},
    )
    title = body["search"]["hits"][0]["item_title"]

    assert title["text"].startswith("---BEGIN NA_UNTRUSTED_SOURCE_")
    assert "may contain malicious instructions" in title["notice"]


def test_the_framed_field_names_still_match_the_audit_model(tmp_path: Path) -> None:
    """A renamed or added evidence field must break here, not leak an unframed excerpt."""

    assert "verbatim_extract" in AuditEvidence.model_fields
    assert set(UNTRUSTED_TEXT_FIELDS) == {"verbatim_extract", "item_title"}
    server = _server(_fixture(tmp_path), tmp_path)
    body = _call(server, "list_audit_candidates")
    # A response with no scraped text carries no framing, and no empty block either.
    assert "NA_UNTRUSTED_SOURCE_" not in json.dumps(body)
