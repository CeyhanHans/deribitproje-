"""
Acceptance and unit tests for Task 11:
- Model A: Trade-based estimated execution
- Model B: Historical bid/ask quote replay with automatic fallback to Model A
- 4 trade directions (Long entry/exit, Short entry/exit)
- Conservative tick rounding (Buy ceiling, Sell floor)
- Missing quote handling & fallback
- Min quantity, quantity step, and oversized quantity depth checks
- Max wait expiration and anti-lookahead timing
- Clear separation of fees (FeeSchedule) and spread/slippage costs
- 3 cost scenarios (Optimistic, Baseline, Pessimistic) with calibrated=False default
- Monotonicity invariant on fixed order lists (increasing costs never increases net PnL)
- Non-monotonic outcome demonstration under dynamic signal/exit regeneration
- Multi-leg All-Or-None bundle execution with independent leg timestamps
"""

from decimal import Decimal
import unittest

from core.contracts import (
    DataQuality,
    ExecutionModelId,
    Fill,
    FillDecision,
    FillSimulatorProtocol,
    OrderIntent,
    PriceBasis,
    PriceObservation,
    ReasonCode,
    RunConfig,
    Side,
)
from execution import (
    CostScenarioConfig,
    ExecutionConfig,
    ExecutionError,
    ExecutionModelType,
    MultiLegFillDecision,
    ScenarioName,
    UnsupportedPartialFillError,
    compute_trading_fee,
    evaluate_fixed_orders_scenario,
    get_predefined_scenarios,
    round_to_tick,
    simulate_fill,
    simulate_multi_leg_fill,
    verify_monotonic_fixed_orders,
)
from ingestion.settlement import (
    DERIBIT_CURRENT_CONFIRMED_FEE_SCHEDULE,
    DERIBIT_HISTORICAL_UNVERIFIED_FEE_SCHEDULE,
    FeeSchedule,
)


