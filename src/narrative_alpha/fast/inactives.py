"""Deterministic official-inactives path to a re-frozen affected-lineup upload."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Literal, cast
from uuid import uuid4

from narrative_alpha import __version__
from narrative_alpha.build import build_decision
from narrative_alpha.build_cli import DEFAULT_ARTIFACT_DIRECTORY
from narrative_alpha.fast.rules import (
    DEFAULT_FAST_LANE_RULES_PATH,
    FastLaneRule,
    FastLaneRules,
    load_fast_lane_rules,
)
from narrative_alpha.identity import PlayerCrosswalk, PlayerIdentityInput, normalize_name
from narrative_alpha.ingest.slates import SlateSummary, list_slates, normalize_site
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.portfolio import (
    Lineup,
    OptimizerAdapter,
    PydfsAdapter,
    UploadEntry,
    ValidationResult,
)
from narrative_alpha.replay import FrozenDecision, read_frozen_decision
from narrative_alpha.snapshots import CaptureKind, capture_files
from narrative_alpha.store import (
    ModelRunRow,
    PlayerAvailabilityRow,
    apply_migrations,
    connect_database,
)

OFFICIAL_INACTIVES_SOURCE = "official-inactive-list"
_POSITION_CODES = frozenset(
    {"QB", "RB", "FB", "WR", "TE", "OL", "DL", "DE", "DT", "LB", "CB", "S", "K", "P"}
)


class FastInactivesError(RuntimeError):
    """Raised when the deterministic inactive-list action cannot safely finish."""


class FastLaneCapError(FastInactivesError):
    """Raised before decision commit when the replacement-lineup mean exceeds its cap."""


@dataclass(frozen=True)
class InactivePlayer:
    player_id: int
    name: str
    team: str


@dataclass(frozen=True)
class LineupDiff:
    prior_lineup_id: str
    replacement_lineup_id: str
    out: tuple[str, ...]
    in_: tuple[str, ...]


@dataclass(frozen=True)
class FastInactivesReport:
    run_id: str
    season: int
    week: int
    site: str
    observed_at: datetime
    rule_id: str
    rules_version: str
    inactive_players: tuple[InactivePlayer, ...]
    affected_lineups: int
    portfolio_lineups: int
    mean_change: float
    decision_snapshot_id: str
    upload_csv_path: Path
    diffs: tuple[LineupDiff, ...]
    elapsed_seconds: float


@dataclass(frozen=True)
class _RosterIdentity:
    player_id: int
    name: str
    team: str
    position: str | None


class _CappedAdapter:
    """Check the pre-approved mean cap before build_decision writes any artifacts."""

    def __init__(
        self,
        delegate: OptimizerAdapter,
        *,
        prior_lineups: tuple[Lineup, ...],
        pinned_count: int,
        mean_cap: float,
        rule: FastLaneRule,
    ) -> None:
        self.delegate = delegate
        self.prior_lineups = prior_lineups
        self.pinned_count = pinned_count
        self.mean_cap = mean_cap
        self.rule = rule
        self.mean_change: float | None = None

    def build_lineups(self, request: object) -> tuple[Lineup, ...]:
        # OptimizerAdapter is structural; retaining the concrete annotation here would
        # repeat its long request import solely for typing this transparent wrapper.
        lineups = self.delegate.build_lineups(request)  # type: ignore[arg-type]
        # "Do nothing" is the affected lineups priced with today's projections (an
        # inactive player keeps the projection he was frozen with, since today's pool no
        # longer prices him); the comparison is then the swap alone, not projection drift.
        scenario = request.candidate_player_scenario  # type: ignore[attr-defined]
        today = {player.player_id: float(player.projection) for player in scenario.players}
        prior_mean = fmean(
            sum(today.get(player.player_id, player.projection) for player in lineup.players)
            for lineup in self.prior_lineups
        )
        replacements = lineups[self.pinned_count :]
        replacement_mean = fmean(lineup.total_projection for lineup in replacements)
        change = abs(replacement_mean - prior_mean)
        self.mean_change = change
        if change > self.mean_cap:
            raise FastLaneCapError(
                f"replacement-lineup mean changed by {change:.3f} fantasy points, above "
                f"rule {self.rule.rule_id!r}'s {self.mean_cap:.3f}-point cap; a human must "
                "confirm this decision before any new snapshot or upload is written"
            )
        return lineups

    def validate_lineup(self, lineup: Lineup, request: object) -> ValidationResult:
        return self.delegate.validate_lineup(lineup, request)  # type: ignore[arg-type]

    def export_upload_csv(
        self,
        lineups: tuple[Lineup, ...],
        site: object,
        entries: tuple[UploadEntry, ...] = (),
    ) -> bytes:
        return self.delegate.export_upload_csv(lineups, site, entries)  # type: ignore[arg-type]


def process_official_inactives(
    database: Path,
    *,
    season: int,
    week: int,
    site: str,
    text: str,
    snapshot_root: Path,
    artifact_directory: Path = DEFAULT_ARTIFACT_DIRECTORY,
    rules_path: Path = DEFAULT_FAST_LANE_RULES_PATH,
    now: datetime | None = None,
    adapter: OptimizerAdapter | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> FastInactivesReport:
    """Record official inactives and re-freeze the whole portfolio, rebuilding only affected rows.

    The availability rows and the new decision are one transaction: a refusal of any
    kind — cap, optimizer, duplicate — leaves no availability fact behind for a later
    build to act on without a human. The lineups the inactives did not touch are pinned
    into the new snapshot verbatim, so the frozen record stays the complete decision and
    a second wave of inactives sees every lineup.
    """

    observed_at = ensure_utc(now or datetime.now(UTC))
    started = monotonic()
    canonical_site = normalize_site(site).value
    typed_site = cast(Literal["draftkings", "fanduel"], canonical_site)
    rules = load_fast_lane_rules(rules_path, at=observed_at)
    rule = rules.require_rule(
        trigger_source_class="official_inactive_list",
        claim_type="availability",
        at=observed_at,
    )
    if rule.max_automatic_adjustment.availability < 1:
        raise FastLaneCapError(
            f"rule {rule.rule_id!r} does not authorize a full unavailable status; "
            "a human must confirm the action"
        )
    if not text.strip():
        raise FastInactivesError("the official inactive list is empty")
    input_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()

    with connect_database(database) as connection:
        apply_migrations(connection)
        slate = _one_slate(connection, season=season, week=week, site=typed_site)
        base = _latest_decision(
            connection,
            slate=slate,
            before=observed_at,
            artifact_directory=artifact_directory,
        )
        roster = _slate_roster(connection, slate.slate_id, as_of=observed_at)
        inactive_players, unresolved = _resolve_inactives(
            connection,
            text=text,
            roster=roster,
            site=typed_site,
            observed_at=observed_at,
        )
        # Crosswalk review rows are useful even though the availability write is refused.
        connection.commit()
        if unresolved:
            detail = "\n".join(
                f"  na-crosswalk resolve --unresolved-id {queue_id} "
                f"--player-id <player_id>  # {name}"
                for name, queue_id in unresolved
            )
            raise FastInactivesError(
                f"{len(unresolved)} inactive name(s) are unresolved; the whole command "
                f"was refused and no availability row was written. Clear the unresolved "
                f"queue, then rerun:\n{detail}"
            )
        inactive_ids = {player.player_id for player in inactive_players}
        affected_indexes = tuple(
            index
            for index, lineup in enumerate(base.lineups)
            if any(player.player_id in inactive_ids for player in lineup.players)
        )
        if not affected_indexes:
            names = ", ".join(player.name for player in inactive_players)
            raise FastInactivesError(
                f"none of the frozen decision's {len(base.lineups)} lineups contains the "
                f"inactive player(s) {names}; no replacement upload is needed"
            )
        unaffected_indexes = tuple(
            index for index in range(len(base.lineups)) if index not in affected_indexes
        )
        affected = tuple(base.lineups[index] for index in affected_indexes)
        unaffected = tuple(base.lineups[index] for index in unaffected_indexes)
        entries = base.request.upload_entries
        # Pinned lineups come first in the new snapshot, so their entries do too.
        ordered_entries = (
            tuple(entries[index] for index in (*unaffected_indexes, *affected_indexes))
            if entries
            else ()
        )

        # The pasted list is an external input like any other: frozen and hashed before
        # anything is derived from it, so the availability rows' source hash names bytes.
        _capture_paste(
            snapshot_root,
            season=season,
            week=week,
            site=typed_site,
            text=text,
            observed_at=observed_at,
        )

        run_id = f"fast-inactives-{uuid4().hex}"
        capped = _CappedAdapter(
            adapter or PydfsAdapter(),
            prior_lineups=affected,
            pinned_count=len(unaffected),
            mean_cap=rule.max_automatic_adjustment.mean,
            rule=rule,
        )
        connection.execute("BEGIN IMMEDIATE")
        try:
            _start_run(connection, run_id=run_id, rules=rules, started_at=observed_at)
            _store_availability(
                connection,
                run_id=run_id,
                slate=slate,
                season=season,
                week=week,
                site=typed_site,
                players=inactive_players,
                observed_at=observed_at,
                input_sha256=input_sha256,
                rules=rules,
                rule=rule,
            )
            built = build_decision(
                database,
                slate_id=slate.slate_id,
                site=typed_site,
                decision_at=observed_at,
                artifact_directory=artifact_directory,
                number_of_lineups=len(base.lineups),
                contest_archetype=base.request.contest_archetype,
                pinned_lineups=unaffected,
                upload_entries=ordered_entries,
                lineup_uniqueness=base.request.lineup_uniqueness,
                run_type="fast_inactives_decision",
                note=(
                    f"na-fast official inactives; rule={rule.rule_id}; "
                    f"rules_version={rules.rules_version}; "
                    f"base={base.snapshot.decision_snapshot_id}; "
                    f"replaced={len(affected)} of {len(base.lineups)}"
                ),
                adapter=capped,
                connection=connection,
            )
            _mark_run(connection, run_id=run_id, status="succeeded", at=observed_at)
        except Exception as error:
            connection.rollback()
            # The refusal itself is a fact worth keeping — just not the availability
            # rows or the snapshot it refused to make.
            _record_refusal(connection, run_id=run_id, rules=rules, at=observed_at, error=error)
            raise
        connection.commit()

    mean_change = capped.mean_change
    if mean_change is None:
        raise FastInactivesError("the decision build returned without checking the mean cap")
    replacements = built.lineups[len(unaffected) :]
    diffs = _lineup_diffs(affected, replacements)
    return FastInactivesReport(
        run_id=run_id,
        season=season,
        week=week,
        site=typed_site,
        observed_at=observed_at,
        rule_id=rule.rule_id,
        rules_version=rules.rules_version,
        inactive_players=inactive_players,
        affected_lineups=len(affected),
        portfolio_lineups=len(base.lineups),
        mean_change=mean_change,
        decision_snapshot_id=built.snapshot.decision_snapshot_id,
        upload_csv_path=built.generated_lineups_path,
        diffs=diffs,
        elapsed_seconds=max(0.0, monotonic() - started),
    )


def _capture_paste(
    snapshot_root: Path,
    *,
    season: int,
    week: int,
    site: str,
    text: str,
    observed_at: datetime,
) -> Path:
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / f"official-inactives-{site}.txt"
        path.write_text(text, encoding="utf-8")
        return capture_files(
            snapshot_root,
            season,
            week,
            CaptureKind.INACTIVES,
            OFFICIAL_INACTIVES_SOURCE,
            (path,),
            observed_at=observed_at,
        )


def _one_slate(
    connection: sqlite3.Connection,
    *,
    season: int,
    week: int,
    site: str,
) -> SlateSummary:
    slates = list_slates(connection, season=season, week=week, site=site)
    if len(slates) != 1:
        ids = ", ".join(str(slate.slate_id) for slate in slates) or "none"
        raise FastInactivesError(
            f"expected exactly one {site} slate for {season} week {week}, found "
            f"{len(slates)} (slate ids: {ids})"
        )
    return slates[0]


def _latest_decision(
    connection: sqlite3.Connection,
    *,
    slate: SlateSummary,
    before: datetime,
    artifact_directory: Path,
) -> FrozenDecision:
    """The newest frozen decision, read from its verified artifacts — not replayed.

    A replay re-optimizes every lineup and is the one cost that grows with the
    portfolio; on a Sunday with 150 entries it would dominate the eight-minute budget.
    The reader checks every byte against the manifest instead.
    """

    row = connection.execute(
        "SELECT decision_snapshot_id, decision_at FROM decision_snapshots "
        "WHERE slate_id = ? AND rtrim(decision_at, 'Z') < rtrim(?, 'Z') "
        "ORDER BY rtrim(decision_at, 'Z') DESC, decision_snapshot_id DESC LIMIT 1",
        (slate.slate_id, utc_timestamp(before)),
    ).fetchone()
    if row is None:
        raise FastInactivesError(
            f"slate {slate.slate_id} has no earlier frozen decision to update; run "
            "`na-ops slate` first"
        )
    decision_at = ensure_utc(datetime.fromisoformat(str(row["decision_at"]).replace("Z", "+00:00")))
    return read_frozen_decision(
        connection,
        decision_snapshot_id=str(row["decision_snapshot_id"]),
        decision_at=decision_at,
        artifact_root=artifact_directory,
    )


def _slate_roster(
    connection: sqlite3.Connection,
    slate_id: int,
    *,
    as_of: datetime,
) -> tuple[_RosterIdentity, ...]:
    stamp = utc_timestamp(as_of)
    rows = connection.execute(
        """
        WITH ranked AS (
            SELECT s.*,
                   row_number() OVER (
                       PARTITION BY s.player_id
                       ORDER BY rtrim(s.observed_at, 'Z') DESC, s.salary_id DESC
                   ) AS version_rank
            FROM salaries AS s
            WHERE s.slate_id = ?
              AND rtrim(s.observed_at, 'Z') <= rtrim(?, 'Z')
              AND rtrim(s.valid_from, 'Z') <= rtrim(?, 'Z')
              AND (s.valid_to IS NULL OR rtrim(s.valid_to, 'Z') > rtrim(?, 'Z'))
        )
        SELECT r.player_id, p.canonical_name, p.position, t.abbreviation AS team
        FROM ranked AS r
        JOIN players AS p ON p.player_id = r.player_id
        JOIN teams AS t ON t.team_id = r.team_id
        WHERE r.version_rank = 1
        ORDER BY r.player_id
        """,
        (slate_id, stamp, stamp, stamp),
    ).fetchall()
    return tuple(
        _RosterIdentity(
            player_id=int(row["player_id"]),
            name=str(row["canonical_name"]),
            team=str(row["team"]),
            position=None if row["position"] is None else str(row["position"]),
        )
        for row in rows
    )


def _resolve_inactives(
    connection: sqlite3.Connection,
    *,
    text: str,
    roster: tuple[_RosterIdentity, ...],
    site: str,
    observed_at: datetime,
) -> tuple[tuple[InactivePlayer, ...], tuple[tuple[str, int], ...]]:
    raw_lines = tuple(_clean_line(line) for line in text.splitlines())
    lines = tuple(line for line in raw_lines if line and not line.startswith("#"))
    if not lines:
        raise FastInactivesError("the official inactive list contains no player lines")
    crosswalk = PlayerCrosswalk(connection)
    resolved: list[InactivePlayer] = []
    unresolved: list[tuple[str, int]] = []
    for line in lines:
        candidate = _roster_identity_for_line(line, roster)
        name = candidate.name if candidate is not None else _best_name_fragment(line, roster)
        team = candidate.team if candidate is not None else _team_fragment(line, roster) or "UNK"
        result = crosswalk.match(
            PlayerIdentityInput(
                source=OFFICIAL_INACTIVES_SOURCE,
                site=site,
                name_raw=name,
                team=team,
                position=None if candidate is None else candidate.position,
                roster_status="OUT",
                observed_at=observed_at,
                ingested_at=observed_at,
                run_id=None,
            )
        )
        if not result.matched:
            if result.unresolved_id is None:
                raise FastInactivesError(f"inactive name {name!r} did not enter the queue")
            unresolved.append((name, result.unresolved_id))
            continue
        if candidate is None or result.player_id != candidate.player_id:
            raise FastInactivesError(
                f"inactive name {name!r} resolved outside slate {site}; the whole command "
                "was refused"
            )
        resolved.append(InactivePlayer(candidate.player_id, candidate.name, candidate.team))
    ids = [player.player_id for player in resolved]
    if len(ids) != len(set(ids)):
        raise FastInactivesError("the official inactive list names the same player more than once")
    return tuple(resolved), tuple(unresolved)


_STATUS_WORDS = frozenset(
    {"OUT", "INACTIVE", "INACTIVES", "DNP", "QUESTIONABLE", "DOUBTFUL", "ACTIVE", "IR", "PUP"}
)


def _clean_line(line: str) -> str:
    """Reduce one pasted line to name and team: list markers, parentheses, and status go."""

    text = re.sub(r"^(?:[-*\u2022]\s*|\d+[.)]\s*)", "", line.strip()).strip()
    # "Josh Allen (BUF) — OUT (knee)": a spaced dash starts a note, never a name.
    text = re.sub(r"\s+[-\u2013\u2014]+\s+.*$", "", text)
    # "(BUF)" becomes ", BUF" so the team survives as a fragment of its own.
    text = re.sub(r"\s*\(([^()]*)\)", lambda match: f", {match.group(1)}", text)
    words = text.split()
    while words and words[-1].upper().strip(",;") in _STATUS_WORDS:
        words.pop()
    return " ".join(words).strip(" ,;")


def _roster_identity_for_line(
    line: str,
    roster: tuple[_RosterIdentity, ...],
) -> _RosterIdentity | None:
    fragments = _line_fragments(line)
    matches = {
        player.player_id: player
        for player in roster
        if any(normalize_name(fragment) == normalize_name(player.name) for fragment in fragments)
    }
    return next(iter(matches.values())) if len(matches) == 1 else None


def _line_fragments(line: str) -> tuple[str, ...]:
    pieces = [line]
    pieces.extend(part.strip() for part in re.split(r"[|,:\t]", line) if part.strip())
    expanded: list[str] = []
    for piece in pieces:
        words = piece.split()
        expanded.append(piece)
        if words and words[0].upper().rstrip(":") in _POSITION_CODES:
            expanded.append(" ".join(words[1:]))
        if len(words) > 1:
            expanded.extend((" ".join(words[1:]), " ".join(words[:-1])))
    return tuple(dict.fromkeys(fragment for fragment in expanded if fragment))


def _best_name_fragment(line: str, roster: tuple[_RosterIdentity, ...]) -> str:
    teams = {player.team.upper() for player in roster}
    for fragment in _line_fragments(line):
        if fragment.upper() not in teams and fragment.upper() not in _POSITION_CODES:
            return fragment
    return line


def _team_fragment(line: str, roster: tuple[_RosterIdentity, ...]) -> str | None:
    teams = {player.team.upper() for player in roster}
    return next(
        (fragment.upper() for fragment in _line_fragments(line) if fragment.upper() in teams),
        None,
    )


def _start_run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    rules: FastLaneRules,
    started_at: datetime,
) -> None:
    row = ModelRunRow(
        run_id=run_id,
        run_type="fast_official_inactives",
        started_at=started_at,
        completed_at=None,
        status="running",
        code_version=__version__,
        config_sha256=rules.rules_sha256,
        parent_run_id=None,
        error_message=None,
        created_at=started_at,
    )
    _insert(connection, "model_runs", row.db_values())


def _store_availability(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    slate: SlateSummary,
    season: int,
    week: int,
    site: Literal["draftkings", "fanduel"],
    players: tuple[InactivePlayer, ...],
    observed_at: datetime,
    input_sha256: str,
    rules: FastLaneRules,
    rule: FastLaneRule,
) -> None:
    for player in players:
        identity = json.dumps(
            {
                "input_sha256": input_sha256,
                "observed_at": utc_timestamp(observed_at),
                "player_id": player.player_id,
                "rule_id": rule.rule_id,
                "site": site,
                "slate_id": slate.slate_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        row = PlayerAvailabilityRow(
            availability_id=f"availability-{hashlib.sha256(identity).hexdigest()}",
            slate_id=slate.slate_id,
            player_id=player.player_id,
            season=season,
            week=week,
            site=site,
            availability_status="unavailable",
            rule_id=rule.rule_id,
            rules_version=rules.rules_version,
            source_file_sha256=input_sha256,
            source=OFFICIAL_INACTIVES_SOURCE,
            published_at=None,
            observed_at=observed_at,
            ingested_at=observed_at,
            effective_at=observed_at,
            valid_from=observed_at,
            valid_to=None,
            source_version=f"{rules.rules_version}:{rule.rule_id}:{input_sha256}",
            run_id=run_id,
        )
        _insert(connection, "player_availability", row.db_values())


def _mark_run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    status: str,
    at: datetime,
    error: Exception | None = None,
) -> None:
    cursor = connection.execute(
        "UPDATE model_runs SET completed_at = ?, status = ?, error_message = ? "
        "WHERE run_id = ? AND status = 'running'",
        (
            utc_timestamp(at),
            status,
            None if error is None else f"{type(error).__name__}: {error}"[:2000],
            run_id,
        ),
    )
    if cursor.rowcount != 1:
        raise FastInactivesError(f"could not mark fast run {run_id!r} {status}")


def _record_refusal(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    rules: FastLaneRules,
    at: datetime,
    error: Exception,
) -> None:
    """After a rollback, keep the refusal as a failed run and nothing else."""

    row = ModelRunRow(
        run_id=run_id,
        run_type="fast_official_inactives",
        started_at=at,
        completed_at=at,
        status="failed",
        code_version=__version__,
        config_sha256=rules.rules_sha256,
        parent_run_id=None,
        error_message=f"{type(error).__name__}: {error}"[:2000],
        created_at=at,
    )
    _insert(connection, "model_runs", row.db_values())
    connection.commit()


def _lineup_diffs(
    prior: tuple[Lineup, ...], replacement: tuple[Lineup, ...]
) -> tuple[LineupDiff, ...]:
    diffs: list[LineupDiff] = []
    for old, new in zip(prior, replacement, strict=True):
        old_names = {player.name for player in old.players}
        new_names = {player.name for player in new.players}
        diffs.append(
            LineupDiff(
                prior_lineup_id=old.lineup_id,
                replacement_lineup_id=new.lineup_id,
                out=tuple(sorted(old_names - new_names)),
                in_=tuple(sorted(new_names - old_names)),
            )
        )
    return tuple(diffs)


def _insert(
    connection: sqlite3.Connection,
    table: str,
    values: Mapping[str, object],
) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )


__all__ = [
    "FastInactivesError",
    "FastInactivesReport",
    "FastLaneCapError",
    "InactivePlayer",
    "LineupDiff",
    "process_official_inactives",
]
