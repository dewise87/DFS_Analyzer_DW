"""Local review samples and durable metrics for Stage 1 prompt/model evaluation."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from narrative_alpha import __version__
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.narrative.collectors import normalize_item_text

DEFAULT_STAGE1_EVAL_DIRECTORY = Path("data/eval/stage1")
SAMPLE_SCHEMA_VERSION = "stage1-review-v1"

REVIEW_COLUMNS = (
    "sample_schema_version",
    "source_item_id",
    "stratum",
    "source_id",
    "canonical_text",
    "claim_id",
    "prompt_version_id",
    "model_id",
    "claim_type",
    "claim_dimension",
    "outcome_direction",
    "roster_behavior_direction",
    "evidence_class",
    "evidence_basis",
    "falsifiable",
    "specificity",
    "actionability",
    "novelty",
    "model_confidence",
    "team_refs_json",
    "uncertainty_flags_json",
    "ambiguity_flags_json",
    "suggested_channels_json",
    "disconfirming_context",
    "player_refs_json",
    "evidence_refs_json",
    "claim_present",
    "injection_flag",
    "label_claim_present",
    "label_player_refs_correct",
    "label_claim_dimension",
    "label_outcome_direction",
    "label_roster_behavior_direction",
    "label_evidence_spans_exact",
    "label_injection_flag",
    "label_notes",
)

LABEL_COLUMNS = (
    "label_claim_present",
    "label_player_refs_correct",
    "label_claim_dimension",
    "label_outcome_direction",
    "label_roster_behavior_direction",
    "label_evidence_spans_exact",
    "label_injection_flag",
    "label_notes",
)

_STRATA = ("claims", "zero_claim", "flagged")
_TRUE = frozenset({"1", "true", "yes", "y"})
_FALSE = frozenset({"0", "false", "no", "n"})


class Stage1EvaluationError(ValueError):
    """Raised when a sample or label set cannot be evaluated safely."""


@dataclass(frozen=True)
class ReviewSampleReport:
    output_path: Path
    sampled_items: int
    rows_written: int
    strata_counts: Mapping[str, int]
    prompt_version_id: str
    model_id: str


@dataclass(frozen=True)
class Stage1EvalReport:
    model_eval_id: str
    run_id: str
    prompt_version_id: str
    model_id: str
    label_set_sha256: str
    item_count: int
    label_row_count: int
    metrics: Mapping[str, object]


@dataclass(frozen=True)
class _Candidate:
    source_item_id: int
    stratum: str


def create_review_sample(
    connection: sqlite3.Connection,
    *,
    size: int,
    output_dir: Path,
    sampled_at: datetime | None = None,
) -> ReviewSampleReport:
    """Write a deterministic, stratified item sample with blank human-label columns."""

    if isinstance(size, bool) or size <= 0:
        raise Stage1EvaluationError("sample size must be a positive integer")
    lineage = connection.execute(
        """
        SELECT extraction.prompt_version_id, extraction.model_id,
               max(extraction.observed_at) AS latest_at
        FROM source_item_extractions AS extraction
        JOIN source_items AS item
          ON item.source_item_id = extraction.source_item_id
        WHERE extraction.status IN ('succeeded', 'flagged')
          AND item.title IS NOT NULL AND item.cleaned_text IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM content_tombstones AS tombstone
              WHERE tombstone.source_item_id = item.source_item_id
          )
        GROUP BY extraction.prompt_version_id, extraction.model_id
        ORDER BY latest_at DESC, extraction.prompt_version_id DESC, extraction.model_id DESC
        LIMIT 1
        """
    ).fetchone()
    if lineage is None:
        raise Stage1EvaluationError("no retained terminal Stage 1 results are available to sample")
    prompt_version_id = str(lineage["prompt_version_id"])
    model_id = str(lineage["model_id"])

    candidates = tuple(
        _Candidate(source_item_id=int(row["source_item_id"]), stratum=str(row["stratum"]))
        for row in connection.execute(
            """
            SELECT extraction.source_item_id,
                   CASE
                     WHEN extraction.status = 'flagged' THEN 'flagged'
                     WHEN EXISTS (
                         SELECT 1 FROM claims AS claim
                         WHERE claim.extraction_id = extraction.extraction_id
                     ) THEN 'claims'
                     ELSE 'zero_claim'
                   END AS stratum
            FROM source_item_extractions AS extraction
            JOIN source_items AS item
              ON item.source_item_id = extraction.source_item_id
            WHERE extraction.prompt_version_id = ? AND extraction.model_id = ?
              AND extraction.status IN ('succeeded', 'flagged')
              AND item.title IS NOT NULL AND item.cleaned_text IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM content_tombstones AS tombstone
                  WHERE tombstone.source_item_id = item.source_item_id
              )
            """,
            (prompt_version_id, model_id),
        )
    )
    selected = _stratified_candidates(candidates, size=size)
    if not selected:
        raise Stage1EvaluationError("no retained terminal Stage 1 results are available to sample")

    rows: list[dict[str, object]] = []
    for candidate in selected:
        rows.extend(
            _review_rows(
                connection,
                candidate,
                prompt_version_id=prompt_version_id,
                model_id=model_id,
            )
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    now = ensure_utc(sampled_at or datetime.now(UTC))
    stamp = utc_timestamp(now).replace("-", "").replace(":", "").replace(".", "")
    output_path = output_dir / f"stage1-review-{stamp}-{uuid4().hex[:8]}.csv"
    with output_path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_COLUMNS, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())

    strata_counts = {
        stratum: sum(candidate.stratum == stratum for candidate in selected)
        for stratum in _STRATA
    }
    return ReviewSampleReport(
        output_path=output_path,
        sampled_items=len(selected),
        rows_written=len(rows),
        strata_counts=strata_counts,
        prompt_version_id=prompt_version_id,
        model_id=model_id,
    )


def evaluate_labels(
    connection: sqlite3.Connection,
    labels_path: Path,
    *,
    evaluated_at: datetime | None = None,
) -> Stage1EvalReport:
    """Score stored Stage 1 results against completed human labels and persist the eval."""

    raw_labels = labels_path.read_bytes()
    label_set_sha256 = hashlib.sha256(raw_labels).hexdigest()
    try:
        text = raw_labels.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise Stage1EvaluationError("label file must be UTF-8 CSV") from error
    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = tuple(reader.fieldnames or ())
    required = {
        "source_item_id",
        "claim_id",
        "prompt_version_id",
        "model_id",
        *LABEL_COLUMNS[:-1],
    }
    missing = sorted(required - set(headers))
    if missing:
        raise Stage1EvaluationError(
            f"label file is missing required columns: {', '.join(missing)}"
        )
    rows = list(reader)
    if not rows:
        raise Stage1EvaluationError("label file contains no review rows")

    lineages = {
        ((row.get("prompt_version_id") or "").strip(), (row.get("model_id") or "").strip())
        for row in rows
    }
    if len(lineages) != 1 or any(not value for lineage in lineages for value in lineage):
        raise Stage1EvaluationError("label file must contain exactly one prompt/model lineage")
    prompt_version_id, model_id = next(iter(lineages))

    by_item: dict[int, list[dict[str, str]]] = defaultdict(list)
    seen_claim_rows: set[tuple[int, str]] = set()
    for row_number, row in enumerate(rows, start=2):
        item_id = _positive_item_id(row.get("source_item_id"), row_number=row_number)
        claim_id = (row.get("claim_id") or "").strip()
        key = (item_id, claim_id)
        if key in seen_claim_rows:
            raise Stage1EvaluationError(
                f"row {row_number}: duplicate source-item/claim row {item_id}/{claim_id or '-'}"
            )
        seen_claim_rows.add(key)
        by_item[item_id].append(row)

    claim_truth: dict[int, bool] = {}
    injection_truth: dict[int, bool] = {}
    claim_predictions: dict[int, bool] = {}
    injection_predictions: dict[int, bool] = {}
    detail_correct: dict[str, list[bool]] = {
        "player_reference_resolution": [],
        "claim_dimension": [],
        "outcome_direction": [],
        "roster_behavior_direction": [],
        "evidence_span_exactness": [],
    }
    resolved_refs = 0
    total_refs = 0

    for item_id, item_rows in sorted(by_item.items()):
        if connection.execute(
            "SELECT 1 FROM content_tombstones WHERE source_item_id = ?", (item_id,)
        ).fetchone() is not None:
            raise Stage1EvaluationError(
                f"source item {item_id} is tombstoned; run purge to remove it from local eval CSVs"
            )
        attempt = connection.execute(
            """
            SELECT extraction_id, status, error_code
            FROM source_item_extractions
            WHERE source_item_id = ? AND prompt_version_id = ? AND model_id = ?
              AND status IN ('succeeded', 'flagged')
            """,
            (item_id, prompt_version_id, model_id),
        ).fetchone()
        if attempt is None:
            raise Stage1EvaluationError(
                f"source item {item_id} has no stored terminal result for the label lineage"
            )
        item_claim_truth = _consistent_item_bool(
            item_rows, "label_claim_present", item_id=item_id
        )
        item_injection_truth = _consistent_item_bool(
            item_rows, "label_injection_flag", item_id=item_id
        )
        has_claim = connection.execute(
            "SELECT 1 FROM claims WHERE extraction_id = ? LIMIT 1",
            (str(attempt["extraction_id"]),),
        ).fetchone() is not None
        is_injection = str(attempt["error_code"] or "").startswith("prompt_injection_")
        claim_truth[item_id] = item_claim_truth
        injection_truth[item_id] = item_injection_truth
        claim_predictions[item_id] = has_claim
        injection_predictions[item_id] = is_injection

        for row in item_rows:
            claim_id = (row.get("claim_id") or "").strip()
            if not claim_id:
                continue
            claim = connection.execute(
                """
                SELECT claim_dimension, outcome_direction, roster_behavior_direction
                FROM claims
                WHERE claim_id = ? AND source_item_id = ? AND prompt_version_id = ?
                  AND model_id = ?
                """,
                (claim_id, item_id, prompt_version_id, model_id),
            ).fetchone()
            if claim is None:
                raise Stage1EvaluationError(
                    f"claim {claim_id!r} is not a stored claim for source item {item_id}"
                )
            ref_counts = connection.execute(
                """
                SELECT count(*) AS total,
                       sum(CASE WHEN player_id IS NOT NULL THEN 1 ELSE 0 END) AS resolved
                FROM claim_player_refs WHERE claim_id = ?
                """,
                (claim_id,),
            ).fetchone()
            total_refs += int(ref_counts["total"])
            resolved_refs += int(ref_counts["resolved"] or 0)
            if not item_claim_truth:
                continue
            detail_correct["player_reference_resolution"].append(
                _required_bool(row, "label_player_refs_correct", claim_id=claim_id)
            )
            detail_correct["claim_dimension"].append(
                str(claim["claim_dimension"])
                == _required_text(row, "label_claim_dimension", claim_id=claim_id)
            )
            detail_correct["outcome_direction"].append(
                str(claim["outcome_direction"])
                == _required_text(row, "label_outcome_direction", claim_id=claim_id)
            )
            detail_correct["roster_behavior_direction"].append(
                str(claim["roster_behavior_direction"])
                == _required_text(
                    row, "label_roster_behavior_direction", claim_id=claim_id
                )
            )
            detail_correct["evidence_span_exactness"].append(
                _required_bool(row, "label_evidence_spans_exact", claim_id=claim_id)
            )

    metrics: dict[str, object] = {
        "item_count": len(by_item),
        "claim_presence": _binary_metrics(claim_predictions, claim_truth),
        "player_reference_resolution": _accuracy_metrics(
            detail_correct["player_reference_resolution"]
        ),
        "stored_player_reference_resolution_rate": _ratio_metrics(
            resolved_refs, total_refs
        ),
        "claim_dimension": _accuracy_metrics(detail_correct["claim_dimension"]),
        "outcome_direction": _accuracy_metrics(detail_correct["outcome_direction"]),
        "roster_behavior_direction": _accuracy_metrics(
            detail_correct["roster_behavior_direction"]
        ),
        "evidence_span_exactness": _accuracy_metrics(
            detail_correct["evidence_span_exactness"]
        ),
        "injection_flag": _binary_metrics(injection_predictions, injection_truth),
    }

    at = ensure_utc(evaluated_at or datetime.now(UTC))
    timestamp = utc_timestamp(at)
    run_id = f"stage1-eval-{uuid4().hex}"
    model_eval_id = f"model-eval-{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO model_runs(
            run_id, run_type, started_at, completed_at, status, code_version,
            config_sha256, parent_run_id, error_message, created_at
        ) VALUES (?, 'stage_1_eval', ?, NULL, 'running', ?, ?, NULL, NULL, ?)
        """,
        (run_id, timestamp, __version__, label_set_sha256, timestamp),
    )
    connection.execute(
        """
        INSERT INTO model_evals(
            model_eval_id, prompt_version_id, model_id, label_set_sha256,
            item_count, label_row_count, metrics_json, source, published_at,
            observed_at, ingested_at, effective_at, valid_from, valid_to,
            source_version, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'operator_labels', NULL, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            model_eval_id,
            prompt_version_id,
            model_id,
            label_set_sha256,
            len(by_item),
            len(rows),
            _canonical_json(metrics),
            timestamp,
            timestamp,
            timestamp,
            timestamp,
            SAMPLE_SCHEMA_VERSION,
            run_id,
        ),
    )
    connection.execute(
        """
        UPDATE model_runs
        SET completed_at = ?, status = 'succeeded'
        WHERE run_id = ? AND status = 'running'
        """,
        (timestamp, run_id),
    )
    return Stage1EvalReport(
        model_eval_id=model_eval_id,
        run_id=run_id,
        prompt_version_id=prompt_version_id,
        model_id=model_id,
        label_set_sha256=label_set_sha256,
        item_count=len(by_item),
        label_row_count=len(rows),
        metrics=metrics,
    )


def format_eval_report(report: Stage1EvalReport) -> str:
    """Render the compact operator table required for prompt/model release review."""

    lines = [
        f"prompt={report.prompt_version_id}  model={report.model_id}",
        "metric                         score      correct/total   precision   recall   f1",
    ]
    for name in (
        "claim_presence",
        "player_reference_resolution",
        "claim_dimension",
        "outcome_direction",
        "roster_behavior_direction",
        "evidence_span_exactness",
        "injection_flag",
    ):
        metric = report.metrics[name]
        assert isinstance(metric, Mapping)
        score = float(metric.get("accuracy", 0.0))
        correct = metric.get("correct", "-")
        total = metric.get("total", report.item_count)
        precision = metric.get("precision", "-")
        recall = metric.get("recall", "-")
        f1 = metric.get("f1", "-")
        lines.append(
            f"{name:30} {score:7.3f}   {correct!s:>7}/{total!s:<7}"
            f" {precision!s:>9} {recall!s:>8} {f1!s:>6}"
        )
    lines.extend(
        (
            f"items={report.item_count} rows={report.label_row_count}",
            f"label_set_sha256={report.label_set_sha256}",
            f"model_eval_id={report.model_eval_id}",
        )
    )
    return "\n".join(lines) + "\n"


def purge_tombstoned_eval_rows(
    connection: sqlite3.Connection,
    *,
    eval_root: Path = DEFAULT_STAGE1_EVAL_DIRECTORY,
) -> tuple[int, int]:
    """Remove tombstoned item rows from local review/label CSVs, preserving their headers."""

    if not eval_root.exists():
        return 0, 0
    tombstoned = {
        int(row[0]) for row in connection.execute("SELECT source_item_id FROM content_tombstones")
    }
    if not tombstoned:
        return 0, 0
    files_changed = 0
    rows_removed = 0
    for path in sorted(eval_root.rglob("*.csv")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = tuple(reader.fieldnames or ())
            if "source_item_id" not in fieldnames:
                continue
            rows = list(reader)
        retained: list[dict[str, str]] = []
        removed_here = 0
        for row_number, row in enumerate(rows, start=2):
            item_id = _positive_item_id(row.get("source_item_id"), row_number=row_number)
            if item_id in tombstoned:
                removed_here += 1
            else:
                retained.append(row)
        if removed_here == 0:
            continue
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="raise")
                writer.writeheader()
                writer.writerows(retained)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        files_changed += 1
        rows_removed += removed_here
    return files_changed, rows_removed


def _stratified_candidates(
    candidates: Sequence[_Candidate], *, size: int
) -> tuple[_Candidate, ...]:
    buckets: dict[str, list[_Candidate]] = {stratum: [] for stratum in _STRATA}
    for candidate in candidates:
        if candidate.stratum not in buckets:
            raise Stage1EvaluationError(f"unknown sample stratum {candidate.stratum!r}")
        buckets[candidate.stratum].append(candidate)
    for stratum, bucket in buckets.items():
        bucket.sort(
            key=lambda item: hashlib.sha256(
                f"{SAMPLE_SCHEMA_VERSION}:{stratum}:{item.source_item_id}".encode()
            ).digest()
        )
    selected: list[_Candidate] = []
    while len(selected) < min(size, len(candidates)):
        added = False
        for stratum in _STRATA:
            bucket = buckets[stratum]
            if bucket:
                selected.append(bucket.pop(0))
                added = True
                if len(selected) == min(size, len(candidates)):
                    break
        if not added:
            break
    return tuple(selected)


def _review_rows(
    connection: sqlite3.Connection,
    candidate: _Candidate,
    *,
    prompt_version_id: str,
    model_id: str,
) -> tuple[dict[str, object], ...]:
    item = connection.execute(
        """
        SELECT item.source_id, item.title, item.cleaned_text,
               extraction.extraction_id, extraction.error_code
        FROM source_items AS item
        JOIN source_item_extractions AS extraction
          ON extraction.source_item_id = item.source_item_id
        WHERE item.source_item_id = ? AND extraction.prompt_version_id = ?
          AND extraction.model_id = ? AND extraction.status IN ('succeeded', 'flagged')
        """,
        (candidate.source_item_id, prompt_version_id, model_id),
    ).fetchone()
    if item is None or item["title"] is None or item["cleaned_text"] is None:
        raise Stage1EvaluationError(
            f"source item {candidate.source_item_id} lost retained text during sampling"
        )
    base: dict[str, object] = {
        "sample_schema_version": SAMPLE_SCHEMA_VERSION,
        "source_item_id": candidate.source_item_id,
        "stratum": candidate.stratum,
        "source_id": str(item["source_id"]),
        "canonical_text": normalize_item_text(str(item["title"]), str(item["cleaned_text"])),
        "prompt_version_id": prompt_version_id,
        "model_id": model_id,
        "injection_flag": _bool_text(
            str(item["error_code"] or "").startswith("prompt_injection_")
        ),
        **{column: "" for column in LABEL_COLUMNS},
    }
    claims = connection.execute(
        "SELECT * FROM claims WHERE extraction_id = ? ORDER BY claim_id",
        (str(item["extraction_id"]),),
    ).fetchall()
    if not claims:
        return ({**base, **_empty_claim_fields(), "claim_present": "false"},)

    output: list[dict[str, object]] = []
    for claim in claims:
        claim_id = str(claim["claim_id"])
        player_refs = [
            {
                "name_raw": str(row["name_raw"]),
                "player_id": row["player_id"],
                "unresolved_id": row["unresolved_id"],
                "resolution_method": row["resolution_method"],
                "resolution_confidence": row["resolution_confidence"],
                "manual_override": bool(row["manual_override"]),
            }
            for row in connection.execute(
                "SELECT * FROM claim_player_refs WHERE claim_id = ? ORDER BY ordinal",
                (claim_id,),
            )
        ]
        evidence_refs = [
            {
                "source_item_id": int(row["source_item_id"]),
                "extract_start": int(row["extract_start"]),
                "extract_end": int(row["extract_end"]),
                "verbatim_extract": row["verbatim_extract"],
                "redacted_at": row["redacted_at"],
            }
            for row in connection.execute(
                "SELECT * FROM claim_evidence_refs WHERE claim_id = ? ORDER BY ordinal",
                (claim_id,),
            )
        ]
        output.append(
            {
                **base,
                "claim_id": claim_id,
                "claim_type": claim["claim_type"],
                "claim_dimension": claim["claim_dimension"],
                "outcome_direction": claim["outcome_direction"],
                "roster_behavior_direction": claim["roster_behavior_direction"],
                "evidence_class": claim["evidence_class"],
                "evidence_basis": claim["evidence_basis"],
                "falsifiable": _bool_text(bool(claim["falsifiable"])),
                "specificity": claim["specificity"],
                "actionability": claim["actionability"],
                "novelty": claim["novelty"],
                "model_confidence": claim["model_confidence"],
                "team_refs_json": claim["team_refs_json"],
                "uncertainty_flags_json": claim["uncertainty_flags_json"],
                "ambiguity_flags_json": claim["ambiguity_flags_json"],
                "suggested_channels_json": claim["suggested_channels_json"],
                "disconfirming_context": claim["disconfirming_context"] or "",
                "player_refs_json": _canonical_json(player_refs),
                "evidence_refs_json": _canonical_json(evidence_refs),
                "claim_present": "true",
            }
        )
    return tuple(output)


def _empty_claim_fields() -> dict[str, str]:
    return {
        column: ""
        for column in REVIEW_COLUMNS[
            REVIEW_COLUMNS.index("claim_id") : REVIEW_COLUMNS.index("claim_present")
        ]
        if column not in {"prompt_version_id", "model_id"}
    }


def _consistent_item_bool(
    rows: Iterable[Mapping[str, str]], column: str, *, item_id: int
) -> bool:
    values = {
        _parse_bool(row.get(column), context=f"source item {item_id} {column}")
        for row in rows
    }
    if len(values) != 1:
        raise Stage1EvaluationError(
            f"source item {item_id} has inconsistent values for {column}"
        )
    return next(iter(values))


def _required_bool(row: Mapping[str, str], column: str, *, claim_id: str) -> bool:
    return _parse_bool(row.get(column), context=f"claim {claim_id} {column}")


def _parse_bool(value: str | None, *, context: str) -> bool:
    normalized = (value or "").strip().casefold()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    raise Stage1EvaluationError(f"{context} must be labeled true or false")


def _required_text(row: Mapping[str, str], column: str, *, claim_id: str) -> str:
    value = (row.get(column) or "").strip()
    if not value:
        raise Stage1EvaluationError(f"claim {claim_id} {column} must be labeled")
    return value


def _positive_item_id(value: str | None, *, row_number: int) -> int:
    try:
        item_id = int(value or "")
    except ValueError as error:
        raise Stage1EvaluationError(
            f"row {row_number}: source_item_id must be a positive integer"
        ) from error
    if item_id <= 0:
        raise Stage1EvaluationError(
            f"row {row_number}: source_item_id must be a positive integer"
        )
    return item_id


def _binary_metrics(
    predictions: Mapping[int, bool], truth: Mapping[int, bool]
) -> dict[str, object]:
    if predictions.keys() != truth.keys():  # pragma: no cover - internal contract
        raise AssertionError("prediction and truth items differ")
    tp = sum(predictions[key] and truth[key] for key in predictions)
    fp = sum(predictions[key] and not truth[key] for key in predictions)
    fn = sum(not predictions[key] and truth[key] for key in predictions)
    tn = sum(not predictions[key] and not truth[key] for key in predictions)
    total = tp + fp + fn + tn
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    return {
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "true_negative": tn,
        "correct": tp + tn,
        "total": total,
        "accuracy": _ratio(tp + tn, total),
        "precision": precision,
        "recall": recall,
        "f1": _ratio(2 * precision * recall, precision + recall),
    }


def _accuracy_metrics(results: Sequence[bool]) -> dict[str, object]:
    correct = sum(results)
    return {"correct": correct, "total": len(results), "accuracy": _ratio(correct, len(results))}


def _ratio_metrics(numerator: int, denominator: int) -> dict[str, object]:
    return {"resolved": numerator, "total": denominator, "rate": _ratio(numerator, denominator)}


def _ratio(numerator: int | float, denominator: int | float) -> float:
    # With no positive/scorable examples, the evaluator has found no errors in that class.
    return 1.0 if denominator == 0 else round(float(numerator / denominator), 6)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


__all__ = [
    "DEFAULT_STAGE1_EVAL_DIRECTORY",
    "LABEL_COLUMNS",
    "REVIEW_COLUMNS",
    "SAMPLE_SCHEMA_VERSION",
    "ReviewSampleReport",
    "Stage1EvalReport",
    "Stage1EvaluationError",
    "create_review_sample",
    "evaluate_labels",
    "format_eval_report",
    "purge_tombstoned_eval_rows",
]
