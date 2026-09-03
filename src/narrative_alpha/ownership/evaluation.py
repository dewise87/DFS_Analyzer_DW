"""Strict expanding-window evaluation beside the untouched vendor baseline."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import minimize  # type: ignore[import-untyped]

from narrative_alpha import __version__
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.ownership.data import MissingTrainingRow, TrainingData
from narrative_alpha.ownership.model import (
    OwnershipModelError,
    OwnershipTrainingRow,
    fit_ownership_model,
    is_synthetic_source,
    predict_ownership,
)
from narrative_alpha.ownership_config import OwnershipModelConfig


class OwnershipEvaluationError(OwnershipModelError):
    """Raised when an ownership evaluation cannot remain prequential."""


@dataclass(frozen=True)
class MetricComparison:
    model: float | None
    baseline: float | None


@dataclass(frozen=True)
class EvaluationFold:
    test_season: int
    test_week: int
    training_weeks: tuple[tuple[int, int], ...]
    training_rows: int
    scored_rows: int


@dataclass(frozen=True)
class OwnershipEvaluationReport:
    site: str
    contest_archetype: str
    model_version: str
    feature_version: str
    config_sha256: str
    evaluated_at: datetime
    folds: tuple[EvaluationFold, ...]
    rows_scored: int
    missing_feature_rows: int
    missing_baseline_rows: int
    missing_rows: tuple[MissingTrainingRow, ...]
    label_set_sha256: str
    mae_percentage_points: MetricComparison
    log_score: MetricComparison
    brier_score: MetricComparison
    calibration_slope: MetricComparison
    calibration_intercept: MetricComparison
    top20_rank_correlation: MetricComparison
    material_delta_directional_accuracy: MetricComparison
    material_delta_rows: int
    beat_baseline: bool
    model_eval_id: str | None = None
    run_id: str | None = None
    report_path: Path | None = None


def evaluate_forward_chaining(
    data: TrainingData,
    *,
    config: OwnershipModelConfig,
    site: str,
    contest_archetype: str,
    evaluated_at: datetime | None = None,
    allow_synthetic: bool = False,
) -> OwnershipEvaluationReport:
    """Fit on weeks strictly before k and score only week k, for every fold."""

    rows = data.rows
    if not rows:
        raise OwnershipEvaluationError("no complete ownership rows are available to evaluate")
    synthetic = tuple(row for row in rows if is_synthetic_source(row.label_source))
    if synthetic and not allow_synthetic:
        raise OwnershipEvaluationError(
            f"refusing {len(synthetic)} synthetic fixture/test label row(s); "
            "allow_synthetic=True is test-only"
        )
    weeks = tuple(sorted({(row.season, row.week) for row in rows}))
    roles = ("captain", "flex") if contest_archetype == "showdown" else ("classic",)
    folds: list[EvaluationFold] = []
    model_probabilities: list[float] = []
    baseline_probabilities: list[float] = []
    actual_probabilities: list[float] = []
    fold_top20_model: list[float] = []
    fold_top20_baseline: list[float] = []

    for fold_index, test_week in enumerate(weeks):
        training = tuple(
            row for row in rows if (row.season, row.week) < test_week
        )
        scored = tuple(row for row in rows if (row.season, row.week) == test_week)
        if any((row.season, row.week) >= test_week for row in training):
            raise OwnershipEvaluationError("forward-chaining fold contains its future")
        model = fit_ownership_model(
            training,
            config=config,
            contest_archetype=contest_archetype,
            site=site,
            allow_synthetic=allow_synthetic,
            roles=roles,
        )
        predictions = predict_ownership(
            model,
            scored,
            draw_count=config.posterior_draws,
            seed=config.posterior_seed + fold_index,
        )
        predicted = [prediction.ownership_p50 for prediction in predictions]
        baseline = [row.baseline_ownership for row in scored]
        actual = [row.actual_ownership for row in scored]
        model_probabilities.extend(predicted)
        baseline_probabilities.extend(baseline)
        actual_probabilities.extend(actual)
        top_indices = sorted(range(len(scored)), key=lambda index: actual[index], reverse=True)[:20]
        model_rank = _spearman(
            [predicted[index] for index in top_indices],
            [actual[index] for index in top_indices],
        )
        baseline_rank = _spearman(
            [baseline[index] for index in top_indices],
            [actual[index] for index in top_indices],
        )
        if model_rank is not None:
            fold_top20_model.append(model_rank)
        if baseline_rank is not None:
            fold_top20_baseline.append(baseline_rank)
        folds.append(
            EvaluationFold(
                test_season=test_week[0],
                test_week=test_week[1],
                training_weeks=tuple(sorted({(row.season, row.week) for row in training})),
                training_rows=len(training),
                scored_rows=len(scored),
            )
        )

    model_calibration = _calibration(model_probabilities, actual_probabilities)
    baseline_calibration = _calibration(baseline_probabilities, actual_probabilities)
    material = [
        index
        for index, (actual, baseline) in enumerate(
            zip(actual_probabilities, baseline_probabilities, strict=True)
        )
        if abs(actual - baseline) > config.evaluation.material_delta
    ]
    model_directional = _directional_accuracy(
        model_probabilities, baseline_probabilities, actual_probabilities, material
    )
    baseline_directional = 0.0 if material else None
    model_mae = _mae(model_probabilities, actual_probabilities) * 100.0
    baseline_mae = _mae(baseline_probabilities, actual_probabilities) * 100.0
    model_log_score = _log_score(model_probabilities, actual_probabilities)
    baseline_log_score = _log_score(baseline_probabilities, actual_probabilities)
    model_brier = _brier(model_probabilities, actual_probabilities)
    baseline_brier = _brier(baseline_probabilities, actual_probabilities)
    beat = (
        model_mae < baseline_mae
        and model_log_score > baseline_log_score
        and model_brier < baseline_brier
    )
    label_set_sha256 = _label_set_sha256(rows)
    return OwnershipEvaluationReport(
        site=site,
        contest_archetype=contest_archetype,
        model_version=config.model_version,
        feature_version=config.feature_version,
        config_sha256=config.config_sha256,
        evaluated_at=ensure_utc(evaluated_at or datetime.now(UTC)),
        folds=tuple(folds),
        rows_scored=len(actual_probabilities),
        missing_feature_rows=data.missing_feature_rows,
        missing_baseline_rows=data.missing_baseline_rows,
        missing_rows=data.missing,
        label_set_sha256=label_set_sha256,
        mae_percentage_points=MetricComparison(model=model_mae, baseline=baseline_mae),
        log_score=MetricComparison(model=model_log_score, baseline=baseline_log_score),
        brier_score=MetricComparison(model=model_brier, baseline=baseline_brier),
        calibration_slope=MetricComparison(
            model=model_calibration[1], baseline=baseline_calibration[1]
        ),
        calibration_intercept=MetricComparison(
            model=model_calibration[0], baseline=baseline_calibration[0]
        ),
        top20_rank_correlation=MetricComparison(
            model=_mean_or_none(fold_top20_model),
            baseline=_mean_or_none(fold_top20_baseline),
        ),
        material_delta_directional_accuracy=MetricComparison(
            model=model_directional, baseline=baseline_directional
        ),
        material_delta_rows=len(material),
        beat_baseline=beat,
    )


def render_evaluation_report(report: OwnershipEvaluationReport) -> str:
    """Render a stable text report with every metric beside the vendor baseline."""

    output = io.StringIO(newline="")
    output.write("OWNERSHIP MODEL PREQUENTIAL EVALUATION\n")
    output.write(f"evaluated_at={utc_timestamp(report.evaluated_at)}\n")
    output.write(f"site={report.site}\n")
    output.write(f"contest_archetype={report.contest_archetype}\n")
    output.write(f"model_version={report.model_version}\n")
    output.write(f"feature_version={report.feature_version}\n")
    output.write(f"config_sha256={report.config_sha256}\n")
    output.write("split=expanding-window; every training week is strictly before its test week\n")
    output.write(f"rows_scored={report.rows_scored}\n")
    output.write(f"missing_feature_rows={report.missing_feature_rows}\n")
    output.write(f"missing_baseline_rows={report.missing_baseline_rows}\n")
    for missing in report.missing_rows:
        reasons = "|".join(
            reason
            for reason, present in (
                ("feature", missing.missing_feature),
                ("baseline", missing.missing_baseline),
            )
            if present
        )
        output.write(
            "missing="
            f"{missing.season}-W{missing.week:02d}/slate-{missing.slate_id}/"
            f"player-{missing.player_id}/{missing.role}/{reasons}\n"
        )
    output.write("\nfold,test_week,training_weeks,training_rows,scored_rows\n")
    for index, fold in enumerate(report.folds, start=1):
        training_weeks = "|".join(f"{season}-W{week:02d}" for season, week in fold.training_weeks)
        output.write(
            f"{index},{fold.test_season}-W{fold.test_week:02d},{training_weeks},"
            f"{fold.training_rows},{fold.scored_rows}\n"
        )
    output.write("\nmetric,model,vendor_baseline,better_direction\n")
    rows = (
        ("mae_percentage_points", report.mae_percentage_points, "lower"),
        ("log_score", report.log_score, "higher"),
        ("brier_score", report.brier_score, "lower"),
        ("calibration_slope", report.calibration_slope, "closer_to_1"),
        ("calibration_intercept", report.calibration_intercept, "closer_to_0"),
        ("top20_rank_correlation", report.top20_rank_correlation, "higher"),
        (
            "material_delta_directional_accuracy",
            report.material_delta_directional_accuracy,
            "higher",
        ),
    )
    for name, comparison, direction in rows:
        output.write(
            f"{name},{_format_metric(comparison.model)},"
            f"{_format_metric(comparison.baseline)},{direction}\n"
        )
    output.write(f"material_delta_rows={report.material_delta_rows}\n")
    verdict = "YES" if report.beat_baseline else "NO"
    output.write(f"OUT-OF-WEEK: model beat untouched vendor baseline = {verdict}\n")
    return output.getvalue()


def persist_evaluation(
    connection: sqlite3.Connection,
    report: OwnershipEvaluationReport,
    *,
    report_directory: Path,
) -> OwnershipEvaluationReport:
    """Write the text artifact and matching immutable model_evals row."""

    at = ensure_utc(report.evaluated_at)
    timestamp = utc_timestamp(at)
    stamp = timestamp.replace("-", "").replace(":", "").replace(".", "")
    path = report_directory / "ownership" / f"{report.contest_archetype}-{stamp}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_evaluation_report(report)
    with path.open("x", encoding="utf-8", newline="") as handle:
        handle.write(rendered)
        handle.flush()
        os.fsync(handle.fileno())

    run_id = f"ownership-eval-{uuid4().hex}"
    model_eval_id = f"model-eval-{uuid4().hex}"
    metrics = _metrics_json(report)
    connection.execute("SAVEPOINT ownership_eval")
    try:
        connection.execute(
            """
            INSERT INTO model_runs(
                run_id, run_type, started_at, completed_at, status, code_version,
                config_sha256, parent_run_id, error_message, created_at
            ) VALUES (?, 'ownership_eval', ?, NULL, 'running', ?, ?, NULL, NULL, ?)
            """,
            (run_id, timestamp, __version__, report.config_sha256, timestamp),
        )
        connection.execute(
            """
            INSERT INTO model_evals(
                model_eval_id, evaluation_kind, prompt_version_id, model_id,
                label_set_sha256, item_count, label_row_count, metrics_json,
                ownership_archetype, ownership_site, feature_version, config_sha256,
                report_path, beat_baseline, source, published_at, observed_at,
                ingested_at, effective_at, valid_from, valid_to, source_version, run_id
            ) VALUES (?, 'ownership', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      'ownership-forward-chain', NULL, ?, ?, ?, ?, NULL,
                      'ownership-eval-v1', ?)
            """,
            (
                model_eval_id,
                report.model_version,
                report.label_set_sha256,
                report.rows_scored,
                report.rows_scored,
                json.dumps(metrics, sort_keys=True, separators=(",", ":")),
                report.contest_archetype,
                report.site,
                report.feature_version,
                report.config_sha256,
                str(path),
                int(report.beat_baseline),
                timestamp,
                timestamp,
                timestamp,
                timestamp,
                run_id,
            ),
        )
        cursor = connection.execute(
            """
            UPDATE model_runs SET completed_at = ?, status = 'succeeded'
            WHERE run_id = ? AND status = 'running'
            """,
            (timestamp, run_id),
        )
        if cursor.rowcount != 1:
            raise OwnershipEvaluationError("could not complete ownership evaluation run")
    except Exception:
        connection.execute("ROLLBACK TO ownership_eval")
        connection.execute("RELEASE ownership_eval")
        raise
    else:
        connection.execute("RELEASE ownership_eval")
    return OwnershipEvaluationReport(
        **{
            **report.__dict__,
            "model_eval_id": model_eval_id,
            "run_id": run_id,
            "report_path": path,
        }
    )


def _metrics_json(report: OwnershipEvaluationReport) -> dict[str, object]:
    return {
        "split": "forward_chaining",
        "folds": [fold.__dict__ for fold in report.folds],
        "rows_scored": report.rows_scored,
        "missing_feature_rows": report.missing_feature_rows,
        "missing_baseline_rows": report.missing_baseline_rows,
        "missing_rows": [row.__dict__ for row in report.missing_rows],
        "mae_percentage_points": report.mae_percentage_points.__dict__,
        "log_score": report.log_score.__dict__,
        "brier_score": report.brier_score.__dict__,
        "calibration_slope": report.calibration_slope.__dict__,
        "calibration_intercept": report.calibration_intercept.__dict__,
        "top20_rank_correlation": report.top20_rank_correlation.__dict__,
        "material_delta_directional_accuracy": (
            report.material_delta_directional_accuracy.__dict__
        ),
        "material_delta_rows": report.material_delta_rows,
        "beat_baseline": report.beat_baseline,
    }


def _label_set_sha256(rows: Sequence[OwnershipTrainingRow]) -> str:
    payload = [row.__dict__ for row in rows]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _mae(predicted: Sequence[float], actual: Sequence[float]) -> float:
    total = math.fsum(
        abs(left - right) for left, right in zip(predicted, actual, strict=True)
    )
    return total / len(actual)


def _log_score(predicted: Sequence[float], actual: Sequence[float]) -> float:
    epsilon = 1e-12
    return math.fsum(
        truth * math.log(min(max(value, epsilon), 1.0 - epsilon))
        + (1.0 - truth) * math.log1p(-min(max(value, epsilon), 1.0 - epsilon))
        for value, truth in zip(predicted, actual, strict=True)
    ) / len(actual)


def _brier(predicted: Sequence[float], actual: Sequence[float]) -> float:
    return math.fsum(
        (value - truth) ** 2 for value, truth in zip(predicted, actual, strict=True)
    ) / len(actual)


def _calibration(
    predicted: Sequence[float], actual: Sequence[float]
) -> tuple[float | None, float | None]:
    epsilon = 1e-6
    x = np.asarray(
        [
            math.log(
                min(max(value, epsilon), 1 - epsilon)
                / (1 - min(max(value, epsilon), 1 - epsilon))
            )
            for value in predicted
        ],
        dtype=np.float64,
    )
    truth = np.asarray(actual, dtype=np.float64)
    if len(x) < 2 or float(np.ptp(x)) < 1e-12:
        return None, None

    def objective(theta: NDArray[np.float64]) -> float:
        eta = theta[0] + theta[1] * x
        return float(np.sum(np.logaddexp(0.0, eta) - truth * eta))

    result = minimize(objective, np.asarray((0.0, 1.0)), method="BFGS")
    if not result.success or not np.all(np.isfinite(result.x)):
        return None, None
    return float(result.x[0]), float(result.x[1])


def _directional_accuracy(
    predicted: Sequence[float],
    baseline: Sequence[float],
    actual: Sequence[float],
    selected: Sequence[int],
) -> float | None:
    if not selected:
        return None
    correct = sum(
        (predicted[index] - baseline[index]) * (actual[index] - baseline[index]) > 0
        for index in selected
    )
    return correct / len(selected)


def _spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 2:
        return None
    left_ranks = _ranks(left)
    right_ranks = _ranks(right)
    left_centered = left_ranks - np.mean(left_ranks)
    right_centered = right_ranks - np.mean(right_ranks)
    denominator = math.sqrt(
        float(np.sum(np.square(left_centered)) * np.sum(np.square(right_centered)))
    )
    if denominator == 0:
        return None
    return float(np.sum(left_centered * right_centered) / denominator)


def _ranks(values: Sequence[float]) -> NDArray[np.float64]:
    array = np.asarray(values, dtype=np.float64)
    order = np.argsort(array, kind="mergesort")
    ranks = np.empty(len(array), dtype=np.float64)
    start = 0
    while start < len(array):
        end = start + 1
        while end < len(array) and array[order[end]] == array[order[start]]:
            end += 1
        ranks[order[start:end]] = (start + end - 1) / 2.0 + 1.0
        start = end
    return ranks


def _mean_or_none(values: Sequence[float]) -> float | None:
    return None if not values else math.fsum(values) / len(values)


def _format_metric(value: float | None) -> str:
    return "NA" if value is None else f"{value:.10f}"
