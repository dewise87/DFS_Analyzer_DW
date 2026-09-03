"""Strict, versioned authorization for Sunday fast-lane actions."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import yaml  # type: ignore[import-untyped]
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from narrative_alpha.ingest.timestamps import ensure_utc

DEFAULT_FAST_LANE_RULES_PATH = Path("config/fast_lane_rules.yaml")

FastSourceClass = Literal["official_inactive_list", "a_graded_source"]
FastClaimType = Literal[
    "availability",
    "usage",
    "health",
    "team_context",
]


class FastLaneRuleError(RuntimeError):
    """Raised when an automatic fast-lane action is not explicitly authorized."""


class _RuleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChannelAdjustmentCaps(_RuleModel):
    """Absolute caps in each channel's native unit.

    Availability is a binary gate, mean is fantasy points, and the remaining channels
    are fractional deltas.  The initial official-inactives rule only fires the binary
    gate; its mean cap bounds the resulting replacement-lineup projection difference.
    """

    availability: float = Field(ge=0, le=1)
    mean: float = Field(ge=0)
    shape: float = Field(ge=0)
    dependence: float = Field(ge=0)
    ownership: float = Field(ge=0)


class FastLaneRule(_RuleModel):
    rule_id: str
    trigger_source_class: FastSourceClass
    source_id: str | None = None
    claim_type: FastClaimType
    max_automatic_adjustment: ChannelAdjustmentCaps
    expires_at: datetime

    @field_validator("rule_id", "source_id")
    @classmethod
    def nonempty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("rule text fields must not be empty")
        return normalized

    @field_validator("expires_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def source_scope_matches_trigger(self) -> Self:
        if self.trigger_source_class == "official_inactive_list" and self.source_id is not None:
            raise ValueError("an official inactive-list rule must not name a source_id")
        if self.trigger_source_class == "a_graded_source" and self.source_id is None:
            raise ValueError("an A-graded source rule must name its exact source_id")
        return self


class FastLaneRules(_RuleModel):
    rules_version: str
    approved_at: datetime
    expires_at: datetime
    approved_by: str
    rules: tuple[FastLaneRule, ...] = Field(min_length=1)
    _rules_sha256: str = PrivateAttr()

    @property
    def rules_sha256(self) -> str:
        return self._rules_sha256

    @field_validator("rules_version", "approved_by")
    @classmethod
    def signed_nonempty_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("rules_version and approved_by must not be empty")
        return normalized

    @field_validator("approved_at", "expires_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def coherent_approval_window(self) -> Self:
        if self.expires_at <= self.approved_at:
            raise ValueError("rule-set expires_at must be later than approved_at")
        ids = [rule.rule_id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule_id values must be unique")
        for rule in self.rules:
            if rule.expires_at <= self.approved_at:
                raise ValueError(f"rule {rule.rule_id!r} expires before it is approved")
            if rule.expires_at > self.expires_at:
                raise ValueError(f"rule {rule.rule_id!r} expires after the enclosing rule set")
        return self

    def require_active(self, *, at: datetime | None = None) -> None:
        checked_at = ensure_utc(at or datetime.now(UTC))
        if self.approved_at > checked_at:
            raise FastLaneRuleError(
                f"fast-lane rules {self.rules_version!r} are not approved until "
                f"{self.approved_at.isoformat()}"
            )
        if self.expires_at <= checked_at:
            raise FastLaneRuleError(
                f"fast-lane rules {self.rules_version!r} expired at "
                f"{self.expires_at.isoformat()}; a human must review and re-sign "
                f"{DEFAULT_FAST_LANE_RULES_PATH}"
            )

    def require_rule(
        self,
        *,
        trigger_source_class: FastSourceClass,
        claim_type: FastClaimType,
        source_id: str | None = None,
        at: datetime | None = None,
    ) -> FastLaneRule:
        checked_at = ensure_utc(at or datetime.now(UTC))
        self.require_active(at=checked_at)
        matches = tuple(
            rule
            for rule in self.rules
            if rule.trigger_source_class == trigger_source_class
            and rule.claim_type == claim_type
            and rule.source_id == source_id
        )
        if len(matches) != 1:
            raise FastLaneRuleError(
                f"no single pre-approved rule covers source class "
                f"{trigger_source_class!r}, claim type {claim_type!r}, and source "
                f"{source_id!r}; a human must confirm the action"
            )
        rule = matches[0]
        if rule.expires_at <= checked_at:
            raise FastLaneRuleError(
                f"fast-lane rule {rule.rule_id!r} expired at {rule.expires_at.isoformat()}; "
                "a human must review and re-sign it"
            )
        return rule


def load_fast_lane_rules(
    path: Path = DEFAULT_FAST_LANE_RULES_PATH,
    *,
    at: datetime | None = None,
    require_active: bool = True,
) -> FastLaneRules:
    """Load a strict YAML artifact and optionally require its signature window to be active."""

    try:
        content = path.read_bytes()
    except OSError as error:
        raise FastLaneRuleError(f"cannot read fast-lane rules {path}: {error}") from error
    try:
        decoded = content.decode("utf-8")
        raw = yaml.safe_load(decoded)
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise FastLaneRuleError(f"invalid fast-lane rules {path}: {error}") from error
    if not isinstance(raw, dict):
        raise FastLaneRuleError(f"invalid fast-lane rules {path}: top level must be a mapping")
    try:
        rules = FastLaneRules.model_validate(raw)
    except ValidationError as error:
        raise FastLaneRuleError(f"invalid fast-lane rules {path}: {error}") from error
    object.__setattr__(rules, "_rules_sha256", hashlib.sha256(content).hexdigest())
    if require_active:
        rules.require_active(at=at)
    return rules


__all__ = [
    "DEFAULT_FAST_LANE_RULES_PATH",
    "ChannelAdjustmentCaps",
    "FastLaneRule",
    "FastLaneRuleError",
    "FastLaneRules",
    "load_fast_lane_rules",
]
