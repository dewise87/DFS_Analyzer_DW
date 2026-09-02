"""Deterministic Stage 3 episode heat and Appendix B feature snapshots."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import tomllib
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from narrative_alpha import __version__
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.narrative.episodes import METHOD_VERSION as DEFAULT_EPISODE_METHOD_VERSION
from narrative_alpha.store import (
    ModelRunRow,
    NarrativeFeatureRow,
    NarrativeFeatureVersionRow,
)

DEFAULT_HEAT_CONFIG_PATH = Path("config/heat.toml")
FORMULA_VERSION = "section-12.2.2-v1"
DERIVATION_SOURCE = "narrative_alpha.features"
NOVELTY_METHOD = "aligned-vendor-baseline-change-v1"
STANDARDIZATION_METHOD = "population-zscore-within-slate-channel-v1"

SourceClass = Literal["mainstream", "dfs", "team_fan"]
Site = Literal["draftkings", "fanduel"]
Role = Literal["classic", "flex", "captain"]

_SOURCE_CLASSES = frozenset({"mainstream", "dfs", "team_fan"})
_EVIDENCE_CLASSES = frozenset({"A", "B", "C"})
_EVIDENCE_BASES = frozenset(
    {
        "official",
        "direct_quote",
        "beat_report",
        "film_claim",
        "play_by_play",
        "statistics",
        "community_observation",
        "generic_sentiment",
        "joke",
        "unknown",
    }
)
_EVENT_RELATIONS = frozenset({"origin", "independent", "corroborating"})
_DIRECTION_VALUES = {"decrease": -1.0, "neutral": 0.0, "increase": 1.0, "unknown": 0.0}
_STANDARDIZED_FIELDS = (
    "h_signed",
    "h_absolute",
    "h_mainstream",
    "h_dfs",
    "h_team_fan",
    "h_velocity_6h",
    "h_acceleration",
    "h_consensus",
    "h_source_entropy",
    "h_novelty_share",
    "unique_episode_count",
    "unique_source_count",
    "source_overlap_index",
)


class FeatureError(RuntimeError):
    """Base error for invalid Stage 3 inputs or inconsistent feature snapshots."""


class HeatConfigError(FeatureError):
    """Raised when the versioned heat configuration cannot be trusted."""


class FeatureVersionMismatchError(FeatureError):
    """Raised when a feature version is reused for different configuration semantics."""


class FeatureSnapshotConflictError(FeatureError):
    """Raised when an existing feature snapshot differs from a deterministic rebuild."""


class FeatureInputError(FeatureError):
    """Raised when the requested point-in-time slate inputs are incomplete or inconsistent."""


class _StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _SourceFamilyConfig(_StrictConfig):
    source_class: SourceClass
    quality: float = Field(ge=0, le=1, allow_inf_nan=False)


class _HeatConfigFile(_StrictConfig):
    feature_version: str = Field(min_length=1)
    formula_version: str = Field(min_length=1)
    episode_method_version: str = Field(min_length=1)
    episode_window_hours: float = Field(gt=0, allow_inf_nan=False)
    soft_factor_floor: float = Field(gt=0, lt=1, allow_inf_nan=False)
    winsor_limit: float = Field(gt=0, allow_inf_nan=False)
    velocity_window_hours: float = Field(gt=0, allow_inf_nan=False)
    novelty_method: str
    standardization_method: str
    # Smallest aligned baseline-ownership move (a fraction, 0.01 = one point) that counts
    # as "the story is already in the baseline". Below it, novelty stays 1.0.
    novelty_min_baseline_move: float = Field(gt=0, lt=1, allow_inf_nan=False)
    half_life_hours: dict[SourceClass, float]
    evidence_class_quality: dict[str, float]
    evidence_basis_quality: dict[str, float]
    source_families: dict[str, _SourceFamilyConfig]

    @model_validator(mode="after")
    def validate_complete_semantics(self) -> _HeatConfigFile:
        if self.formula_version != FORMULA_VERSION:
            raise ValueError(
                f"formula_version must be {FORMULA_VERSION!r} for this implementation"
            )
        if self.novelty_method != NOVELTY_METHOD:
            raise ValueError(f"novelty_method must be {NOVELTY_METHOD!r}")
        if self.standardization_method != STANDARDIZATION_METHOD:
            raise ValueError(
                f"standardization_method must be {STANDARDIZATION_METHOD!r}"
            )
        if self.winsor_limit != 4.0:
            raise ValueError("winsor_limit must be 4.0 for the Appendix B v1 columns")
        if self.velocity_window_hours != 6.0:
            raise ValueError("velocity_window_hours must be 6.0 for the *_6h fields")
        if set(self.half_life_hours) != _SOURCE_CLASSES:
            raise ValueError("half_life_hours must define mainstream, dfs, and team_fan")
        if any(value <= 0 or not math.isfinite(value) for value in self.half_life_hours.values()):
            raise ValueError("source-class half-lives must be positive and finite")
        if set(self.evidence_class_quality) != _EVIDENCE_CLASSES:
            raise ValueError("evidence_class_quality must define exactly A, B, and C")
        if set(self.evidence_basis_quality) != _EVIDENCE_BASES:
            raise ValueError("evidence_basis_quality does not cover the Stage 1 taxonomy")
        quality_values = (
            *self.evidence_class_quality.values(),
            *self.evidence_basis_quality.values(),
        )
        if any(value < 0 or value > 1 or not math.isfinite(value) for value in quality_values):
            raise ValueError("all configured quality scores must be finite values in [0, 1]")
        if not self.source_families:
            raise ValueError("at least one source family must be configured")
        if any(not family.strip() for family in self.source_families):
            raise ValueError("source family names must not be blank")
        return self


@dataclass(frozen=True)
class SourceFamilyHeatConfig:
    source_class: SourceClass
    quality: float


@dataclass(frozen=True)
class HeatConfig:
    """Strict heat settings plus their canonical semantic identity."""

    feature_version: str
    formula_version: str
    episode_method_version: str
    episode_window_hours: float
    soft_factor_floor: float
    winsor_limit: float
    velocity_window: timedelta
    novelty_method: str
    standardization_method: str
    novelty_min_baseline_move: float
    half_life_hours: dict[SourceClass, float]
    evidence_class_quality: dict[str, float]
    evidence_basis_quality: dict[str, float]
    source_families: dict[str, SourceFamilyHeatConfig]
    canonical_config: dict[str, Any]
    config_sha256: str


@dataclass(frozen=True)
class EpisodeHeat:
    """Auditable §12.2.2 factors and heat for one episode at one evaluation time."""

    episode_id: str
    source_class: SourceClass
    direction: float
    quality_raw: float
    quality: float
    specificity_raw: float
    specificity: float
    novelty: float
    independence_raw: float
    independence: float
    reach: int
    age_hours: float
    half_life_hours: float
    heat: float
    heat_without_novelty: float
    n_events: int
    item_count: int
    source_families_by_source: tuple[tuple[str, str], ...]
    independent_classes_by_source: tuple[tuple[str, SourceClass], ...]
    ownership_baseline_ids: tuple[int, ...]


@dataclass(frozen=True)
class FeatureBuildReport:
    """Summary of one deterministic player/slate feature build."""

    slate_id: int
    site: Site
    as_of: datetime
    feature_version: str
    player_count: int
    episode_count: int
    features_inserted: int
    reused_existing: bool
    run_id: str | None


@dataclass(frozen=True)
class _Salary:
    salary_id: int
    player_id: int
    salary: int


@dataclass(frozen=True)
class _Ownership:
    snapshot_id: int
    player_id: int
    source: str
    ownership: float
    observed_at: datetime
    ingested_at: datetime
    valid_from: datetime
    valid_to: datetime | None


@dataclass(frozen=True)
class _Projection:
    snapshot_id: int
    player_id: int
    source: str
    projection_mean: float
    observed_at: datetime
    ingested_at: datetime
    valid_from: datetime
    valid_to: datetime | None


@dataclass(frozen=True)
class _EpisodeMember:
    claim_id: str
    source_item_id: int
    source_id: str
    source_family: str
    relation: str
    evidence_class: str
    evidence_basis: str
    specificity: float
    actionability: float
    roster_direction: str
    claim_observed_at: datetime
    claim_ingested_at: datetime
    claim_valid_from: datetime
    claim_valid_to: datetime | None
    item_observed_at: datetime
    item_ingested_at: datetime
    item_valid_from: datetime
    item_valid_to: datetime | None


@dataclass(frozen=True)
class _EpisodeInput:
    episode_id: str
    player_id: int
    opened_at: datetime
    members: tuple[_EpisodeMember, ...]


@dataclass(frozen=True)
class _RawFeature:
    salary: _Salary
    baseline: _Ownership | None
    baseline_previous: _Ownership | None
    projection: _Projection | None
    projection_previous: _Projection | None
    values: dict[str, float]
    episode_ids: tuple[str, ...]
    ownership_baseline_ids: tuple[int, ...]


def load_heat_config(path: Path = DEFAULT_HEAT_CONFIG_PATH) -> HeatConfig:
    if DEFAULT_EPISODE_METHOD_VERSION != "deterministic-token-set-jaccard-v1":
        raise FeatureVersionMismatchError(
            "config/heat.toml must be reviewed when the default episode method changes"
        )
    """Load and hash the complete semantic heat configuration."""

    try:
        raw = tomllib.loads(path.read_bytes().decode("utf-8"))
    except OSError as error:
        raise HeatConfigError(f"cannot read heat config {path}: {error}") from error
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise HeatConfigError(f"invalid heat config {path}: {error}") from error
    try:
        parsed = _HeatConfigFile.model_validate(raw)
    except ValidationError as error:
        raise HeatConfigError(f"invalid heat config {path}: {error}") from error

    canonical_config = parsed.model_dump(mode="json")
    canonical_json = json.dumps(canonical_config, sort_keys=True, separators=(",", ":"))
    return HeatConfig(
        feature_version=parsed.feature_version,
        formula_version=parsed.formula_version,
        episode_method_version=parsed.episode_method_version,
        episode_window_hours=parsed.episode_window_hours,
        soft_factor_floor=parsed.soft_factor_floor,
        winsor_limit=parsed.winsor_limit,
        velocity_window=timedelta(hours=parsed.velocity_window_hours),
        novelty_method=parsed.novelty_method,
        novelty_min_baseline_move=parsed.novelty_min_baseline_move,
        standardization_method=parsed.standardization_method,
        half_life_hours=dict(parsed.half_life_hours),
        evidence_class_quality=dict(parsed.evidence_class_quality),
        evidence_basis_quality=dict(parsed.evidence_basis_quality),
        source_families={
            family: SourceFamilyHeatConfig(
                source_class=settings.source_class,
                quality=settings.quality,
            )
            for family, settings in parsed.source_families.items()
        },
        canonical_config=canonical_config,
        config_sha256=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
    )


def map_soft_factor(value: float, *, floor: float) -> float:
    """Affine-map a soft [0, 1] judgment into [floor, 1]."""

    if not math.isfinite(value) or value < 0 or value > 1:
        raise FeatureInputError("soft factor must be a finite value in [0, 1]")
    if not math.isfinite(floor) or floor <= 0 or floor >= 1:
        raise FeatureInputError("soft-factor floor must be a finite value in (0, 1)")
    return floor + (1.0 - floor) * value


def calculate_episode_heat(
    *,
    direction: float,
    quality: float,
    specificity: float,
    novelty: float,
    independence: float,
    reach: int,
    age_hours: float,
    half_life_hours: float,
    soft_factor_floor: float,
) -> float:
    """Evaluate the exact §12.2.2 product, flooring only its three soft factors."""

    if not math.isfinite(direction) or direction < -1 or direction > 1:
        raise FeatureInputError("direction must be a finite value in [-1, 1]")
    if not math.isfinite(novelty) or novelty < 0 or novelty > 1:
        raise FeatureInputError("novelty must be a finite value in [0, 1]")
    if reach < 0:
        raise FeatureInputError("reach cannot be negative")
    if not math.isfinite(age_hours) or age_hours < 0:
        raise FeatureInputError("episode age must be finite and nonnegative")
    if not math.isfinite(half_life_hours) or half_life_hours <= 0:
        raise FeatureInputError("half-life must be positive and finite")
    quality_factor = map_soft_factor(quality, floor=soft_factor_floor)
    specificity_factor = map_soft_factor(specificity, floor=soft_factor_floor)
    independence_factor = map_soft_factor(independence, floor=soft_factor_floor)
    decay = math.exp(-math.log(2.0) * age_hours / half_life_hours)
    return (
        direction
        * quality_factor
        * specificity_factor
        * novelty
        * independence_factor
        * math.log1p(reach)
        * decay
    )


def build_features(
    connection: sqlite3.Connection,
    *,
    slate_id: int,
    site: str,
    as_of: datetime,
    config_path: Path = DEFAULT_HEAT_CONFIG_PATH,
    built_at: datetime | None = None,
) -> FeatureBuildReport:
    """Build one immutable player/slate feature snapshot from an exact Stage 2 snapshot."""

    if slate_id <= 0:
        raise FeatureInputError("slate_id must be positive")
    cutoff = ensure_utc(as_of)
    build_time = ensure_utc(built_at or datetime.now(UTC))
    if build_time < cutoff:
        raise FeatureInputError("built_at cannot precede the feature as_of cutoff")
    canonical_site = _normalize_site(site)
    config = load_heat_config(config_path)
    _check_feature_version(connection, config)
    slate_type = _load_slate_type(connection, slate_id, canonical_site, cutoff)
    role: Role = "classic" if slate_type == "classic" else "flex"
    salaries = _load_salaries(connection, slate_id, cutoff)
    if not salaries:
        raise FeatureInputError(
            f"slate {slate_id} has no point-in-time eligible salary rows at "
            f"{utc_timestamp(cutoff)}"
        )
    player_ids = tuple(salary.player_id for salary in salaries)
    _require_complete_episode_snapshot(connection, player_ids, config, cutoff)
    episodes = _load_episodes(connection, player_ids, config, cutoff)
    ownership = _load_ownership(connection, slate_id, canonical_site, role, player_ids, cutoff)
    projections = _load_projections(connection, slate_id, canonical_site, player_ids, cutoff)
    ownership_by_player = _group_by_player(ownership)
    projections_by_player = _group_by_player(projections)

    raw_rows = tuple(
        _raw_feature(
            salary,
            episodes=episodes,
            ownership=ownership_by_player.get(salary.player_id, ()),
            projections=projections_by_player.get(salary.player_id, ()),
            as_of=cutoff,
            config=config,
        )
        for salary in salaries
    )
    standardized = _standardize(raw_rows, config.winsor_limit)
    semantic_payloads = {
        raw.salary.player_id: _semantic_payload(
            raw,
            standardized[raw.salary.player_id],
            slate_id=slate_id,
            site=canonical_site,
            role=role,
            as_of=cutoff,
            config=config,
        )
        for raw in raw_rows
    }
    input_hashes = {
        player_id: _sha256_json(payload)
        for player_id, payload in semantic_payloads.items()
    }

    existing_rows = connection.execute(
        """
        SELECT * FROM narrative_features
        WHERE slate_id = ? AND site = ? AND as_of = ? AND feature_version = ?
        ORDER BY player_id
        """,
        (slate_id, canonical_site, utc_timestamp(cutoff), config.feature_version),
    ).fetchall()
    if existing_rows:
        stored = tuple(NarrativeFeatureRow.from_db(row) for row in existing_rows)
        stored_hashes = {row.player_id: row.input_sha256 for row in stored}
        if stored_hashes != input_hashes:
            raise FeatureSnapshotConflictError(
                f"stored feature snapshot {config.feature_version!r} for slate {slate_id} "
                f"at {utc_timestamp(cutoff)} differs from the deterministic rebuild"
            )
        return FeatureBuildReport(
            slate_id=slate_id,
            site=canonical_site,
            as_of=cutoff,
            feature_version=config.feature_version,
            player_count=len(raw_rows),
            episode_count=len({item for raw in raw_rows for item in raw.episode_ids}),
            features_inserted=0,
            reused_existing=True,
            run_id=None,
        )

    run_id = f"stage3-{uuid4().hex}"
    run = ModelRunRow(
        run_id=run_id,
        run_type="stage_3_features",
        started_at=build_time,
        completed_at=None,
        status="running",
        code_version=__version__,
        config_sha256=config.config_sha256,
        parent_run_id=None,
        error_message=None,
        created_at=build_time,
    )
    version = NarrativeFeatureVersionRow(
        feature_version=config.feature_version,
        formula_version=config.formula_version,
        config_sha256=config.config_sha256,
        config_json=config.canonical_config,
        registered_at=build_time,
        source=DERIVATION_SOURCE,
    )

    connection.execute("SAVEPOINT narrative_feature_build")
    try:
        if connection.execute(
            "SELECT 1 FROM narrative_feature_versions WHERE feature_version = ?",
            (config.feature_version,),
        ).fetchone() is None:
            _insert_row(connection, "narrative_feature_versions", version)
        _insert_row(connection, "model_runs", run)
        for raw in raw_rows:
            player_id = raw.salary.player_id
            feature_row = _feature_row(
                semantic_payloads[player_id],
                input_sha256=input_hashes[player_id],
                built_at=build_time,
                run_id=run_id,
            )
            _insert_row(connection, "narrative_features", feature_row)
        cursor = connection.execute(
            """
            UPDATE model_runs SET completed_at = ?, status = 'succeeded'
            WHERE run_id = ? AND status = 'running'
            """,
            (utc_timestamp(build_time), run_id),
        )
        if cursor.rowcount != 1:
            raise FeatureError(f"could not mark Stage 3 run {run_id!r} succeeded")
    except Exception:
        connection.execute("ROLLBACK TO narrative_feature_build")
        connection.execute("RELEASE narrative_feature_build")
        raise
    else:
        connection.execute("RELEASE narrative_feature_build")

    return FeatureBuildReport(
        slate_id=slate_id,
        site=canonical_site,
        as_of=cutoff,
        feature_version=config.feature_version,
        player_count=len(raw_rows),
        episode_count=len({item for raw in raw_rows for item in raw.episode_ids}),
        features_inserted=len(raw_rows),
        reused_existing=False,
        run_id=run_id,
    )


def load_feature_rows(
    connection: sqlite3.Connection,
    *,
    slate_id: int,
    site: str,
    as_of: datetime,
    feature_version: str,
) -> tuple[NarrativeFeatureRow, ...]:
    """Load one exact feature snapshot in stable player order."""

    canonical_site = _normalize_site(site)
    cutoff = ensure_utc(as_of)
    rows = connection.execute(
        """
        SELECT * FROM narrative_features
        WHERE slate_id = ? AND site = ? AND as_of = ? AND feature_version = ?
        ORDER BY player_id
        """,
        (slate_id, canonical_site, utc_timestamp(cutoff), feature_version),
    ).fetchall()
    return tuple(NarrativeFeatureRow.from_db(row) for row in rows)


def load_episode_heats(
    connection: sqlite3.Connection,
    *,
    player_id: int,
    slate_id: int,
    site: str,
    as_of: datetime,
    config_path: Path = DEFAULT_HEAT_CONFIG_PATH,
) -> tuple[EpisodeHeat, ...]:
    """Reconstruct auditable per-episode heat factors without writing feature rows."""

    if player_id <= 0 or slate_id <= 0:
        raise FeatureInputError("player_id and slate_id must be positive")
    cutoff = ensure_utc(as_of)
    canonical_site = _normalize_site(site)
    config = load_heat_config(config_path)
    slate_type = _load_slate_type(connection, slate_id, canonical_site, cutoff)
    role: Role = "classic" if slate_type == "classic" else "flex"
    _require_complete_episode_snapshot(connection, (player_id,), config, cutoff)
    episodes = _load_episodes(connection, (player_id,), config, cutoff)
    ownership = _load_ownership(
        connection, slate_id, canonical_site, role, (player_id,), cutoff
    )
    return _episode_heats(episodes, ownership, cutoff, config)


def _normalize_site(site: str) -> Site:
    normalized = site.strip().casefold()
    aliases: dict[str, Site] = {
        "dk": "draftkings",
        "draftkings": "draftkings",
        "fd": "fanduel",
        "fanduel": "fanduel",
    }
    try:
        return aliases[normalized]
    except KeyError as error:
        raise FeatureInputError("site must be dk, fd, draftkings, or fanduel") from error


def _check_feature_version(connection: sqlite3.Connection, config: HeatConfig) -> None:
    row = connection.execute(
        "SELECT * FROM narrative_feature_versions WHERE feature_version = ?",
        (config.feature_version,),
    ).fetchone()
    if row is None:
        return
    stored = NarrativeFeatureVersionRow.from_db(row)
    if (
        stored.config_sha256 != config.config_sha256
        or stored.formula_version != config.formula_version
        or stored.config_json != config.canonical_config
    ):
        raise FeatureVersionMismatchError(
            f"feature_version {config.feature_version!r} is already bound to different "
            "formula/config values; bump feature_version"
        )


def _load_slate_type(
    connection: sqlite3.Connection,
    slate_id: int,
    site: Site,
    as_of: datetime,
) -> Literal["classic", "showdown"]:
    cutoff = utc_timestamp(as_of)
    row = connection.execute(
        """
        SELECT slate_type FROM slates
        WHERE slate_id = ? AND site = ?
          AND observed_at <= ? AND ingested_at <= ?
          AND valid_from <= ? AND (valid_to IS NULL OR ? < valid_to)
        """,
        (slate_id, site, cutoff, cutoff, cutoff, cutoff),
    ).fetchone()
    if row is None:
        raise FeatureInputError(
            f"slate {slate_id} is not a point-in-time eligible {site} slate at {cutoff}"
        )
    slate_type = str(row["slate_type"])
    if slate_type == "classic":
        return "classic"
    if slate_type == "showdown":
        return "showdown"
    raise FeatureInputError(f"slate {slate_id} has unsupported type {slate_type!r}")


def _load_salaries(
    connection: sqlite3.Connection,
    slate_id: int,
    as_of: datetime,
) -> tuple[_Salary, ...]:
    cutoff = utc_timestamp(as_of)
    rows = connection.execute(
        """
        WITH eligible AS (
            SELECT salary_id, player_id, salary, observed_at, ingested_at,
                   row_number() OVER (
                       PARTITION BY player_id
                       ORDER BY observed_at DESC, ingested_at DESC, salary_id DESC
                   ) AS ordinal
            FROM salaries
            WHERE slate_id = ?
              AND observed_at <= ? AND ingested_at <= ?
              AND valid_from <= ? AND (valid_to IS NULL OR ? < valid_to)
        )
        SELECT salary_id, player_id, salary FROM eligible
        WHERE ordinal = 1 ORDER BY player_id
        """,
        (slate_id, cutoff, cutoff, cutoff, cutoff),
    ).fetchall()
    return tuple(
        _Salary(
            salary_id=int(row["salary_id"]),
            player_id=int(row["player_id"]),
            salary=int(row["salary"]),
        )
        for row in rows
    )


def _require_complete_episode_snapshot(
    connection: sqlite3.Connection,
    player_ids: tuple[int, ...],
    config: HeatConfig,
    as_of: datetime,
) -> None:
    placeholders = ", ".join("?" for _ in player_ids)
    cutoff = utc_timestamp(as_of)
    row = connection.execute(
        f"""
        SELECT ref.player_id, claim.claim_id
        FROM claims AS claim
        JOIN source_items AS item ON item.source_item_id = claim.source_item_id
        JOIN source_item_extractions AS extraction
          ON extraction.extraction_id = claim.extraction_id
        JOIN claim_player_refs AS ref ON ref.claim_id = claim.claim_id
        WHERE ref.player_id IN ({placeholders})
          AND extraction.status = 'succeeded'
          AND claim.observed_at <= ? AND claim.ingested_at <= ?
          AND claim.valid_from <= ? AND (claim.valid_to IS NULL OR ? < claim.valid_to)
          AND item.observed_at <= ? AND item.ingested_at <= ?
          AND item.valid_from <= ? AND (item.valid_to IS NULL OR ? < item.valid_to)
          AND NOT EXISTS (
              SELECT 1
              FROM narrative_episodes AS episode
              JOIN episode_claims AS member ON member.episode_id = episode.episode_id
              WHERE episode.subject_type = 'player'
                AND episode.subject_player_id = ref.player_id
                AND episode.method_version = ? AND episode.as_of = ?
                AND member.claim_id = claim.claim_id
          )
        ORDER BY ref.player_id, claim.claim_id
        LIMIT 1
        """,
        (
            *player_ids,
            cutoff,
            cutoff,
            cutoff,
            cutoff,
            cutoff,
            cutoff,
            cutoff,
            cutoff,
            config.episode_method_version,
            cutoff,
        ),
    ).fetchone()
    if row is not None:
        raise FeatureInputError(
            f"claim {row['claim_id']} for player {row['player_id']} has no "
            f"{config.episode_method_version!r} episode at {cutoff}; run na-episodes build "
            "for the identical --as-of first"
        )


def _load_episodes(
    connection: sqlite3.Connection,
    player_ids: tuple[int, ...],
    config: HeatConfig,
    as_of: datetime,
) -> tuple[_EpisodeInput, ...]:
    placeholders = ", ".join("?" for _ in player_ids)
    cutoff = utc_timestamp(as_of)
    rows = connection.execute(
        f"""
        SELECT
            episode.episode_id,
            episode.subject_player_id AS player_id,
            episode.opened_at,
            episode.window_hours,
            member.claim_id,
            member.source_item_id,
            member.source_id,
            member.source_family,
            member.relation,
            claim.evidence_class,
            claim.evidence_basis,
            claim.specificity,
            claim.actionability,
            claim.roster_behavior_direction,
            claim.observed_at AS claim_observed_at,
            claim.ingested_at AS claim_ingested_at,
            claim.valid_from AS claim_valid_from,
            claim.valid_to AS claim_valid_to,
            item.observed_at AS item_observed_at,
            item.ingested_at AS item_ingested_at,
            item.valid_from AS item_valid_from,
            item.valid_to AS item_valid_to
        FROM narrative_episodes AS episode
        JOIN episode_claims AS member ON member.episode_id = episode.episode_id
        JOIN claims AS claim ON claim.claim_id = member.claim_id
        JOIN source_items AS item ON item.source_item_id = member.source_item_id
        WHERE episode.subject_type = 'player'
          AND episode.subject_player_id IN ({placeholders})
          AND episode.method_version = ? AND episode.as_of = ?
        ORDER BY episode.episode_id, item.observed_at, member.claim_id
        """,
        (*player_ids, config.episode_method_version, cutoff),
    ).fetchall()
    grouped: dict[tuple[str, int, datetime], list[_EpisodeMember]] = defaultdict(list)
    for row in rows:
        if not math.isclose(
            float(row["window_hours"]),
            config.episode_window_hours,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise FeatureInputError(
                f"episode {row['episode_id']} uses a {row['window_hours']}-hour window, "
                f"but feature_version {config.feature_version!r} requires "
                f"{config.episode_window_hours} hours"
            )
        member = _EpisodeMember(
            claim_id=str(row["claim_id"]),
            source_item_id=int(row["source_item_id"]),
            source_id=str(row["source_id"]),
            source_family=str(row["source_family"]),
            relation=str(row["relation"]),
            evidence_class=str(row["evidence_class"]),
            evidence_basis=str(row["evidence_basis"]),
            specificity=float(row["specificity"]),
            actionability=float(row["actionability"]),
            roster_direction=str(row["roster_behavior_direction"]),
            claim_observed_at=_parse_timestamp(str(row["claim_observed_at"])),
            claim_ingested_at=_parse_timestamp(str(row["claim_ingested_at"])),
            claim_valid_from=_parse_timestamp(str(row["claim_valid_from"])),
            claim_valid_to=_optional_timestamp(row["claim_valid_to"]),
            item_observed_at=_parse_timestamp(str(row["item_observed_at"])),
            item_ingested_at=_parse_timestamp(str(row["item_ingested_at"])),
            item_valid_from=_parse_timestamp(str(row["item_valid_from"])),
            item_valid_to=_optional_timestamp(row["item_valid_to"]),
        )
        if not _member_eligible(member, as_of):
            raise FeatureInputError(
                f"episode {row['episode_id']} contains claim {member.claim_id} unavailable "
                f"at {cutoff}"
            )
        key = (
            str(row["episode_id"]),
            int(row["player_id"]),
            _parse_timestamp(str(row["opened_at"])),
        )
        grouped[key].append(member)
    return tuple(
        _EpisodeInput(
            episode_id=episode_id,
            player_id=player_id,
            opened_at=opened_at,
            members=tuple(members),
        )
        for (episode_id, player_id, opened_at), members in sorted(
            grouped.items(), key=lambda value: value[0][0]
        )
    )


def _load_ownership(
    connection: sqlite3.Connection,
    slate_id: int,
    site: Site,
    role: Role,
    player_ids: tuple[int, ...],
    as_of: datetime,
) -> tuple[_Ownership, ...]:
    placeholders = ", ".join("?" for _ in player_ids)
    cutoff = utc_timestamp(as_of)
    rows = connection.execute(
        f"""
        SELECT ownership_baseline_id, player_id, source, ownership,
               observed_at, ingested_at, valid_from, valid_to
        FROM ownership_baselines
        WHERE slate_id = ? AND site = ? AND role = ?
          AND player_id IN ({placeholders})
          AND observed_at <= ? AND ingested_at <= ? AND valid_from <= ?
        ORDER BY player_id, observed_at, ingested_at, ownership_baseline_id
        """,
        (slate_id, site, role, *player_ids, cutoff, cutoff, cutoff),
    ).fetchall()
    return tuple(
        _Ownership(
            snapshot_id=int(row["ownership_baseline_id"]),
            player_id=int(row["player_id"]),
            source=str(row["source"]),
            ownership=float(row["ownership"]),
            observed_at=_parse_timestamp(str(row["observed_at"])),
            ingested_at=_parse_timestamp(str(row["ingested_at"])),
            valid_from=_parse_timestamp(str(row["valid_from"])),
            valid_to=_optional_timestamp(row["valid_to"]),
        )
        for row in rows
    )


def _load_projections(
    connection: sqlite3.Connection,
    slate_id: int,
    site: Site,
    player_ids: tuple[int, ...],
    as_of: datetime,
) -> tuple[_Projection, ...]:
    placeholders = ", ".join("?" for _ in player_ids)
    cutoff = utc_timestamp(as_of)
    rows = connection.execute(
        f"""
        SELECT projection_snapshot_id, player_id, source, projection_mean,
               observed_at, ingested_at, valid_from, valid_to
        FROM projection_snapshots
        WHERE slate_id = ? AND site = ? AND player_id IN ({placeholders})
          AND observed_at <= ? AND ingested_at <= ? AND valid_from <= ?
        ORDER BY player_id, observed_at, ingested_at, projection_snapshot_id
        """,
        (slate_id, site, *player_ids, cutoff, cutoff, cutoff),
    ).fetchall()
    return tuple(
        _Projection(
            snapshot_id=int(row["projection_snapshot_id"]),
            player_id=int(row["player_id"]),
            source=str(row["source"]),
            projection_mean=float(row["projection_mean"]),
            observed_at=_parse_timestamp(str(row["observed_at"])),
            ingested_at=_parse_timestamp(str(row["ingested_at"])),
            valid_from=_parse_timestamp(str(row["valid_from"])),
            valid_to=_optional_timestamp(row["valid_to"]),
        )
        for row in rows
    )


def _group_by_player[T: _Ownership | _Projection](
    rows: tuple[T, ...],
) -> dict[int, tuple[T, ...]]:
    grouped: dict[int, list[T]] = defaultdict(list)
    for row in rows:
        grouped[row.player_id].append(row)
    return {player_id: tuple(items) for player_id, items in grouped.items()}


def _raw_feature(
    salary: _Salary,
    *,
    episodes: tuple[_EpisodeInput, ...],
    ownership: tuple[_Ownership, ...],
    projections: tuple[_Projection, ...],
    as_of: datetime,
    config: HeatConfig,
) -> _RawFeature:
    player_episodes = tuple(
        episode for episode in episodes if episode.player_id == salary.player_id
    )
    current = _episode_heats(player_episodes, ownership, as_of, config, strict_origin=True)
    prior_at = as_of - config.velocity_window
    older_at = prior_at - config.velocity_window
    # Novelty is a property of the story at the decision instant. Re-deciding it at t-6h
    # and t-12h would turn a gate flip into a spurious velocity, so the earlier instants
    # reuse the as_of decision per episode.
    fixed_novelty = {heat.episode_id: heat.novelty for heat in current}
    prior = _episode_heats(
        player_episodes, ownership, prior_at, config, fixed_novelty=fixed_novelty
    )
    older = _episode_heats(
        player_episodes, ownership, older_at, config, fixed_novelty=fixed_novelty
    )
    aggregate = _aggregate_heat(current)
    prior_signed = math.fsum(heat.heat for heat in prior)
    older_signed = math.fsum(heat.heat for heat in older)
    velocity = aggregate["h_signed"] - prior_signed
    prior_velocity = prior_signed - older_signed

    baseline = _latest_snapshot(ownership, as_of)
    baseline_previous = (
        None
        if baseline is None
        else _latest_snapshot(
            ownership,
            prior_at,
            source=baseline.source,
        )
    )
    projection = _latest_snapshot(projections, as_of)
    projection_previous = (
        None
        if projection is None
        else _latest_snapshot(
            projections,
            prior_at,
            source=projection.source,
        )
    )
    aggregate["h_velocity_6h"] = velocity
    aggregate["h_acceleration"] = velocity - prior_velocity
    ownership_ids = {
        snapshot_id
        for heat in (*current, *prior, *older)
        for snapshot_id in heat.ownership_baseline_ids
    }
    if baseline is not None:
        ownership_ids.add(baseline.snapshot_id)
    if baseline_previous is not None:
        ownership_ids.add(baseline_previous.snapshot_id)
    return _RawFeature(
        salary=salary,
        baseline=baseline,
        baseline_previous=baseline_previous,
        projection=projection,
        projection_previous=projection_previous,
        values=aggregate,
        episode_ids=tuple(sorted(heat.episode_id for heat in current)),
        ownership_baseline_ids=tuple(sorted(ownership_ids)),
    )


def _episode_heats(
    episodes: tuple[_EpisodeInput, ...],
    ownership: tuple[_Ownership, ...],
    evaluation_at: datetime,
    config: HeatConfig,
    *,
    strict_origin: bool = False,
    fixed_novelty: Mapping[str, float] | None = None,
) -> tuple[EpisodeHeat, ...]:
    heats: list[EpisodeHeat] = []
    for episode in episodes:
        heat = _episode_heat(
            episode,
            ownership,
            evaluation_at,
            config,
            strict_origin=strict_origin,
            fixed_novelty=None if fixed_novelty is None else fixed_novelty.get(episode.episode_id),
        )
        if heat is not None:
            heats.append(heat)
    return tuple(heats)


def _episode_heat(
    episode: _EpisodeInput,
    ownership: tuple[_Ownership, ...],
    evaluation_at: datetime,
    config: HeatConfig,
    *,
    strict_origin: bool = False,
    fixed_novelty: float | None = None,
) -> EpisodeHeat | None:
    members = tuple(
        member for member in episode.members if _member_eligible(member, evaluation_at)
    )
    if not members:
        return None
    if not any(member.relation == "origin" for member in members):
        # At an earlier evaluation instant the origin may not have been extracted yet
        # (a retried or re-extracted claim): the episode did not exist then. Only the
        # as_of evaluation, whose inputs the snapshot check already proved complete, may
        # treat a missing origin as corruption.
        if strict_origin:
            raise FeatureInputError(f"episode {episode.episode_id} has no available origin")
        return None
    items: dict[int, list[_EpisodeMember]] = defaultdict(list)
    for member in members:
        items[member.source_item_id].append(member)

    factor_items = tuple(
        item_members
        for _, item_members in sorted(items.items())
        if any(member.relation != "derivative" for member in item_members)
    )
    if not factor_items:
        raise FeatureInputError(f"episode {episode.episode_id} has no non-derivative item")

    item_directions: list[float] = []
    item_qualities: list[float] = []
    item_specificities: list[float] = []
    for item_members in factor_items:
        directions: list[float] = []
        qualities: list[float] = []
        specificities: list[float] = []
        for member in item_members:
            if member.relation == "derivative":
                continue
            try:
                direction = _DIRECTION_VALUES[member.roster_direction]
                class_quality = config.evidence_class_quality[member.evidence_class]
                basis_quality = config.evidence_basis_quality[member.evidence_basis]
                source_quality = config.source_families[member.source_family].quality
            except KeyError as error:
                raise FeatureInputError(
                    f"episode {episode.episode_id} uses unconfigured Stage 1 value "
                    f"{error.args[0]!r}"
                ) from error
            directions.append(direction)
            qualities.append((class_quality + basis_quality + source_quality) / 3.0)
            specificities.append((member.specificity + member.actionability) / 2.0)
        item_directions.append(_mean(directions))
        item_qualities.append(_mean(qualities))
        item_specificities.append(_mean(specificities))

    direction = _mean(item_directions)
    quality_raw = _mean(item_qualities)
    specificity_raw = _mean(item_specificities)
    source_families_by_source: dict[str, str] = {}
    for member in members:
        prior_family = source_families_by_source.setdefault(
            member.source_id, member.source_family
        )
        if prior_family != member.source_family:
            raise FeatureInputError(
                f"source {member.source_id!r} has conflicting frozen families in episode "
                f"{episode.episode_id}"
            )
    reach = len(source_families_by_source)
    independence_raw = len(set(source_families_by_source.values())) / reach

    origin = next(member for member in members if member.relation == "origin")
    try:
        source_class = config.source_families[origin.source_family].source_class
    except KeyError as error:
        raise FeatureInputError(
            f"episode {episode.episode_id} uses unconfigured source family "
            f"{origin.source_family!r}"
        ) from error
    half_life = config.half_life_hours[source_class]
    last_non_derivative = max(
        member.item_observed_at
        for item_members in factor_items
        for member in item_members
        if member.relation != "derivative"
    )
    age_hours = (evaluation_at - last_non_derivative).total_seconds() / 3600.0
    novelty, baseline_ids = _episode_novelty(
        direction,
        episode_opened_at=episode.opened_at,
        evaluation_at=evaluation_at,
        ownership=ownership,
        minimum_move=config.novelty_min_baseline_move,
    )
    if fixed_novelty is not None:
        novelty = fixed_novelty
    heat = calculate_episode_heat(
        direction=direction,
        quality=quality_raw,
        specificity=specificity_raw,
        novelty=novelty,
        independence=independence_raw,
        reach=reach,
        age_hours=age_hours,
        half_life_hours=half_life,
        soft_factor_floor=config.soft_factor_floor,
    )
    heat_without_novelty = calculate_episode_heat(
        direction=direction,
        quality=quality_raw,
        specificity=specificity_raw,
        novelty=1.0,
        independence=independence_raw,
        reach=reach,
        age_hours=age_hours,
        half_life_hours=half_life,
        soft_factor_floor=config.soft_factor_floor,
    )
    independent_sources: dict[str, SourceClass] = {}
    for item_members in factor_items:
        for member in item_members:
            if member.relation == "derivative":
                continue
            family = config.source_families.get(member.source_family)
            if family is None:
                raise FeatureInputError(
                    f"episode {episode.episode_id} uses unconfigured source family "
                    f"{member.source_family!r}"
                )
            independent_sources[member.source_id] = family.source_class
    n_events = sum(
        any(member.relation in _EVENT_RELATIONS for member in item_members)
        for item_members in items.values()
    )
    return EpisodeHeat(
        episode_id=episode.episode_id,
        source_class=source_class,
        direction=direction,
        quality_raw=quality_raw,
        quality=map_soft_factor(quality_raw, floor=config.soft_factor_floor),
        specificity_raw=specificity_raw,
        specificity=map_soft_factor(specificity_raw, floor=config.soft_factor_floor),
        novelty=novelty,
        independence_raw=independence_raw,
        independence=map_soft_factor(independence_raw, floor=config.soft_factor_floor),
        reach=reach,
        age_hours=age_hours,
        half_life_hours=half_life,
        heat=heat,
        heat_without_novelty=heat_without_novelty,
        n_events=n_events,
        item_count=len(items),
        source_families_by_source=tuple(sorted(source_families_by_source.items())),
        independent_classes_by_source=tuple(sorted(independent_sources.items())),
        ownership_baseline_ids=baseline_ids,
    )


def _episode_novelty(
    direction: float,
    *,
    episode_opened_at: datetime,
    evaluation_at: datetime,
    ownership: tuple[_Ownership, ...],
    minimum_move: float,
) -> tuple[float, tuple[int, ...]]:
    """§12.2.2 novelty, as a coarse gate: 0.0 only for a material aligned baseline move.

    A 0.1-point tick from any cause is not "the story is already in the baseline"; the
    zero-gate caution in the design doc is exactly about deleting a real episode on thin
    evidence, so the move must be at least ``minimum_move`` in the episode's direction.
    """

    current = _latest_snapshot(ownership, evaluation_at)
    if current is None:
        return 1.0, ()
    before = _latest_snapshot(ownership, episode_opened_at, source=current.source)
    ids = {current.snapshot_id}
    if before is None:
        return 1.0, tuple(ids)
    ids.add(before.snapshot_id)
    baseline_change = current.ownership - before.ownership
    if (
        current.snapshot_id != before.snapshot_id
        and baseline_change * direction > 0
        and abs(baseline_change) >= minimum_move
    ):
        return 0.0, tuple(sorted(ids))
    return 1.0, tuple(sorted(ids))


def _aggregate_heat(heats: tuple[EpisodeHeat, ...]) -> dict[str, float]:
    signed = math.fsum(heat.heat for heat in heats)
    absolute = math.fsum(abs(heat.heat) for heat in heats)
    values = {
        "h_signed": signed,
        "h_absolute": absolute,
        "h_mainstream": math.fsum(
            heat.heat for heat in heats if heat.source_class == "mainstream"
        ),
        "h_dfs": math.fsum(heat.heat for heat in heats if heat.source_class == "dfs"),
        "h_team_fan": math.fsum(
            heat.heat for heat in heats if heat.source_class == "team_fan"
        ),
        "h_velocity_6h": 0.0,
        "h_acceleration": 0.0,
        "h_consensus": abs(signed) / absolute if absolute > 0 else 0.0,
        "h_source_entropy": _source_class_entropy(heats),
        "h_novelty_share": _novelty_share(heats),
        "unique_episode_count": float(len(heats)),
        "unique_source_count": 0.0,
        "source_overlap_index": 0.0,
    }
    sources: dict[str, str] = {}
    for heat in heats:
        for source_id, family in heat.source_families_by_source:
            prior = sources.setdefault(source_id, family)
            if prior != family:
                raise FeatureInputError(
                    f"source {source_id!r} has inconsistent source families across episodes"
                )
    values["unique_source_count"] = float(len(sources))
    if sources:
        values["source_overlap_index"] = 1.0 - (
            len(set(sources.values())) / len(sources)
        )
    return values


def _source_class_entropy(heats: tuple[EpisodeHeat, ...]) -> float:
    sources: dict[str, SourceClass] = {}
    for heat in heats:
        for source_id, source_class in heat.independent_classes_by_source:
            prior = sources.setdefault(source_id, source_class)
            if prior != source_class:
                raise FeatureInputError(
                    f"source {source_id!r} maps to conflicting heat classes"
                )
    if not sources:
        return 0.0
    counts = Counter(sources.values())
    total = len(sources)
    entropy = -math.fsum(
        (count / total) * math.log(count / total) for count in counts.values()
    )
    return max(0.0, min(1.0, entropy / math.log(len(_SOURCE_CLASSES))))


def _novelty_share(heats: tuple[EpisodeHeat, ...]) -> float:
    denominator = math.fsum(abs(heat.heat_without_novelty) for heat in heats)
    if denominator == 0:
        return 0.0
    numerator = math.fsum(abs(heat.heat) for heat in heats)
    return max(0.0, min(1.0, numerator / denominator))


def _standardize(
    rows: tuple[_RawFeature, ...],
    winsor_limit: float,
) -> dict[int, dict[str, float]]:
    result: dict[int, dict[str, float]] = {
        row.salary.player_id: {} for row in rows
    }
    for field in _STANDARDIZED_FIELDS:
        values = [row.values[field] for row in rows]
        mean = math.fsum(values) / len(values)
        variance = math.fsum((value - mean) ** 2 for value in values) / len(values)
        standard_deviation = math.sqrt(max(0.0, variance))
        for row, value in zip(rows, values, strict=True):
            standardized = 0.0 if standard_deviation == 0 else (value - mean) / standard_deviation
            result[row.salary.player_id][field] = max(
                -winsor_limit, min(winsor_limit, standardized)
            )
    return result


def _semantic_payload(
    raw: _RawFeature,
    standardized: dict[str, float],
    *,
    slate_id: int,
    site: Site,
    role: Role,
    as_of: datetime,
    config: HeatConfig,
) -> dict[str, Any]:
    baseline_change = (
        None
        if raw.baseline is None or raw.baseline_previous is None
        else raw.baseline.ownership - raw.baseline_previous.ownership
    )
    projection_change = (
        None
        if raw.projection is None or raw.projection_previous is None
        else raw.projection.projection_mean - raw.projection_previous.projection_mean
    )
    feature_key = {
        "as_of": utc_timestamp(as_of),
        "feature_version": config.feature_version,
        "player_id": raw.salary.player_id,
        "site": site,
        "slate_id": slate_id,
    }
    feature_id = "feature-" + _sha256_json(feature_key)[:32]
    return {
        "feature_id": feature_id,
        "player_id": raw.salary.player_id,
        "slate_id": slate_id,
        "contest_archetype": None,
        "site": site,
        "role": role,
        "as_of": utc_timestamp(as_of),
        "baseline_ownership": None if raw.baseline is None else raw.baseline.ownership,
        "baseline_ownership_change_6h": baseline_change,
        "projection_change_6h": projection_change,
        "salary": raw.salary.salary,
        "value_rank": None,
        "position_scarcity": None,
        "alternative_quality_index": None,
        **{field: raw.values[field] for field in _STANDARDIZED_FIELDS},
        **{f"{field}_z": standardized[field] for field in _STANDARDIZED_FIELDS},
        "unique_author_count": None,
        "unique_author_count_z": None,
        "model_version": None,
        "feature_version": config.feature_version,
        "formula_version": config.formula_version,
        "feature_config_sha256": config.config_sha256,
        "episode_method_version": config.episode_method_version,
        "episode_ids_json": raw.episode_ids,
        "ownership_baseline_ids_json": raw.ownership_baseline_ids,
        "baseline_ownership_snapshot_id": (
            None if raw.baseline is None else raw.baseline.snapshot_id
        ),
        "baseline_previous_snapshot_id": (
            None if raw.baseline_previous is None else raw.baseline_previous.snapshot_id
        ),
        "projection_snapshot_id": (
            None if raw.projection is None or raw.projection_previous is None
            else raw.projection.snapshot_id
        ),
        "projection_previous_snapshot_id": (
            None if raw.projection_previous is None else raw.projection_previous.snapshot_id
        ),
        "salary_id": raw.salary.salary_id,
    }


def _feature_row(
    payload: dict[str, Any],
    *,
    input_sha256: str,
    built_at: datetime,
    run_id: str,
) -> NarrativeFeatureRow:
    values = dict(payload)
    values.update(
        {
            "input_sha256": input_sha256,
            "source": DERIVATION_SOURCE,
            "published_at": None,
            "observed_at": built_at,
            "ingested_at": built_at,
            "effective_at": payload["as_of"],
            "valid_from": built_at,
            "valid_to": None,
            "source_version": payload["feature_version"],
            "run_id": run_id,
        }
    )
    return NarrativeFeatureRow.model_validate(values)


def _latest_snapshot[T: _Ownership | _Projection](
    rows: tuple[T, ...],
    at: datetime,
    *,
    source: str | None = None,
) -> T | None:
    eligible = [
        row
        for row in rows
        if (source is None or row.source == source) and _snapshot_eligible(row, at)
    ]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda row: (row.observed_at, row.ingested_at, row.snapshot_id, row.source),
    )


def _snapshot_eligible(row: _Ownership | _Projection, at: datetime) -> bool:
    return (
        row.observed_at <= at
        and row.ingested_at <= at
        and row.valid_from <= at
        and (row.valid_to is None or at < row.valid_to)
    )


def _member_eligible(member: _EpisodeMember, at: datetime) -> bool:
    return (
        member.claim_observed_at <= at
        and member.claim_ingested_at <= at
        and member.claim_valid_from <= at
        and (member.claim_valid_to is None or at < member.claim_valid_to)
        and member.item_observed_at <= at
        and member.item_ingested_at <= at
        and member.item_valid_from <= at
        and (member.item_valid_to is None or at < member.item_valid_to)
    )


def _mean(values: list[float]) -> float:
    if not values:
        raise FeatureInputError("cannot average an empty factor set")
    return math.fsum(values) / len(values)


def _sha256_json(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _insert_row(
    connection: sqlite3.Connection,
    table: str,
    row: ModelRunRow | NarrativeFeatureVersionRow | NarrativeFeatureRow,
) -> None:
    values = row.db_values()
    columns = ", ".join(values)
    placeholders = ", ".join(f":{column}" for column in values)
    connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", values)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FeatureInputError(f"stored timestamp is timezone-naive: {value!r}")
    return parsed.astimezone(UTC)


def _optional_timestamp(value: object) -> datetime | None:
    return None if value is None else _parse_timestamp(str(value))


# Explicit long name for callers that prefer the table name over the CLI verb.
build_narrative_features = build_features

