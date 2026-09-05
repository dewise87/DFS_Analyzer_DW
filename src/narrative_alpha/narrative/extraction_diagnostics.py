"""Content-bearing refusal diagnostics; retained only until source tombstoning.

Similarity is an explanation, never permission to accept an approximate quote.
"""

from __future__ import annotations

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
    return {"kind": "schema", "bucket": "schema_violation", "fields": fields}


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
    return {
        "kind": "evidence",
        "bucket": "evidence_not_in_source"
        if evidence_ref_index is not None
        else "entity_or_context",
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
