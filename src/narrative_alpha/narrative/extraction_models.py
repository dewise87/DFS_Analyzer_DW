"""Strict contracts at the Stage 1 model boundary.

The model may classify source text, but it cannot emit canonical IDs, tools, instructions,
or projection adjustments. Every free-text field is subsequently checked against the source
or screened as untrusted output before anything is stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from narrative_alpha.store.models import (
    ClaimDimensionValue,
    ClaimDirectionValue,
    ClaimNoveltyValue,
    ClaimTypeValue,
    EvidenceBasisValue,
    EvidenceClassValue,
    ModelConfidenceValue,
    SuggestedChannelValue,
)

SCHEMA_VERSION = "stage1-extraction-v1"

UncertaintyFlag = Literal[
    "source_hedging",
    "conditional",
    "timeframe_unclear",
    "scope_unclear",
    "conflicting_evidence",
    "secondhand",
    "none",
]
AmbiguityFlag = Literal[
    "player_resolution",
    "team_resolution",
    "sarcasm",
    "conditional_claim",
    "timeframe",
    "scope",
    "conflicting_context",
    "none",
]


class StrictOutputModel(BaseModel):
    """Forbid every field outside the reviewed schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ExtractedPlayerRef(StrictOutputModel):
    """A name copied exactly as written; canonical IDs are intentionally impossible here."""

    name_raw: str = Field(min_length=1, max_length=64)

    @field_validator("name_raw")
    @classmethod
    def plausible_person_name(cls, value: str) -> str:
        tokens = value.split()
        allowed_punctuation = {"'", "\N{RIGHT SINGLE QUOTATION MARK}", "-", "."}
        if not 1 <= len(tokens) <= 4:
            raise ValueError("player name must contain one to four tokens")
        if any(
            not (character.isalpha() or character in allowed_punctuation)
            for character in value
            if not character.isspace()
        ):
            raise ValueError("player name contains non-name characters")
        if not any(character.isalpha() for character in value):
            raise ValueError("player name must contain a letter")
        return value


class ExtractedEvidenceRef(StrictOutputModel):
    """A zero-based Unicode character span with an exclusive end."""

    source_item_id: int = Field(gt=0)
    extract_start: int = Field(ge=0)
    extract_end: int = Field(gt=0)
    verbatim_extract: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.extract_end <= self.extract_start:
            raise ValueError("extract_end must be greater than extract_start")
        return self


class ExtractedClaim(StrictOutputModel):
    """One claim exactly as Stage 1 is permitted to represent it."""

    player_refs: tuple[ExtractedPlayerRef, ...] = Field(min_length=1, max_length=12)
    team_refs: tuple[str, ...] = Field(max_length=8)
    claim_type: ClaimTypeValue
    claim_dimension: ClaimDimensionValue
    outcome_direction: ClaimDirectionValue
    roster_behavior_direction: ClaimDirectionValue
    evidence_class: EvidenceClassValue
    evidence_basis: EvidenceBasisValue
    falsifiable: bool
    specificity: float = Field(ge=0, le=1)
    actionability: float = Field(ge=0, le=1)
    novelty: ClaimNoveltyValue
    model_confidence: ModelConfidenceValue
    uncertainty_flags: tuple[UncertaintyFlag, ...] = Field(max_length=7)
    ambiguity_flags: tuple[AmbiguityFlag, ...] = Field(max_length=8)
    suggested_channels: tuple[SuggestedChannelValue, ...] = Field(max_length=5)
    disconfirming_context: str | None = Field(max_length=500)
    evidence_refs: tuple[ExtractedEvidenceRef, ...] = Field(min_length=1, max_length=8)

    @field_validator(
        "player_refs",
    )
    @classmethod
    def unique_player_names(
        cls, value: tuple[ExtractedPlayerRef, ...]
    ) -> tuple[ExtractedPlayerRef, ...]:
        names = [reference.name_raw for reference in value]
        if len(names) != len(set(names)):
            raise ValueError("player_refs must contain unique names")
        return value

    @field_validator(
        "team_refs",
        "uncertainty_flags",
        "ambiguity_flags",
        "suggested_channels",
    )
    @classmethod
    def unique_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("list values must not be empty")
        if len(value) != len(set(value)):
            raise ValueError("list values must be unique")
        if "none" in value and len(value) != 1:
            raise ValueError("the 'none' flag cannot be combined with other flags")
        return value


