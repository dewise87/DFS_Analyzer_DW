"""Confidence-gated canonical player identity matching."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import cast

from rapidfuzz import fuzz

from narrative_alpha.identity.models import (
    IdentityMatchResult,
    MatchCandidate,
    MatchMethod,
    PlayerIdentityInput,
)
from narrative_alpha.identity.normalization import (
    has_name_suffix,
    name_without_suffix,
    normalize_name,
    normalize_team_code,
    team_code_variants,
)
from narrative_alpha.store import UnresolvedPlayerMatchRow

DEFAULT_FUZZY_THRESHOLD = 0.92
DEFAULT_AMBIGUITY_MARGIN = 0.03
SUFFIX_MATCH_CONFIDENCE = 0.98
_SOURCE_VERSION = "identity-crosswalk-v1"


class CrosswalkError(RuntimeError):
    """Raised for an invalid or conflicting crosswalk operation."""


@dataclass(frozen=True)
class _PlayerCandidate:
    player_id: int
    canonical_name: str
    team: str
    position: str | None
    birth_date: date | None
    score: float = 0.0

    def public(self) -> MatchCandidate:
        return MatchCandidate(
            player_id=self.player_id,
            canonical_name=self.canonical_name,
            team=self.team,
            position=self.position,
            score=self.score,
        )


class PlayerCrosswalk:
    """Resolve source identities without permitting silent low-confidence matches."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
        ambiguity_margin: float = DEFAULT_AMBIGUITY_MARGIN,
    ) -> None:
        if not 0 <= fuzzy_threshold <= 1:
            raise ValueError("fuzzy_threshold must be between zero and one")
        if not 0 <= ambiguity_margin <= 1:
            raise ValueError("ambiguity_margin must be between zero and one")
        self.connection = connection
        self.fuzzy_threshold = fuzzy_threshold
        self.ambiguity_margin = ambiguity_margin

    def match(self, identity: PlayerIdentityInput) -> IdentityMatchResult:
        """Run the ordered match pipeline and queue any unresolved identity."""

        identity = identity.model_copy(update={"team": normalize_team_code(identity.team)})
        vendor_match = self._match_external_id(identity)
        if vendor_match is not None:
            return IdentityMatchResult(
                player_id=int(vendor_match["player_id"]),
                method=MatchMethod.EXACT_VENDOR_ID,
                confidence=float(vendor_match["match_confidence"]),
                manual_override=bool(vendor_match["manual_override"]),
            )

        candidates = self._candidate_pool(identity)
        normalized_input = normalize_name(identity.name_raw)

        exact = tuple(
            candidate
            for candidate in candidates
            if normalize_name(candidate.canonical_name) == normalized_input
        )
        if len(exact) == 1:
            return self._accept(identity, exact[0], MatchMethod.EXACT_NAME_TEAM, 1.0)
        if len(exact) > 1:
            return self._unresolved(identity, exact)

        aliases, manual_alias_ids = self._alias_candidates(
            identity, candidates, normalized_input
        )
        if len(aliases) == 1:
            return self._accept(
                identity,
                aliases[0],
                MatchMethod.DETERMINISTIC_ALIAS,
                1.0,
                manual_override=aliases[0].player_id in manual_alias_ids,
            )
        if len(aliases) > 1:
            return self._unresolved(identity, aliases)

        suffix_matches = self._suffix_tolerant_candidates(identity, candidates)
        if len(suffix_matches) == 1:
            return self._accept(
                identity,
                suffix_matches[0],
                MatchMethod.SUFFIX_TOLERANT_NAME,
                SUFFIX_MATCH_CONFIDENCE,
                persist_alias=False,
            )
        if len(suffix_matches) > 1:
            return self._unresolved(identity, suffix_matches)

        fuzzy_candidates = self._score_fuzzy_candidates(identity, candidates)
        input_has_position = identity.position is not None or bool(identity.eligible_positions)
        if input_has_position and fuzzy_candidates:
            top = fuzzy_candidates[0]
            runner_up_score = fuzzy_candidates[1].score if len(fuzzy_candidates) > 1 else 0.0
            if (
                top.score >= self.fuzzy_threshold
                and top.score - runner_up_score >= self.ambiguity_margin
            ):
                return self._accept(
                    identity, top, MatchMethod.FUZZY, top.score, persist_alias=False
                )
        return self._unresolved(identity, fuzzy_candidates)

    def list_unresolved(self) -> tuple[UnresolvedPlayerMatchRow, ...]:
        rows = self.connection.execute(
            """
            SELECT * FROM unresolved_player_matches
            WHERE status = 'pending'
            ORDER BY last_observed_at, unresolved_id
            """
        ).fetchall()
        return tuple(UnresolvedPlayerMatchRow.from_db(row) for row in rows)

    def require_all_resolved(self, *, site: str | None = None) -> None:
        """Fail closed before lineup generation while any relevant identity is pending."""

        if site is None:
            count = int(
                self.connection.execute(
                    "SELECT count(*) FROM unresolved_player_matches WHERE status = 'pending'"
                ).fetchone()[0]
            )
        else:
            count = int(
                self.connection.execute(
                    """
                    SELECT count(*) FROM unresolved_player_matches
                    WHERE status = 'pending' AND site = ?
                    """,
                    (_site(site),),
                ).fetchone()[0]
            )
        if count:
            scope = "all sites" if site is None else _site(site)
            raise CrosswalkError(
                f"{count} unresolved player identity match(es) remain for {scope}; "
                "lineup generation must stop"
            )

    def resolve(
        self,
        unresolved_id: int,
        player_id: int,
        *,
        note: str | None = None,
        resolved_at: datetime | None = None,
    ) -> IdentityMatchResult:
        """Persist a human decision as both an alias and external-ID mapping."""

        row = self.connection.execute(
            "SELECT * FROM unresolved_player_matches WHERE unresolved_id = ?",
            (unresolved_id,),
        ).fetchone()
        if row is None:
            raise CrosswalkError(f"unresolved match does not exist: {unresolved_id}")
        if str(row["status"]) != "pending":
            raise CrosswalkError(f"unresolved match {unresolved_id} is already {row['status']}")
        player = self.connection.execute(
            "SELECT player_id FROM players WHERE player_id = ?",
            (player_id,),
        ).fetchone()
        if player is None:
            raise CrosswalkError(f"canonical player does not exist: {player_id}")

        completed_at = _utc(resolved_at or datetime.now(UTC))
        identity = PlayerIdentityInput(
            source=str(row["source"]),
            site=None if row["site"] is None else str(row["site"]),
            external_player_id=(
                None if row["external_player_id"] is None else str(row["external_player_id"])
            ),
            name_raw=str(row["name_raw"]),
            team=normalize_team_code(str(row["team"])),
            opponent=None if row["opponent"] is None else str(row["opponent"]),
            position=None if row["position"] is None else str(row["position"]),
            roster_status=(None if row["roster_status"] is None else str(row["roster_status"])),
            birth_date=None if row["birth_date"] is None else date.fromisoformat(row["birth_date"]),
            eligible_positions=tuple(json.loads(str(row["eligible_positions_json"]))),
            observed_at=_parse_timestamp(str(row["last_observed_at"])),
            ingested_at=completed_at,
            source_file_sha256=(
                None if row["source_file_sha256"] is None else str(row["source_file_sha256"])
            ),
            run_id=None if row["run_id"] is None else str(row["run_id"]),
        )
        self._persist_mapping(
            identity,
            player_id,
            MatchMethod.MANUAL,
            1.0,
            manual=True,
            recorded_at=completed_at,
            valid_from=_parse_timestamp(str(row["first_observed_at"])),
        )
        self.connection.execute(
            """
            UPDATE unresolved_player_matches
            SET status = 'resolved', resolved_player_id = ?, resolved_at = ?,
                resolution_note = ?, match_method = ?, match_confidence = 1.0,
                manual_override = 1
            WHERE unresolved_id = ? AND status = 'pending'
            """,
            (player_id, _timestamp(completed_at), note, MatchMethod.MANUAL.value, unresolved_id),
        )
        return IdentityMatchResult(
            player_id=player_id,
            method=MatchMethod.MANUAL,
            confidence=1.0,
            manual_override=True,
        )

    def ignore(
        self,
        unresolved_id: int,
        *,
        note: str | None = None,
        resolved_at: datetime | None = None,
    ) -> None:
        completed_at = _utc(resolved_at or datetime.now(UTC))
        cursor = self.connection.execute(
            """
            UPDATE unresolved_player_matches
            SET status = 'ignored', resolved_at = ?, resolution_note = ?
            WHERE unresolved_id = ? AND status = 'pending'
            """,
            (_timestamp(completed_at), note, unresolved_id),
        )
        if cursor.rowcount != 1:
            raise CrosswalkError(f"pending unresolved match does not exist: {unresolved_id}")

    def _match_external_id(self, identity: PlayerIdentityInput) -> sqlite3.Row | None:
        if identity.external_player_id is None:
            return None
        observed_at = _timestamp(identity.observed_at)
        return cast(
            sqlite3.Row | None,
            self.connection.execute(
                """
            SELECT player_id, match_method, match_confidence, manual_override
            FROM external_player_ids
            WHERE source = ? AND site IS ? AND external_player_id = ?
              AND (
                  (manual_override = 1 AND valid_to IS NULL) OR
                  (valid_from <= ? AND (valid_to IS NULL OR valid_to > ?))
              )
            ORDER BY observed_at DESC, external_player_id_record_id DESC
            LIMIT 1
            """,
                (
                    _source(identity.source),
                    _site(identity.site),
                    identity.external_player_id,
                    observed_at,
                    observed_at,
                ),
            ).fetchone(),
        )

    def _candidate_pool(self, identity: PlayerIdentityInput) -> tuple[_PlayerCandidate, ...]:
        observed_at = _timestamp(identity.observed_at)
        variants = team_code_variants(identity.team)
        placeholders = ", ".join("?" for _ in variants)
        rows = self.connection.execute(
            f"""
            SELECT p.player_id, p.canonical_name, p.position AS player_position,
                   p.birth_date, h.team, h.position AS team_position
            FROM players AS p
            JOIN player_team_history AS h ON h.player_id = p.player_id
            WHERE upper(h.team) IN ({placeholders})
              AND p.observed_at <= ? AND p.valid_from <= ?
              AND (p.valid_to IS NULL OR p.valid_to > ?)
              AND h.observed_at <= ? AND h.valid_from <= ?
              AND (h.valid_to IS NULL OR h.valid_to > ?)
            ORDER BY p.player_id
            """,
            (
                *variants,
                observed_at,
                observed_at,
                observed_at,
                observed_at,
                observed_at,
                observed_at,
            ),
        ).fetchall()
        candidates: dict[int, _PlayerCandidate] = {}
        for row in rows:
            player_id = int(row["player_id"])
            candidates[player_id] = _PlayerCandidate(
                player_id=player_id,
                canonical_name=str(row["canonical_name"]),
                team=normalize_team_code(str(row["team"])),
                position=(
                    str(row["team_position"] or row["player_position"]).upper()
                    if row["team_position"] is not None or row["player_position"] is not None
                    else None
                ),
                birth_date=(
                    None
                    if row["birth_date"] is None
                    else date.fromisoformat(str(row["birth_date"]))
                ),
            )
        return tuple(candidates.values())

    def _alias_candidates(
        self,
        identity: PlayerIdentityInput,
        candidates: tuple[_PlayerCandidate, ...],
        normalized_input: str,
    ) -> tuple[tuple[_PlayerCandidate, ...], set[int]]:
        observed_at = _timestamp(identity.observed_at)
        rows = self.connection.execute(
            """
            SELECT player_id, manual_override
            FROM player_aliases
            WHERE source = ? AND normalized_alias = ?
              AND (
                  (manual_override = 1 AND valid_to IS NULL) OR
                  (valid_from <= ? AND (valid_to IS NULL OR valid_to > ?))
              )
            """,
            (_source(identity.source), normalized_input, observed_at, observed_at),
        ).fetchall()
        manual_ids = {int(row["player_id"]) for row in rows if bool(row["manual_override"])}
        alias_ids = manual_ids or {int(row["player_id"]) for row in rows}
        return (
            tuple(candidate for candidate in candidates if candidate.player_id in alias_ids),
            manual_ids,
        )

    def _suffix_tolerant_candidates(
        self,
        identity: PlayerIdentityInput,
        candidates: tuple[_PlayerCandidate, ...],
    ) -> tuple[_PlayerCandidate, ...]:
        """Match a suffixed name against its unsuffixed spelling, never two suffixes."""

        input_stripped = name_without_suffix(identity.name_raw)
        input_has_suffix = has_name_suffix(identity.name_raw)
        positions = {_position(value) for value in identity.eligible_positions}
        if identity.position is not None:
            positions.add(_position(identity.position))

        matches: list[_PlayerCandidate] = []
        for candidate in candidates:
            candidate_has_suffix = has_name_suffix(candidate.canonical_name)
            if input_has_suffix and candidate_has_suffix:
                # Two different suffixes are two different players; equal
                # suffixes were already handled by the exact-name stage.
                continue
            if not (input_has_suffix or candidate_has_suffix):
                continue
            if name_without_suffix(candidate.canonical_name) != input_stripped:
                continue
            if (
                positions
                and candidate.position is not None
                and _position(candidate.position) not in positions
            ):
                continue
            matches.append(candidate)
        return tuple(matches)

    def _score_fuzzy_candidates(
        self,
        identity: PlayerIdentityInput,
        candidates: tuple[_PlayerCandidate, ...],
    ) -> tuple[_PlayerCandidate, ...]:
        source_name = name_without_suffix(identity.name_raw)
        positions = {_position(value) for value in identity.eligible_positions}
        if identity.position is not None:
            positions.add(_position(identity.position))

        scored: list[_PlayerCandidate] = []
        for candidate in candidates:
            position_mismatch = (
                candidate.position is None or _position(candidate.position) not in positions
            )
            if positions and position_mismatch:
                continue
            if identity.birth_date is not None and candidate.birth_date != identity.birth_date:
                continue
            score = fuzz.ratio(source_name, name_without_suffix(candidate.canonical_name)) / 100.0
            scored.append(replace(candidate, score=score))
        return tuple(sorted(scored, key=lambda item: (-item.score, item.player_id)))

    def _accept(
        self,
        identity: PlayerIdentityInput,
        candidate: _PlayerCandidate,
        method: MatchMethod,
        confidence: float,
        *,
        persist_alias: bool = True,
        manual_override: bool = False,
    ) -> IdentityMatchResult:
        self._persist_mapping(
            identity,
            candidate.player_id,
            method,
            confidence,
            manual=False,
            persist_alias=persist_alias,
        )
        return IdentityMatchResult(
            player_id=candidate.player_id,
            method=method,
            confidence=confidence,
            manual_override=manual_override,
            candidates=(replace(candidate, score=confidence).public(),),
        )

    def _persist_mapping(
        self,
        identity: PlayerIdentityInput,
        player_id: int,
        method: MatchMethod,
        confidence: float,
        *,
        manual: bool,
        recorded_at: datetime | None = None,
        valid_from: datetime | None = None,
        persist_alias: bool = True,
    ) -> None:
        identity_observed_at = _timestamp(identity.observed_at)
        observed_at = _timestamp(recorded_at or identity.observed_at)
        ingested_at = _timestamp(identity.ingested_at or datetime.now(UTC))
        effective_at = _timestamp(valid_from or identity.observed_at)
        storage_valid_from = _timestamp(recorded_at or valid_from or identity.observed_at)
        team_code = normalize_team_code(identity.team)
        normalized_alias = normalize_name(identity.name_raw)
        variants = team_code_variants(identity.team)
        placeholders = ", ".join("?" for _ in variants)
        team_row = self.connection.execute(
            f"""
            SELECT team_id FROM teams
            WHERE upper(abbreviation) IN ({placeholders})
              AND observed_at <= ? AND valid_from <= ?
              AND (valid_to IS NULL OR valid_to > ?)
            ORDER BY observed_at DESC LIMIT 1
            """,
            (
                *variants,
                identity_observed_at,
                identity_observed_at,
                identity_observed_at,
            ),
        ).fetchone()
        team_id = None if team_row is None else int(team_row["team_id"])

        if manual:
            # The human decision wins: close conflicting active rows first,
            # scoped to the same (source, normalized_alias, team) identity so
            # identical vendor labels on other teams survive.
            self.connection.execute(
                """
                UPDATE player_aliases
                SET valid_to = ?
                WHERE source = ? AND normalized_alias = ? AND coalesce(team, '') = ?
                  AND valid_to IS NULL AND valid_from < ?
                """,
                (
                    storage_valid_from,
                    _source(identity.source),
                    normalized_alias,
                    team_code,
                    storage_valid_from,
                ),
            )
            if identity.external_player_id is not None:
                self.connection.execute(
                    """
                    UPDATE external_player_ids
                    SET valid_to = ?
                    WHERE source = ? AND site IS ? AND external_player_id = ?
                      AND valid_to IS NULL AND valid_from < ?
                    """,
                    (
                        storage_valid_from,
                        _source(identity.source),
                        _site(identity.site),
                        identity.external_player_id,
                        storage_valid_from,
                    ),
                )

        if persist_alias:
            active_alias_players = {
                int(row["player_id"])
                for row in self.connection.execute(
                    """
                    SELECT player_id FROM player_aliases
                    WHERE source = ? AND normalized_alias = ? AND coalesce(team, '') = ?
                      AND valid_to IS NULL
                    """,
                    (_source(identity.source), normalized_alias, team_code),
                ).fetchall()
            }
            conflicting = active_alias_players - {player_id}
            if conflicting:
                raise CrosswalkError(
                    f"active alias {normalized_alias!r} for source {identity.source!r} "
                    f"on team {team_code} already maps to player {sorted(conflicting)[0]}; "
                    f"refusing to also map player {player_id}"
                )
            if not active_alias_players:
                self.connection.execute(
                    """
                    INSERT INTO player_aliases(
                        player_id, team_id, team, alias, normalized_alias, match_method,
                        match_confidence, manual_override, source, published_at,
                        observed_at, ingested_at, effective_at, valid_from, valid_to,
                        source_version, run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        player_id,
                        team_id,
                        team_code,
                        identity.name_raw,
                        normalized_alias,
                        method.value,
                        confidence,
                        int(manual),
                        _source(identity.source),
                        observed_at,
                        ingested_at,
                        effective_at,
                        storage_valid_from,
                        _SOURCE_VERSION,
                        identity.run_id,
                    ),
                )
        if identity.external_player_id is not None:
            external_insert = (
                """
                INSERT INTO external_player_ids(
                    player_id, source, site, external_player_id, published_at,
                    observed_at, ingested_at, effective_at, valid_from, valid_to,
                    source_version, run_id, match_method, match_confidence, manual_override
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                """
                if manual
                else """
                INSERT OR IGNORE INTO external_player_ids(
                    player_id, source, site, external_player_id, published_at,
                    observed_at, ingested_at, effective_at, valid_from, valid_to,
                    source_version, run_id, match_method, match_confidence, manual_override
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                """
            )
            self.connection.execute(
                external_insert,
                (
                    player_id,
                    _source(identity.source),
                    _site(identity.site),
                    identity.external_player_id,
                    observed_at,
                    ingested_at,
                    effective_at,
                    storage_valid_from,
                    _SOURCE_VERSION,
                    identity.run_id,
                    method.value,
                    confidence,
                    int(manual),
                ),
            )

    def _unresolved(
        self,
        identity: PlayerIdentityInput,
        candidates: tuple[_PlayerCandidate, ...],
    ) -> IdentityMatchResult:
        public_candidates = tuple(candidate.public() for candidate in candidates[:10])
        identity_key = _identity_key(identity)
        observed_at = _timestamp(identity.observed_at)
        cursor = self.connection.execute(
            """
            INSERT INTO unresolved_player_matches(
                identity_key, source, site, external_player_id, name_raw,
                normalized_name, team, opponent, position, roster_status, birth_date,
                eligible_positions_json,
                candidates_json, source_file_sha256, first_observed_at,
                last_observed_at, occurrences, status, resolved_player_id,
                resolved_at, resolution_note, match_method, match_confidence,
                manual_override, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'pending',
                      NULL, NULL, NULL, NULL, NULL, 0, ?)
            ON CONFLICT(identity_key) DO UPDATE SET
                name_raw = excluded.name_raw,
                normalized_name = excluded.normalized_name,
                team = excluded.team,
                opponent = excluded.opponent,
                position = excluded.position,
                roster_status = excluded.roster_status,
                birth_date = excluded.birth_date,
                eligible_positions_json = excluded.eligible_positions_json,
                candidates_json = excluded.candidates_json,
                source_file_sha256 = excluded.source_file_sha256,
                last_observed_at = excluded.last_observed_at,
                occurrences = unresolved_player_matches.occurrences + 1,
                status = 'pending',
                resolved_player_id = NULL,
                resolved_at = NULL,
                resolution_note = NULL,
                match_method = NULL,
                match_confidence = NULL,
                manual_override = 0,
                run_id = excluded.run_id
            RETURNING unresolved_id
            """,
            (
                identity_key,
                _source(identity.source),
                _site(identity.site),
                identity.external_player_id,
                identity.name_raw,
                normalize_name(identity.name_raw),
                identity.team,
                identity.opponent,
                identity.position,
                identity.roster_status,
                None if identity.birth_date is None else identity.birth_date.isoformat(),
                json.dumps(identity.eligible_positions, separators=(",", ":")),
                json.dumps(
                    [candidate.model_dump(mode="json") for candidate in public_candidates],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                identity.source_file_sha256,
                observed_at,
                observed_at,
                identity.run_id,
            ),
        )
        unresolved_id = int(cursor.fetchone()[0])
        return IdentityMatchResult(
            player_id=None,
            method=None,
            confidence=None,
            unresolved_id=unresolved_id,
            candidates=public_candidates,
        )


def _identity_key(identity: PlayerIdentityInput) -> str:
    parts: tuple[str, ...]
    if identity.external_player_id is not None:
        parts = (_source(identity.source), _site(identity.site) or "", identity.external_player_id)
    else:
        parts = (
            _source(identity.source),
            _site(identity.site) or "",
            normalize_name(identity.name_raw),
            identity.team,
            identity.position or "",
        )
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _source(value: str) -> str:
    return value.strip().casefold()


def _site(value: str | None) -> str | None:
    return None if value is None else value.strip().casefold()


def _position(value: str) -> str:
    position = value.strip().upper()
    return "DST" if position in {"D", "DEF"} else position


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
