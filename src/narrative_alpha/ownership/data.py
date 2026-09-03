"""Point-in-time ownership training and decision-scenario rows."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from narrative_alpha import __version__
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.ops.results import label_cohorts
from narrative_alpha.ownership.model import (
    FittedOwnershipModel,
    OwnershipModelError,
    OwnershipScenarioInput,
    OwnershipTrainingRow,
    is_synthetic_source,
)
from narrative_alpha.ownership_config import OwnershipModelConfig
from narrative_alpha.replay import PointInTimeSession


class OwnershipDataError(OwnershipModelError):
    """Raised when stored ownership inputs cannot be selected without guessing."""


@dataclass(frozen=True)
class MissingTrainingRow:
    season: int
    week: int
    slate_id: int
    player_id: int
    role: str
    external_contest_id: str
    missing_feature: bool
    missing_baseline: bool


@dataclass(frozen=True)
class TrainingData:
    rows: tuple[OwnershipTrainingRow, ...]
    missing: tuple[MissingTrainingRow, ...]
    decision_snapshot_ids: tuple[str, ...]

    @property
    def missing_feature_rows(self) -> int:
        return sum(row.missing_feature for row in self.missing)

    @property
    def missing_baseline_rows(self) -> int:
        return sum(row.missing_baseline for row in self.missing)


@dataclass(frozen=True)
class LabelGate:
    site: str
    contest_archetype: str
    distinct_weeks: int
    label_rows: int


def canonical_site(site: str) -> str:
    normalized = site.strip().casefold()
    aliases = {
        "dk": "draftkings",
        "draftkings": "draftkings",
        "fd": "fanduel",
        "fanduel": "fanduel",
    }
    try:
        return aliases[normalized]
    except KeyError as error:
        raise OwnershipDataError("site must be dk, fd, draftkings, or fanduel") from error


def validate_label_gate(
    connection: sqlite3.Connection,
    *,
    site: str,
    contest_archetype: str,
    minimum_weeks: int,
    as_of: datetime | None = None,
) -> LabelGate:
    """Require three site/archetype weeks and reject test-derived stored labels."""

    selected_site = canonical_site(site)
    selected = tuple(
        cohort
        for cohort in label_cohorts(connection)
        if cohort.site == selected_site and cohort.contest_archetype == contest_archetype
    )
    week_count = len({(row.season, row.week) for row in selected})
    row_count = sum(row.label_rows for row in selected)
    if week_count < minimum_weeks:
        raise OwnershipDataError(
            f"ownership fit requires at least {minimum_weeks} distinct labeled weeks for "
            f"{selected_site}/{contest_archetype}; found {week_count}"
        )

    cutoff = ensure_utc(as_of or datetime.now(UTC))
    rows = PointInTimeSession(connection).query(
        """
        SELECT actual_ownership_id, source
        FROM actual_ownership
        WHERE site = :site AND contest_archetype = :contest_archetype
          AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(:as_of, 'Z'))
        ORDER BY actual_ownership_id
        """,
        {"site": selected_site, "contest_archetype": contest_archetype},
        as_of=cutoff,
    )
    synthetic = tuple(row for row in rows if is_synthetic_source(str(row["source"])))
    if synthetic:
        raise OwnershipDataError(
            f"refusing {len(synthetic)} fixture/test ownership label row(s) for "
            f"{selected_site}/{contest_archetype}"
        )
    return LabelGate(
        site=selected_site,
        contest_archetype=contest_archetype,
        distinct_weeks=week_count,
        label_rows=row_count,
    )


def load_training_data(
    connection: sqlite3.Connection,
    *,
    site: str,
    contest_archetype: str,
    feature_version: str,
    as_of: datetime,
) -> TrainingData:
    """Join each label to predictors frozen at that slate's latest decision snapshot."""

    selected_site = canonical_site(site)
    cutoff = ensure_utc(as_of)
    rows = PointInTimeSession(connection).query(
        """
        WITH eligible_labels AS (
            SELECT ao.*,
                   row_number() OVER (
                       PARTITION BY ao.external_contest_id, ao.site, ao.slate_id,
                                    ao.player_id, ao.role
                       ORDER BY rtrim(ao.observed_at, 'Z') DESC,
                                rtrim(ao.ingested_at, 'Z') DESC,
                                ao.actual_ownership_id DESC
                   ) AS label_rank
            FROM actual_ownership AS ao
            WHERE ao.site = :site AND ao.contest_archetype = :contest_archetype
              AND rtrim(ao.observed_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(ao.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(ao.valid_from, 'Z') <= rtrim(:as_of, 'Z')
              AND (ao.valid_to IS NULL OR rtrim(ao.valid_to, 'Z') > rtrim(:as_of, 'Z'))
        ),
        ranked_decisions AS (
            -- The newest decision that froze features at its own instant. A fast-lane
            -- re-freeze (`na-fast inactives`) is a later decision with no feature rows of
            -- its own; ranking it first would make the whole week "missing".
            SELECT ds.*,
                   row_number() OVER (
                       PARTITION BY ds.slate_id
                       ORDER BY rtrim(ds.decision_at, 'Z') DESC,
                                ds.decision_snapshot_id DESC
                   ) AS decision_rank
            FROM decision_snapshots AS ds
            WHERE rtrim(ds.decision_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(ds.created_at, 'Z') <= rtrim(:as_of, 'Z')
              AND EXISTS (
                  SELECT 1 FROM narrative_features AS featured
                  WHERE featured.slate_id = ds.slate_id
                    AND featured.as_of = ds.decision_at
                    AND featured.feature_version = :feature_version
              )
        ),
        candidates AS (
            SELECT ao.*, s.season, s.week, ds.decision_snapshot_id, ds.decision_at,
                   ds.created_at
            FROM eligible_labels AS ao
            JOIN slates AS s ON s.slate_id = ao.slate_id
            LEFT JOIN ranked_decisions AS ds
              ON ds.slate_id = ao.slate_id AND ds.decision_rank = 1
            WHERE ao.label_rank = 1
        ),
        baseline_candidates AS (
            SELECT candidate.actual_ownership_id, ob.ownership_baseline_id, ob.ownership,
                   row_number() OVER (
                       PARTITION BY candidate.actual_ownership_id
                       ORDER BY rtrim(ob.observed_at, 'Z') DESC,
                                rtrim(ob.ingested_at, 'Z') DESC,
                                ob.ownership_baseline_id DESC
                   ) AS baseline_rank
            FROM candidates AS candidate
            JOIN ownership_baselines AS ob
              ON ob.slate_id = candidate.slate_id
             AND ob.player_id = candidate.player_id
             AND ob.site = candidate.site
             AND ob.role = candidate.role
             AND rtrim(ob.observed_at, 'Z') <= rtrim(candidate.decision_at, 'Z')
             AND rtrim(ob.ingested_at, 'Z') <= rtrim(candidate.decision_at, 'Z')
             AND rtrim(ob.valid_from, 'Z') <= rtrim(candidate.decision_at, 'Z')
             AND (
                 ob.valid_to IS NULL
                 OR rtrim(ob.valid_to, 'Z') > rtrim(candidate.decision_at, 'Z')
             )
        )
        SELECT candidate.actual_ownership_id, candidate.external_contest_id,
               candidate.site, candidate.slate_id, candidate.contest_archetype,
               candidate.player_id, candidate.role, candidate.actual_ownership,
               candidate.roster_count, candidate.lineup_count, candidate.source AS label_source,
               candidate.season, candidate.week, candidate.decision_snapshot_id,
               candidate.decision_at, nf.feature_id, nf.feature_version,
               nf.h_signed_z, nf.h_dfs_z, nf.h_velocity_6h_z,
               nf.baseline_ownership_snapshot_id AS feature_baseline_id,
               baseline.ownership_baseline_id, baseline.ownership AS baseline_ownership,
               p.position
        FROM candidates AS candidate
        LEFT JOIN narrative_features AS nf
          ON nf.player_id = candidate.player_id
         AND nf.slate_id = candidate.slate_id
         AND nf.site = candidate.site
         AND nf.as_of = candidate.decision_at
         AND nf.feature_version = :feature_version
         AND nf.role = CASE WHEN candidate.role = 'captain' THEN 'flex' ELSE candidate.role END
         AND rtrim(nf.observed_at, 'Z') <= rtrim(candidate.created_at, 'Z')
         AND rtrim(nf.ingested_at, 'Z') <= rtrim(candidate.created_at, 'Z')
        LEFT JOIN baseline_candidates AS baseline
          ON baseline.actual_ownership_id = candidate.actual_ownership_id
         AND baseline.baseline_rank = 1
        LEFT JOIN players AS p
          ON p.player_id = candidate.player_id
         AND rtrim(p.observed_at, 'Z') <= rtrim(candidate.decision_at, 'Z')
         AND rtrim(p.ingested_at, 'Z') <= rtrim(candidate.decision_at, 'Z')
         AND rtrim(p.valid_from, 'Z') <= rtrim(candidate.decision_at, 'Z')
         AND (p.valid_to IS NULL OR rtrim(p.valid_to, 'Z') > rtrim(candidate.decision_at, 'Z'))
        ORDER BY candidate.season, candidate.week, candidate.slate_id,
                 candidate.external_contest_id, candidate.role, candidate.player_id
        """,
        {
            "site": selected_site,
            "contest_archetype": contest_archetype,
            "feature_version": feature_version,
        },
        as_of=cutoff,
    )
    return _training_data(rows, feature_version=feature_version)