class ExtractionEnvelope(StrictOutputModel):
    """The only output shape accepted from the provider."""

    schema_version: Literal["stage1-extraction-v1"]
    prompt_injection_detected: bool
    claims: tuple[ExtractedClaim, ...] = Field(max_length=12)

    @model_validator(mode="after")
    def injection_has_no_claims(self) -> Self:
        if self.prompt_injection_detected and self.claims:
            raise ValueError("an injection-flagged response must not contain claims")
        return self


@dataclass(frozen=True)
class PreparedExtraction:
    """One fully rendered request; source text appears only inside ``user_prompt``."""

    custom_id: str
    source_item_id: int
    source_id: str
    source_family: str
    source_policy_id: int
    content_sha256: str
    source_text: str
    published_at: datetime | None
    observed_at: datetime
    effective_at: datetime | None
    system_prompt: str
    user_prompt: str
    max_output_tokens: int
    estimated_input_tokens: int
    quarantine_reason: str | None = None


@dataclass(frozen=True)
class ProviderResult:
    """Transport-neutral result returned by the native Anthropic batch adapter."""

    custom_id: str
    provider_request_id: str | None
    batch_submission_request_id: str | None
    provider_batch_id: str | None
    provider_message_id: str | None
    actual_model_id: str | None
    output_json: str | None
    content_types: tuple[str, ...]
    stop_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class ProviderBatchSubmission:
    """Durable identifiers returned when Anthropic accepts a Message Batch."""

    provider_batch_id: str
    batch_submission_request_id: str | None

    def __post_init__(self) -> None:
        if not self.provider_batch_id.strip():
            raise ValueError("provider_batch_id must not be empty")
        if (
            self.batch_submission_request_id is not None
            and not self.batch_submission_request_id.strip()
        ):
            raise ValueError("batch_submission_request_id must not be empty")


@dataclass(frozen=True)
class ExtractionItemError:
    source_item_id: int
    code: str
    message: str


@dataclass(frozen=True)
class ExtractionPlan:
    window_start: datetime
    window_end: datetime
    prompt_version_id: str
    prompt_sha256: str
    schema_version: str
    model_id: str
    ready: tuple[PreparedExtraction, ...]
    resumable: tuple[PreparedExtraction, ...]
    submission_unknown: tuple[PreparedExtraction, ...]
    injection_blocked: tuple[PreparedExtraction, ...]
    estimated_input_tokens: int
    estimated_max_output_tokens: int
    estimated_cost_nanos_usd: int
    token_estimate_method: str
    skipped_terminal_items: int
    ineligible: tuple[ExtractionItemError, ...] = ()
    deferred_items: int = 0


@dataclass(frozen=True)
class ExtractionReport:
    run_id: str | None
    window_start: datetime
    window_end: datetime
    selected_items: int
    submitted_items: int
    succeeded_items: int
    claims_stored: int
    flagged_item_ids: tuple[int, ...]
    skipped_terminal_items: int
    errors: tuple[ExtractionItemError, ...]
    ineligible: tuple[ExtractionItemError, ...] = ()
    deferred_items: int = 0
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """No item error. Review flags are an expected terminal outcome, not a failure."""

        return not self.errors

    @property
    def pending(self) -> bool:
        """Every error is a still-processing provider batch: rerun the same command."""

        return bool(self.errors) and all(
            error.code == "provider_batch_pending" for error in self.errors
        )


class ExtractionProvider(Protocol):
    """Structural protocol implemented by the Anthropic batch adapter and test doubles."""

    def submit_batch(
        self,
        requests: tuple[PreparedExtraction, ...],
    ) -> ProviderBatchSubmission: ...

    def retrieve_batch(
        self,
        requests: tuple[PreparedExtraction, ...],
        submission: ProviderBatchSubmission,
    ) -> tuple[ProviderResult, ...]: ...
