"""
Chronological Multi-Trade Event Engine.
Implements BacktestRunnerProtocol conforming to schema 1.0.

Strict chronological loop order at each UTC grid step T:
1. Make available market data visible (available_at_ms <= T).
2. Expiry settlement for any active contracts expiring at T.
3. Process previously queued eligible orders (simulate fills with anti-lookahead).
4. Portfolio valuation and exit decisions (stop-loss / take-profit with gap pricing).
5. Generate new entry signals for available capital.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from core.contracts import (
    BacktestRunnerProtocol,
    CoverageReport,
    Currency,
    DataQuality,
    EntryType,
    EquityPoint,
    ExecutionModelId,
    Fill,
    FillDecision,
    Instrument,
    LedgerEntry,
    OptionType,
    OrderIntent,
    PortfolioState,
    Position,
    PositionStatus,
    PriceObservation,
    ReasonCode,
    RunConfig,
    RunResult,
    SettlementObservation,
    Side,
    to_dict,
)
from engine.data_bundle import DataBundle
from engine.metrics import compute_run_metrics
from execution import (
    CostScenarioConfig,
    ExecutionConfig,
    ExecutionModelType,
    round_to_tick,
    simulate_fill,
)
from portfolio import (
    MarginModelConfig,
    apply_fill,
    evaluate_equity,
    settle,
)
from portfolio.settlement_service import (
    calculate_inverse_option_payoff_per_contract,
    compute_delivery_fee,
)


class EventEngine:
    """Chronological event-driven backtesting engine for multi-trade option strategies."""

    def __init__(
        self,
        config: RunConfig,
        data_bundle: DataBundle,
        margin_config: Optional[MarginModelConfig] = None,
        execution_config: Optional[ExecutionConfig] = None,
        signal_fn: Optional[Callable[..., Sequence[Any]]] = None,
        selection_fn: Optional[Callable[..., Any]] = None,
        forced_close_at_end: bool = False,
    ) -> None:
        self.config = config
        self.data_bundle = data_bundle
        self.margin_config = margin_config or MarginModelConfig(
            model_name="conservative_stress_reserve",
            stress_reserve_ratio=config.stress_reserve_ratio,
            exchange_margin_equivalent=False,
            allow_naked_short=False,
        )
        self.execution_config = execution_config or ExecutionConfig.from_run_config(config)
        self.signal_fn = signal_fn
        self.selection_fn = selection_fn
        self.forced_close_at_end = forced_close_at_end

        # Pre-index instruments
        self.instruments_by_name: Dict[str, Instrument] = {
            inst.instrument_name: inst for inst in data_bundle.instruments
        }

        # Parse strategy params into dictionary
        self.strategy_params: Dict[str, str] = dict(config.strategy_params)

        # Parse risk management parameters
        self.stop_loss_btc = Decimal(self.strategy_params.get("stop_loss_btc", "0.05"))
        self.take_profit_btc = Decimal(self.strategy_params.get("take_profit_btc", "0.10"))
        self.max_open_positions = int(self.strategy_params.get("max_open_positions", "1"))
        self.order_validity_hours = int(self.strategy_params.get("order_validity_hours", "2"))

    def run(self) -> RunResult:
        """Execute the chronological event loop from start_ms to end_ms."""
        config = self.config
        step_ms = config.decision_resolution_hours * 3600 * 1000
        order_validity_ms = self.order_validity_hours * 3600 * 1000

        # Initialize portfolio state
        portfolio_state = PortfolioState(
            timestamp_ms=config.start_ms,
            cash_balance_btc=config.initial_capital_btc,
            reserved_balance_btc=Decimal("0.0"),
            open_positions=(),
            closed_positions=(),
            ledger=(),
        )

        all_orders: List[OrderIntent] = []
        all_fills: List[Fill] = []
        pending_orders: List[OrderIntent] = []
        equity_curve: List[EquityPoint] = []
        skipped_reasons: Dict[str, int] = {}
        warnings: List[str] = []

        # Build chronological timeline from start_ms to end_ms (inclusive of start, exclusive of end)
        timestamps = list(range(config.start_ms, config.end_ms, step_ms))
        if not timestamps or timestamps[-1] < config.start_ms:
            timestamps = [config.start_ms]

        for t in timestamps:
            # ------------------------------------------------------------------
            # Step 1: Make available market data visible (available_at_ms <= T)
            # ------------------------------------------------------------------
            visible_obs = [
                obs for obs in self.data_bundle.observations
                if obs.available_at_ms <= t
            ]
            current_underlying_usd = self.data_bundle.get_underlying_price_at(t) or Decimal("60000.0")

            mark_prices: Dict[str, Decimal] = {}
            for obs in visible_obs:
                if obs.mark_price_btc is not None:
                    mark_prices[obs.instrument_name] = obs.mark_price_btc
                elif obs.trade_price_btc is not None:
                    mark_prices[obs.instrument_name] = obs.trade_price_btc

            # ------------------------------------------------------------------
            # Step 2: Expiry Settlement
            # ------------------------------------------------------------------
            # Settle any active open positions whose legs expire at T
            for settlement in self.data_bundle.settlements:
                if settlement.expiry_ms == t:
                    has_open_match = any(
                        any(leg.instrument_name == settlement.instrument_name for leg in pos.legs)
                        for pos in portfolio_state.open_positions
                    )
                    if has_open_match:
                        portfolio_state = settle(
                            state=portfolio_state,
                            settlement=settlement,
                            margin_config=self.margin_config,
                        )
                    else:
                        # Multi-leg position settlement handling:
                        # Check if a position closed at this timestamp has another leg matching this settlement
                        # that has not yet had settlement ledger entries created.
                        matching_closed = [
                            pos for pos in portfolio_state.closed_positions
                            if pos.closed_at_ms == t
                            and any(leg.instrument_name == settlement.instrument_name for leg in pos.legs)
                            and not any(
                                e.event_id.startswith(f"settle_{settlement.expiry_ms}_{settlement.instrument_name}_{pos.position_id}")
                                for e in portfolio_state.ledger
                            )
                        ]
                        if matching_closed:
                            current_cash = portfolio_state.cash_balance_btc
                            current_reserved = portfolio_state.reserved_balance_btc
                            new_ledger = list(portfolio_state.ledger)
                            event_id_prefix = f"settle_{settlement.expiry_ms}_{settlement.instrument_name}_"
                            stress_ratio = self.margin_config.stress_reserve_ratio if self.margin_config else Decimal("0.5")
                            updated_closed = list(portfolio_state.closed_positions)

                            for pos in matching_closed:
                                total_settlement_cashflow = Decimal("0.0")
                                total_delivery_fees = Decimal("0.0")
                                reserved_to_release = Decimal("0.0")

                                for idx, leg in enumerate(pos.legs):
                                    if leg.instrument_name != settlement.instrument_name:
                                        continue
                                    open_f = pos.open_fills[idx] if idx < len(pos.open_fills) else None
                                    qty = pos.leg_quantities[idx]
                                    contract_size = leg.contract_size

                                    payoff_per_contract = calculate_inverse_option_payoff_per_contract(
                                        option_type=leg.option_type,
                                        strike_usd=leg.strike_usd,
                                        settlement_price_usd=settlement.settlement_price_usd,
                                    )
                                    total_payoff = payoff_per_contract * qty * contract_size
                                    deliv_fee = compute_delivery_fee(
                                        quantity=qty,
                                        payoff_btc=payoff_per_contract,
                                        instrument_name=leg.instrument_name,
                                        contract_size=contract_size,
                                    )

                                    if open_f is not None and open_f.side == Side.BUY:
                                        if total_payoff > Decimal("0.0"):
                                            current_cash += total_payoff
                                            new_ledger.append(
                                                LedgerEntry(
                                                    event_id=f"{event_id_prefix}{pos.position_id}_payoff",
                                                    position_id=pos.position_id,
                                                    timestamp_ms=settlement.expiry_ms,
                                                    currency=Currency.BTC,
                                                    amount=total_payoff,
                                                    entry_type=EntryType.SETTLEMENT,
                                                    balance_after=current_cash,
                                                    description=f"Settlement payoff credit for {leg.instrument_name}",
                                                )
                                            )
                                        if deliv_fee > Decimal("0.0"):
                                            current_cash -= deliv_fee
                                            new_ledger.append(
                                                LedgerEntry(
                                                    event_id=f"{event_id_prefix}{pos.position_id}_fee",
                                                    position_id=pos.position_id,
                                                    timestamp_ms=settlement.expiry_ms,
                                                    currency=Currency.BTC,
                                                    amount=-deliv_fee,
                                                    entry_type=EntryType.FEE,
                                                    balance_after=current_cash,
                                                    description=f"Delivery fee debit for {leg.instrument_name}",
                                                )
                                            )
                                        total_settlement_cashflow += total_payoff
                                        total_delivery_fees += deliv_fee
                                    else:
                                        if total_payoff > Decimal("0.0"):
                                            current_cash -= total_payoff
                                            new_ledger.append(
                                                LedgerEntry(
                                                    event_id=f"{event_id_prefix}{pos.position_id}_payoff",
                                                    position_id=pos.position_id,
                                                    timestamp_ms=settlement.expiry_ms,
                                                    currency=Currency.BTC,
                                                    amount=-total_payoff,
                                                    entry_type=EntryType.SETTLEMENT,
                                                    balance_after=current_cash,
                                                    description=f"Settlement payoff debit for short {leg.instrument_name}",
                                                )
                                            )
                                        if deliv_fee > Decimal("0.0"):
                                            current_cash -= deliv_fee
                                            new_ledger.append(
                                                LedgerEntry(
                                                    event_id=f"{event_id_prefix}{pos.position_id}_fee",
                                                    position_id=pos.position_id,
                                                    timestamp_ms=settlement.expiry_ms,
                                                    currency=Currency.BTC,
                                                    amount=-deliv_fee,
                                                    entry_type=EntryType.FEE,
                                                    balance_after=current_cash,
                                                    description=f"Delivery fee debit for short {leg.instrument_name}",
                                                )
                                            )
                                        total_settlement_cashflow -= total_payoff
                                        total_delivery_fees += deliv_fee
                                        rel = qty * contract_size * stress_ratio
                                        reserved_to_release += rel

                                if reserved_to_release > Decimal("0.0"):
                                    current_reserved = max(Decimal("0.0"), current_reserved - reserved_to_release)
                                    new_ledger.append(
                                        LedgerEntry(
                                            event_id=f"{event_id_prefix}{pos.position_id}_reserve_release",
                                            position_id=pos.position_id,
                                            timestamp_ms=settlement.expiry_ms,
                                            currency=Currency.BTC,
                                            amount=Decimal("0.0"),
                                            entry_type=EntryType.RESERVE_ADJUSTMENT,
                                            balance_after=current_cash,
                                            description=f"Released {reserved_to_release} BTC stress reserve upon settlement",
                                        )
                                    )

                                leg_pnl_btc = total_settlement_cashflow - total_delivery_fees
                                new_pnl_btc = pos.realized_pnl_btc + leg_pnl_btc
                                new_pnl_usd = pos.realized_pnl_usd + (leg_pnl_btc * settlement.settlement_price_usd)

                                updated_pos = Position(
                                    position_id=pos.position_id,
                                    strategy_name=pos.strategy_name,
                                    status=pos.status,
                                    legs=pos.legs,
                                    leg_quantities=pos.leg_quantities,
                                    opened_at_ms=pos.opened_at_ms,
                                    closed_at_ms=pos.closed_at_ms,
                                    open_fills=pos.open_fills,
                                    close_fills=pos.close_fills,
                                    realized_pnl_btc=new_pnl_btc,
                                    realized_pnl_usd=new_pnl_usd,
                                    exit_reason=pos.exit_reason,
                                )
                                pos_idx = updated_closed.index(pos)
                                updated_closed[pos_idx] = updated_pos

                            portfolio_state = PortfolioState(
                                timestamp_ms=t,
                                cash_balance_btc=current_cash,
                                reserved_balance_btc=current_reserved,
                                open_positions=portfolio_state.open_positions,
                                closed_positions=tuple(updated_closed),
                                ledger=tuple(new_ledger),
                            )

            # ------------------------------------------------------------------
            # Step 3: Process Previously Queued Eligible Orders
            # ------------------------------------------------------------------
            remaining_pending: List[OrderIntent] = []
            for ord_intent in pending_orders:
                # Check expiration
                if t >= ord_intent.expires_at_ms:
                    skipped_reasons["order_expired"] = skipped_reasons.get("order_expired", 0) + 1
                    continue

                # Anti-lookahead: Order must be eligible at or before T
                if ord_intent.eligible_after_ms <= t:
                    # Collect observations available between order eligibility and T
                    eligible_for_order = [
                        obs for obs in visible_obs
                        if obs.instrument_name == ord_intent.instrument_name
                        and obs.available_at_ms >= ord_intent.eligible_after_ms
                    ]
                    decision = simulate_fill(
                        order=ord_intent,
                        eligible_observations=eligible_for_order,
                        execution_config=self.execution_config,
                        fee_schedule=None,
                    )
                    if decision.filled and decision.fill is not None:
                        portfolio_state = apply_fill(
                            state=portfolio_state,
                            fill=decision.fill,
                            underlying_price_usd=current_underlying_usd,
                            margin_config=self.margin_config,
                            instrument_map=self.instruments_by_name,
                        )
                        all_fills.append(decision.fill)

                        # Preserve specific exit reason from the order on closed position
                        if ord_intent.reason in (
                            ReasonCode.STOP_LOSS,
                            ReasonCode.TAKE_PROFIT,
                            ReasonCode.TIME_EXPIRY,
                            ReasonCode.CONTRACT_EXPIRY,
                        ):
                            closed_list = []
                            modified = False
                            for cp in portfolio_state.closed_positions:
                                if cp.position_id == ord_intent.position_id and cp.exit_reason != ord_intent.reason:
                                    updated_cp = Position(
                                        position_id=cp.position_id,
                                        strategy_name=cp.strategy_name,
                                        status=cp.status,
                                        legs=cp.legs,
                                        leg_quantities=cp.leg_quantities,
                                        opened_at_ms=cp.opened_at_ms,
                                        closed_at_ms=cp.closed_at_ms,
                                        open_fills=cp.open_fills,
                                        close_fills=cp.close_fills,
                                        realized_pnl_btc=cp.realized_pnl_btc,
                                        realized_pnl_usd=cp.realized_pnl_usd,
                                        exit_reason=ord_intent.reason,
                                    )
                                    closed_list.append(updated_cp)
                                    modified = True
                                else:
                                    closed_list.append(cp)
                            if modified:
                                portfolio_state = PortfolioState(
                                    timestamp_ms=t,
                                    cash_balance_btc=portfolio_state.cash_balance_btc,
                                    reserved_balance_btc=portfolio_state.reserved_balance_btc,
                                    open_positions=portfolio_state.open_positions,
                                    closed_positions=tuple(closed_list),
                                    ledger=portfolio_state.ledger,
                                )
                    else:
                        # Keep pending to retry next period until expires_at_ms
                        remaining_pending.append(ord_intent)
                else:
                    remaining_pending.append(ord_intent)

            pending_orders = remaining_pending

            # Ensure portfolio_state timestamp is aligned with current grid step t
            if portfolio_state.timestamp_ms != t:
                portfolio_state = PortfolioState(
                    timestamp_ms=t,
                    cash_balance_btc=portfolio_state.cash_balance_btc,
                    reserved_balance_btc=portfolio_state.reserved_balance_btc,
                    open_positions=portfolio_state.open_positions,
                    closed_positions=portfolio_state.closed_positions,
                    ledger=portfolio_state.ledger,
                )

            # ------------------------------------------------------------------
            # Step 4: Portfolio Valuation and Exit Decisions
            # ------------------------------------------------------------------
            is_reliable = True
            quality_note = ""
            # Verify if any open position has missing valuation data
            for pos in portfolio_state.open_positions:
                for leg in pos.legs:
                    if leg.instrument_name not in mark_prices:
                        is_reliable = False
                        quality_note = f"valuation_unknown: missing observation for {leg.instrument_name}"

            equity_pt = evaluate_equity(
                state=portfolio_state,
                timestamp_ms=t,
                underlying_price_usd=current_underlying_usd,
                benchmark_initial_btc=config.initial_capital_btc,
                option_mark_prices=mark_prices,
                is_valuation_reliable=is_reliable,
                quality_note=quality_note,
            )
            equity_curve.append(equity_pt)

            # Evaluate stop loss and take profit for active open positions
            # "stop/TP bar-close net liquidation estimate ile tespit edilir, sonraki eligible observation'da fill.
            #  Threshold fiyatından garantili stop üretme."
            for pos in list(portfolio_state.open_positions):
                # Don't queue duplicate exits if already queued for this position
                if any(po.position_id == pos.position_id for po in pending_orders):
                    continue

                pos_unrealized_btc = Decimal("0.0")
                has_mark_data = True
                for idx, leg in enumerate(pos.legs):
                    if leg.instrument_name in mark_prices and idx < len(pos.open_fills):
                        mark = mark_prices[leg.instrument_name]
                        of = pos.open_fills[idx]
                        qty = pos.leg_quantities[idx]
                        if of.side == Side.BUY:
                            pos_unrealized_btc += (mark - of.price_btc) * qty
                        else:
                            pos_unrealized_btc += (of.price_btc - mark) * qty
                    else:
                        has_mark_data = False

                if not has_mark_data:
                    continue  # Wait for observation or settlement

                exit_reason: Optional[ReasonCode] = None
                if pos_unrealized_btc <= -self.stop_loss_btc:
                    exit_reason = ReasonCode.STOP_LOSS
                elif pos_unrealized_btc >= self.take_profit_btc:
                    exit_reason = ReasonCode.TAKE_PROFIT

                if exit_reason is not None:
                    # Emit market exit orders for all legs of the position
                    for idx, leg in enumerate(pos.legs):
                        if idx < len(pos.open_fills):
                            of = pos.open_fills[idx]
                            exit_side = Side.SELL if of.side == Side.BUY else Side.BUY
                            exit_order = OrderIntent(
                                order_id=f"ord_exit_{pos.position_id}_{leg.instrument_name}_{t}",
                                position_id=pos.position_id,
                                leg_id=of.leg_id,
                                instrument_name=leg.instrument_name,
                                side=exit_side,
                                quantity=of.quantity,
                                decision_at_ms=t,
                                eligible_after_ms=t + 1,  # Stop/TP fills at subsequent observation
                                expires_at_ms=t + order_validity_ms,
                                reason=exit_reason,
                            )
                            pending_orders.append(exit_order)
                            all_orders.append(exit_order)

            # ------------------------------------------------------------------
            # Step 5: New Entry Signals
            # ------------------------------------------------------------------
            # Check maximum concurrent open positions gate
            if len(portfolio_state.open_positions) < self.max_open_positions:
                # Evaluate signals if signal_fn provided
                signals = ()
                if self.signal_fn is not None:
                    signals = self.signal_fn(config, visible_obs, portfolio_state)
                elif self.strategy_params.get("auto_entry_hourly", "true").lower() == "true":
                    # Default: check if any eligible active instruments exist for new position
                    # Ensure we don't open multiple positions at once if max reached
                    if not portfolio_state.open_positions and not pending_orders:
                        # Simple candidate selection from instruments
                        candidate_legs = [
                            inst for inst in self.data_bundle.instruments
                            if inst.creation_ms <= t < inst.expiry_ms
                        ]
                        if candidate_legs:
                            # Select call and put pair sharing same expiry
                            expiries = sorted(set(inst.expiry_ms for inst in candidate_legs))
                            selected_expiry = expiries[0]
                            calls = [i for i in candidate_legs if i.expiry_ms == selected_expiry and i.option_type == OptionType.CALL]
                            puts = [i for i in candidate_legs if i.expiry_ms == selected_expiry and i.option_type == OptionType.PUT]
                            if calls and puts:
                                # ATM selection
                                calls.sort(key=lambda x: abs(x.strike_usd - current_underlying_usd))
                                puts.sort(key=lambda x: abs(x.strike_usd - current_underlying_usd))
                                sel_call = calls[0]
                                sel_put = puts[0]
                                signals = [("long_straddle", (sel_call, sel_put))]

                for sig in signals:
                    if len(portfolio_state.open_positions) >= self.max_open_positions:
                        break

                    new_pos_id = f"pos_{config.strategy_name}_{t}"
                    if isinstance(sig, tuple) and len(sig) == 2 and isinstance(sig[1], (tuple, list)):
                        selected_instruments = sig[1]
                    else:
                        selected_instruments = [self.data_bundle.instruments[0]] if self.data_bundle.instruments else []

                    for inst in selected_instruments:
                        ord_intent = OrderIntent(
                            order_id=f"ord_entry_{new_pos_id}_{inst.instrument_name}_{t}",
                            position_id=new_pos_id,
                            leg_id=f"leg_{inst.instrument_name}",
                            instrument_name=inst.instrument_name,
                            side=Side.BUY,
                            quantity=Decimal("1.0"),
                            decision_at_ms=t,
                            eligible_after_ms=t + 1,  # Anti-lookahead: fills on observations subsequent to decision at T
                            expires_at_ms=t + order_validity_ms,
                            reason=ReasonCode.ENTRY_SIGNAL,
                        )
                        pending_orders.append(ord_intent)
                        all_orders.append(ord_intent)

        # ----------------------------------------------------------------------
        # Step 6: End of Backtest (T == config.end_ms)
        # ----------------------------------------------------------------------
        # "End-time default açık pozisyonu open_at_end tutar; kapatılmış kazanç oranına katma."
        # "forced_close yalnız end öncesi gerçek eligible fiyat mevcutsa ayrı policy."
        open_at_end = portfolio_state.open_positions

        if self.forced_close_at_end and open_at_end:
            # Explicit forced close policy
            for pos in list(open_at_end):
                for idx, leg in enumerate(pos.legs):
                    of = pos.open_fills[idx]
                    # Find last observation before end_ms
                    obs_candidates = [
                        obs for obs in self.data_bundle.observations
                        if obs.instrument_name == leg.instrument_name and obs.available_at_ms < config.end_ms
                    ]
                    if obs_candidates:
                        last_obs = sorted(obs_candidates, key=lambda x: x.available_at_ms)[-1]
                        price = last_obs.trade_price_btc or last_obs.bid_price_btc or Decimal("0.0")
                        forced_fill = Fill(
                            fill_id=f"fill_forced_{pos.position_id}_{leg.instrument_name}",
                            order_id=f"ord_forced_{pos.position_id}_{leg.instrument_name}",
                            position_id=pos.position_id,
                            leg_id=of.leg_id,
                            instrument_name=leg.instrument_name,
                            side=Side.SELL if of.side == Side.BUY else Side.BUY,
                            actual_ms=last_obs.observed_at_ms,
                            model_timestamp_ms=config.end_ms,
                            quantity=of.quantity,
                            price_btc=price,
                            fee_btc=Decimal("0.0003"),
                            reference_price_btc=price,
                            spread_slippage_cost_btc=Decimal("0.0"),
                            execution_model_id=ExecutionModelId.MODEL_A_BID_ASK,
                            source_ref="forced_close_at_end",
                        )
                        portfolio_state = apply_fill(
                            state=portfolio_state,
                            fill=forced_fill,
                            underlying_price_usd=current_underlying_usd,
                            margin_config=self.margin_config,
                        )
                        all_fills.append(forced_fill)
            open_at_end = portfolio_state.open_positions

        # Metrics computation across completed trades
        completed_trades = portfolio_state.closed_positions
        metrics = compute_run_metrics(
            trades=completed_trades,
            equity_curve=equity_curve,
        )

        # Build deterministic hashes
        cfg_dict = to_dict(config)
        config_hash = hashlib.sha256(json.dumps(cfg_dict, sort_keys=True).encode()).hexdigest()[:16]
        data_hash = hashlib.sha256(
            f"obs_{len(self.data_bundle.observations)}_set_{len(self.data_bundle.settlements)}".encode()
        ).hexdigest()[:16]
        code_hash = "engine_v1_deterministic"

        skipped_items = tuple(sorted(skipped_reasons.items()))

        return RunResult(
            config_hash=config_hash,
            data_hash=data_hash,
            code_hash=code_hash,
            run_timestamp_ms=config.start_ms,
            start_ms=config.start_ms,
            end_ms=config.end_ms,
            schema_version=config.schema_version,
            orders=tuple(all_orders),
            fills=tuple(all_fills),
            ledger=portfolio_state.ledger,
            trades=completed_trades,
            equity_curve=tuple(equity_curve),
            coverage_reports=(),
            metrics=metrics,
            open_positions_at_end=open_at_end,
            skipped_reasons=skipped_items,
            warnings=tuple(warnings),
            capabilities=(
                "hourly_backtest",
                "inverse_btc",
                "multi_trade_event_engine",
                "chronological_event_loop",
            ),
        )


def run(config: RunConfig, data_bundle: Any) -> RunResult:
    """Entry point for executing a backtest conforming to BacktestRunnerProtocol."""
    if not isinstance(data_bundle, DataBundle):
        # Convert or wrap
        if isinstance(data_bundle, dict):
            bundle = DataBundle(**data_bundle)
        else:
            bundle = DataBundle()
    else:
        bundle = data_bundle

    engine = EventEngine(config=config, data_bundle=bundle)
    return engine.run()
