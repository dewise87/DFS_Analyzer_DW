"""Deterministic nearest-target grading and append-only source credibility snapshots.

The ledger is deliberately downstream of extraction and lineup construction. Nothing in
this module updates claims, source catalog grades, features, routing, or build inputs.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean
from typing import Any, Literal, cast
from uuid import uuid4

from scipy.stats import beta as beta_distribution  # type: ignore[import-untyped]

from narrative_alpha.grading.config import (
    DEFAULT_GRADING_CONFIG_PATH,
    AvailabilityRule,
    FieldPropagationRule,
    LoadedGradingConfig,
    UsageRule,
    load_grading_config,
)
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.store.models import (
    ClaimDimensionValue,
    ClaimDirectionValue,
    ClaimTypeValue,
)

GradeVerdict = Literal["correct", "incorrect", "ungradable", "indeterminate"]
_VERDICTS: tuple[GradeVerdict, ...] = (
    "correct",
    "incorrect",
    "ungradable",
    "indeterminate",
)


class GradingError(RuntimeError):
    """The stored inputs cannot be graded without inventing a fact."""


@dataclass(frozen=True)
class RuleVerdict:
    verdict: GradeVerdict
    reason: str
    outcome: dict[str, object]


@dataclass(frozen=True)
class ClaimGrade:
    claim_grade_id: str
    grading_run_id: str
    season: int
    week: int
    site: str
    slate_id: int
    player_id: int
    team: str
    claim_id: str
    source_id: str
    claim_type: ClaimTypeValue
    claim_dimension: ClaimDimensionValue
    claim_falsifiable: bool
    grade_target_key: str
    rule_id: str | None
    rule_sha256: str | None
    result_id: int | None
    availability_id: str | None
    actual_ownership_id: int | None
    ownership_baseline_id: int | None
    outcome: dict[str, object]
    verdict: GradeVerdict
    reason: str
    claim_observed_at: datetime
    slate_lock_at: datetime
    lead_time_minutes: float
    graded_at: datetime


@dataclass(frozen=True)
class GradeWeekReport:
    grading_run_id: str
    season: int
    week: int
    site: str
    graded_at: datetime
    grading_config_version: str
    grading_config_sha256: str
    claim_targets_seen: int
    grades_inserted: int
    ledger_rows_inserted: int
    # Claims left ungraded for a stated reason, counted so the step summary says so.
    claims_excluded_post_lock: int
    claims_excluded_stale: int
    verdict_counts: dict[GradeVerdict, int]
    by_claim_type: dict[str, dict[GradeVerdict, int]]


@dataclass(frozen=True)
class _ClaimTarget:
    claim_id: str
    source_id: str
    claim_type: ClaimTypeValue
    claim_dimension: ClaimDimensionValue
    outcome_direction: ClaimDirectionValue
    roster_behavior_direction: ClaimDirectionValue
    falsifiable: bool
    observed_at: datetime
    novelty: str
    slate_id: int
    slate_type: str
    season: int
    week: int
    site: str
    locks_at: datetime
    game_id: int
    player_id: int
    team: str
    result_id: int | None
    fantasy_points: float | None
    stat_line_json: object
    availability_id: str | None
    availability_status: str | None
    # The official row observed AFTER lock: the outcome. The pre-lock row above is an
    # input the claim may have been reacting to, and is never the ground truth.
    post_lock_availability_id: str | None
    post_lock_availability_status: str | None
    decision_snapshot_id: str | None
    decision_at: datetime | None


@dataclass(frozen=True)
class _OwnershipOutcome:
    target_key: str
    actual_ownership_id: int
    actual_ownership: float
    external_contest_id: str
    contest_archetype: str
    role: str
    ownership_baseline_id: int | None
    baseline_ownership: float | None


def grade_availability_claim(
    claimed_direction: ClaimDirectionValue,
    *,
    availability_status: str | None,
    stat_line: object,
    fantasy_points: float | None,
    pre_lock_status: str | None = None,
) -> RuleVerdict:
    """Grade active/inactive direction from post-lock facts only.

    ``availability_status`` is the official row observed after lock; ``pre_lock_status``
    is what was known before lock and is recorded as context, never used as the outcome.
    A pre-lock "available" that becomes a Sunday-morning scratch is exactly the case the
    ledger exists to score, and grading against the pre-lock row would invert it.
    """

    official: bool | None
    if availability_status == "available":
        official = True
    elif availability_status == "unavailable":
        official = False
    else:
        official = None

    played, activity_error = _played_fact(stat_line, fantasy_points)
    outcome: dict[str, object] = {
        "post_lock_official_availability": availability_status,
        "pre_lock_official_availability": pre_lock_status,
        "played": played,
        "fantasy_points": fantasy_points,
    }
    if activity_error is not None:
        outcome["activity_error"] = activity_error
        return RuleVerdict("indeterminate", activity_error, outcome)
    if official is not None and played is not None and official != played:
        return RuleVerdict(
            "indeterminate",
            "official availability and the result played/DNP fact conflict",
            outcome,
        )
    actual = played if played is not None else official
    if actual is None:
        return RuleVerdict(
            "indeterminate",
            "no post-lock official availability row and no explicit played/DNP fact exists; "
            "the pre-lock status is not an outcome",
            outcome,
        )
    if claimed_direction not in ("increase", "decrease"):
        return RuleVerdict(
            "indeterminate",
            f"active-status direction {claimed_direction!r} is not a testable binary prediction",
            outcome,
        )
    expected = claimed_direction == "increase"
    actual_label = "played/available" if actual else "DNP/unavailable"
    expected_label = "played/available" if expected else "DNP/unavailable"
    verdict: GradeVerdict = "correct" if actual == expected else "incorrect"
    return RuleVerdict(
        verdict,
        f"claim expected {expected_label}; outcome was {actual_label}",
        outcome | {"classified_active": actual},
    )


def grade_usage_claim(
    claimed_direction: ClaimDirectionValue,
    claim_dimension: ClaimDimensionValue,
    *,
    stat_line: object,
    rule: UsageRule,
) -> RuleVerdict:
    """Grade workload direction from a configured stat-line share and visible threshold."""

    dimension_rule = rule.dimensions.get(claim_dimension)
    if dimension_rule is None:
        return RuleVerdict(
            "ungradable",
            f"no usage threshold is configured for dimension {claim_dimension!r}",
            {},
        )
    parsed, error = _stat_line_object(stat_line)
    context: dict[str, object] = {
        "stat_key": dimension_rule.stat_key,
        "reference_key": dimension_rule.reference_key,
    }
    if error is not None or parsed is None or dimension_rule.stat_key not in parsed:
        # Not "cannot decide": the outcome data carries no workload fact at all. Today no
        # ingestion writes shares into the stat line; until one does, every usage claim
        # is honestly ungradable rather than quietly indeterminate.
        return RuleVerdict(
            "ungradable",
            f"no workload stat source: the result stat line carries no "
            f"{dimension_rule.stat_key!r} value",
            context,
        )
    value, value_error = _share_value(parsed[dimension_rule.stat_key], dimension_rule.stat_key)
    if value_error is not None:
        return RuleVerdict("indeterminate", value_error, context)
    assert value is not None
    reference_raw = parsed.get(dimension_rule.reference_key)
    if reference_raw is None:
        # A claim about *this player's* workload is graded against this player's own
        # reference, never a league constant: a starter's share exceeds any constant on
        # most Sundays, which would credit "he plays more" for being a starter (§5.9).
        return RuleVerdict(
            "ungradable",
            f"no per-player workload reference {dimension_rule.reference_key!r} at the "
            "decision; a league-wide constant is not a reference",
            context,
        )
    reference_value, reference_error = _share_value(reference_raw, dimension_rule.reference_key)
    if reference_error is not None:
        return RuleVerdict("indeterminate", reference_error, context)
    assert reference_value is not None
    reference = reference_value
    reference_source = "stat_line"
    delta = value - reference
    actual_direction = _direction_from_delta(delta, dimension_rule.direction_threshold)
    outcome = {
        "stat_key": dimension_rule.stat_key,
        "value": value,
        "reference_key": dimension_rule.reference_key,
        "reference": reference,
        "reference_source": reference_source,
        "delta": delta,
        "direction_threshold": dimension_rule.direction_threshold,
        "classified_direction": actual_direction,
    }
    if claimed_direction == "unknown":
        return RuleVerdict(
            "indeterminate",
            "claim direction is unknown, so it cannot be compared with workload",
            outcome,
        )
    verdict: GradeVerdict = "correct" if claimed_direction == actual_direction else "incorrect"
    return RuleVerdict(
        verdict,
        f"claim expected {claimed_direction}; workload classified {actual_direction}",
        outcome,
    )


def grade_ownership_claim(
    claimed_direction: ClaimDirectionValue,
    *,
    actual_ownership: float | None,
    baseline_ownership: float | None,
    neutral_threshold: float,
) -> RuleVerdict:
    """Grade roster-behavior direction from actual minus decision-time vendor ownership."""

    outcome: dict[str, object] = {
        "actual_ownership": actual_ownership,
        "baseline_ownership": baseline_ownership,
        "neutral_threshold": neutral_threshold,
    }
    for name, value in (
        ("actual ownership", actual_ownership),
        ("decision-time vendor baseline", baseline_ownership),
    ):
        if value is None:
            return RuleVerdict("indeterminate", f"{name} is missing", outcome)
        if not math.isfinite(value) or not 0 <= value <= 1:
            return RuleVerdict("indeterminate", f"{name} is not a fraction in [0, 1]", outcome)
    assert actual_ownership is not None and baseline_ownership is not None
    residual = actual_ownership - baseline_ownership
    actual_direction = _direction_from_delta(residual, neutral_threshold)
    outcome |= {
        "residual": residual,
        "classified_direction": actual_direction,
    }
    if claimed_direction == "unknown":
        return RuleVerdict(
            "indeterminate",
            "roster-behavior direction is unknown, so it cannot be compared with ownership",
            outcome,
        )
    verdict: GradeVerdict = "correct" if claimed_direction == actual_direction else "incorrect"
    return RuleVerdict(
        verdict,
        f"claim expected {claimed_direction}; ownership residual classified {actual_direction}",
        outcome,
    )


def grade_week(
    connection: sqlite3.Connection,
    *,
    season: int,
    week: int,
    site: str,
    grading_run_id: str,
    graded_at: datetime,
    config_path: Path = DEFAULT_GRADING_CONFIG_PATH,
) -> GradeWeekReport:
    """Append grades for one week/site and then append a cumulative ledger snapshot."""

    if season < 1 or not 1 <= week <= 99:
        raise ValueError("season and week must be positive NFL identifiers")
    canonical_site = _site(site)
    at = ensure_utc(graded_at)
    loaded = load_grading_config(config_path)
    selection = _claim_targets(
        connection,
        season=season,
        week=week,
        site=canonical_site,
        graded_at=at,
        lookback_days=loaded.config.claim_lookback_days,
    )
    targets = selection.targets
    grades: list[ClaimGrade] = []
    emitted: set[tuple[str, str]] = set()
    for target in _deduplication_order(targets):
        for grade in _grade_target(connection, target, loaded, grading_run_id, at):
            key = (grade.claim_id, grade.grade_target_key)
            if key in emitted:  # the same outcome reached by a second slate of one game
                continue
            emitted.add(key)
            grades.append(grade)

    connection.execute("SAVEPOINT claim_grading")
    inserted = 0
    try:
        for grade in grades:
            inserted += _insert_grade(connection, grade, loaded)
        ledger_rows = _refresh_ledger(
            connection,
            grading_run_id=grading_run_id,
            season=season,
            week=week,
            as_of=at,
            loaded=loaded,
        )
        connection.execute("RELEASE SAVEPOINT claim_grading")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT claim_grading")
        connection.execute("RELEASE SAVEPOINT claim_grading")
        raise

    verdict_counts = Counter(grade.verdict for grade in grades)
    by_type: dict[str, Counter[GradeVerdict]] = defaultdict(Counter)
    for grade in grades:
        by_type[grade.claim_type][grade.verdict] += 1
    return GradeWeekReport(
        grading_run_id=grading_run_id,
        season=season,
        week=week,
        site=canonical_site,
        graded_at=at,
        grading_config_version=loaded.config.config_version,
        grading_config_sha256=loaded.sha256,
        claim_targets_seen=len(targets),
        grades_inserted=inserted,
        ledger_rows_inserted=ledger_rows,
        claims_excluded_post_lock=selection.excluded_post_lock,
        claims_excluded_stale=selection.excluded_stale,
        verdict_counts={verdict: verdict_counts[verdict] for verdict in _VERDICTS},
        by_claim_type={
            claim_type: {verdict: counts[verdict] for verdict in _VERDICTS}
            for claim_type, counts in sorted(by_type.items())
        },
    )


def _grade_target(
    connection: sqlite3.Connection,
    target: _ClaimTarget,
    loaded: LoadedGradingConfig,
    grading_run_id: str,
    graded_at: datetime,
) -> tuple[ClaimGrade, ...]:
    rule = loaded.rule_for(target.claim_type, target.claim_dimension)
    if not target.falsifiable:
        return (
            _make_grade(
                target,
                grading_run_id=grading_run_id,
                graded_at=graded_at,
                target_key=_player_target_key(target),
                rule=None,
                rule_sha256=None,
                verdict=RuleVerdict(
                    "ungradable",
                    "claim is marked falsifiable=false; later player performance is ignored",
                    {},
                ),
            ),
        )
    if rule is None:
        return (
            _make_grade(
                target,
                grading_run_id=grading_run_id,
                graded_at=graded_at,
                target_key=_player_target_key(target),
                rule=None,
                rule_sha256=None,
                verdict=RuleVerdict(
                    "ungradable",
                    f"no configured rule for claim_type={target.claim_type!r}, "
                    f"claim_dimension={target.claim_dimension!r}",
                    {},
                ),
            ),
        )
    rule_sha256 = loaded.rule_sha256(rule)
    if isinstance(rule, AvailabilityRule):
        verdict = grade_availability_claim(
            target.outcome_direction,
            availability_status=target.post_lock_availability_status,
            stat_line=target.stat_line_json,
            fantasy_points=target.fantasy_points,
            pre_lock_status=target.availability_status,
        )
        return (
            _make_grade(
                target,
                grading_run_id=grading_run_id,
                graded_at=graded_at,
                target_key=_player_target_key(target),
                rule=rule,
                rule_sha256=rule_sha256,
                verdict=verdict,
            ),
        )
    if isinstance(rule, UsageRule):
        verdict = grade_usage_claim(
            target.outcome_direction,
            target.claim_dimension,
            stat_line=target.stat_line_json,
            rule=rule,
        )
        return (
            _make_grade(
                target,
                grading_run_id=grading_run_id,
                graded_at=graded_at,
                target_key=_player_target_key(target),
                rule=rule,
                rule_sha256=rule_sha256,
                verdict=verdict,
            ),
        )

    outcomes = _ownership_outcomes(connection, target, graded_at=graded_at)
    if not outcomes:
        threshold = (
            rule.showdown_neutral_threshold
            if target.slate_type == "showdown"
            else rule.classic_neutral_threshold
        )
        verdict = grade_ownership_claim(
            cast(ClaimDirectionValue, getattr(target, rule.direction_field)),
            actual_ownership=None,
            baseline_ownership=None,
            neutral_threshold=threshold,
        )
        return (
            _make_grade(
                target,
                grading_run_id=grading_run_id,
                graded_at=graded_at,
                target_key=_player_target_key(target) + ":ownership-missing",
                rule=rule,
                rule_sha256=rule_sha256,
                verdict=verdict,
                result_id=None,
            ),
        )
    grades: list[ClaimGrade] = []
    for outcome in outcomes:
        threshold = (
            rule.showdown_neutral_threshold
            if target.slate_type == "showdown"
            else rule.classic_neutral_threshold
        )
        verdict = grade_ownership_claim(
            cast(ClaimDirectionValue, getattr(target, rule.direction_field)),
            actual_ownership=outcome.actual_ownership,
            baseline_ownership=outcome.baseline_ownership,
            neutral_threshold=threshold,
        )
        verdict = RuleVerdict(
            verdict.verdict,
            verdict.reason,
            verdict.outcome
            | {
                "external_contest_id": outcome.external_contest_id,
                "contest_archetype": outcome.contest_archetype,
                "role": outcome.role,
                "decision_snapshot_id": target.decision_snapshot_id,
                "decision_at": (
                    None if target.decision_at is None else utc_timestamp(target.decision_at)
                ),
            },
        )
        grades.append(
            _make_grade(
                target,
                grading_run_id=grading_run_id,
                graded_at=graded_at,
                target_key=outcome.target_key,
                rule=rule,
                rule_sha256=rule_sha256,
                verdict=verdict,
                result_id=None,
                actual_ownership_id=outcome.actual_ownership_id,
                ownership_baseline_id=outcome.ownership_baseline_id,
            )
        )
    return tuple(grades)


def _make_grade(
    target: _ClaimTarget,
    *,
    grading_run_id: str,
    graded_at: datetime,
    target_key: str,
    rule: AvailabilityRule | UsageRule | FieldPropagationRule | None,
    rule_sha256: str | None,
    verdict: RuleVerdict,
    result_id: int | Literal[False] | None = False,
    actual_ownership_id: int | None = None,
    ownership_baseline_id: int | None = None,
) -> ClaimGrade:
    lead = (target.locks_at - target.observed_at).total_seconds() / 60.0
    if lead < 0:  # the selector should make this impossible
        raise GradingError(f"claim {target.claim_id} was selected after slate lock")
    if graded_at < target.locks_at:
        raise GradingError(f"claim {target.claim_id} cannot be graded before slate lock")
    selected_result_id = target.result_id if result_id is False else result_id
    availability_id = (
        target.post_lock_availability_id if isinstance(rule, AvailabilityRule) else None
    )
    # The id is the grade's content, so an identical regrade on a later Tuesday is the
    # same row (INSERT OR IGNORE) and only a changed verdict, rule, or outcome appends.
    identity = json.dumps(
        {
            "claim_id": target.claim_id,
            "target": target_key,
            "rule_sha256": rule_sha256,
            "verdict": verdict.verdict,
            "outcome": verdict.outcome,
            "result_id": selected_result_id,
            "availability_id": availability_id,
            "actual_ownership_id": actual_ownership_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return ClaimGrade(
        claim_grade_id=f"claim-grade-{hashlib.sha256(identity).hexdigest()}",
        grading_run_id=grading_run_id,
        season=target.season,
        week=target.week,
        site=target.site,
        slate_id=target.slate_id,
        player_id=target.player_id,
        team=target.team,
        claim_id=target.claim_id,
        source_id=target.source_id,
        claim_type=target.claim_type,
        claim_dimension=target.claim_dimension,
        claim_falsifiable=target.falsifiable,
        grade_target_key=target_key,
        rule_id=None if rule is None else rule.rule_id,
        rule_sha256=rule_sha256,
        result_id=selected_result_id,
        availability_id=availability_id,
        actual_ownership_id=actual_ownership_id,
        ownership_baseline_id=ownership_baseline_id,
        outcome=verdict.outcome,
        verdict=verdict.verdict,
        reason=verdict.reason,
        claim_observed_at=target.observed_at,
        slate_lock_at=target.locks_at,
        lead_time_minutes=lead,
        graded_at=graded_at,
    )


def _insert_grade(
    connection: sqlite3.Connection,
    grade: ClaimGrade,
    loaded: LoadedGradingConfig,
) -> int:
    """Insert one grade; 0 when an identical grade already exists (a repeat regrade)."""

    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO claim_grades(
            claim_grade_id, grading_run_id, season, week, site, slate_id, player_id,
            team, claim_id, source_id, claim_type, claim_dimension, claim_falsifiable,
            grade_target_key, rule_id, rule_sha256, grading_config_version,
            grading_config_sha256, result_id, availability_id, actual_ownership_id,
            ownership_baseline_id, outcome_json, verdict, reason, claim_observed_at,
            slate_lock_at, lead_time_minutes, graded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            grade.claim_grade_id,
            grade.grading_run_id,
            grade.season,
            grade.week,
            grade.site,
            grade.slate_id,
            grade.player_id,
            grade.team,
            grade.claim_id,
            grade.source_id,
            grade.claim_type,
            grade.claim_dimension,
            int(grade.claim_falsifiable),
            grade.grade_target_key,
            grade.rule_id,
            grade.rule_sha256,
            loaded.config.config_version,
            loaded.sha256,
            grade.result_id,
            grade.availability_id,
            grade.actual_ownership_id,
            grade.ownership_baseline_id,
            json.dumps(grade.outcome, sort_keys=True, separators=(",", ":")),
            grade.verdict,
            grade.reason,
            utc_timestamp(grade.claim_observed_at),
            utc_timestamp(grade.slate_lock_at),
            grade.lead_time_minutes,
            utc_timestamp(grade.graded_at),
        ),
    )
    return int(cursor.rowcount)


def _refresh_ledger(
    connection: sqlite3.Connection,
    *,
    grading_run_id: str,
    season: int,
    week: int,
    as_of: datetime,
    loaded: LoadedGradingConfig,
) -> int:
    rows = connection.execute(
        """
        WITH ranked AS (
            SELECT grade.*, claim.novelty,
                   row_number() OVER (
                       PARTITION BY grade.claim_id, grade.grade_target_key
                       ORDER BY rtrim(grade.graded_at, 'Z') DESC,
                                grade.rowid DESC
                   ) AS grade_rank
            FROM claim_grades AS grade
            JOIN claims AS claim ON claim.claim_id = grade.claim_id
            WHERE grade.season = ? AND grade.week <= ?
              AND rtrim(grade.graded_at, 'Z') <= rtrim(?, 'Z')
        )
        SELECT * FROM ranked
        WHERE grade_rank = 1
        ORDER BY source_id, team, claim_type, claim_dimension,
                 claim_observed_at, claim_id, grade_target_key
        """,
        (season, week, utc_timestamp(as_of)),
    ).fetchall()
    grouped: dict[tuple[str, str, str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[
            (
                str(row["source_id"]),
                str(row["team"]),
                str(row["claim_type"]),
                str(row["claim_dimension"]),
            )
        ].append(row)

    inserted = 0
    prior_alpha = loaded.config.beta_prior_alpha
    prior_beta = loaded.config.beta_prior_beta
    interval_mass = loaded.config.posterior_interval_mass
    half_life = loaded.config.decay_half_life_days
    for (source_id, team, claim_type, claim_dimension), group in sorted(grouped.items()):
        counts = Counter(str(row["verdict"]) for row in group)
        correct = counts["correct"]
        incorrect = counts["incorrect"]
        n_graded = correct + incorrect
        total = len(group)
        observed = tuple(_parse_timestamp(str(row["claim_observed_at"])) for row in group)
        # Time decay enters the posterior as weighted counts: a claim graded correct a
        # half-life ago counts half. The raw counts stay beside it so the report can show
        # both the honest n and the decayed estimate.
        weighted_correct = math.fsum(
            _decay(as_of, _parse_timestamp(str(row["claim_observed_at"])), half_life)
            for row in group
            if row["verdict"] == "correct"
        )
        weighted_incorrect = math.fsum(
            _decay(as_of, _parse_timestamp(str(row["claim_observed_at"])), half_life)
            for row in group
            if row["verdict"] == "incorrect"
        )
        decay_weight = weighted_correct + weighted_incorrect
        posterior_mean, interval_low, interval_high = posterior_from_weights(
            prior_alpha,
            prior_beta,
            weighted_correct,
            weighted_incorrect,
            interval_mass=interval_mass,
        )
        connection.execute(
            """
            INSERT INTO source_credibility(
                source_credibility_id, grading_run_id, season, week, source_id, team,
                claim_type, claim_dimension, as_of_at, n_graded, correct_count,
                incorrect_count, indeterminate_count, ungradable_count, beta_prior_alpha,
                beta_prior_beta, accuracy_posterior_mean, accuracy_interval_low,
                accuracy_interval_high, posterior_interval_mass, precision, coverage,
                average_lead_time_minutes, correction_rate, last_claim_at, decay_weight,
                decay_half_life_days, grading_config_version, grading_config_sha256,
                weighted_correct, weighted_incorrect
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"source-credibility-{uuid4().hex}",
                grading_run_id,
                season,
                week,
                source_id,
                team,
                claim_type,
                claim_dimension,
                utc_timestamp(as_of),
                n_graded,
                correct,
                incorrect,
                counts["indeterminate"],
                counts["ungradable"],
                prior_alpha,
                prior_beta,
                posterior_mean,
                interval_low,
                interval_high,
                interval_mass,
                None if n_graded == 0 else correct / n_graded,
                n_graded / total,
                fmean(float(row["lead_time_minutes"]) for row in group),
                sum(str(row["novelty"]) == "contradicting" for row in group) / total,
                utc_timestamp(max(observed)),
                decay_weight,
                half_life,
                loaded.config.config_version,
                loaded.sha256,
                weighted_correct,
                weighted_incorrect,
            ),
        )
        inserted += 1
    return inserted


