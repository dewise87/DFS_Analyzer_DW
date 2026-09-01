"""Point-in-time-safe persistence for fitted player outcome distributions."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from narrative_alpha.quant.distributions import DistributionFitResult
from narrative_alpha.store.models import (
    PlayerDistributionCreate,
    PlayerDistributionRow,
    PlayerRow,
    ProjectionSnapshotRow,
    SlateRow,
    canonical_distribution_source_set,
    distribution_source_set_sha256,
)


class PlayerDistributionStoreError(ValueError):
    """Raised when a distribution or one of its exact inputs cannot be stored safely."""


def insert_player_distribution(
    connection: sqlite3.Connection,
    create: PlayerDistributionCreate,
    *,
    fit_result: DistributionFitResult,
) -> PlayerDistributionRow:
    """Validate exact provenance and persist one immutable fitted marginal.

    Fit-derived columns are deliberately taken only from the validated fit result. The
    caller supplies point-in-time metadata and the exact projection snapshot reference,
    while SQLite assigns the primary key.
    """

    started_outer_transaction = not connection.in_transaction
    if started_outer_transaction:
        connection.execute("BEGIN IMMEDIATE")
    connection.execute("SAVEPOINT insert_player_distribution")
    try:
        validated_create = _revalidate_create(create)
        validated_fit = _revalidate_fit_result(fit_result)
        _validate_fit_source(validated_create, validated_fit)
        _validate_source_set(connection, validated_create, validated_fit)
        values = _distribution_values(validated_create, validated_fit)
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        cursor = connection.execute(
            f"INSERT INTO player_distributions ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        row_id = cursor.lastrowid
        if row_id is None or row_id <= 0:
            raise PlayerDistributionStoreError(
                "inserted player distribution has no SQLite row id"
            )
        stored = connection.execute(
            "SELECT * FROM player_distributions WHERE player_distribution_id = ?",
            (row_id,),
        ).fetchone()
        if stored is None:
            raise PlayerDistributionStoreError(
                "inserted player distribution could not be reloaded"
            )
        restored = PlayerDistributionRow.from_db(stored)
    except sqlite3.IntegrityError as error:
        _rollback_insert(connection, started_outer_transaction)
        raise PlayerDistributionStoreError(
            f"player distribution violates the store schema: {error}"
        ) from error
    except Exception:
        _rollback_insert(connection, started_outer_transaction)
        raise
    else:
        connection.execute("RELEASE SAVEPOINT insert_player_distribution")
    return restored


def _rollback_insert(
    connection: sqlite3.Connection,
    started_outer_transaction: bool,
) -> None:
    connection.execute("ROLLBACK TO SAVEPOINT insert_player_distribution")
    connection.execute("RELEASE SAVEPOINT insert_player_distribution")
    if started_outer_transaction:
        connection.rollback()


def _revalidate_fit_result(fit_result: DistributionFitResult) -> DistributionFitResult:
    try:
        return DistributionFitResult.model_validate(
            fit_result.model_dump(mode="python")
        )
    except (TypeError, ValueError) as error:
        raise PlayerDistributionStoreError(
            "validated fit result is internally inconsistent"
        ) from error


def _revalidate_create(create: PlayerDistributionCreate) -> PlayerDistributionCreate:
    try:
        return PlayerDistributionCreate.model_validate(create.model_dump(mode="python"))
    except (TypeError, ValueError) as error:
        raise PlayerDistributionStoreError(
            "distribution create metadata is internally inconsistent"
        ) from error


def _validate_fit_source(
    create: PlayerDistributionCreate,
    fit_result: DistributionFitResult,
) -> None:
    if _canonical_source(create.source) != fit_result.source:
        raise PlayerDistributionStoreError(
            "distribution metadata source does not match the validated fit result"
        )


def _validate_source_set(
    connection: sqlite3.Connection,
    create: PlayerDistributionCreate,
    fit_result: DistributionFitResult,
) -> None:
    if len(create.source_set_json) != 1:
        raise PlayerDistributionStoreError(
            "distribution fitter v1 requires exactly one projection snapshot"
        )

    stored_player = connection.execute(
        "SELECT * FROM players WHERE player_id = ?",
        (create.player_id,),
    ).fetchone()
    if stored_player is None:
        raise PlayerDistributionStoreError(
            f"distribution player {create.player_id} does not exist"
        )
    player = PlayerRow.from_db(stored_player)
    _validate_version_at_cutoff(
        label=f"player {create.player_id}",
        observed_at=player.observed_at,
        valid_from=player.valid_from,
        valid_to=player.valid_to,
        as_of_at=create.as_of_at,
    )
    stored_position = str(player.position or "").strip().upper()
    if stored_position != fit_result.position:
        raise PlayerDistributionStoreError(
            f"fit position {fit_result.position!r} does not match player position "
            f"{stored_position or 'unknown'!r}"
        )

    stored_slate = connection.execute(
        "SELECT * FROM slates WHERE slate_id = ?",
        (create.slate_id,),
    ).fetchone()
    if stored_slate is None:
        raise PlayerDistributionStoreError(
            f"distribution slate {create.slate_id} does not exist"
        )
    slate = SlateRow.from_db(stored_slate)
    _validate_version_at_cutoff(
        label=f"slate {create.slate_id}",
        observed_at=slate.observed_at,
        valid_from=slate.valid_from,
        valid_to=slate.valid_to,
        as_of_at=create.as_of_at,
    )

    reference = create.source_set_json[0]
    stored = connection.execute(
        "SELECT * FROM projection_snapshots WHERE projection_snapshot_id = ?",
        (reference.projection_snapshot_id,),
    ).fetchone()
    if stored is None:
        raise PlayerDistributionStoreError(
            f"projection snapshot {reference.projection_snapshot_id} does not exist"
        )
    projection = ProjectionSnapshotRow.from_db(stored)
    if projection.slate_id != create.slate_id:
        raise PlayerDistributionStoreError(
            f"projection snapshot {reference.projection_snapshot_id} belongs to "
            f"slate {projection.slate_id}, not {create.slate_id}"
        )
    if projection.player_id != create.player_id:
        raise PlayerDistributionStoreError(
            f"projection snapshot {reference.projection_snapshot_id} belongs to "
            f"player {projection.player_id}, not {create.player_id}"
        )
    if _canonical_source(projection.site) != _canonical_source(slate.site):
        raise PlayerDistributionStoreError(
            f"projection snapshot {reference.projection_snapshot_id} site "
            f"{projection.site!r} does not match slate site {slate.site!r}"
        )
    projection_source = _canonical_source(projection.source)
    if projection_source != reference.source:
        raise PlayerDistributionStoreError(
            f"projection snapshot {reference.projection_snapshot_id} source does not "
            "match its source-set reference"
        )
    if projection.source_file_sha256 != reference.source_file_sha256:
        raise PlayerDistributionStoreError(
            f"projection snapshot {reference.projection_snapshot_id} file hash does not "
            "match its source-set reference"
        )
    if projection_source != fit_result.source:
        raise PlayerDistributionStoreError(
            f"projection snapshot {reference.projection_snapshot_id} source does not "
            "match the fitted source"
        )
    if (
        projection.projection_mean != fit_result.input_mean
        or projection.projection_floor != fit_result.input_floor
        or projection.projection_ceiling != fit_result.input_ceiling
    ):
        raise PlayerDistributionStoreError(
            f"projection snapshot {reference.projection_snapshot_id} values do not "
            "match the fitted mean/floor/ceiling inputs"
        )
    if projection.observed_at > create.as_of_at:
        raise PlayerDistributionStoreError(
            f"projection snapshot {reference.projection_snapshot_id} was observed after "
            "the distribution as-of cutoff"
        )
    if projection.valid_from > create.as_of_at:
        raise PlayerDistributionStoreError(
            f"projection snapshot {reference.projection_snapshot_id} was not valid at "
            "the distribution as-of cutoff"
        )
    if projection.valid_to is not None and projection.valid_to <= create.as_of_at:
        raise PlayerDistributionStoreError(
            f"projection snapshot {reference.projection_snapshot_id} had expired by "
            "the distribution as-of cutoff"
        )


def _validate_version_at_cutoff(
    *,
    label: str,
    observed_at: datetime,
    valid_from: datetime,
    valid_to: datetime | None,
    as_of_at: datetime,
) -> None:
    if observed_at > as_of_at:
        raise PlayerDistributionStoreError(
            f"{label} was observed after the distribution as-of cutoff"
        )
    if valid_from > as_of_at:
        raise PlayerDistributionStoreError(
            f"{label} was not valid at the distribution as-of cutoff"
        )
    if valid_to is not None and valid_to <= as_of_at:
        raise PlayerDistributionStoreError(
            f"{label} had expired by the distribution as-of cutoff"
        )


def _distribution_values(
    create: PlayerDistributionCreate,
    fit_result: DistributionFitResult,
) -> dict[str, object]:
    metadata = create.db_values()
    fitted = fit_result.distribution
    return {
        "slate_id": create.slate_id,
        "player_id": create.player_id,
        "position": fit_result.position,
        "source_set_json": canonical_distribution_source_set(create.source_set_json),
        "source_set_sha256": distribution_source_set_sha256(create.source_set_json),
        "as_of_at": metadata["as_of_at"],
        "distribution_family": fitted.distribution_family,
        "p_active": fitted.p_active,
        "p_full_role_given_active": fitted.p_full_role_given_active,
        "conditional_location": fitted.conditional_location,
        "conditional_scale": fitted.conditional_scale,
        "conditional_shape": fitted.conditional_shape,
        "input_mean": fit_result.input_mean,
        "input_floor": fit_result.input_floor,
        "input_ceiling": fit_result.input_ceiling,
        "floor_quantile": fit_result.floor_quantile,
        "ceiling_quantile": fit_result.ceiling_quantile,
        "fit_tolerance": fit_result.fit_tolerance,
        "fit_max_relative_error": fit_result.fit_max_relative_error,
        "fit_config_sha256": fit_result.fit_config_sha256,
        "fitter_version": fit_result.fitter_version,
        "source": fit_result.source,
        "published_at": metadata["published_at"],
        "observed_at": metadata["observed_at"],
        "ingested_at": metadata["ingested_at"],
        "effective_at": metadata["effective_at"],
        "valid_from": metadata["valid_from"],
        "valid_to": metadata["valid_to"],
        "source_version": metadata["source_version"],
        "run_id": metadata["run_id"],
    }


def _canonical_source(value: str) -> str:
    return value.strip().casefold()