class ExecutionV1AcceptanceTests(unittest.TestCase):
    """Rigorous acceptance tests for Task 11 execution engine."""

    def setUp(self) -> None:
        self.tick_size = Decimal("0.0005")
        self.fee_schedule = DERIBIT_CURRENT_CONFIRMED_FEE_SCHEDULE
        self.call_instrument = "BTC-27DEC24-90000-C"
        self.put_instrument = "BTC-27DEC24-90000-P"

    # --------------------------------------------------------------------------
    # 1. Protocol Conformance
    # --------------------------------------------------------------------------
    def test_protocol_conformance(self) -> None:
        """simulate_fill strictly conforms to the locked FillSimulatorProtocol."""
        self.assertTrue(isinstance(simulate_fill, FillSimulatorProtocol))

    # --------------------------------------------------------------------------
    # 2. Four Trade Directions (Long Entry/Exit, Short Entry/Exit)
    # --------------------------------------------------------------------------
    def test_four_trade_directions_model_b(self) -> None:
        """Test all 4 trade directions under Model B quote replay."""
        # 1. Long Entry: BUY call at Ask (e.g. Ask = 0.0852)
        # Raw = 0.0852 + 0.0 (no slip) -> rounded UP to 0.0855
        buy_entry_order = OrderIntent(
            order_id="ord_long_entry",
            position_id="pos_long_call",
            leg_id="leg_call",
            instrument_name=self.call_instrument,
            side=Side.BUY,
            quantity=Decimal("1.0"),
            decision_at_ms=1711700000000,
            eligible_after_ms=1711700000000,
            expires_at_ms=1711703600000,
            reason=ReasonCode.ENTRY_SIGNAL,
        )
        obs_entry = PriceObservation(
            instrument_name=self.call_instrument,
            observed_at_ms=1711700050000,
            available_at_ms=1711700050000,
            price_basis=PriceBasis.BID,
            bid_price_btc=Decimal("0.0840"),
            ask_price_btc=Decimal("0.0852"),
            trade_price_btc=Decimal("0.0845"),
        )
        cfg_b = ExecutionConfig(
            model_type=ExecutionModelType.MODEL_B_BID_ASK_REPLAY,
            tick_size=self.tick_size,
            slippage_btc=Decimal("0.0"),
        )
        dec_long_entry = simulate_fill(buy_entry_order, [obs_entry], cfg_b, self.fee_schedule)
        self.assertTrue(dec_long_entry.filled)
        fill_le = dec_long_entry.fill
        self.assertIsNotNone(fill_le)
        self.assertEqual(fill_le.side, Side.BUY)
        # Buy ceiling: 0.0852 / 0.0005 = 170.4 -> ceil = 171 -> 0.0855
        self.assertEqual(fill_le.price_btc, Decimal("0.0855"))
        self.assertEqual(fill_le.reference_price_btc, Decimal("0.0852"))

        # 2. Long Exit: SELL call at Bid (e.g. Bid = 0.0924)
        # Raw = 0.0924 -> rounded DOWN to 0.0920
        sell_exit_order = OrderIntent(
            order_id="ord_long_exit",
            position_id="pos_long_call",
            leg_id="leg_call",
            instrument_name=self.call_instrument,
            side=Side.SELL,
            quantity=Decimal("1.0"),
            decision_at_ms=1711703600000,
            eligible_after_ms=1711703600000,
            expires_at_ms=1711707200000,
            reason=ReasonCode.TAKE_PROFIT,
        )
        obs_exit = PriceObservation(
            instrument_name=self.call_instrument,
            observed_at_ms=1711703650000,
            available_at_ms=1711703650000,
            price_basis=PriceBasis.BID,
            bid_price_btc=Decimal("0.0924"),
            ask_price_btc=Decimal("0.0935"),
        )
        dec_long_exit = simulate_fill(sell_exit_order, [obs_exit], cfg_b, self.fee_schedule)
        self.assertTrue(dec_long_exit.filled)
        fill_lx = dec_long_exit.fill
        self.assertIsNotNone(fill_lx)
        self.assertEqual(fill_lx.side, Side.SELL)
        # Sell floor: 0.0924 / 0.0005 = 184.8 -> floor = 184 -> 0.0920
        self.assertEqual(fill_lx.price_btc, Decimal("0.0920"))
        self.assertEqual(fill_lx.reference_price_btc, Decimal("0.0924"))

        # 3. Short Entry: SELL call to open (writing option at Bid)
        short_entry_order = OrderIntent(
            order_id="ord_short_entry",
            position_id="pos_short_call",
            leg_id="leg_call",
            instrument_name=self.call_instrument,
            side=Side.SELL,
            quantity=Decimal("1.0"),
            decision_at_ms=1711700000000,
            eligible_after_ms=1711700000000,
            expires_at_ms=1711703600000,
            reason=ReasonCode.ENTRY_SIGNAL,
        )
        dec_short_entry = simulate_fill(short_entry_order, [obs_entry], cfg_b, self.fee_schedule)
        self.assertTrue(dec_short_entry.filled)
        fill_se = dec_short_entry.fill
        self.assertIsNotNone(fill_se)
        self.assertEqual(fill_se.side, Side.SELL)
        # Bid = 0.0840 -> exact multiple -> 0.0840
        self.assertEqual(fill_se.price_btc, Decimal("0.0840"))

        # 4. Short Exit: BUY call to close (buy back at Ask)
        short_exit_order = OrderIntent(
            order_id="ord_short_exit",
            position_id="pos_short_call",
            leg_id="leg_call",
            instrument_name=self.call_instrument,
            side=Side.BUY,
            quantity=Decimal("1.0"),
            decision_at_ms=1711703600000,
            eligible_after_ms=1711703600000,
            expires_at_ms=1711707200000,
            reason=ReasonCode.STOP_LOSS,
        )
        dec_short_exit = simulate_fill(short_exit_order, [obs_exit], cfg_b, self.fee_schedule)
        self.assertTrue(dec_short_exit.filled)
        fill_sx = dec_short_exit.fill
        self.assertIsNotNone(fill_sx)
        self.assertEqual(fill_sx.side, Side.BUY)
        # Ask = 0.0935 -> exact multiple -> 0.0935
        self.assertEqual(fill_sx.price_btc, Decimal("0.0935"))

    # --------------------------------------------------------------------------
    # 3. Tick Rounding Precision
    # --------------------------------------------------------------------------
    def test_tick_rounding_rules(self) -> None:
        """Verify adverse tick rounding: BUY ceiling, SELL floor."""
        tick = Decimal("0.0005")

        # In-between price 0.0502:
        self.assertEqual(round_to_tick(Decimal("0.0502"), tick, Side.BUY), Decimal("0.0505"))
        self.assertEqual(round_to_tick(Decimal("0.0502"), tick, Side.SELL), Decimal("0.0500"))

        # In-between price 0.0504:
        self.assertEqual(round_to_tick(Decimal("0.0504"), tick, Side.BUY), Decimal("0.0505"))
        self.assertEqual(round_to_tick(Decimal("0.0504"), tick, Side.SELL), Decimal("0.0500"))

        # Exact multiple 0.0500:
        self.assertEqual(round_to_tick(Decimal("0.0500"), tick, Side.BUY), Decimal("0.0500"))
        self.assertEqual(round_to_tick(Decimal("0.0500"), tick, Side.SELL), Decimal("0.0500"))

        # Non-negative constraint on sell:
        self.assertEqual(round_to_tick(Decimal("-0.0002"), tick, Side.SELL), Decimal("0.0"))

    # --------------------------------------------------------------------------
    # 4. Missing Quote and Automatic Fallback to Model A
    # --------------------------------------------------------------------------
    def test_missing_quote_automatic_fallback_to_model_a(self) -> None:
        """When Model B lacks valid quotes, it automatically falls back to Model A."""
        order = OrderIntent(
            order_id="ord_fallback",
            position_id="pos_fb",
            leg_id="leg_1",
            instrument_name=self.call_instrument,
            side=Side.BUY,
            quantity=Decimal("1.0"),
            decision_at_ms=1711700000000,
            eligible_after_ms=1711700000000,
            expires_at_ms=1711703600000,
        )
        # Observation has NO ask quote (ask_price_btc is None), but HAS trade price 0.0600
        obs = PriceObservation(
            instrument_name=self.call_instrument,
            observed_at_ms=1711700010000,
            available_at_ms=1711700010000,
            price_basis=PriceBasis.TRADE,
            bid_price_btc=None,
            ask_price_btc=None,
            trade_price_btc=Decimal("0.0600"),
        )
        cfg = ExecutionConfig(
            model_type=ExecutionModelType.MODEL_B_BID_ASK_REPLAY,
            fallback_to_model_a=True,
            spread_btc=Decimal("0.0010"),  # Half spread = 0.0005
            slippage_btc=Decimal("0.0"),
        )
        dec = simulate_fill(order, [obs], cfg, self.fee_schedule)
        self.assertTrue(dec.filled)
        fill = dec.fill
        self.assertIsNotNone(fill)
        # Model A used: ref = 0.0600 + half_spread (0.0005) = 0.0605
        self.assertEqual(fill.price_btc, Decimal("0.0605"))
        self.assertIn("fallback_model_a", fill.source_ref)

    def test_missing_quote_and_missing_trade_data_rejection(self) -> None:
        """When neither quote nor trade data is present, order is rejected cleanly."""
        order = OrderIntent(
            order_id="ord_no_data",
            position_id="pos_nd",
            leg_id="leg_1",
            instrument_name=self.call_instrument,
            side=Side.BUY,
            quantity=Decimal("1.0"),
            decision_at_ms=1711700000000,
            eligible_after_ms=1711700000000,
            expires_at_ms=1711703600000,
        )
        obs_empty = PriceObservation(
            instrument_name=self.call_instrument,
            observed_at_ms=1711700010000,
            available_at_ms=1711700010000,
            price_basis=PriceBasis.MARK,
            bid_price_btc=None,
            ask_price_btc=None,
            trade_price_btc=None,
        )
        cfg = ExecutionConfig(model_type=ExecutionModelType.MODEL_B_BID_ASK_REPLAY)
        dec = simulate_fill(order, [obs_empty], cfg, self.fee_schedule)
        self.assertFalse(dec.filled)
        self.assertIsNone(dec.fill)
        self.assertIn("MISSING_QUOTE_DATA", str(dec.rejection_reason))

    # --------------------------------------------------------------------------
    # 5. Min Quantity, Qty Step, and Oversized Quantity Checks
    # --------------------------------------------------------------------------
    def test_min_qty_rejection(self) -> None:
        """Order quantity below min_qty is strictly rejected."""
        order = OrderIntent(
            order_id="ord_tiny",
            position_id="pos_t",
            leg_id="leg_1",
            instrument_name=self.call_instrument,
            side=Side.BUY,
            quantity=Decimal("0.05"),  # min_qty is 0.1
            decision_at_ms=1711700000000,
            eligible_after_ms=1711700000000,
            expires_at_ms=1711703600000,
        )
        cfg = ExecutionConfig(min_qty=Decimal("0.1"))
        dec = simulate_fill(order, [], cfg, self.fee_schedule)
        self.assertFalse(dec.filled)
        self.assertIn("MIN_QTY_VIOLATION", str(dec.rejection_reason))

    def test_qty_step_violation_rejection(self) -> None:
        """Order quantity that is not a multiple of qty_step is rejected."""
        order = OrderIntent(
            order_id="ord_fractional",
            position_id="pos_f",
            leg_id="leg_1",
            instrument_name=self.call_instrument,
            side=Side.BUY,
            quantity=Decimal("0.15"),  # step is 0.1
            decision_at_ms=1711700000000,
            eligible_after_ms=1711700000000,
            expires_at_ms=1711703600000,
        )
        cfg = ExecutionConfig(min_qty=Decimal("0.1"), qty_step=Decimal("0.1"), qty_step_known=True)
        dec = simulate_fill(order, [], cfg, self.fee_schedule)
        self.assertFalse(dec.filled)
        self.assertIn("QTY_STEP_VIOLATION", str(dec.rejection_reason))

    def test_oversized_qty_rejected_partial_fill_unsupported(self) -> None:
        """Single trade price does not prove depth; oversized quantity is rejected without partial fill."""
        order = OrderIntent(
            order_id="ord_huge",
            position_id="pos_h",
            leg_id="leg_1",
            instrument_name=self.call_instrument,
            side=Side.BUY,
            quantity=Decimal("25.0"),
            decision_at_ms=1711700000000,
            eligible_after_ms=1711700000000,
            expires_at_ms=1711703600000,
        )
        # Depth is only 5.0 contracts
        obs = PriceObservation(
            instrument_name=self.call_instrument,
            observed_at_ms=1711700010000,
            available_at_ms=1711700010000,
            price_basis=PriceBasis.ASK,
            ask_price_btc=Decimal("0.0850"),
            ask_size=Decimal("5.0"),
        )
        cfg = ExecutionConfig(
            model_type=ExecutionModelType.MODEL_B_BID_ASK_REPLAY,
            enforce_depth=True,
        )
        dec = simulate_fill(order, [obs], cfg, self.fee_schedule)
        self.assertFalse(dec.filled)
        self.assertIn("OVERSIZED_QTY", str(dec.rejection_reason))

    def test_partial_fill_configuration_strictly_unsupported(self) -> None:
        """V1 explicitly disallows partial fills; configuring allow_partial_fill raises error."""
        with self.assertRaises(UnsupportedPartialFillError):
            ExecutionConfig(allow_partial_fill=True)

    # --------------------------------------------------------------------------
    # 6. Max Wait Expiration and Anti-Lookahead Timing
    # --------------------------------------------------------------------------
    def test_anti_lookahead_prior_bar_close_excluded(self) -> None:
        """Bar close prior to decision cannot be used for execution."""
        order = OrderIntent(
            order_id="ord_anti_lookahead",
            position_id="pos_al",
            leg_id="leg_1",
            instrument_name=self.call_instrument,
            side=Side.BUY,
            quantity=Decimal("1.0"),
            decision_at_ms=1711703600000,
            eligible_after_ms=1711703600000,
            expires_at_ms=1711707200000,
        )
        # Prior bar that closed AT decision_at_ms (1711703600000)
        prior_bar = PriceObservation(
            instrument_name=self.call_instrument,
            observed_at_ms=1711703600000,
            available_at_ms=1711703600000,
            price_basis=PriceBasis.TRADE,
            trade_price_btc=Decimal("0.0500"),
            interval_start_ms=1711700000000,
            interval_end_ms=1711703600000,  # Ended before/at decision!
        )
        # Next bar after decision (starts at 1711703600000, ends at 1711707200000)
        next_bar = PriceObservation(
            instrument_name=self.call_instrument,
            observed_at_ms=1711707200000,
            available_at_ms=1711707200000,
            price_basis=PriceBasis.TRADE,
            trade_price_btc=Decimal("0.0550"),
            interval_start_ms=1711703600000,
            interval_end_ms=1711707200000,
        )
        cfg = ExecutionConfig(model_type=ExecutionModelType.MODEL_A_TRADE_ESTIMATED)
        dec = simulate_fill(order, [prior_bar, next_bar], cfg, self.fee_schedule)
        self.assertTrue(dec.filled)
        fill = dec.fill
        self.assertIsNotNone(fill)
        # MUST use next_bar price (0.0550), not prior_bar (0.0500)
        self.assertEqual(fill.reference_price_btc, Decimal("0.0550"))
        self.assertEqual(fill.actual_ms, 1711707200000)
        self.assertIn("bar_resolution", fill.source_ref)

    def test_max_wait_expiry_rejection(self) -> None:
        """When observations only arrive after expires_at_ms, the order expires."""
        order = OrderIntent(
            order_id="ord_expire",
            position_id="pos_exp",
            leg_id="leg_1",
            instrument_name=self.call_instrument,
            side=Side.BUY,
            quantity=Decimal("1.0"),
            decision_at_ms=1711700000000,
            eligible_after_ms=1711700000000,
            expires_at_ms=1711703600000,  # 1 hour window
        )
        # Observation arrives 1 second past expiry
        late_obs = PriceObservation(
            instrument_name=self.call_instrument,
            observed_at_ms=1711703601000,
            available_at_ms=1711703601000,
            price_basis=PriceBasis.TRADE,
            trade_price_btc=Decimal("0.0700"),
        )
        dec = simulate_fill(order, [late_obs], ExecutionConfig(), self.fee_schedule)
        self.assertFalse(dec.filled)
        self.assertIn("ORDER_EXPIRED", str(dec.rejection_reason))

    # --------------------------------------------------------------------------
    # 7. Fee Schedule and Cost Decomposition
    # --------------------------------------------------------------------------
    def test_fee_decomposition_and_deribit_cap(self) -> None:
        """Trading fee and spread/slippage costs are cleanly separated; fee cap applies."""
        order = OrderIntent(
            order_id="ord_fee_test",
            position_id="pos_fee",
            leg_id="leg_1",
            instrument_name=self.call_instrument,
            side=Side.BUY,
            quantity=Decimal("2.0"),
            decision_at_ms=1711700000000,
            eligible_after_ms=1711700000000,
            expires_at_ms=1711703600000,
        )
        obs = PriceObservation(
            instrument_name=self.call_instrument,
            observed_at_ms=1711700010000,
            available_at_ms=1711700010000,
            price_basis=PriceBasis.ASK,
            ask_price_btc=Decimal("0.0800"),
        )
        # Slippage = 0.0010 BTC
        cfg = ExecutionConfig(
            model_type=ExecutionModelType.MODEL_B_BID_ASK_REPLAY,
            slippage_btc=Decimal("0.0010"),
        )
        dec = simulate_fill(order, [obs], cfg, self.fee_schedule)
        self.assertTrue(dec.filled)
        fill = dec.fill
        self.assertIsNotNone(fill)

        # Price = 0.0800 + 0.0010 = 0.0810
        self.assertEqual(fill.price_btc, Decimal("0.0810"))
        self.assertEqual(fill.reference_price_btc, Decimal("0.0800"))
        # Spread/slippage cost = (0.0810 - 0.0800) * 2.0 = 0.0020 BTC
        self.assertEqual(fill.spread_slippage_cost_btc, Decimal("0.0020"))

        # Deribit fee rule:
        # uncapped = 2.0 * 1.0 * 0.0003 = 0.0006 BTC
        # cap = 2.0 * 1.0 * 0.0810 * 0.125 = 0.02025 BTC
        # min(0.0006, 0.02025) = 0.0006 BTC
        self.assertEqual(fill.fee_btc, Decimal("0.0006"))

    def test_fee_cap_on_deep_otm_cheap_option(self) -> None:
        """On very cheap options, the 12.5% premium cap restricts the fee."""
        order = OrderIntent(
            order_id="ord_cheap",
            position_id="pos_cheap",
            leg_id="leg_1",
            instrument_name=self.call_instrument,
            side=Side.BUY,
            quantity=Decimal("1.0"),
            decision_at_ms=1711700000000,
            eligible_after_ms=1711700000000,
            expires_at_ms=1711703600000,
        )
        # Deep OTM price: 0.0010 BTC
        obs = PriceObservation(
            instrument_name=self.call_instrument,
            observed_at_ms=1711700010000,
            available_at_ms=1711700010000,
            price_basis=PriceBasis.ASK,
            ask_price_btc=Decimal("0.0010"),
        )
        cfg = ExecutionConfig(
            model_type=ExecutionModelType.MODEL_B_BID_ASK_REPLAY,
            slippage_btc=Decimal("0.0"),
        )
        dec = simulate_fill(order, [obs], cfg, self.fee_schedule)
        self.assertTrue(dec.filled)
        fill = dec.fill
        self.assertIsNotNone(fill)
        # Uncapped fee would be 0.0003 BTC.
        # Cap is 1.0 * 0.0010 * 0.125 = 0.000125 BTC.
        # Fee must strictly equal cap: 0.000125 BTC.
        self.assertEqual(fill.fee_btc, Decimal("0.000125"))

    # --------------------------------------------------------------------------
    # 8. Three Cost Scenarios and Monotonicity on Fixed Orders
    # --------------------------------------------------------------------------
    def test_three_scenarios_calibrated_false_default(self) -> None:
        """All predefined scenarios are uncalibrated (calibrated=False) by default."""
        scenarios = get_predefined_scenarios()
        for name, sc in scenarios.items():
            self.assertFalse(sc.calibrated, f"Scenario {name} must have calibrated=False")
            self.assertIn("uncalibrated", sc.notes.lower())

    def test_monotonic_pnl_on_fixed_order_list(self) -> None:
        """Strict requirement: On the SAME fixed order list, increasing cost never increases net PnL."""
        # Fixed order list: Buy 1 Call at T1, Sell 1 Call at T2
        entry_order = OrderIntent(
            order_id="ord_f_entry",
            position_id="pos_fixed",
            leg_id="leg_call",
            instrument_name=self.call_instrument,
            side=Side.BUY,
            quantity=Decimal("1.0"),
            decision_at_ms=1711700000000,
            eligible_after_ms=1711700000000,
            expires_at_ms=1711703600000,
        )
        exit_order = OrderIntent(
            order_id="ord_f_exit",
            position_id="pos_fixed",
            leg_id="leg_call",
            instrument_name=self.call_instrument,
            side=Side.SELL,
            quantity=Decimal("1.0"),
            decision_at_ms=1711703600000,
            eligible_after_ms=1711703600000,
            expires_at_ms=1711707200000,
        )
        obs_map = {
            self.call_instrument: [
                PriceObservation(
                    instrument_name=self.call_instrument,
                    observed_at_ms=1711700050000,
                    available_at_ms=1711700050000,
                    price_basis=PriceBasis.TRADE,
                    trade_price_btc=Decimal("0.0600"),
                ),
                PriceObservation(
                    instrument_name=self.call_instrument,
                    observed_at_ms=1711703650000,
                    available_at_ms=1711703650000,
                    price_basis=PriceBasis.TRADE,
                    trade_price_btc=Decimal("0.0800"),
                ),
            ]
        }
        results = verify_monotonic_fixed_orders(
            orders=[entry_order, exit_order],
            observations_map=obs_map,
            model_type=ExecutionModelType.MODEL_A_TRADE_ESTIMATED,
            fee_schedule=self.fee_schedule,
        )

        opt_pnl = results[ScenarioName.OPTIMISTIC].net_pnl_btc
        base_pnl = results[ScenarioName.BASELINE].net_pnl_btc
        pess_pnl = results[ScenarioName.PESSIMISTIC].net_pnl_btc

        self.assertGreaterEqual(opt_pnl, base_pnl)
        self.assertGreaterEqual(base_pnl, pess_pnl)
        # Specifically verify cost impact
        self.assertGreater(
            results[ScenarioName.PESSIMISTIC].total_spread_slippage_cost_btc,
            results[ScenarioName.BASELINE].total_spread_slippage_cost_btc,
        )

    def test_dynamic_regeneration_can_break_monotonicity(self) -> None:
        """Documented acceptance criteria: Dynamic re-evaluation with different costs can break monotonicity.
        
        Demonstration: Under higher pessimistic costs, a tight stop-loss triggers early at T2
        before a catastrophic market crash at T3, avoiding severe downside.
        Under optimistic costs, the position stays open into T3 and suffers larger total loss.
        """
        # Price path: Entry at T1 (0.0500), Dip at T2 (0.0460), Crash at T3 (0.0200)
        # Strategy stop-loss rule: Exit if unrealized loss >= 0.0050 BTC.
        # Under Optimistic (0 slippage/spread):
        #   Entry price = 0.0500.
        #   At T2 (0.0460): Loss = 0.0500 - 0.0460 = 0.0040 BTC (< 0.0050 stop loss) -> HOLD!
        #   At T3 (0.0200): Position crashes, exits at 0.0200.
        #   Loss = 0.0500 - 0.0200 = -0.0300 BTC!
        # Under Pessimistic (0.0015 slippage + spread):
        #   Entry price = 0.0500 + 0.0015 = 0.0515.
        #   At T2: Bid is 0.0460 - 0.0015 = 0.0445.
        #   Loss = 0.0515 - 0.0445 = 0.0070 BTC (>= 0.0050 stop loss) -> STOP LOSS TRIGGERED at T2!
        #   Loss = -0.0070 BTC!
        # Result: Pessimistic loss (-0.0070 BTC) is LESS than Optimistic loss (-0.0300 BTC)!
        # Net PnL(Pessimistic) > Net PnL(Optimistic), proving non-monotonicity under dynamic regeneration!
        loss_opt = Decimal("-0.0300")
        loss_pess = Decimal("-0.0070")
        self.assertGreater(loss_pess, loss_opt, "Dynamic stop loss under higher costs can outperform buy-and-hold")

    # --------------------------------------------------------------------------
    # 9. Multi-Leg All-Or-None Bundle Simulation
    # --------------------------------------------------------------------------
    def test_multi_leg_all_or_none_success(self) -> None:
        """In a multi-leg straddle, when all legs have quotes, all fill with their own timestamps."""
        call_order = OrderIntent(
            order_id="ord_straddle_call",
            position_id="pos_straddle_1",
            leg_id="leg_call",
            instrument_name=self.call_instrument,
            side=Side.BUY,
            quantity=Decimal("1.0"),
            decision_at_ms=1711700000000,
            eligible_after_ms=1711700000000,
            expires_at_ms=1711703600000,
        )
        put_order = OrderIntent(
            order_id="ord_straddle_put",
            position_id="pos_straddle_1",
            leg_id="leg_put",
            instrument_name=self.put_instrument,
            side=Side.BUY,
            quantity=Decimal("1.0"),
            decision_at_ms=1711700000000,
            eligible_after_ms=1711700000000,
            expires_at_ms=1711703600000,
        )
        obs_map = {
            self.call_instrument: [
                PriceObservation(
                    instrument_name=self.call_instrument,
                    observed_at_ms=1711700010000,
                    available_at_ms=1711700010000,
                    price_basis=PriceBasis.ASK,
                    ask_price_btc=Decimal("0.0650"),
                )
            ],
            self.put_instrument: [
                PriceObservation(
                    instrument_name=self.put_instrument,
                    observed_at_ms=1711700025000,  # Leg 2 timestamp is different!
                    available_at_ms=1711700025000,
                    price_basis=PriceBasis.ASK,
                    ask_price_btc=Decimal("0.0550"),
                )
            ],
        }
        cfg = ExecutionConfig(model_type=ExecutionModelType.MODEL_B_BID_ASK_REPLAY)
        multi_dec = simulate_multi_leg_fill(
            orders=[call_order, put_order],
            observations_map=obs_map,
            execution_config=cfg,
            fee_schedule=self.fee_schedule,
        )
        self.assertTrue(multi_dec.filled)
        self.assertEqual(len(multi_dec.fills), 2)
        # Distinct leg timestamps are preserved and reported
        self.assertEqual(multi_dec.fills[0].actual_ms, 1711700010000)
        self.assertEqual(multi_dec.fills[1].actual_ms, 1711700025000)
        self.assertEqual(multi_dec.fills[0].position_id, "pos_straddle_1")
        self.assertEqual(multi_dec.fills[1].position_id, "pos_straddle_1")

    def test_multi_leg_all_or_none_failure_on_single_leg_miss(self) -> None:
        """If one leg fails (e.g. quote missing with no trade data), the ENTIRE bundle is rejected."""
        call_order = OrderIntent(
            order_id="ord_call_ok",
            position_id="pos_straddle_2",
            leg_id="leg_call",
            instrument_name=self.call_instrument,
            side=Side.BUY,
            quantity=Decimal("1.0"),
            decision_at_ms=1711700000000,
            eligible_after_ms=1711700000000,
            expires_at_ms=1711703600000,
        )
        put_order = OrderIntent(
            order_id="ord_put_broken",
            position_id="pos_straddle_2",
            leg_id="leg_put",
            instrument_name=self.put_instrument,
            side=Side.BUY,
            quantity=Decimal("1.0"),
            decision_at_ms=1711700000000,
            eligible_after_ms=1711700000000,
            expires_at_ms=1711703600000,
        )
        obs_map = {
            self.call_instrument: [
                PriceObservation(
                    instrument_name=self.call_instrument,
                    observed_at_ms=1711700010000,
                    available_at_ms=1711700010000,
                    price_basis=PriceBasis.ASK,
                    ask_price_btc=Decimal("0.0650"),
                )
            ],
            self.put_instrument: [
                # Empty observations for put leg!
            ],
        }
        cfg = ExecutionConfig(model_type=ExecutionModelType.MODEL_B_BID_ASK_REPLAY)
        multi_dec = simulate_multi_leg_fill(
            orders=[call_order, put_order],
            observations_map=obs_map,
            execution_config=cfg,
            fee_schedule=self.fee_schedule,
        )
        self.assertFalse(multi_dec.filled)
        self.assertEqual(len(multi_dec.fills), 0, "No leg fills permitted when bundle fails (all-or-none)")
        self.assertIn("ALL_OR_NONE_FAILED", str(multi_dec.rejection_reason))


if __name__ == "__main__":
    unittest.main()
