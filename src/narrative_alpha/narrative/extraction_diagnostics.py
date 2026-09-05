"""Content-bearing refusal diagnostics; retained only until source tombstoning.

Similarity is an explanation, never permission to accept an approximate quote.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from pydantic import ValidationError
from rapidfuzz.fuzz import partial_ratio_alignment


def schema_error_detail(error: ValidationError) -> dict[str, object]:
    fields: list[dict[str, object]] = []
    for failure in error.errors(include_url=False):
        value = failure.get("input")
        fields.append(
            {
                "field_path": ".".join(str(part) for part in failure["loc"]) or "$",
                "error_type": failure["type"],
                "value_length": len(value) if isinstance(value, (str, list, tuple, dict)) else None,
                "value_type": type(value).__name__,
            }
        )
    detail: dict[str, object] = {"kind": "schema", "fields": fields}
    detail["bucket"] = refusal_bucket(detail)
    return detail


def refusal_bucket(detail: dict[str, object]) -> str:
    """Classify the first refusal; never infer unavailable historical output."""
    if detail.get("kind") == "schema":
        fields = detail.get("fields")
        if isinstance(fields, list) and fields:
            path = str(fields[0]["field_path"])
            if "player_refs" in path:
                return "invalid_player_reference"
            if path.endswith("ambiguity_flags"):
                return "invalid_ambiguity_flags"
        return "schema_violation"
    path = str(detail.get("field_path", ""))
    if path.endswith("disconfirming_context"):
        return "nonverbatim_context"
    if "player_refs" in path:
        return "invalid_player_reference"
    if "team_refs" in path:
        if "lexicon" in str(detail.get("reason", "")):
            return "non_nfl_team_reference"
        return "team_reference_not_in_source"
    return "evidence_not_in_source"


def evidence_error_detail(
    source_text: str,
    extract: str,
    *,
    claim_index: int,
    field_path: str,
    evidence_ref_index: int | None = None,
    reason: str,
) -> dict[str, object]:
    alignment = partial_ratio_alignment(extract, source_text)
    start = 0 if alignment is None else alignment.dest_start
    end = 0 if alignment is None else alignment.dest_end
    detail: dict[str, object] = {
        "kind": "evidence",
        "reason": reason,
        "claim_index": claim_index,
        "evidence_ref_index": evidence_ref_index,
        "field_path": f"claims.{claim_index}.{field_path}",
        "verbatim_extract": extract[:80],
        "value_length": len(extract),
        "closest_substring": source_text[start:end],
        "closest_start": start,
        "closest_end": end,
        "similarity": 0.0 if alignment is None else round(alignment.score / 100, 6),
        "similarity_method": "rapidfuzz.partial_ratio_alignment (diagnostic only)",
    }
    detail["bucket"] = refusal_bucket(detail)
    return detail


def diagnostic_message(detail: dict[str, object]) -> str:
    if detail["kind"] == "schema":
        fields = detail["fields"]
        assert isinstance(fields, list)
        return "strict Stage 1 schema violation: " + "; ".join(
            f"{field['field_path']}: {field['error_type']} "
            f"(value_length={field['value_length']}, type={field['value_type']})"
            for field in fields
        )
    return (
        f"{detail['field_path']}: {detail['reason']}; "
        f"verbatim_extract={detail['verbatim_extract']!r}; "
        f"closest_substring={detail['closest_substring']!r}; "
        f"similarity={detail['similarity']}"
    )


def list_refused_attempts(
    connection: sqlite3.Connection,
    *,
    prompt_version_id: str | None = None,
) -> tuple[dict[str, object], ...]:
    """Group every failed attempt, retaining honest labels for legacy missing outputs."""
    groups: dict[str, list[dict[str, object]]] = {}
    for row in connection.execute(
        "SELECT extraction_id, source_item_id, run_id, prompt_version_id, error_code, "
        "error_message, error_detail_json, refusal_bucket, output_sha256, output_redacted_at "
        "FROM source_item_extractions WHERE status = 'failed' "
        "AND (? IS NULL OR prompt_version_id = ?) ORDER BY ingested_at DESC, extraction_id",
        (prompt_version_id, prompt_version_id),
    ):
        attempt = dict(row)
        bucket = attempt.pop("refusal_bucket")
        if bucket is None:
            bucket = (
                "legacy_output_unavailable"
                if attempt["error_code"] in ("schema_violation", "evidence_validation_error")
                else str(attempt["error_code"])
            )
        raw_detail = attempt["error_detail_json"]
        if isinstance(raw_detail, str):
            detail = json.loads(raw_detail)
            attempt["error_detail_json"] = detail
            bucket = refusal_bucket(detail)
        groups.setdefault(str(bucket), []).append(attempt)
    return tuple(
        {"bucket": bucket, "count": len(attempts), "attempts": attempts}
        for bucket, attempts in sorted(groups.items())
    )


@dataclass(frozen=True)
class LastExtractionRefusals:
    run_id: str
    status: str
    by_code: dict[str, int]


def last_extraction_refusals(connection: sqlite3.Connection) -> LastExtractionRefusals | None:
    """Count terminal refusals settled by the latest Stage 1 run, including recovery.

    A recovery run can settle reservations owned by an ancestor. Settlement timestamps
    prevent older refusals by that ancestor leaking into the latest run's counts.
    """
    run = connection.execute(
        "SELECT run_id, status, started_at, completed_at FROM model_runs "
        "WHERE run_type = 'stage_1_extraction' ORDER BY started_at DESC, run_id DESC LIMIT 1"
    ).fetchone()
    if run is None:
        return None
    counts = connection.execute(
        """
        WITH RECURSIVE owners(run_id) AS (
            SELECT ? UNION
            SELECT parent.parent_run_id FROM model_run_parents AS parent
            JOIN owners ON parent.child_run_id = owners.run_id
            WHERE parent.relationship IN ('stage1_recovery', 'stage1_recovery_takeover')
        )
        SELECT error_code, count(*) AS n FROM source_item_extractions
        WHERE status = 'failed' AND run_id IN (SELECT run_id FROM owners)
          AND ingested_at >= ? AND (? IS NULL OR ingested_at <= ?)
        GROUP BY error_code ORDER BY error_code
        """,
        (run["run_id"], run["started_at"], run["completed_at"], run["completed_at"]),
    )
    return LastExtractionRefusals(
        run_id=run["run_id"],
        status=run["status"],
        by_code={row["error_code"]: int(row["n"]) for row in counts},
    )
