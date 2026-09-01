"""L2: canonical player identities, durable aliases, and manual review."""

from narrative_alpha.identity.crosswalk import (
    DEFAULT_AMBIGUITY_MARGIN,
    DEFAULT_FUZZY_THRESHOLD,
    CrosswalkError,
    PlayerCrosswalk,
)
from narrative_alpha.identity.models import (
    IdentityMatchResult,
    MatchCandidate,
    MatchMethod,
    PlayerIdentityInput,
)
from narrative_alpha.identity.nflverse import (
    NFLVERSE_SOURCE,
    PINNED_ROSTER_RELEASES,
    NflverseRosterError,
    PinnedRosterRelease,
    RosterHashError,
    RosterSchemaError,
    RosterSeedIssue,
    RosterSeedReport,
    fetch_pinned_roster,
    pinned_roster_release,
    seed_nflverse_roster,
)
from narrative_alpha.identity.normalization import (
    CANONICAL_TEAM_CODES,
    TEAM_CODE_ALIASES,
    name_without_suffix,
    normalize_name,
    normalize_team_code,
    team_code_variants,
)

__all__ = [
    "CANONICAL_TEAM_CODES",
    "DEFAULT_AMBIGUITY_MARGIN",
    "DEFAULT_FUZZY_THRESHOLD",
    "NFLVERSE_SOURCE",
    "PINNED_ROSTER_RELEASES",
    "TEAM_CODE_ALIASES",
    "CrosswalkError",
    "IdentityMatchResult",
    "MatchCandidate",
    "MatchMethod",
    "NflverseRosterError",
    "PinnedRosterRelease",
    "PlayerCrosswalk",
    "PlayerIdentityInput",
    "RosterHashError",
    "RosterSchemaError",
    "RosterSeedIssue",
    "RosterSeedReport",
    "fetch_pinned_roster",
    "name_without_suffix",
    "normalize_name",
    "normalize_team_code",
    "pinned_roster_release",
    "seed_nflverse_roster",
    "team_code_variants",
]
