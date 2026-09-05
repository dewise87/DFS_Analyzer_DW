"""Real anonymized refusals: preserve strict rejection and exercise the instructed response."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from narrative_alpha.narrative.anthropic_provider import DEFAULT_MODEL_ID
from narrative_alpha.narrative.extraction import (
    EvidenceValidationError,
    ExtractionSchemaError,
    _validate_provider_envelope,
)
from narrative_alpha.narrative.extraction_models import (
    ExtractionEnvelope,
    PreparedExtraction,
    ProviderResult,
)

FIXTURES = Path(__file__).with_name("fixtures") / "stage1_refusals"


def _fixture(bucket: str) -> tuple[PreparedExtraction, ProviderResult]:
    fixture = json.loads((FIXTURES / f"{bucket}.json").read_text())
    item = PreparedExtraction(
        custom_id="source_item_1", source_item_id=1, source_id="anonymized",
        source_family="fixture", source_policy_id=1, content_sha256="a" * 64,
        source_text=fixture["source_text"], published_at=None,
        observed_at=datetime(2026, 9, 5, tzinfo=UTC), effective_at=None,
        system_prompt="fixture", user_prompt="fixture", max_output_tokens=4096,
        estimated_input_tokens=100,
    )
    result = ProviderResult(
        custom_id=item.custom_id, provider_request_id=None,
        batch_submission_request_id="fixture", provider_batch_id="fixture",
        provider_message_id="fixture", actual_model_id=DEFAULT_MODEL_ID,
        output_json=json.dumps(fixture["output"]), content_types=("text",),
        stop_reason="end_turn", input_tokens=100, output_tokens=100, latency_ms=1,
    )
    return item, result


def _validate(item: PreparedExtraction, result: ProviderResult) -> ExtractionEnvelope:
    return _validate_provider_envelope(item, result, model_id=DEFAULT_MODEL_ID)


def test_other_sport_names_remain_refused_and_explicit_empty_claims_are_valid() -> None:
    item, result = _fixture("non_nfl_team")
    with pytest.raises(EvidenceValidationError) as caught:
        _validate(item, result)
    assert caught.value.detail is not None
    assert caught.value.detail["bucket"] == "non_nfl_team_reference"
    assert caught.value.detail["similarity"] == 1.0  # Verbatim, but outside the NFL lexicon.
    payload = json.loads(result.output_json or "")
    payload["claims"] = []
    assert _validate(item, replace(result, output_json=json.dumps(payload))).claims == ()


def test_no_named_player_uses_empty_claims_without_weakening_name_minimum() -> None:
    item, result = _fixture("placeholder_player")
    with pytest.raises(ExtractionSchemaError) as caught:
        _validate(item, result)
    assert caught.value.detail is not None
    assert caught.value.detail["bucket"] == "invalid_player_reference"
    assert "value_length=0" in str(caught.value)
    payload = json.loads(result.output_json or "")
    payload["claims"] = []
    assert _validate(item, replace(result, output_json=json.dumps(payload))).claims == ()


def test_paraphrased_context_stays_refused_but_contiguous_source_quote_is_valid() -> None:
    item, result = _fixture("paraphrased_context")
    with pytest.raises(EvidenceValidationError) as caught:
        _validate(item, result)
    assert caught.value.detail is not None
    assert caught.value.detail["bucket"] == "nonverbatim_context"
    payload = json.loads(result.output_json or "")
    payload["claims"][0]["disconfirming_context"] = item.source_text[
        item.source_text.index("The NFL has yet"):
    ]
    repaired = _validate(item, replace(result, output_json=json.dumps(payload)))
    assert repaired.claims[0].disconfirming_context in item.source_text