def load_scenario_inputs(
    connection: sqlite3.Connection,
    *,
    decision_snapshot_id: str,
    contest_archetype: str,
    feature_version: str,
) -> tuple[datetime, str, str, tuple[OwnershipScenarioInput, ...]]:
    """Load complete player-role inputs from one immutable frozen decision."""

    identity = connection.execute(
        """
        SELECT decision_at
        FROM decision_snapshots
        WHERE decision_snapshot_id = ?
        """,
        (decision_snapshot_id,),
    ).fetchone()
    if identity is None:
        raise OwnershipDataError(f"unknown decision snapshot {decision_snapshot_id!r}")
    decision_at = ensure_utc(
        datetime.fromisoformat(str(identity["decision_at"]).replace("Z", "+00:00"))
    )
    session = PointInTimeSession(connection)
    snapshot = session.decision_snapshot(decision_snapshot_id, as_of=decision_at)
    slate = session.slate(snapshot.slate_id, as_of=decision_at)
    roles = ("captain", "flex") if slate.slate_type == "showdown" else ("classic",)
    role_values = ", ".join(f"'{role}'" for role in roles)
    rows = session.query(
        f"""
        WITH ranked_baselines AS (
            SELECT ob.*,
                   row_number() OVER (
                       PARTITION BY ob.player_id, ob.role
                       ORDER BY rtrim(ob.observed_at, 'Z') DESC,
                                rtrim(ob.ingested_at, 'Z') DESC,
                                ob.ownership_baseline_id DESC
                   ) AS baseline_rank
            FROM ownership_baselines AS ob
            WHERE ob.slate_id = :slate_id AND ob.site = :site
              AND ob.role IN ({role_values})
              AND rtrim(ob.observed_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(ob.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(ob.valid_from, 'Z') <= rtrim(:as_of, 'Z')
              AND (ob.valid_to IS NULL OR rtrim(ob.valid_to, 'Z') > rtrim(:as_of, 'Z'))
        )
        SELECT nf.feature_id, nf.feature_version, nf.player_id, nf.slate_id,
               nf.h_signed_z, nf.h_dfs_z, nf.h_velocity_6h_z,
               baseline.role, baseline.ownership, baseline.ownership_baseline_id,
               p.position
        FROM narrative_features AS nf
        JOIN ranked_baselines AS baseline
          ON baseline.player_id = nf.player_id AND baseline.baseline_rank = 1
         AND (
             baseline.role = 'captain'
             OR nf.baseline_ownership_snapshot_id = baseline.ownership_baseline_id
         )
        JOIN players AS p ON p.player_id = nf.player_id
        WHERE nf.slate_id = :slate_id AND nf.site = :site
          AND nf.as_of = :decision_at AND nf.feature_version = :feature_version
          AND rtrim(nf.observed_at, 'Z') <= rtrim(:snapshot_created_at, 'Z')
          AND rtrim(nf.ingested_at, 'Z') <= rtrim(:snapshot_created_at, 'Z')
          AND rtrim(p.observed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(p.ingested_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(p.valid_from, 'Z') <= rtrim(:as_of, 'Z')
          AND (p.valid_to IS NULL OR rtrim(p.valid_to, 'Z') > rtrim(:as_of, 'Z'))
        ORDER BY baseline.role, nf.player_id
        """,
        {
            "slate_id": slate.slate_id,
            "site": slate.site,
            "decision_at": utc_timestamp(decision_at),
            "snapshot_created_at": utc_timestamp(snapshot.created_at),
            "feature_version": feature_version,
        },
        as_of=decision_at,
    )
    values = tuple(
        OwnershipScenarioInput(
            player_id=int(row["player_id"]),
            slate_id=int(row["slate_id"]),
            decision_snapshot_id=decision_snapshot_id,
            site=str(slate.site),
            contest_archetype=contest_archetype,
            role=str(row["role"]),
            position=str(row["position"] or "UNKNOWN").upper(),
            baseline_ownership=float(row["ownership"]),
            h_signed_z=float(row["h_signed_z"]),
            h_dfs_z=float(row["h_dfs_z"]),
            h_velocity_z=float(row["h_velocity_6h_z"]),
            feature_id=str(row["feature_id"]),
            feature_version=str(row["feature_version"]),
            ownership_baseline_id=int(row["ownership_baseline_id"]),
        )
        for row in rows
    )
    expected_players = int(
        session.query(
            """
            SELECT count(DISTINCT player_id) AS row_count
            FROM salaries
            WHERE slate_id = :slate_id
              AND rtrim(observed_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(ingested_at, 'Z') <= rtrim(:as_of, 'Z')
              AND rtrim(valid_from, 'Z') <= rtrim(:as_of, 'Z')
              AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(:as_of, 'Z'))
            """,
            {
                "slate_id": slate.slate_id,
            },
            as_of=decision_at,
        )[0]["row_count"]
    )
    expected = expected_players * len(roles)
    if len(values) != expected:
        raise OwnershipDataError(
            f"decision {decision_snapshot_id!r} has {expected_players} salary player(s) but "
            f"only {len(values)} of {expected} required player-role feature/baseline row(s)"
        )
    return decision_at, slate.site, slate.slate_type, values


