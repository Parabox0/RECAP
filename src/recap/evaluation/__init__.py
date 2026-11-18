"""Evaluation helpers: gate statistics, economics and deterministic fixtures."""

from .economics import break_even, packet_cost, search_cost
from .gates import GateReport, evaluate_gates
from .replay import FixtureReport, run_fixtures
from .stats import clopper_pearson_upper, cluster_bootstrap, design_effect, holdout_size

__all__ = [
    "FixtureReport",
    "GateReport",
    "break_even",
    "clopper_pearson_upper",
    "cluster_bootstrap",
    "design_effect",
    "evaluate_gates",
    "holdout_size",
    "packet_cost",
    "run_fixtures",
    "search_cost",
]
