"""
Cost scenario definitions and comparative evaluation.
Provides 3 explicit cost scenarios (Optimistic, Baseline, Pessimistic)
with calibrated=False default (explicit uncalibrated assumptions).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from core.contracts import (
    Fill,
    FillDecision,
    OrderIntent,
    PriceObservation,
    Side,
)
from execution.fill_simulator import (
    simulate_fill,
    simulate_multi_leg_fill,
)
from execution.models import (
    CostScenarioConfig,
    ExecutionConfig,
    ExecutionModelType,
    ScenarioName,
)


def get_predefined_scenarios() -> Dict[ScenarioName, CostScenarioConfig]:
    """Return the 3 canonical cost scenarios."""
    return {
        ScenarioName.OPTIMISTIC: CostScenarioConfig(
            scenario_name=ScenarioName.OPTIMISTIC,
            slippage_btc=Decimal("0.0"),
            spread_btc=Decimal("0.0"),
            fee_rate=Decimal("0.0003"),
            fee_cap_ratio=Decimal("0.125"),
            calibrated=False,
            notes="Optimistic scenario: zero slippage and zero spread (uncalibrated assumption)",
        ),
        ScenarioName.BASELINE: CostScenarioConfig(
            scenario_name=ScenarioName.BASELINE,
            slippage_btc=Decimal("0.0005"),  # 1 tick slippage
            spread_btc=Decimal("0.0005"),    # 1 tick spread
            fee_rate=Decimal("0.0003"),
            fee_cap_ratio=Decimal("0.125"),
            calibrated=False,
            notes="Baseline scenario: 1 tick spread, 1 tick slippage (uncalibrated assumption)",
        ),
        ScenarioName.PESSIMISTIC: CostScenarioConfig(
            scenario_name=ScenarioName.PESSIMISTIC,
            slippage_btc=Decimal("0.0015"),  # 3 ticks slippage
            spread_btc=Decimal("0.0015"),    # 3 ticks spread
            fee_rate=Decimal("0.0003"),
            fee_cap_ratio=Decimal("0.125"),
            calibrated=False,
            notes="Pessimistic scenario: 3 ticks spread, 3 ticks slippage (uncalibrated assumption)",
        ),
    }


@dataclass(frozen=True)
class ScenarioEvaluationResult:
    """Summary of fills and PnL under a specific cost scenario."""
    scenario_name: ScenarioName
    config: CostScenarioConfig
    fills: Tuple[Fill, ...]
    total_fees_btc: Decimal
    total_spread_slippage_cost_btc: Decimal
    gross_cashflow_btc: Decimal  # Cash flow from trade prices (sell + / buy -)
    net_pnl_btc: Decimal          # gross_cashflow - total_fees


def evaluate_fixed_orders_scenario(
    orders: Sequence[OrderIntent],
    observations_map: Mapping[str, Sequence[PriceObservation]],
    scenario: CostScenarioConfig,
    model_type: ExecutionModelType = ExecutionModelType.MODEL_A_TRADE_ESTIMATED,
    tick_size: Decimal = Decimal("0.0005"),
    fee_schedule: Any = None,
) -> ScenarioEvaluationResult:
    """Evaluate a fixed set of orders under a given cost scenario."""
    exec_cfg = ExecutionConfig(
        model_type=model_type,
        tick_size=tick_size,
        cost_scenario=scenario,
        slippage_btc=scenario.slippage_btc,
        spread_btc=scenario.spread_btc,
        calibrated=scenario.calibrated,
    )

    fills: List[Fill] = []
    for ord_intent in orders:
        obs_seq = observations_map.get(ord_intent.instrument_name, ())
        decision = simulate_fill(
            order=ord_intent,
            eligible_observations=obs_seq,
            execution_config=exec_cfg,
            fee_schedule=fee_schedule,
        )
        if not decision.filled or decision.fill is None:
            raise RuntimeError(f"Fixed order {ord_intent.order_id} failed to fill: {decision.rejection_reason}")
        fills.append(decision.fill)

    total_fees = Decimal("0.0")
    total_spread_slip = Decimal("0.0")
    gross_cashflow = Decimal("0.0")

    for f in fills:
        total_fees += f.fee_btc
        total_spread_slip += f.spread_slippage_cost_btc
        if f.side == Side.BUY:
            gross_cashflow -= (f.price_btc * f.quantity)
        else:
            gross_cashflow += (f.price_btc * f.quantity)

    net_pnl = gross_cashflow - total_fees

    return ScenarioEvaluationResult(
        scenario_name=scenario.scenario_name,
        config=scenario,
        fills=tuple(fills),
        total_fees_btc=total_fees,
        total_spread_slippage_cost_btc=total_spread_slip,
        gross_cashflow_btc=gross_cashflow,
        net_pnl_btc=net_pnl,
    )


def verify_monotonic_fixed_orders(
    orders: Sequence[OrderIntent],
    observations_map: Mapping[str, Sequence[PriceObservation]],
    model_type: ExecutionModelType = ExecutionModelType.MODEL_A_TRADE_ESTIMATED,
    fee_schedule: Any = None,
) -> Dict[ScenarioName, ScenarioEvaluationResult]:
    """Verify that across a FIXED order list, increasing cost never increases net PnL:
    
    Invariant:
    net_pnl(Optimistic) >= net_pnl(Baseline) >= net_pnl(Pessimistic)
    """
    scenarios = get_predefined_scenarios()
    results: Dict[ScenarioName, ScenarioEvaluationResult] = {}

    for name in (ScenarioName.OPTIMISTIC, ScenarioName.BASELINE, ScenarioName.PESSIMISTIC):
        sc_cfg = scenarios[name]
        res = evaluate_fixed_orders_scenario(
            orders=orders,
            observations_map=observations_map,
            scenario=sc_cfg,
            model_type=model_type,
            fee_schedule=fee_schedule,
        )
        results[name] = res

    opt_pnl = results[ScenarioName.OPTIMISTIC].net_pnl_btc
    base_pnl = results[ScenarioName.BASELINE].net_pnl_btc
    pess_pnl = results[ScenarioName.PESSIMISTIC].net_pnl_btc

    if not (opt_pnl >= base_pnl >= pess_pnl):
        raise AssertionError(
            f"Monotonicity violation on fixed orders: opt={opt_pnl}, base={base_pnl}, pess={pess_pnl}"
        )

    return results