def persist_fit(
    connection: sqlite3.Connection,
    model: FittedOwnershipModel,
    data: TrainingData,
    *,
    config: OwnershipModelConfig,
    fitted_at: datetime,
) -> FittedOwnershipModel:
    """Persist one append-only fit and its model-run lineage transactionally."""

    if not data.rows:
        raise OwnershipDataError("cannot persist a fit with no complete training rows")
    at = ensure_utc(fitted_at)
    stamp = utc_timestamp(at)
    run_id = f"ownership-fit-{uuid4().hex}"
    input_hash = _training_sha256(data.rows)
    connection.execute("SAVEPOINT ownership_fit")
    try:
        connection.execute(
            """
            INSERT INTO model_runs(
                run_id, run_type, started_at, completed_at, status, code_version,
                config_sha256, parent_run_id, error_message, created_at
            ) VALUES (?, 'ownership_fit', ?, NULL, 'running', ?, ?, NULL, NULL, ?)
            """,
            (run_id, stamp, __version__, config.config_sha256, stamp),
        )
        connection.execute(
            """
            INSERT INTO ownership_model_fits(
                run_id, model_version, config_version, config_sha256, feature_version,
                site, contest_archetype, amplitude, parameter_names_json,
                map_parameters_json, covariance_json, training_rows, training_weeks,
                missing_feature_rows, missing_baseline_rows, training_start, training_end,
                input_sha256, created_at, source, dispersion
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                model.model_version,
                model.config_version,
                model.config_sha256,
                model.feature_version,
                model.site,
                model.contest_archetype,
                model.amplitude,
                json.dumps(model.parameter_names, separators=(",", ":")),
                json.dumps(model.map_parameters, separators=(",", ":")),
                json.dumps(model.covariance, separators=(",", ":")),
                len(data.rows),
                len(model.training_weeks),
                data.missing_feature_rows,
                data.missing_baseline_rows,
                min(row.decision_at for row in data.rows),
                max(row.decision_at for row in data.rows),
                input_hash,
                stamp,
                "ownership-map-laplace",
                model.dispersion,
            ),
        )
        _finish_run(connection, run_id, stamp)
    except Exception:
        connection.execute("ROLLBACK TO ownership_fit")
        connection.execute("RELEASE ownership_fit")
        raise
    else:
        connection.execute("RELEASE ownership_fit")
    return replace(model, run_id=run_id)


def load_latest_fit(
    connection: sqlite3.Connection,
    *,
    site: str,
    contest_archetype: str,
    config: OwnershipModelConfig,
    as_of: datetime,
) -> FittedOwnershipModel:
    """Load the latest successful fit matching the exact active config and feature contract."""

    rows = PointInTimeSession(connection).query(
        """
        SELECT fit.*
        FROM ownership_model_fits AS fit
        JOIN model_runs AS run ON run.run_id = fit.run_id
        WHERE fit.site = :site AND fit.contest_archetype = :contest_archetype
          AND fit.config_sha256 = :config_sha256
          AND fit.feature_version = :feature_version
          AND run.status = 'succeeded'
          AND rtrim(run.completed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(fit.created_at, 'Z') <= rtrim(:as_of, 'Z')
        ORDER BY rtrim(fit.created_at, 'Z') DESC, fit.run_id DESC
        LIMIT 1
        """,
        {
            "site": canonical_site(site),
            "contest_archetype": contest_archetype,
            "config_sha256": config.config_sha256,
            "feature_version": config.feature_version,
        },
        as_of=ensure_utc(as_of),
    )
    if not rows:
        raise OwnershipDataError(
            f"no successful ownership fit matches {canonical_site(site)}/{contest_archetype} "
            "and the active config"
        )
    row = rows[0]
    names = tuple(str(value) for value in _json_list(row["parameter_names_json"]))
    parameters = tuple(float(value) for value in _json_list(row["map_parameters_json"]))
    covariance = tuple(
        tuple(float(value) for value in _require_list(value))
        for value in _json_list(row["covariance_json"])
    )
    return FittedOwnershipModel(
        model_version=str(row["model_version"]),
        config_version=str(row["config_version"]),
        config_sha256=str(row["config_sha256"]),
        feature_version=str(row["feature_version"]),
        site=str(row["site"]),
        contest_archetype=str(row["contest_archetype"]),
        amplitude=float(row["amplitude"]),
        probability_epsilon=config.probability_epsilon,
        parameter_names=names,
        map_parameters=parameters,
        covariance=covariance,
        training_rows=int(row["training_rows"]),
        training_weeks=(),
        converged=True,
        objective=math.nan,
        run_id=str(row["run_id"]),
        dispersion=float(row["dispersion"]),
    )


def available_fit_archetypes(
    connection: sqlite3.Connection, *, site: str, config: OwnershipModelConfig, as_of: datetime
) -> tuple[str, ...]:
    rows = PointInTimeSession(connection).query(
        """
        SELECT DISTINCT fit.contest_archetype
        FROM ownership_model_fits AS fit
        JOIN model_runs AS run ON run.run_id = fit.run_id
        WHERE fit.site = :site AND fit.config_sha256 = :config_sha256
          AND fit.feature_version = :feature_version AND run.status = 'succeeded'
          AND rtrim(run.completed_at, 'Z') <= rtrim(:as_of, 'Z')
          AND rtrim(fit.created_at, 'Z') <= rtrim(:as_of, 'Z')
        ORDER BY fit.contest_archetype
        """,
        {
            "site": canonical_site(site),
            "config_sha256": config.config_sha256,
            "feature_version": config.feature_version,
        },
        as_of=ensure_utc(as_of),
    )
    return tuple(str(row["contest_archetype"]) for row in rows)


def _training_data(rows: Iterable[sqlite3.Row], *, feature_version: str) -> TrainingData:
    complete: list[OwnershipTrainingRow] = []
    missing: list[MissingTrainingRow] = []
    cohorts: dict[tuple[int, int, str], set[str]] = defaultdict(set)
    decisions: set[str] = set()
    for row in rows:
        season = int(row["season"])
        week = int(row["week"])
        slate_id = int(row["slate_id"])
        external_contest_id = str(row["external_contest_id"])
        role = str(row["role"])
        cohorts[(season, week, role)].add(external_contest_id)
        feature_missing = row["feature_id"] is None
        baseline_missing = row["ownership_baseline_id"] is None or (
            role != "captain" and row["feature_baseline_id"] is None
        )
        if feature_missing or baseline_missing or row["decision_snapshot_id"] is None:
            missing.append(
                MissingTrainingRow(
                    season=season,
                    week=week,
                    slate_id=slate_id,
                    player_id=int(row["player_id"]),
                    role=role,
                    external_contest_id=external_contest_id,
                    missing_feature=feature_missing or row["decision_snapshot_id"] is None,
                    missing_baseline=baseline_missing,
                )
            )
            continue
        if role != "captain" and int(row["feature_baseline_id"]) != int(
            row["ownership_baseline_id"]
        ):
            raise OwnershipDataError(
                f"feature {row['feature_id']} does not cite the latest baseline at its decision"
            )
        decision_snapshot_id = str(row["decision_snapshot_id"])
        decisions.add(decision_snapshot_id)
        complete.append(
            OwnershipTrainingRow(
                player_id=int(row["player_id"]),
                season=season,
                week=week,
                slate_id=slate_id,
                decision_snapshot_id=decision_snapshot_id,
                decision_at=str(row["decision_at"]),
                site=str(row["site"]),
                contest_archetype=str(row["contest_archetype"]),
                role=role,
                position=str(row["position"] or "UNKNOWN").upper(),
                baseline_ownership=float(row["baseline_ownership"]),
                h_signed_z=float(row["h_signed_z"]),
                h_dfs_z=float(row["h_dfs_z"]),
                h_velocity_z=float(row["h_velocity_6h_z"]),
                actual_ownership=float(row["actual_ownership"]),
                roster_count=int(row["roster_count"]),
                lineup_count=int(row["lineup_count"]),
                label_source=str(row["label_source"]),
                feature_id=str(row["feature_id"]),
                feature_version=feature_version,
                ownership_baseline_id=int(row["ownership_baseline_id"]),
                actual_ownership_id=int(row["actual_ownership_id"]),
            )
        )
    mixed = {key: values for key, values in cohorts.items() if len(values) > 1}
    if mixed:
        descriptions = "; ".join(
            f"{season}-W{week:02d} {role}: {', '.join(sorted(ids))}"
            for (season, week, role), ids in sorted(mixed.items())
        )
        raise OwnershipDataError(
            "one ownership fit cannot mix multiple contest cohorts in a week: " + descriptions
        )
    return TrainingData(
        rows=tuple(complete),
        missing=tuple(missing),
        decision_snapshot_ids=tuple(sorted(decisions)),
    )


def _training_sha256(rows: tuple[OwnershipTrainingRow, ...]) -> str:
    payload = [row.__dict__ for row in rows]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _json_list(value: object) -> list[Any]:
    parsed = json.loads(str(value))
    return _require_list(parsed)


def _require_list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise OwnershipDataError("stored ownership fit JSON must be an array")
    return value


def _finish_run(connection: sqlite3.Connection, run_id: str, stamp: str) -> None:
    cursor = connection.execute(
        """
        UPDATE model_runs SET completed_at = ?, status = 'succeeded'
        WHERE run_id = ? AND status = 'running'
        """,
        (stamp, run_id),
    )
    if cursor.rowcount != 1:
        raise OwnershipDataError(f"could not mark ownership run {run_id!r} succeeded")
