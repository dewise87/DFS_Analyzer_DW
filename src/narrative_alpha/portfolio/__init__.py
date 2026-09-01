"""L5: solver-independent lineup construction and portfolio controls."""

from typing import TYPE_CHECKING, Any

from narrative_alpha.portfolio.adapter import (
    OptimizerAdapter,
    OptimizerError,
    UnsupportedOptimizationFeature,
)
from narrative_alpha.portfolio.export import export_upload_csv
from narrative_alpha.portfolio.heuristic_report import (
    HEURISTIC_NOTICE,
    HeuristicLineupRow,
    HeuristicReport,
    HeuristicReportError,
    HeuristicThresholds,
    build_heuristic_report,
    render_heuristic_report,
)
from narrative_alpha.portfolio.models import (
    CLASSIC_SITE_RULES,
    BringBackRule,
    CandidatePlayer,
    CandidatePlayerScenario,
    ClassicSiteRules,
    ContestArchetype,
    DfsSite,
    ExposureLimit,
    Lineup,
    LineupPlayer,
    NumericRange,
    OptimizationRequest,
    PlayerExposureRange,
    SlateType,
    StackRule,
    UploadEntry,
    ValidationIssue,
    ValidationResult,
    lineup_sha256,
)
from narrative_alpha.portfolio.validation import (
    validate_lineup,
    validate_portfolio,
)

if TYPE_CHECKING:
    from narrative_alpha.portfolio.pydfs_adapter import PydfsAdapter

__all__ = [
    "CLASSIC_SITE_RULES",
    "HEURISTIC_NOTICE",
    "BringBackRule",
    "CandidatePlayer",
    "CandidatePlayerScenario",
    "ClassicSiteRules",
    "ContestArchetype",
    "DfsSite",
    "ExposureLimit",
    "HeuristicLineupRow",
    "HeuristicReport",
    "HeuristicReportError",
    "HeuristicThresholds",
    "Lineup",
    "LineupPlayer",
    "NumericRange",
    "OptimizationRequest",
    "OptimizerAdapter",
    "OptimizerError",
    "PlayerExposureRange",
    "PydfsAdapter",
    "SlateType",
    "StackRule",
    "UnsupportedOptimizationFeature",
    "UploadEntry",
    "ValidationIssue",
    "ValidationResult",
    "build_heuristic_report",
    "export_upload_csv",
    "lineup_sha256",
    "render_heuristic_report",
    "validate_lineup",
    "validate_portfolio",
]


def __getattr__(name: str) -> Any:
    """Load the pydfs-backed adapter lazily (PEP 562).

    Importing narrative_alpha.portfolio must not transitively import the
    legacy pydfs optimizer package; only touching PydfsAdapter does.
    """

    if name == "PydfsAdapter":
        from narrative_alpha.portfolio.pydfs_adapter import PydfsAdapter

        return PydfsAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
