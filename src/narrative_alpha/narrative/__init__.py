"""Narrative evidence collection and, in later slices, signal processing."""

from narrative_alpha.narrative.collectors import (
    DEFAULT_POLICY_MAX_AGE,
    CollectedItem,
    CollectionError,
    CollectionReport,
    CollectorBatch,
    FeedParseError,
    PolicyGateError,
    PurgeReport,
    RssAtomCollector,
    SourceCollector,
    clean_markup,
    collect_source,
    normalize_item_text,
    purge_expired_content,
    require_current_policy,
    tombstone_removed_item,
)

__all__ = [
    "DEFAULT_POLICY_MAX_AGE",
    "CollectedItem",
    "CollectionError",
    "CollectionReport",
    "CollectorBatch",
    "FeedParseError",
    "PolicyGateError",
    "PurgeReport",
    "RssAtomCollector",
    "SourceCollector",
    "clean_markup",
    "collect_source",
    "normalize_item_text",
    "purge_expired_content",
    "require_current_policy",
    "tombstone_removed_item",
]
