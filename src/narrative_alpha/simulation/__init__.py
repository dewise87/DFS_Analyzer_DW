"""Experimental, point-in-time contest simulation."""

from narrative_alpha.simulation.calibration import (
    CalibrationResult,
    SimulationCalibrationError,
    calibrate_week,
)
from narrative_alpha.simulation.config import (
    DEFAULT_SIMULATION_CONFIG_PATH,
    CalibrationConfig,
    DependenceConfig,
    FieldConfig,
    SimulationConfig,
    SimulationConfigError,
    load_simulation_config,
)
from narrative_alpha.simulation.evaluation import (
    ContestEvaluation,
    ContestSimulationError,
    evaluate_contest,
    split_tied_payouts,
)
from narrative_alpha.simulation.field import (
    FieldGenerationError,
    FieldGenerationResult,
    calibrate_ownership_targets,
    generate_field,
    lineup_is_legal,
)
from narrative_alpha.simulation.models import (
    EXPERIMENTAL_NOTICE,
    DistributionProvenance,
    LineupSimulationResult,
    OwnershipMarginal,
    PlayerSimulationInput,
    PortfolioSimulationResult,
    SimulationReport,
    SimulationRunResult,
)
from narrative_alpha.simulation.outcomes import (
    OutcomeSimulationError,
    draw_player_outcomes,
    implied_pairwise_correlations,
)
from narrative_alpha.simulation.runner import (
    MAX_SIMULATED_FIELD_SIZE,
    SimulationRunError,
    load_contest_for_decision,
    load_ownership_for_decision,
    load_player_distributions_for_decision,
    render_simulation_report,
    run_simulation,
    simulation_report_path,
)

__all__ = [
    "DEFAULT_SIMULATION_CONFIG_PATH",
    "EXPERIMENTAL_NOTICE",
    "MAX_SIMULATED_FIELD_SIZE",
    "CalibrationConfig",
    "CalibrationResult",
    "ContestEvaluation",
    "ContestSimulationError",
    "DependenceConfig",
    "DistributionProvenance",
    "FieldConfig",
    "FieldGenerationError",
    "FieldGenerationResult",
    "LineupSimulationResult",
    "OutcomeSimulationError",
    "OwnershipMarginal",
    "PlayerSimulationInput",
    "PortfolioSimulationResult",
    "SimulationCalibrationError",
    "SimulationConfig",
    "SimulationConfigError",
    "SimulationReport",
    "SimulationRunError",
    "SimulationRunResult",
    "calibrate_ownership_targets",
    "calibrate_week",
    "draw_player_outcomes",
    "evaluate_contest",
    "generate_field",
    "implied_pairwise_correlations",
    "lineup_is_legal",
    "load_contest_for_decision",
    "load_ownership_for_decision",
    "load_player_distributions_for_decision",
    "load_simulation_config",
    "render_simulation_report",
    "run_simulation",
    "simulation_report_path",
    "split_tied_payouts",
]