def _decay(as_of: datetime, observed_at: datetime, half_life_days: float) -> float:
    age_days = max(0.0, (as_of - observed_at).total_seconds()) / 86400.0
    return float(0.5 ** (age_days / half_life_days))


def posterior_from_weights(
    prior_alpha: float,
    prior_beta: float,
    weighted_correct: float,
    weighted_incorrect: float,
    *,
    interval_mass: float,
) -> tuple[float, float, float]:
    """Beta posterior mean and central interval from (decay-weighted) counts."""

    alpha = prior_alpha + weighted_correct
    beta = prior_beta + weighted_incorrect
    tail = (1.0 - interval_mass) / 2.0
    return (
        alpha / (alpha + beta),
        float(beta_distribution.ppf(tail, alpha, beta)),
        float(beta_distribution.ppf(1.0 - tail, alpha, beta)),
    )


def _claim_targets(
    connection: sqlite3.Connection,
    *,
    season: int,
    week: int,
    site: str,
    graded_at: datetime,
    lookback_days: int,
) -> _TargetSelection:
    rows = connection.execute(
        """
        WITH ranked_salaries AS (
            SELECT salary.*, slate.slate_type, slate.season, slate.week, slate.site,
                   slate.locks_at, team.abbreviation AS team,
                   row_number() OVER (
                       PARTITION BY salary.slate_id, salary.player_id
                       ORDER BY rtrim(salary.observed_at, 'Z') DESC, salary.salary_id DESC
                   ) AS salary_rank
            FROM salaries AS salary
            JOIN slates AS slate ON slate.slate_id = salary.slate_id
            JOIN teams AS team ON team.team_id = salary.team_id
            WHERE slate.season = ? AND slate.week = ? AND slate.site = ?
              AND rtrim(salary.observed_at, 'Z') <= rtrim(slate.locks_at, 'Z')
              AND rtrim(salary.valid_from, 'Z') <= rtrim(slate.locks_at, 'Z')
              AND (
                  salary.valid_to IS NULL
                  OR rtrim(salary.valid_to, 'Z') > rtrim(slate.locks_at, 'Z')
              )
        )
        SELECT claim.claim_id, claim.source AS source_id, claim.claim_type,
               claim.claim_dimension, claim.outcome_direction,
               claim.roster_behavior_direction, claim.falsifiable, claim.observed_at,
               claim.novelty, salary.slate_id, salary.slate_type, salary.season,
               salary.week, salary.site, salary.locks_at, salary.game_id, ref.player_id,
               salary.team,
               result.result_id, result.fantasy_points, result.stat_line_json,
               availability.availability_id, availability.availability_status,
               post_lock.availability_id AS post_lock_availability_id,
               post_lock.availability_status AS post_lock_availability_status,
               decision.decision_snapshot_id, decision.decision_at
        FROM claims AS claim
        JOIN claim_player_refs AS ref ON ref.claim_id = claim.claim_id
        JOIN ranked_salaries AS salary
          ON salary.player_id = ref.player_id AND salary.salary_rank = 1
        LEFT JOIN results AS result ON result.result_id = (
            SELECT candidate.result_id
            FROM results AS candidate
            WHERE candidate.player_id = ref.player_id
              AND candidate.game_id = salary.game_id
              AND candidate.site = salary.site
              AND rtrim(candidate.observed_at, 'Z') <= rtrim(?, 'Z')
              AND rtrim(candidate.ingested_at, 'Z') <= rtrim(?, 'Z')
              AND rtrim(candidate.valid_from, 'Z') <= rtrim(?, 'Z')
              AND (
                  candidate.valid_to IS NULL
                  OR rtrim(candidate.valid_to, 'Z') > rtrim(?, 'Z')
              )
            ORDER BY rtrim(candidate.observed_at, 'Z') DESC,
                     rtrim(candidate.ingested_at, 'Z') DESC, candidate.result_id DESC
            LIMIT 1
        )
        LEFT JOIN player_availability AS availability ON availability.availability_id = (
            SELECT candidate.availability_id
            FROM player_availability AS candidate
            WHERE candidate.slate_id = salary.slate_id
              AND candidate.player_id = ref.player_id
              AND candidate.site = salary.site
              AND rtrim(candidate.observed_at, 'Z') <= rtrim(salary.locks_at, 'Z')
              AND rtrim(candidate.ingested_at, 'Z') <= rtrim(?, 'Z')
              AND rtrim(candidate.valid_from, 'Z') <= rtrim(salary.locks_at, 'Z')
              AND (
                  candidate.valid_to IS NULL
                  OR rtrim(candidate.valid_to, 'Z') > rtrim(salary.locks_at, 'Z')
              )
            ORDER BY rtrim(candidate.observed_at, 'Z') DESC,
                     rtrim(candidate.ingested_at, 'Z') DESC, candidate.availability_id DESC
            LIMIT 1
        )
        LEFT JOIN player_availability AS post_lock ON post_lock.availability_id = (
            SELECT candidate.availability_id
            FROM player_availability AS candidate
            WHERE candidate.slate_id = salary.slate_id
              AND candidate.player_id = ref.player_id
              AND candidate.site = salary.site
              AND rtrim(candidate.observed_at, 'Z') > rtrim(salary.locks_at, 'Z')
              AND rtrim(candidate.observed_at, 'Z') <= rtrim(?, 'Z')
              AND rtrim(candidate.ingested_at, 'Z') <= rtrim(?, 'Z')
            ORDER BY rtrim(candidate.observed_at, 'Z') DESC,
                     rtrim(candidate.ingested_at, 'Z') DESC, candidate.availability_id DESC
            LIMIT 1
        )
        LEFT JOIN decision_snapshots AS decision ON decision.decision_snapshot_id = (
            SELECT candidate.decision_snapshot_id
            FROM decision_snapshots AS candidate
            WHERE candidate.slate_id = salary.slate_id
              AND rtrim(candidate.decision_at, 'Z') <= rtrim(salary.locks_at, 'Z')
              AND rtrim(candidate.created_at, 'Z') <= rtrim(?, 'Z')
            ORDER BY rtrim(candidate.decision_at, 'Z') DESC,
                     candidate.decision_snapshot_id DESC
            LIMIT 1
        )
        WHERE ref.player_id IS NOT NULL
          AND rtrim(claim.observed_at, 'Z') <= rtrim(?, 'Z')
        ORDER BY salary.slate_id, ref.player_id, claim.observed_at, claim.claim_id
        """,
        (
            season,
            week,
            site,
            *([utc_timestamp(graded_at)] * 9),
        ),
    ).fetchall()
    targets: list[_ClaimTarget] = []
    seen: set[tuple[str, int, int]] = set()
    excluded_post_lock: set[str] = set()
    excluded_stale: set[str] = set()
    for row in rows:
        observed_at = _parse_timestamp(str(row["observed_at"]))
        locks_at = _parse_timestamp(str(row["locks_at"]))
        if observed_at > locks_at:
            # Observed after lock: not a prediction, so not graded as one — and counted.
            excluded_post_lock.add(str(row["claim_id"]))
            continue
        if observed_at < locks_at - timedelta(days=lookback_days):
            excluded_stale.add(str(row["claim_id"]))
            continue
        has_actual = connection.execute(
            """
            SELECT 1 FROM actual_ownership
            WHERE slate_id = ? AND player_id = ? AND site = ?
              AND rtrim(observed_at, 'Z') <= rtrim(?, 'Z')
              AND rtrim(ingested_at, 'Z') <= rtrim(?, 'Z')
            LIMIT 1
            """,
            (
                int(row["slate_id"]),
                int(row["player_id"]),
                site,
                utc_timestamp(graded_at),
                utc_timestamp(graded_at),
            ),
        ).fetchone()
        if row["result_id"] is None and row["availability_id"] is None and has_actual is None:
            continue
        identity = (str(row["claim_id"]), int(row["slate_id"]), int(row["player_id"]))
        if identity in seen:
            continue
        seen.add(identity)
        targets.append(
            _ClaimTarget(
                claim_id=str(row["claim_id"]),
                source_id=str(row["source_id"]),
                claim_type=cast(ClaimTypeValue, str(row["claim_type"])),
                claim_dimension=cast(ClaimDimensionValue, str(row["claim_dimension"])),
                outcome_direction=cast(ClaimDirectionValue, str(row["outcome_direction"])),
                roster_behavior_direction=cast(
                    ClaimDirectionValue, str(row["roster_behavior_direction"])
                ),
                falsifiable=bool(row["falsifiable"]),
                observed_at=observed_at,
                novelty=str(row["novelty"]),
                slate_id=int(row["slate_id"]),
                slate_type=str(row["slate_type"]),
                season=int(row["season"]),
                week=int(row["week"]),
                site=str(row["site"]),
                locks_at=locks_at,
                game_id=int(row["game_id"]),
                player_id=int(row["player_id"]),
                team=str(row["team"]),
                result_id=None if row["result_id"] is None else int(row["result_id"]),
                fantasy_points=(
                    None if row["fantasy_points"] is None else float(row["fantasy_points"])
                ),
                stat_line_json=row["stat_line_json"],
                availability_id=(
                    None if row["availability_id"] is None else str(row["availability_id"])
                ),
                availability_status=(
                    None if row["availability_status"] is None else str(row["availability_status"])
                ),
                post_lock_availability_id=(
                    None
                    if row["post_lock_availability_id"] is None
                    else str(row["post_lock_availability_id"])
                ),
                post_lock_availability_status=(
                    None
                    if row["post_lock_availability_status"] is None
                    else str(row["post_lock_availability_status"])
                ),
                decision_snapshot_id=(
                    None
                    if row["decision_snapshot_id"] is None
                    else str(row["decision_snapshot_id"])
                ),
                decision_at=(
                    None
                    if row["decision_at"] is None
                    else _parse_timestamp(str(row["decision_at"]))
                ),
            )
        )
    return _TargetSelection(
        targets=tuple(targets),
        excluded_post_lock=len(excluded_post_lock),
        excluded_stale=len(excluded_stale),
    )


