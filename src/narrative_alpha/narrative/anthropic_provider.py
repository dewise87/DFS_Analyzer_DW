"""Native Anthropic Message Batches adapter for Stage 1 extraction."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import cast

import anthropic
from anthropic.types import JSONOutputFormatParam, MessageParam, OutputConfigParam
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages import MessageBatchIndividualResponse
from anthropic.types.messages.batch_create_params import Request

from narrative_alpha.narrative.extraction_models import (
    ExtractionEnvelope,
    PreparedExtraction,
    ProviderBatchSubmission,
    ProviderResult,
)

DEFAULT_MODEL_ID = "claude-haiku-4-5-20251001"
# Twelve claims at ~190 tokens each is ~2.3k tokens; 2048 guaranteed truncation on full
# responses. Batch output pricing makes the ceiling cheap.
DEFAULT_MAX_OUTPUT_TOKENS = 4096
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_BATCH_TIMEOUT_SECONDS = 3600.0
DEFAULT_SUBMISSION_TIMEOUT_SECONDS = 120.0
DEFAULT_PROVIDER_IO_TIMEOUT_SECONDS = 30.0


class AnthropicBatchError(RuntimeError):
    """Raised when the provider cannot produce a complete, attributable batch result."""


class AnthropicBatchPreflightError(ValueError):
    """Raised only when a batch is locally rejected before the create POST begins."""


def stage1_output_schema() -> dict[str, object]:
    """Return the exact provider-compatible schema sent on the wire."""

    schema = cast(dict[str, object], anthropic.transform_schema(ExtractionEnvelope))
    return cast(dict[str, object], _const_as_enum(schema))


def _const_as_enum(node: object) -> object:
    """``transform_schema`` demotes ``const`` to a description hint; ``enum`` is enforced."""

    if isinstance(node, dict):
        rebuilt: dict[str, object] = {}
        for key, value in node.items():
            rebuilt[key] = _const_as_enum(value)
        if "const" in rebuilt and "enum" not in rebuilt:
            rebuilt["enum"] = [rebuilt.pop("const")]
        return rebuilt
    if isinstance(node, list):
        return [_const_as_enum(value) for value in node]
    return node


class AnthropicBatchProvider:
    """Send strict, tool-free extraction requests through the native batch API."""

    def __init__(
        self,
        *,
        client: anthropic.Anthropic | None = None,
        model_id: str = DEFAULT_MODEL_ID,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        timeout_seconds: float = DEFAULT_BATCH_TIMEOUT_SECONDS,
        submission_timeout_seconds: float = DEFAULT_SUBMISSION_TIMEOUT_SECONDS,
        io_timeout_seconds: float = DEFAULT_PROVIDER_IO_TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if model_id != DEFAULT_MODEL_ID:
            raise ValueError(
                f"Stage 1 requires exact model {DEFAULT_MODEL_ID!r}; got {model_id!r}"
            )
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be finite and positive")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if (
            not math.isfinite(submission_timeout_seconds)
            or submission_timeout_seconds <= 0
        ):
            raise ValueError("submission_timeout_seconds must be finite and positive")
        if not math.isfinite(io_timeout_seconds) or io_timeout_seconds <= 0:
            raise ValueError("io_timeout_seconds must be finite and positive")
        if client is None:
            client = anthropic.Anthropic()
            if not (client.api_key or client.auth_token):
                # The SDK defers credential resolution to request time; failing here keeps
                # a missing ANTHROPIC_API_KEY a definite local rejection, never an ambiguous
                # create that strands every reserved item.
                raise ValueError(
                    "ANTHROPIC_API_KEY is not set; export it in the shell that runs na-extract"
                )
        self.client = client
        self.model_id = model_id
        self.max_output_tokens = max_output_tokens
        self.poll_interval_seconds = poll_interval_seconds
        self.timeout_seconds = timeout_seconds
        self.submission_timeout_seconds = submission_timeout_seconds
        self.io_timeout_seconds = io_timeout_seconds
        self.sleep = sleep
        self.monotonic = monotonic
        schema = stage1_output_schema()
        output_format: JSONOutputFormatParam = {"type": "json_schema", "schema": schema}
        self.output_config: OutputConfigParam = {"format": output_format}

    def submit_batch(
        self,
        requests: tuple[PreparedExtraction, ...],
    ) -> ProviderBatchSubmission:
        if not requests:
            raise AnthropicBatchPreflightError("cannot submit an empty extraction batch")
        try:
            batch_requests = [self._request(item) for item in requests]
        except (TypeError, ValueError) as error:
            raise AnthropicBatchPreflightError(
                "batch request failed local preflight before submission"
            ) from error
        # Batch creation is not idempotent and the API does not expose an idempotency key.
        # The SDK otherwise retries transient failures by default, which can create and bill
        # multiple batches when the first POST succeeds but its response is lost.  Keep the
        # durable `creating` reservation as the only retry boundary and issue this POST once.
        batch = self.client.with_options(
            max_retries=0,
            timeout=self.submission_timeout_seconds,
        ).messages.batches.create(requests=batch_requests)
        submission_request_id = batch._request_id
        if submission_request_id is not None and not submission_request_id.strip():
            submission_request_id = None
        try:
            return ProviderBatchSubmission(
                provider_batch_id=batch.id,
                batch_submission_request_id=submission_request_id,
            )
        except ValueError as error:
            # The POST has already returned. A malformed accepted response is ambiguous and
            # must never be reclassified as a safe local rejection by the orchestration layer.
            raise AnthropicBatchError(
                "accepted batch response omitted a usable durable identifier"
            ) from error

    def retrieve_batch(
        self,
        requests: tuple[PreparedExtraction, ...],
        submission: ProviderBatchSubmission,
    ) -> tuple[ProviderResult, ...]:
        if not requests:
            raise ValueError("cannot retrieve an empty extraction batch")
        started = self.monotonic()
        # The durable orchestration lease sizes against one bounded attempt per call. SDK
        # retries would silently multiply that envelope and can outlive ownership.
        client = self.client.with_options(
            max_retries=0,
            timeout=self.io_timeout_seconds,
        )
        batch = client.messages.batches.retrieve(submission.provider_batch_id)
        deadline = started + self.timeout_seconds
        while batch.processing_status != "ended":
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise AnthropicBatchError(
                    f"Anthropic message batch {batch.id!r} did not finish before timeout"
                )
            self.sleep(min(self.poll_interval_seconds, remaining))
            if self.monotonic() >= deadline:
                raise AnthropicBatchError(
                    f"Anthropic message batch {batch.id!r} did not finish before timeout"
                )
            batch = client.messages.batches.retrieve(batch.id)

        if batch.id != submission.provider_batch_id:
            raise AnthropicBatchError(
                f"Anthropic retrieved batch {batch.id!r}; expected "
                f"{submission.provider_batch_id!r}"
            )

        latency_ms = max(0, round((self.monotonic() - started) * 1000))
        results = tuple(
            self._provider_result(
                entry,
                batch_id=batch.id,
                submission_request_id=submission.batch_submission_request_id,
                latency_ms=latency_ms,
            )
            for entry in client.messages.batches.results(batch.id)
        )
        # The orchestration layer validates IDs against the durable full batch ledger. On a
        # resumed partially committed batch, this result file legitimately includes terminal
        # siblings that are not present in ``requests`` anymore.
        return results

    def _request(self, item: PreparedExtraction) -> Request:
        if item.max_output_tokens != self.max_output_tokens:
            raise ValueError(
                "planned max_output_tokens does not match the Anthropic provider configuration"
            )
        messages: list[MessageParam] = [{"role": "user", "content": item.user_prompt}]
        params = MessageCreateParamsNonStreaming(
            model=self.model_id,
            max_tokens=item.max_output_tokens,
            system=item.system_prompt,
            messages=messages,
            output_config=self.output_config,
            # Deliberately no tools, tool_choice, MCP servers, or web search.
        )
        return Request(custom_id=item.custom_id, params=params)

    @staticmethod
    def _provider_result(
        entry: MessageBatchIndividualResponse,
        *,
        batch_id: str,
        submission_request_id: str | None,
        latency_ms: int,
    ) -> ProviderResult:
        custom_id = entry.custom_id
        result = entry.result
        if result.type == "succeeded":
            message = result.message
            content = tuple(message.content)
            content_types = tuple(block.type for block in content)
            output_json: str | None = None
            if len(content) == 1:
                block = content[0]
                if block.type == "text":
                    output_json = block.text
            usage = message.usage
            return ProviderResult(
                custom_id=custom_id,
                # Successful JSONL batch results have no per-item HTTP request ID.
                provider_request_id=None,
                batch_submission_request_id=submission_request_id,
                provider_batch_id=batch_id,
                provider_message_id=message.id,
                actual_model_id=message.model,
                output_json=output_json,
                content_types=content_types,
                stop_reason=message.stop_reason,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                latency_ms=latency_ms,
            )

        provider_request_id: str | None = None
        error_code: str = result.type
        error_message = f"Anthropic batch item ended as {result.type}"
        if result.type == "errored":
            provider_request_id = result.error.request_id
            error_code = result.error.error.type
            error_message = result.error.error.message
        return ProviderResult(
            custom_id=custom_id,
            provider_request_id=provider_request_id,
            batch_submission_request_id=submission_request_id,
            provider_batch_id=batch_id,
            provider_message_id=None,
            actual_model_id=None,
            output_json=None,
            content_types=(),
            stop_reason=None,
            input_tokens=None,
            output_tokens=None,
            latency_ms=latency_ms,
            error_code=error_code,
            error_message=error_message,
        )


__all__ = [
    "DEFAULT_BATCH_TIMEOUT_SECONDS",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_MODEL_ID",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_PROVIDER_IO_TIMEOUT_SECONDS",
    "DEFAULT_SUBMISSION_TIMEOUT_SECONDS",
    "AnthropicBatchError",
    "AnthropicBatchPreflightError",
    "AnthropicBatchProvider",
    "stage1_output_schema",
]
