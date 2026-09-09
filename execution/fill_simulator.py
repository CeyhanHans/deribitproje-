"""
Deterministic Fill Simulator for Deribit BTC Inverse Options.
Supports:
- Model A: Trade-based estimated (first trade / next bar open reference after eligible_after_ms).
- Model B: Historical bid/ask replay (Buy ask / Sell bid, with automatic fallback to Model A).
- Precise tick rounding (Buy ceiling, Sell floor).
- Min quantity, quantity step, and liquidity depth gating.
- Max wait expiry and anti-lookahead timing.
- Explicit cost separation: trading fees vs spread/slippage cost.
- Multi-leg All-Or-None execution with individual leg timestamps.
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from core.contracts import (
    DataQuality,
    ExecutionModelId,
    Fill,
    FillDecision,
    OrderIntent,
    PriceBasis,
    PriceObservation,
    RunConfig,
    Side,
    ValidationError,
    validate_decimal,
)
from execution.models import (
    CostScenarioConfig,
    ExecutionConfig,
    ExecutionError,
    ExecutionModelType,
    MultiLegFillDecision,
    ScenarioName,
    UnsupportedPartialFillError,
)

try:
    from ingestion.settlement import (
        FeeSchedule,
        get_fee_schedule_for_timestamp,
    )
except ImportError:
    FeeSchedule = None
    get_fee_schedule_for_timestamp = None


def round_to_tick(price: Decimal, tick_size: Decimal, side: Side) -> Decimal:
    """Round price to the nearest tick_size.
    
    Deribit execution tick rounding rule:
    - Side.BUY: Rounds UP (ceiling) to tick_size (conservative taker cost).
    - Side.SELL: Rounds DOWN (floor) to tick_size (conservative taker cost).
    Non-positive sell prices are floored at Decimal('0.0').
    """
    if tick_size <= Decimal("0"):
        return max(Decimal("0"), price)

    ticks = price / tick_size
    if side == Side.BUY:
        rounded_ticks = Decimal(str(math.ceil(float(ticks))))
        # Exact decimal check to avoid float precision edge cases
        candidate = rounded_ticks * tick_size
        if candidate - price >= tick_size:
            candidate -= tick_size
        elif price > candidate:
            candidate += tick_size
        return candidate
    else:
        rounded_ticks = Decimal(str(math.floor(float(ticks))))
        candidate = rounded_ticks * tick_size
        if price - candidate >= tick_size:
            candidate += tick_size
        elif candidate > price:
            candidate -= tick_size
        return max(Decimal("0"), candidate)


def compute_trading_fee(
    fee_schedule: Any,
    quantity: Decimal,
    price_btc: Decimal,
    contract_size: Decimal = Decimal("1.0"),
    timestamp_ms: Optional[int] = None,
) -> Decimal:
    """Compute trading fee in BTC using FeeSchedule or standard Deribit rules.
    
    Deribit BTC inverse option trading fee rule:
    fee = min(quantity * contract_size * fee_rate, quantity * contract_size * price * fee_cap_ratio)
    Standard fee_rate = 0.0003 BTC (0.03%), fee_cap_ratio = 0.125 (12.5%).
    """
    if fee_schedule is not None and hasattr(fee_schedule, "compute_trading_fee"):
        return fee_schedule.compute_trading_fee(
            quantity=quantity,
            premium_price_btc=price_btc,
            contract_size=contract_size,
        )

    # Check if fee_schedule is a FeeSchedule-like object or has rates
    fee_rate = Decimal("0.0003")
    fee_cap_ratio = Decimal("0.125")

    if fee_schedule is not None:
        if hasattr(fee_schedule, "trading_fee_rate"):
            fee_rate = Decimal(str(fee_schedule.trading_fee_rate))
        elif hasattr(fee_schedule, "fee_rate_amount"):
            fee_rate = Decimal(str(fee_schedule.fee_rate_amount))

        if hasattr(fee_schedule, "trading_fee_cap_ratio"):
            fee_cap_ratio = Decimal(str(fee_schedule.trading_fee_cap_ratio))
        elif hasattr(fee_schedule, "fee_cap_ratio"):
            fee_cap_ratio = Decimal(str(fee_schedule.fee_cap_ratio))

    uncapped_fee = quantity * contract_size * fee_rate
    cap = quantity * contract_size * price_btc * fee_cap_ratio
    return min(uncapped_fee, cap)


def simulate_fill(
    order: OrderIntent,
    eligible_observations: Sequence[PriceObservation],
    execution_config: Any = None,
    fee_schedule: Any = None,
) -> FillDecision:
    """Simulate filling an order against eligible observations.
    
    Conforms to FillSimulatorProtocol:
    simulate_fill(order, eligible_observations, execution_config, fee_schedule) -> FillDecision
    """
    # 1. Normalize execution_config
    if execution_config is None:
        cfg = ExecutionConfig()
    elif isinstance(execution_config, ExecutionConfig):
        cfg = execution_config
    elif isinstance(execution_config, RunConfig):
        cfg = ExecutionConfig.from_run_config(execution_config)
    elif isinstance(execution_config, dict):
        cfg = ExecutionConfig(**execution_config)
    else:
        cfg = ExecutionConfig()

    # Apply cost scenario parameters if present
    slippage_btc = cfg.slippage_btc
    spread_btc = cfg.spread_btc
    calibrated = cfg.calibrated

    if cfg.cost_scenario is not None:
        slippage_btc = cfg.cost_scenario.slippage_btc
        spread_btc = cfg.cost_scenario.spread_btc
        calibrated = cfg.cost_scenario.calibrated

    # 2. Normalize fee_schedule
    if fee_schedule is None and get_fee_schedule_for_timestamp is not None:
        fee_sched = get_fee_schedule_for_timestamp(order.decision_at_ms)
    else:
        fee_sched = fee_schedule

    # 3. Check order quantity constraints (min_qty and qty_step)
    if order.quantity < cfg.min_qty:
        return FillDecision(
            order_id=order.order_id,
            filled=False,
            fill=None,
            rejection_reason=f"MIN_QTY_VIOLATION: quantity {order.quantity} < min_qty {cfg.min_qty}",
            observation_used=None,
        )

    if cfg.qty_step_known and cfg.qty_step is not None and cfg.qty_step > Decimal("0"):
        remainder = (order.quantity / cfg.qty_step) % Decimal("1")
        if remainder != Decimal("0"):
            return FillDecision(
                order_id=order.order_id,
                filled=False,
                fill=None,
                rejection_reason=f"QTY_STEP_VIOLATION: quantity {order.quantity} not a multiple of step {cfg.qty_step}",
                observation_used=None,
            )

    # Max order quantity check if configured
    if cfg.max_order_quantity is not None and order.quantity > cfg.max_order_quantity:
        return FillDecision(
            order_id=order.order_id,
            filled=False,
            fill=None,
            rejection_reason=f"OVERSIZED_QTY: quantity {order.quantity} exceeds max_order_quantity {cfg.max_order_quantity}",
            observation_used=None,
        )

    # 4. Filter eligible observations with strict anti-lookahead
    candidates: List[PriceObservation] = []
    for obs in eligible_observations:
        if obs.instrument_name != order.instrument_name:
            continue

        # Bar close prior to decision cannot be used:
        # If observation is an aggregated bar with interval_end_ms <= decision_at_ms, reject
        if obs.interval_end_ms is not None and obs.interval_end_ms <= order.decision_at_ms:
            continue

        # Anti-lookahead: observation must be available after order's eligible_after_ms
        if obs.available_at_ms < order.eligible_after_ms:
            continue

        candidates.append(obs)

    # Sort chronological by available_at_ms, then observed_at_ms
    candidates.sort(key=lambda x: (x.available_at_ms, x.observed_at_ms))

    if not candidates:
        return FillDecision(
            order_id=order.order_id,
            filled=False,
            fill=None,
            rejection_reason="NO_ELIGIBLE_OBSERVATIONS",
            observation_used=None,
        )

    # 5. Process candidate observations sequentially
    has_seen_oversized = False
    has_seen_expired = False

    for obs in candidates:
        # Expiration check: if observation timestamp is past order.expires_at_ms
        if obs.observed_at_ms > order.expires_at_ms or obs.available_at_ms > order.expires_at_ms:
            has_seen_expired = True
            break

        # Check Model B (Bid/Ask replay)
        if cfg.model_type == ExecutionModelType.MODEL_B_BID_ASK_REPLAY:
            if order.side == Side.BUY:
                # Need valid ask quote
                if obs.ask_price_btc is not None and obs.ask_price_btc > Decimal("0"):
                    # Depth check
                    if cfg.enforce_depth and obs.ask_size is not None and order.quantity > obs.ask_size:
                        has_seen_oversized = True
                        continue  # Not enough depth in this quote

                    # Valid quote fill
                    ref_price = obs.ask_price_btc
                    # In Model B, market spread is already reflected in the ask quote; only add slippage
                    raw_price = ref_price + slippage_btc
                    fill_price = round_to_tick(raw_price, cfg.tick_size, Side.BUY)
                    spread_slippage_cost = (fill_price - ref_price) * order.quantity
                    actual_ms = obs.observed_at_ms
                    model_exec_id = ExecutionModelId.MODEL_A_BID_ASK
                    resolution_tag = "quote_replay"
                    return _build_fill_decision(
                        order=order,
                        obs=obs,
                        fill_price=fill_price,
                        ref_price=ref_price,
                        spread_slippage_cost=spread_slippage_cost,
                        actual_ms=actual_ms,
                        model_exec_id=model_exec_id,
                        resolution_tag=resolution_tag,
                        fee_sched=fee_sched,
                        cfg=cfg,
                    )
                else:
                    # Missing ask quote: automatic fallback to Model A if enabled
                    if cfg.fallback_to_model_a and obs.trade_price_btc is not None and obs.trade_price_btc > Decimal("0"):
                        # Fallback to trade-based estimated
                        return _simulate_model_a_fill(
                            order=order,
                            obs=obs,
                            slippage_btc=slippage_btc,
                            spread_btc=spread_btc,
                            fee_sched=fee_sched,
                            cfg=cfg,
                            is_fallback=True,
                        )
                    # Quote missing, continue searching
                    continue

            elif order.side == Side.SELL:
                # Need valid bid quote
                if obs.bid_price_btc is not None and obs.bid_price_btc > Decimal("0"):
                    # Depth check
                    if cfg.enforce_depth and obs.bid_size is not None and order.quantity > obs.bid_size:
                        has_seen_oversized = True
                        continue  # Not enough depth in this quote

                    # Valid quote fill
                    ref_price = obs.bid_price_btc
                    # In Model B, market spread is already reflected in the bid quote; only subtract slippage
                    raw_price = max(Decimal("0"), ref_price - slippage_btc)
                    fill_price = round_to_tick(raw_price, cfg.tick_size, Side.SELL)
                    spread_slippage_cost = (ref_price - fill_price) * order.quantity
                    actual_ms = obs.observed_at_ms
                    model_exec_id = ExecutionModelId.MODEL_A_BID_ASK
                    resolution_tag = "quote_replay"
                    return _build_fill_decision(
                        order=order,
                        obs=obs,
                        fill_price=fill_price,
                        ref_price=ref_price,
                        spread_slippage_cost=spread_slippage_cost,
                        actual_ms=actual_ms,
                        model_exec_id=model_exec_id,
                        resolution_tag=resolution_tag,
                        fee_sched=fee_sched,
                        cfg=cfg,
                    )
                else:
                    # Missing bid quote: automatic fallback to Model A if enabled
                    if cfg.fallback_to_model_a and obs.trade_price_btc is not None and obs.trade_price_btc > Decimal("0"):
                        return _simulate_model_a_fill(
                            order=order,
                            obs=obs,
                            slippage_btc=slippage_btc,
                            spread_btc=spread_btc,
                            fee_sched=fee_sched,
                            cfg=cfg,
                            is_fallback=True,
                        )
                    continue

        # Model A: Trade-based estimated
        else:
            if obs.trade_price_btc is not None and obs.trade_price_btc > Decimal("0"):
                if cfg.enforce_depth and obs.trade_size is not None and order.quantity > obs.trade_size:
                    has_seen_oversized = True
                    continue

                return _simulate_model_a_fill(
                    order=order,
                    obs=obs,
                    slippage_btc=slippage_btc,
                    spread_btc=spread_btc,
                    fee_sched=fee_sched,
                    cfg=cfg,
                    is_fallback=False,
                )

    # If no fill was executed
    if has_seen_expired:
        reason = "ORDER_EXPIRED: order reached expires_at_ms without fill"
    elif has_seen_oversized:
        reason = "OVERSIZED_QTY: order quantity exceeds available liquidity depth"
    elif cfg.model_type == ExecutionModelType.MODEL_B_BID_ASK_REPLAY:
        reason = "MISSING_QUOTE_DATA: no valid bid/ask quote or trade fallback found"
    else:
        reason = "MISSING_TRADE_DATA: no valid trade observation found"

    return FillDecision(
        order_id=order.order_id,
        filled=False,
        fill=None,
        rejection_reason=reason,
        observation_used=None,
    )


def _simulate_model_a_fill(
    order: OrderIntent,
    obs: PriceObservation,
    slippage_btc: Decimal,
    spread_btc: Decimal,
    fee_sched: Any,
    cfg: ExecutionConfig,
    is_fallback: bool,
) -> FillDecision:
    """Execute fill under Model A trade-based estimated rules."""
    assert obs.trade_price_btc is not None
    ref_price = obs.trade_price_btc
    half_spread = spread_btc / Decimal("2")

    if order.side == Side.BUY:
        raw_price = ref_price + half_spread + slippage_btc
        fill_price = round_to_tick(raw_price, cfg.tick_size, Side.BUY)
        spread_slippage_cost = (fill_price - ref_price) * order.quantity
    else:
        raw_price = max(Decimal("0"), ref_price - half_spread - slippage_btc)
        fill_price = round_to_tick(raw_price, cfg.tick_size, Side.SELL)
        spread_slippage_cost = (ref_price - fill_price) * order.quantity

    # Determine timestamp accuracy: tick-level vs bar-resolution
    actual_ms = obs.observed_at_ms
    if "first_trade_at" in obs.source_ref:
        resolution_tag = "tick_resolution"
    elif obs.interval_start_ms is not None or obs.interval_end_ms is not None:
        resolution_tag = "bar_resolution"
    elif obs.price_basis == PriceBasis.TRADE:
        resolution_tag = "tick_resolution"
    else:
        resolution_tag = "bar_resolution"

    if is_fallback:
        resolution_tag = f"fallback_model_a:{resolution_tag}"
        model_exec_id = ExecutionModelId.MODEL_A_BID_ASK
    else:
        model_exec_id = cfg.execution_model_id

    return _build_fill_decision(
        order=order,
        obs=obs,
        fill_price=fill_price,
        ref_price=ref_price,
        spread_slippage_cost=spread_slippage_cost,
        actual_ms=actual_ms,
        model_exec_id=model_exec_id,
        resolution_tag=resolution_tag,
        fee_sched=fee_sched,
        cfg=cfg,
    )


def _build_fill_decision(
    order: OrderIntent,
    obs: PriceObservation,
    fill_price: Decimal,
    ref_price: Decimal,
    spread_slippage_cost: Decimal,
    actual_ms: int,
    model_exec_id: ExecutionModelId,
    resolution_tag: str,
    fee_sched: Any,
    cfg: ExecutionConfig,
) -> FillDecision:
    """Helper to assemble a valid Fill and FillDecision."""
    fee_btc = compute_trading_fee(
        fee_schedule=fee_sched,
        quantity=order.quantity,
        price_btc=fill_price,
        contract_size=cfg.contract_size,
        timestamp_ms=actual_ms,
    )

    source_ref = f"{obs.source_ref}|{resolution_tag}|calibrated={cfg.calibrated}".strip("|")

    fill = Fill(
        fill_id=f"fill_{order.order_id}_{actual_ms}",
        order_id=order.order_id,
        position_id=order.position_id,
        leg_id=order.leg_id,
        instrument_name=order.instrument_name,
        side=order.side,
        actual_ms=actual_ms,
        model_timestamp_ms=order.decision_at_ms,
        quantity=order.quantity,
        price_btc=fill_price,
        fee_btc=fee_btc,
        reference_price_btc=ref_price,
        spread_slippage_cost_btc=max(Decimal("0"), spread_slippage_cost),
        execution_model_id=model_exec_id,
        source_ref=source_ref,
    )

    return FillDecision(
        order_id=order.order_id,
        filled=True,
        fill=fill,
        rejection_reason=None,
        observation_used=obs,
    )


def simulate_multi_leg_fill(
    orders: Sequence[OrderIntent],
    observations_map: Mapping[str, Sequence[PriceObservation]],
    execution_config: Any = None,
    fee_schedule: Any = None,
) -> MultiLegFillDecision:
    """Simulate execution for a multi-leg order bundle under All-Or-None semantics.
    
    Strict rules:
    - If ANY leg fails to fill (missing quote without fallback, oversized quantity, expired, etc.),
      ALL legs are rejected (All-Or-None). Partial fill is strictly unsupported.
    - If ALL legs succeed, distinct fills are reported, each preserving its own leg timestamp.
    """
    if not orders:
        raise ExecutionError("Cannot simulate empty orders sequence")

    position_id = orders[0].position_id
    leg_decisions: List[FillDecision] = []
    fills: List[Fill] = []

    for ord_intent in orders:
        obs_seq = observations_map.get(ord_intent.instrument_name, ())
        decision = simulate_fill(
            order=ord_intent,
            eligible_observations=obs_seq,
            execution_config=execution_config,
            fee_schedule=fee_schedule,
        )
        leg_decisions.append(decision)
        if decision.filled and decision.fill is not None:
            fills.append(decision.fill)

    # Check All-Or-None requirement
    all_filled = len(fills) == len(orders) and all(d.filled for d in leg_decisions)

    if all_filled:
        return MultiLegFillDecision(
            position_id=position_id,
            filled=True,
            fills=tuple(fills),
            leg_decisions=tuple(leg_decisions),
            rejection_reason=None,
            all_or_none=True,
        )
    else:
        # Identify first failed leg
        failed_decisions = [d for d in leg_decisions if not d.filled]
        reasons = [f"leg_{d.order_id}:{d.rejection_reason}" for d in failed_decisions]
        rejection_str = f"ALL_OR_NONE_FAILED: {'; '.join(reasons)}"
        return MultiLegFillDecision(
            position_id=position_id,
            filled=False,
            fills=(),  # Strictly empty on rejection
            leg_decisions=tuple(leg_decisions),
            rejection_reason=rejection_str,
            all_or_none=True,
        )
