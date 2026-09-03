"""Versioned configuration for the first ownership-offset model.

A leaf module on purpose (standard library only, no ``narrative_alpha`` import): the
Stage 4 permission layer in :mod:`narrative_alpha.ownership_routing` sits below the
``narrative_alpha.ownership`` package in the import graph — that package reaches ``build``
through ``ops.results`` → ``report_cli`` — and both must read the same file. Anything that
needs a threshold, a cap, or the config hash imports it from here rather than mirroring the
values, so there is one copy of ``config/ownership_model.toml``'s meaning in the tree.
"""

from __future__ import annotations

import hashlib
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _shipped(relative: str) -> Path:
    """The shipped config file: under the source tree when run from it, else the cwd.

    Every lane already resolves `config/` against the repository root, but this file is
    read by a module that `build` imports, so the path must not depend on where the
    process happened to start.
    """

    candidate = _REPOSITORY_ROOT / relative
    return candidate if candidate.is_file() else Path(relative)


DEFAULT_OWNERSHIP_CONFIG_PATH = _shipped("config/ownership_model.toml")
GovernanceStatus = Literal["UNVALIDATED", "TESTING", "PROVISIONAL", "VALIDATED"]
SlateKind = Literal["classic", "showdown"]
_STATUSES: tuple[GovernanceStatus, ...] = (
    "UNVALIDATED",
    "TESTING",
    "PROVISIONAL",
    "VALIDATED",
)


class OwnershipConfigError(ValueError):
    """Raised when ownership configuration is missing or internally inconsistent."""


@dataclass(frozen=True)
class PriorConfig:
    beta_signed_scale: float
    beta_dfs_scale: float
    beta_velocity_scale: float
    intercept_scale: float


@dataclass(frozen=True)
class CapConfig:
    multiplier: float
    maximum_delta: float


@dataclass(frozen=True)
class CalibrationConfig:
    draftkings_classic_slots: float
    fanduel_classic_slots: float
    showdown_captain_slots: float
    showdown_flex_slots: float
    tolerance: float
    maximum_iterations: int


@dataclass(frozen=True)
class EvaluationConfig:
    material_delta: float
    minimum_weeks: int
    beat_rule: str


@dataclass(frozen=True)
class OwnershipModelConfig:
    config_version: str
    model_version: str
    feature_version: str
    amplitude: float
    probability_epsilon: float
    posterior_draws: int
    posterior_seed: int
    priors: PriorConfig
    evaluation: EvaluationConfig
    calibration: CalibrationConfig
    caps: dict[tuple[SlateKind, GovernanceStatus], CapConfig]
    config_sha256: str

    # The exact bytes this configuration was read from, so a decision can freeze them.
    raw_bytes: bytes = b""

    def cap(self, slate_kind: SlateKind, status: GovernanceStatus) -> CapConfig:
        return self.caps[(slate_kind, status)]

    def cap_for(self, slate_kind: str, status: str) -> CapConfig | None:
        """The cap for a governance status read back from the store, or ``None``.

        The store holds statuses as text, and a row carrying one this configuration has no
        cap for is a fact the caller must refuse on rather than a key error here.
        """

        return self.caps.get(cast("tuple[SlateKind, GovernanceStatus]", (slate_kind, status)))


def load_ownership_config(
    path: Path = DEFAULT_OWNERSHIP_CONFIG_PATH,
) -> OwnershipModelConfig:
    """Load and validate the exact versioned TOML whose bytes a run records."""

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise OwnershipConfigError(f"cannot read ownership config {path}: {error}") from error
    return load_ownership_config_bytes(raw, source=str(path))


