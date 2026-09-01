"""Optimizer abstraction and explicit capability failures."""

from __future__ import annotations

from typing import Protocol

from narrative_alpha.portfolio.models import (
    DfsSite,
    Lineup,
    OptimizationRequest,
    UploadEntry,
    ValidationResult,
)


class OptimizerError(RuntimeError):
    """Base error for optimizer construction, solving, or export."""


class UnsupportedOptimizationFeature(OptimizerError):
    """Raised when a request uses controls the selected adapter cannot honor."""

    def __init__(self, features: tuple[str, ...]) -> None:
        self.features = features
        super().__init__(f"adapter does not support: {', '.join(features)}")


class OptimizerAdapter(Protocol):
    def build_lineups(self, request: OptimizationRequest) -> tuple[Lineup, ...]: ...

    def validate_lineup(self, lineup: Lineup, request: OptimizationRequest) -> ValidationResult: ...

    def export_upload_csv(
        self,
        lineups: tuple[Lineup, ...],
        site: DfsSite,
        entries: tuple[UploadEntry, ...] = (),
    ) -> bytes: ...
