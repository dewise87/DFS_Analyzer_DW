"""Validated source-catalog planning, append-only seeding, and feed health checks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import tomllib
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.narrative.collectors import FeedParseError, RssAtomCollector
from narrative_alpha.snapshots.fetch import (
    HTTP_TIMEOUT,
    HttpRequestFailure,
    Sleeper,
    get_with_retry,
)
from narrative_alpha.store import SourcePolicyRow, SourceRow

CATALOG_PROVENANCE_SOURCE = "narrative-source-catalog"


class CatalogError(ValueError):
    """Raised when a source catalog cannot be trusted or resolved completely."""


class _CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PolicyTier(_CatalogModel):
    permitted_use: str
    raw_retention_days: int = Field(ge=0)
    personal_data_fields_allowed: tuple[str, ...]
    must_honor_deletions: bool
    redistribution_allowed: bool
    third_party_processing_allowed: bool
    commercial_use_status: str

    @field_validator("permitted_use", "commercial_use_status")
    @classmethod
    def nonempty_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("policy tier text fields must not be empty")
        return normalized

    @field_validator("personal_data_fields_allowed")
    @classmethod
    def unique_personal_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(field.strip() for field in value)
        if any(not field for field in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("personal data fields must be nonempty and unique")
        return normalized


class CatalogSource(_CatalogModel):
    source_id: str
    display_name: str
    source_family: str
    collector_kind: Literal["rss_atom", "official_team_feed"]
    feed_url: str
    policy_tier: str
    team: str | None = None

    @field_validator(
        "source_id", "display_name", "source_family", "feed_url", "policy_tier", "team"
    )
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("catalog source text fields must not be empty")
        return normalized

    @field_validator("feed_url")
    @classmethod
    def http_feed_url(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError("feed_url must be an HTTP(S) URL")
        return value


class NarrativeSourceCatalog(_CatalogModel):
    policy_tiers: dict[str, PolicyTier]
    sources: tuple[CatalogSource, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def resolve_source_tiers_and_ids(self) -> NarrativeSourceCatalog:
        duplicates = [
            source_id
            for source_id, count in Counter(source.source_id for source in self.sources).items()
            if count > 1
        ]
        if duplicates:
            raise ValueError(f"duplicate source_id values: {', '.join(sorted(duplicates))}")
        missing = sorted({source.policy_tier for source in self.sources} - set(self.policy_tiers))
        if missing:
            raise ValueError(f"undefined policy tiers: {', '.join(missing)}")
        return self


@dataclass(frozen=True)
class TierAttestation:
    tier: str
    source_count: int
    policy: PolicyTier


@dataclass(frozen=True)
class SourceSeedChange:
    source_id: str
    source_action: Literal["insert", "version", "unchanged"]
    source_changed_fields: tuple[str, ...]
    policy_action: Literal["insert", "version", "unchanged"]
    policy_changed_fields: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return self.source_action != "unchanged" or self.policy_action != "unchanged"


@dataclass(frozen=True)
class SourceSeedPlan:
    catalog_path: Path
    catalog_sha256: str
    observed_at: datetime
    terms_reviewed_at: datetime
    tier_attestations: tuple[TierAttestation, ...]
    changes: tuple[SourceSeedChange, ...]
    catalog: NarrativeSourceCatalog

    @property
    def changed_sources(self) -> tuple[SourceSeedChange, ...]:
        return tuple(change for change in self.changes if change.changed)


@dataclass(frozen=True)
class SeedResult:
    source_versions_inserted: int
    policy_versions_inserted: int


@dataclass(frozen=True)
class FeedCheck:
    source_id: str
    feed_url: str
    ok: bool
    attempts: int
    item_count: int | None
    error: str | None


@dataclass(frozen=True)
class FeedCheckReport:
    checks: tuple[FeedCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


def load_source_catalog(path: Path) -> NarrativeSourceCatalog:
    """Load and strictly validate a TOML catalog without accepting review timestamps."""

    catalog, _ = _load_source_catalog_bytes(path)
    return catalog


def _load_source_catalog_bytes(path: Path) -> tuple[NarrativeSourceCatalog, bytes]:
    try:
        raw_bytes = path.read_bytes()
    except OSError as error:
        raise CatalogError(f"cannot read source catalog {path}: {error}") from error
    try:
        raw = tomllib.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise CatalogError(f"invalid source catalog {path}: {error}") from error
    if _contains_key(raw, "terms_reviewed_at"):
        raise CatalogError(
            "terms_reviewed_at must not appear in a catalog; pass an operator attestation"
        )
    try:
        catalog = NarrativeSourceCatalog.model_validate(raw)
    except ValueError as error:
        raise CatalogError(f"invalid source catalog {path}: {error}") from error
    return catalog, raw_bytes


def plan_source_seed(
    connection: sqlite3.Connection,
    catalog_path: Path,
    *,
    terms_reviewed_at: datetime,
    observed_at: datetime | None = None,
) -> SourceSeedPlan:
    """Build a read-only plan by comparing catalog values with latest stored versions."""

    catalog, catalog_bytes = _load_source_catalog_bytes(catalog_path)
    review_time = ensure_utc(terms_reviewed_at)
    seed_time = ensure_utc(observed_at or datetime.now(UTC))
    if review_time > seed_time:
        # Name both instants: the usual cause is a local-date timestamp written as UTC by an
        # operator whose clock is ahead of UTC, which reads as a future review date.
        raise CatalogError(
            f"--terms-reviewed-at {utc_timestamp(review_time)} is in the future; "
            f"the current UTC time is {utc_timestamp(seed_time)}. A review cannot be "
            "attested before it happened — pass a UTC timestamp at or before now."
        )
    catalog_sha256 = hashlib.sha256(catalog_bytes).hexdigest()
    counts = Counter(source.policy_tier for source in catalog.sources)
    attestations = tuple(
        TierAttestation(tier, counts[tier], catalog.policy_tiers[tier])
        for tier in sorted(catalog.policy_tiers)
    )
    changes = tuple(
        _plan_source(
            connection,
            source,
            catalog.policy_tiers[source.policy_tier],
            review_time,
            seed_time,
        )
        for source in catalog.sources
    )
    return SourceSeedPlan(
        catalog_path=catalog_path,
        catalog_sha256=catalog_sha256,
        observed_at=seed_time,
        terms_reviewed_at=review_time,
        tier_attestations=attestations,
        changes=changes,
        catalog=catalog,
    )


def apply_source_seed(connection: sqlite3.Connection, plan: SourceSeedPlan) -> SeedResult:
    """Apply a previously rendered plan using inserts only."""

    sources_by_id = {source.source_id: source for source in plan.catalog.sources}
    timestamp = utc_timestamp(plan.observed_at)
    review_timestamp = utc_timestamp(plan.terms_reviewed_at)
    source_version = f"catalog-sha256:{plan.catalog_sha256}"
    source_count = 0
    policy_count = 0
    for change in plan.changes:
        if not change.changed:
            continue
        source = sources_by_id[change.source_id]
        tier = plan.catalog.policy_tiers[source.policy_tier]
        connection.execute(
            "INSERT INTO source_keys(source_id) VALUES (?) ON CONFLICT(source_id) DO NOTHING",
            (source.source_id,),
        )
        if change.source_action != "unchanged":
            connection.execute(
                """
                INSERT INTO sources(
                    source_id, display_name, source_family, collector_kind, feed_url,
                    enabled, source, published_at, observed_at, ingested_at,
                    effective_at, valid_from, valid_to, source_version, run_id
                ) VALUES (?, ?, ?, ?, ?, 1, ?, NULL, ?, ?, NULL, ?, NULL, ?, NULL)
                """,
                (
                    source.source_id,
                    source.display_name,
                    source.source_family,
                    source.collector_kind,
                    source.feed_url,
                    CATALOG_PROVENANCE_SOURCE,
                    timestamp,
                    timestamp,
                    timestamp,
                    source_version,
                ),
            )
            source_count += 1
        if change.policy_action != "unchanged":
            connection.execute(
                """
                INSERT INTO source_policies(
                    source_id, permitted_use, raw_retention_days,
                    personal_data_fields_allowed, must_honor_deletions,
                    redistribution_allowed, third_party_processing_allowed,
                    commercial_use_status, terms_reviewed_at, source, published_at,
                    observed_at, ingested_at, effective_at, valid_from, valid_to,
                    source_version, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, ?, NULL)
                """,
                (
                    source.source_id,
                    tier.permitted_use,
                    tier.raw_retention_days,
                    json.dumps(
                        tier.personal_data_fields_allowed,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    tier.must_honor_deletions,
                    tier.redistribution_allowed,
                    tier.third_party_processing_allowed,
                    tier.commercial_use_status,
                    review_timestamp,
                    CATALOG_PROVENANCE_SOURCE,
                    timestamp,
                    timestamp,
                    review_timestamp,
                    timestamp,
                    source_version,
                ),
            )
            policy_count += 1
    return SeedResult(source_count, policy_count)


def check_catalog_feeds(
    catalog: NarrativeSourceCatalog,
    *,
    client: httpx.Client | None = None,
    sleep: Sleeper = time.sleep,
) -> FeedCheckReport:
    """Check every feed independently through the shared retry path; never write."""

    owned_client = client is None
    # Must match the collector's redirect behaviour, or the health check reports a feed
    # dead that collection would have fetched fine.
    http_client = client or httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True)
    context = http_client if owned_client else nullcontext(http_client)
    checks: list[FeedCheck] = []
    parser = RssAtomCollector()
    with context as active_client:
        for source in catalog.sources:
            attempts = 0
            try:
                response, attempts = get_with_retry(active_client, source.feed_url, {}, sleep=sleep)
                items = parser.parse(response.content)
            except HttpRequestFailure as error:
                status = "" if error.status_code is None else f" HTTP {error.status_code}"
                checks.append(
                    FeedCheck(
                        source.source_id,
                        source.feed_url,
                        False,
                        error.attempts,
                        None,
                        f"fetch failed after {error.attempts} attempts{status}",
                    )
                )
            except FeedParseError as error:
                checks.append(
                    FeedCheck(
                        source.source_id,
                        source.feed_url,
                        False,
                        attempts,
                        None,
                        str(error),
                    )
                )
            else:
                checks.append(
                    FeedCheck(
                        source.source_id,
                        source.feed_url,
                        True,
                        attempts,
                        len(items),
                        None,
                    )
                )
    return FeedCheckReport(tuple(checks))


def seed_plan_payload(plan: SourceSeedPlan) -> dict[str, object]:
    """JSON-ready plan, including every tier term covered by the attestation."""

    return {
        "catalog": str(plan.catalog_path),
        "catalog_sha256": plan.catalog_sha256,
        "terms_reviewed_at": utc_timestamp(plan.terms_reviewed_at),
        "tier_attestations": [
            {
                "policy": attestation.policy.model_dump(mode="json"),
                "source_count": attestation.source_count,
                "tier": attestation.tier,
            }
            for attestation in plan.tier_attestations
        ],
        "changes": [
            {
                "policy_action": change.policy_action,
                "policy_changed_fields": change.policy_changed_fields,
                "source_action": change.source_action,
                "source_changed_fields": change.source_changed_fields,
                "source_id": change.source_id,
            }
            for change in plan.changed_sources
        ],
        "unchanged_source_count": len(plan.changes) - len(plan.changed_sources),
    }


def feed_check_payload(report: FeedCheckReport) -> dict[str, object]:
    return {
        "failed_count": sum(not check.ok for check in report.checks),
        "feeds": [
            {
                "attempts": check.attempts,
                "error": check.error,
                "feed_url": check.feed_url,
                "item_count": check.item_count,
                "ok": check.ok,
                "source_id": check.source_id,
            }
            for check in report.checks
        ],
        "ok": report.ok,
        "source_count": len(report.checks),
    }


def _plan_source(
    connection: sqlite3.Connection,
    source: CatalogSource,
    tier: PolicyTier,
    review_time: datetime,
    seed_time: datetime,
) -> SourceSeedChange:
    cutoff = utc_timestamp(seed_time)
    current_source_row = connection.execute(
        """
        SELECT * FROM sources
        WHERE source_id = ?
          AND rtrim(observed_at, 'Z') <= rtrim(?, 'Z')
          AND rtrim(valid_from, 'Z') <= rtrim(?, 'Z')
          AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(?, 'Z'))
        ORDER BY observed_at DESC, source_record_id DESC LIMIT 1
        """,
        (source.source_id, cutoff, cutoff, cutoff),
    ).fetchone()
    source_fields = {
        "display_name": source.display_name,
        "source_family": source.source_family,
        "collector_kind": source.collector_kind,
        "feed_url": source.feed_url,
        "enabled": True,
    }
    if current_source_row is None:
        source_action: Literal["insert", "version", "unchanged"] = "insert"
        changed_source_fields = tuple(source_fields)
    else:
        current_source = SourceRow.from_db(current_source_row)
        changed_source_fields = tuple(
            field
            for field, value in source_fields.items()
            if getattr(current_source, field) != value
        )
        source_action = "version" if changed_source_fields else "unchanged"

    current_policy_row = connection.execute(
        """
        SELECT * FROM source_policies
        WHERE source_id = ?
          AND rtrim(observed_at, 'Z') <= rtrim(?, 'Z')
          AND rtrim(valid_from, 'Z') <= rtrim(?, 'Z')
          AND (valid_to IS NULL OR rtrim(valid_to, 'Z') > rtrim(?, 'Z'))
        ORDER BY observed_at DESC, source_policy_id DESC LIMIT 1
        """,
        (source.source_id, cutoff, cutoff, cutoff),
    ).fetchone()
    policy_fields = {
        "permitted_use": tier.permitted_use,
        "raw_retention_days": tier.raw_retention_days,
        "personal_data_fields_allowed": tier.personal_data_fields_allowed,
        "must_honor_deletions": tier.must_honor_deletions,
        "redistribution_allowed": tier.redistribution_allowed,
        "third_party_processing_allowed": tier.third_party_processing_allowed,
        "commercial_use_status": tier.commercial_use_status,
        "terms_reviewed_at": review_time,
    }
    if current_policy_row is None:
        policy_action: Literal["insert", "version", "unchanged"] = "insert"
        changed_policy_fields = tuple(policy_fields)
    else:
        current_policy = SourcePolicyRow.from_db(current_policy_row)
        changed_policy_fields = tuple(
            field
            for field, value in policy_fields.items()
            if getattr(current_policy, field) != value
        )
        policy_action = "version" if changed_policy_fields else "unchanged"
    return SourceSeedChange(
        source.source_id,
        source_action,
        changed_source_fields,
        policy_action,
        changed_policy_fields,
    )


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


__all__ = [
    "CATALOG_PROVENANCE_SOURCE",
    "CatalogError",
    "CatalogSource",
    "FeedCheck",
    "FeedCheckReport",
    "NarrativeSourceCatalog",
    "PolicyTier",
    "SeedResult",
    "SourceSeedChange",
    "SourceSeedPlan",
    "TierAttestation",
    "apply_source_seed",
    "check_catalog_feeds",
    "feed_check_payload",
    "load_source_catalog",
    "plan_source_seed",
    "seed_plan_payload",
]