def load_ownership_config_bytes(raw: bytes, *, source: str) -> OwnershipModelConfig:
    """Validate frozen configuration bytes — the shipped file, or a decision's artifact."""

    path = source
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise OwnershipConfigError(f"ownership config is not valid UTF-8 TOML: {path}") from error

    priors = _table(parsed, "priors")
    if priors.get("beta_signed_family") != "half_normal":
        raise OwnershipConfigError("beta_signed prior must be half_normal")
    for name in ("beta_dfs_family", "beta_velocity_family", "intercept_family"):
        if priors.get(name) != "normal":
            raise OwnershipConfigError(f"{name} must be normal")

    evaluation = _table(parsed, "evaluation")
    calibration = _table(parsed, "calibration")
    caps_table = _table(parsed, "caps")
    caps: dict[tuple[SlateKind, GovernanceStatus], CapConfig] = {}
    for slate_kind in cast(tuple[SlateKind, ...], ("classic", "showdown")):
        by_status = _table(caps_table, slate_kind)
        for status in _STATUSES:
            values = _table(by_status, status)
            multiplier = _fraction(values, "multiplier")
            maximum_points = _positive_float(values, "maximum_points")
            caps[(slate_kind, status)] = CapConfig(
                multiplier=multiplier,
                maximum_delta=maximum_points / 100.0,
            )

    result = OwnershipModelConfig(
        config_version=_text(parsed, "config_version"),
        model_version=_text(parsed, "model_version"),
        feature_version=_text(parsed, "feature_version"),
        amplitude=_positive_float(parsed, "amplitude"),
        probability_epsilon=_probability_epsilon(parsed, "probability_epsilon"),
        posterior_draws=_positive_int(parsed, "posterior_draws"),
        posterior_seed=_integer(parsed, "posterior_seed"),
        priors=PriorConfig(
            beta_signed_scale=_positive_float(priors, "beta_signed_scale"),
            beta_dfs_scale=_positive_float(priors, "beta_dfs_scale"),
            beta_velocity_scale=_positive_float(priors, "beta_velocity_scale"),
            intercept_scale=_positive_float(priors, "intercept_scale"),
        ),
        evaluation=EvaluationConfig(
            material_delta=_positive_float(evaluation, "material_delta_points") / 100.0,
            minimum_weeks=_positive_int(evaluation, "minimum_weeks"),
            beat_rule=_text(evaluation, "beat_rule"),
        ),
        calibration=CalibrationConfig(
            draftkings_classic_slots=_positive_float(
                calibration, "draftkings_classic_slots"
            ),
            fanduel_classic_slots=_positive_float(calibration, "fanduel_classic_slots"),
            showdown_captain_slots=_positive_float(calibration, "showdown_captain_slots"),
            showdown_flex_slots=_positive_float(calibration, "showdown_flex_slots"),
            tolerance=_positive_float(calibration, "tolerance"),
            maximum_iterations=_positive_int(calibration, "maximum_iterations"),
        ),
        caps=caps,
        config_sha256=hashlib.sha256(raw).hexdigest(),
        raw_bytes=raw,
    )
    if result.evaluation.beat_rule != "mae_and_log_score_and_brier":
        raise OwnershipConfigError("unsupported evaluation beat_rule")
    return result


def _table(values: dict[str, Any], key: str) -> dict[str, Any]:
    value = values.get(key)
    if not isinstance(value, dict):
        raise OwnershipConfigError(f"{key} must be a TOML table")
    return cast(dict[str, Any], value)


def _text(values: dict[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OwnershipConfigError(f"{key} must be non-empty text")
    return value.strip()


def _positive_float(values: dict[str, Any], key: str) -> float:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OwnershipConfigError(f"{key} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise OwnershipConfigError(f"{key} must be finite and positive")
    return result


def _fraction(values: dict[str, Any], key: str) -> float:
    value = _positive_float(values, key)
    if value > 1:
        raise OwnershipConfigError(f"{key} must not exceed 1")
    return value


def _probability_epsilon(values: dict[str, Any], key: str) -> float:
    value = _positive_float(values, key)
    if value >= 0.5:
        raise OwnershipConfigError(f"{key} must be below 0.5")
    return value


def _positive_int(values: dict[str, Any], key: str) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OwnershipConfigError(f"{key} must be a positive integer")
    return value


def _integer(values: dict[str, Any], key: str) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise OwnershipConfigError(f"{key} must be an integer")
    return value
