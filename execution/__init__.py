"""
Execution package for Deribit BTC Inverse Options Backtest Catalog.
Provides deterministic fill simulation (Model A, Model B), tick rounding,
multi-leg All-Or-None bundle execution, and cost scenario modeling.
"""

from execution.fill_simulator import (
    compute_trading_fee,
    round_to_tick,
    simulate_fill,
    simulate_multi_leg_fill,
)
from execution.models import (
    CostScenarioConfig,
    ExecutionConfig,
    ExecutionError,
    ExecutionModelType,
    InvalidOrderQuantityError,
    MultiLegFillDecision,
    OrderExpiredError,
    ScenarioName,
    UnsupportedPartialFillError,
)
from execution.scenarios import (
    ScenarioEvaluationResult,
    evaluate_fixed_orders_scenario,
    get_predefined_scenarios,
    verify_monotonic_fixed_orders,
)

__all__ = [
    "compute_trading_fee",
    "round_to_tick",
    "simulate_fill",
    "simulate_multi_leg_fill",
    "CostScenarioConfig",
    "ExecutionConfig",
    "ExecutionError",
    "ExecutionModelType",
    "InvalidOrderQuantityError",
    "MultiLegFillDecision",
    "OrderExpiredError",
    "ScenarioName",
    "UnsupportedPartialFillError",
    "ScenarioEvaluationResult",
    "evaluate_fixed_orders_scenario",
    "get_predefined_scenarios",
    "verify_monotonic_fixed_orders",
]