@dataclass(frozen=True)
class _TargetSelection:
    targets: tuple[_ClaimTarget, ...]
    excluded_post_lock: int
    excluded_stale: int


def _ownership_outcomes(
    connection: sqlite3.Connection,
    target: _ClaimTarget,
    *,
    graded_at: datetime,
) -> tuple[_OwnershipOutcome, ...]:
    rows = connection.execute(
        """
        WITH ranked AS (
            SELECT actual.*,
                   row_number() OVER (
                       PARTITION BY actual.external_contest_id, actual.site,
                                    actual.slate_id, actual.player_id, actual.role
                       ORDER BY rtrim(actual.observed_at, 'Z') DESC,
                                rtrim(actual.ingested_at, 'Z') DESC,
                                actual.actual_ownership_id DESC
                   ) AS actual_rank
            FROM actual_ownership AS actual
            WHERE actual.slate_id = ? AND actual.player_id = ? AND actual.site = ?
              AND rtrim(actual.observed_at, 'Z') <= rtrim(?, 'Z')
              AND rtrim(actual.ingested_at, 'Z') <= rtrim(?, 'Z')
              AND rtrim(actual.valid_from, 'Z') <= rtrim(?, 'Z')
              AND (
                  actual.valid_to IS NULL OR rtrim(actual.valid_to, 'Z') > rtrim(?, 'Z')
              )
        )
        SELECT * FROM ranked WHERE actual_rank = 1
        ORDER BY external_contest_id, role, actual_ownership_id
        """,
        (
            target.slate_id,
            target.player_id,
            target.site,
            *([utc_timestamp(graded_at)] * 4),
        ),
    ).fetchall()
    outcomes: list[_OwnershipOutcome] = []
    for row in rows:
        baseline = None
        if target.decision_at is not None:
            baseline = connection.execute(
                """
                SELECT ownership_baseline_id, ownership
                FROM ownership_baselines
                WHERE slate_id = ? AND player_id = ? AND site = ? AND role = ?
                  AND rtrim(observed_at, 'Z') <= rtrim(?, 'Z')
                  AND rtrim(ingested_at, 'Z') <= rtrim(?, 'Z')
                  AND rtrim(valid_from, 'Z') <= rtrim(?, 'Z')
                  AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(?, 'Z'))
                ORDER BY rtrim(observed_at, 'Z') DESC, rtrim(ingested_at, 'Z') DESC,
                         ownership_baseline_id DESC
                LIMIT 1
                """,
                (
                    target.slate_id,
                    target.player_id,
                    target.site,
                    str(row["role"]),
                    *([utc_timestamp(target.decision_at)] * 4),
                ),
            ).fetchone()
        external_contest_id = str(row["external_contest_id"])
        role = str(row["role"])
        target_key = json.dumps(
            {
                "external_contest_id": external_contest_id,
                "player_id": target.player_id,
                "role": role,
                "site": target.site,
                "slate_id": target.slate_id,
                "target": "ownership",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        outcomes.append(
            _OwnershipOutcome(
                target_key=target_key,
                actual_ownership_id=int(row["actual_ownership_id"]),
                actual_ownership=float(row["actual_ownership"]),
                external_contest_id=external_contest_id,
                contest_archetype=str(row["contest_archetype"]),
                role=role,
                ownership_baseline_id=(
                    None if baseline is None else int(baseline["ownership_baseline_id"])
                ),
                baseline_ownership=None if baseline is None else float(baseline["ownership"]),
            )
        )
    return tuple(outcomes)


def _played_fact(stat_line: object, fantasy_points: float | None) -> tuple[bool | None, str | None]:
    parsed, error = _stat_line_object(stat_line)
    if error is not None and stat_line is not None:
        return None, error
    signals: set[bool] = set()
    if parsed is not None:
        for key, invert in (
            ("active", False),
            ("inactive", True),
            ("played", False),
            ("dnp", True),
        ):
            if key not in parsed:
                continue
            raw = parsed[key]
            if not isinstance(raw, bool):
                return None, f"result stat_line_json {key} must be boolean"
            signals.add(not raw if invert else raw)
        status = parsed.get("status")
        if status is not None:
            if not isinstance(status, str):
                return None, "result stat_line_json status must be a string"
            normalized = status.strip().upper().replace(" ", "_")
            if normalized in {"OUT", "INACTIVE", "DNP", "DID_NOT_PLAY", "NOT_ACTIVE"}:
                signals.add(False)
            elif normalized in {"ACTIVE", "PLAYED"}:
                signals.add(True)
    if fantasy_points is not None:
        if not math.isfinite(fantasy_points):
            return None, "result fantasy_points is not finite"
        if fantasy_points != 0:
            signals.add(True)
    if len(signals) > 1:
        return None, "result row contains conflicting played/DNP facts"
    return (next(iter(signals)) if signals else None), None


def _stat_line_object(raw: object) -> tuple[dict[str, Any] | None, str | None]:
    if raw is None:
        return None, "result stat_line_json is missing"
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return None, "result stat_line_json is invalid JSON"
    if not isinstance(parsed, dict):
        return None, "result stat_line_json must be a JSON object"
    return parsed, None


def _share_value(raw: object, key: str) -> tuple[float | None, str | None]:
    if raw is None:
        return None, f"result stat_line_json has no {key!r} value"
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None, f"result stat_line_json {key!r} must be numeric"
    value = float(raw)
    if not math.isfinite(value) or not 0 <= value <= 1:
        return None, f"result stat_line_json {key!r} must be a fraction in [0, 1]"
    return value, None


def _direction_from_delta(delta: float, threshold: float) -> ClaimDirectionValue:
    if delta >= threshold:
        return "increase"
    if delta <= -threshold:
        return "decrease"
    return "neutral"


def _deduplication_order(targets: tuple[_ClaimTarget, ...]) -> tuple[_ClaimTarget, ...]:
    """Order targets so the richest evidence wins when one outcome appears on two slates.

    A result row is a game fact and is identical across a game's slates, so official
    availability — which is slate-scoped — is the only evidence that can differ. Prefer a
    target that carries it; the sort is stable, so the selector's order breaks every tie.
    """

    return tuple(sorted(targets, key=lambda target: target.availability_id is None))


def _player_target_key(target: _ClaimTarget) -> str:
    """Identify the graded outcome — one player's game — not the slate that priced it.

    Availability and workload claims predict a player's game, so the same claim reaching
    two slates of one game (a classic slate and its showdown) is one observation, not two.
    Keying on the game keeps §12.4.1's unit of analysis out of the accuracy posterior.
    """

    return json.dumps(
        {
            "game_id": target.game_id,
            "player_id": target.player_id,
            "target": "player_outcome",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _site(value: str) -> Literal["draftkings", "fanduel"]:
    normalized = value.strip().lower()
    aliases = {"dk": "draftkings", "fd": "fanduel"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in ("draftkings", "fanduel"):
        raise ValueError(f"unsupported site {value!r}")
    return cast(Literal["draftkings", "fanduel"], normalized)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GradingError(f"stored timestamp is naive: {value!r}")
    return parsed.astimezone(UTC)
