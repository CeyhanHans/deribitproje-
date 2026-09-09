"""
Acceptance and unit tests for Task 13: Chronological Multi-Trade Event Engine.
Covers:
- Hand-calculated 3 consecutive trades
- Concurrent 2 open positions
- Chronological step ordering (settlement -> fills -> valuation/exit -> new entries)
- Gap stop loss (fills at actual gap price, not threshold)
- Settlement after missing put observations
- Contract roll only on new positions
- Single fee deduction per fill
- Open position at end of data (excluded from completed trade metrics)
- No-trade backtest run
- Prefix invariance of event log and ledger
- Deterministic reproducibility
- Roundtrip trade counting (multi-leg position is 1 trade, not separate legs)
- Protocol conformance for BacktestRunnerProtocol
"""

from decimal import Decimal
import unittest

from core.contracts import (
    BacktestRunnerProtocol,
    Currency,
    DataQuality,
    EntryType,
    ExecutionModelId,
    Fill,
    Instrument,
    OptionType,
    OrderIntent,
    PositionStatus,
    PriceBasis,
    PriceObservation,
    ReasonCode,
    RunConfig,
    RunResult,
    SettlementObservation,
    Side,
)
from engine import (
    DataBundle,
    EventEngine,
    compute_run_metrics,
    run,
)


class EventEngineAcceptanceTests(unittest.TestCase):
    """Rigorous acceptance tests for Task 13 chronological event engine."""

    def setUp(self) -> None:
        self.call_name = "BTC-27DEC24-90000-C"
        self.put_name = "BTC-27DEC24-90000-P"
        self.expiry_ms = 1735286400000  # 27DEC24 08:00 UTC

        self.call_inst = Instrument(
            instrument_name=self.call_name,
            option_type=OptionType.CALL,
            strike_usd=Decimal("90000.0"),
            creation_ms=1704067200000,
            expiry_ms=self.expiry_ms,
        )
        self.put_inst = Instrument(
            instrument_name=self.put_name,
            option_type=OptionType.PUT,
            strike_usd=Decimal("90000.0"),
            creation_ms=1704067200000,
            expiry_ms=self.expiry_ms,
        )

    # --------------------------------------------------------------------------
    # 1. Protocol Conformance
    # --------------------------------------------------------------------------
    def test_protocol_conformance(self) -> None:
        """run function strictly satisfies BacktestRunnerProtocol."""
        self.assertTrue(isinstance(run, BacktestRunnerProtocol))

    # --------------------------------------------------------------------------
    # 2. Hand-Calculated 3 Consecutive Trades
    # --------------------------------------------------------------------------
    def test_three_consecutive_trades_hand_calculated(self) -> None:
        """Execute at least 3 consecutive trades with hand-calculated PnL verification.
        
        Trade 1:
          - Enters at T = 1711700000000 (0.05 BTC premium, 0.0003 fee)
          - Exits at T = 1711707200000 on Take Profit (0.08 BTC proceeds, 0.0003 fee)
          - Net PnL = 0.08 - 0.05 - 0.0006 = +0.0294 BTC
        Trade 2:
          - Enters at T = 1711710800000 (0.06 BTC premium, 0.0003 fee)
          - Exits at T = 1711718000000 on Stop Loss (0.03 BTC proceeds, 0.0003 fee)
          - Net PnL = 0.03 - 0.06 - 0.0006 = -0.0306 BTC
        Trade 3:
          - Enters at T = 1711721600000 (0.04 BTC premium, 0.0003 fee)
          - Exits at T = 1711728800000 on Take Profit (0.06 BTC proceeds, 0.0003 fee)
          - Net PnL = 0.06 - 0.04 - 0.0006 = +0.0194 BTC
          
        Total Net PnL = +0.0294 - 0.0306 + 0.0194 = +0.0182 BTC.
        Winning: 2, Losing: 1, Total: 3, Win Rate = 2/3 = 66.67%.
        """
        # Step hours: 1h steps (3600000 ms)
        t0 = 1711700000000
        t_end = 1711736000000

        config = RunConfig(
            strategy_name="consecutive_straddle",
            start_ms=t0,
            end_ms=t_end,
            initial_capital_btc=Decimal("10.0"),
            strategy_params=(
                ("stop_loss_btc", "0.02"),
                ("take_profit_btc", "0.015"),
                ("max_open_positions", "1"),
                ("auto_entry_hourly", "false"),  # Controlled signals
            ),
        )

        observations = [
            # T0: Signal 1 triggered, order queued
            # T1: Trade 1 fills entry at 0.0500
            PriceObservation(
                instrument_name=self.call_name,
                observed_at_ms=t0 + 3600000,
                available_at_ms=t0 + 3600000,
                price_basis=PriceBasis.TRADE,
                trade_price_btc=Decimal("0.0500"),
            ),
            # T2: Trade 1 price jumps to 0.0800 -> Take Profit triggered (+0.03 > +0.015)
            PriceObservation(
                instrument_name=self.call_name,
                observed_at_ms=t0 + 7200000,
                available_at_ms=t0 + 7200000,
                price_basis=PriceBasis.TRADE,
                trade_price_btc=Decimal("0.0800"),
            ),
            # T3: Trade 1 fills exit at 0.0800! Position 1 closed.
            PriceObservation(
                instrument_name=self.call_name,
                observed_at_ms=t0 + 10800000,
                available_at_ms=t0 + 10800000,
                price_basis=PriceBasis.TRADE,
                trade_price_btc=Decimal("0.0800"),
            ),
            # T4: Trade 2 fills entry at 0.0600
            PriceObservation(
                instrument_name=self.call_name,
                observed_at_ms=t0 + 14400000,
                available_at_ms=t0 + 14400000,
                price_basis=PriceBasis.TRADE,
                trade_price_btc=Decimal("0.0600"),
            ),
            # T5: Trade 2 price drops to 0.0300 -> Stop Loss triggered (-0.03 <= -0.02)
            PriceObservation(
                instrument_name=self.call_name,
                observed_at_ms=t0 + 18000000,
                available_at_ms=t0 + 18000000,
                price_basis=PriceBasis.TRADE,
                trade_price_btc=Decimal("0.0300"),
            ),
            # T6: Trade 2 fills exit at 0.0300! Position 2 closed.
            PriceObservation(
                instrument_name=self.call_name,
                observed_at_ms=t0 + 21600000,
                available_at_ms=t0 + 21600000,
                price_basis=PriceBasis.TRADE,
                trade_price_btc=Decimal("0.0300"),
            ),
            # T7: Trade 3 fills entry at 0.0400
            PriceObservation(
                instrument_name=self.call_name,
                observed_at_ms=t0 + 25200000,
                available_at_ms=t0 + 25200000,
                price_basis=PriceBasis.TRADE,
                trade_price_btc=Decimal("0.0400"),
            ),
            # T8: Trade 3 price jumps to 0.0600 -> Take Profit triggered (+0.02 > +0.015)
            PriceObservation(
                instrument_name=self.call_name,
                observed_at_ms=t0 + 28800000,
                available_at_ms=t0 + 28800000,
                price_basis=PriceBasis.TRADE,
                trade_price_btc=Decimal("0.0600"),
            ),
            # T9: Trade 3 fills exit at 0.0600! Position 3 closed.
            PriceObservation(
                instrument_name=self.call_name,
                observed_at_ms=t0 + 32400000,
                available_at_ms=t0 + 32400000,
                price_basis=PriceBasis.TRADE,
                trade_price_btc=Decimal("0.0600"),
            ),
        ]

        # Trigger entries at T0, T3, T6
        def custom_signals(cfg, visible_obs, p_state):
            current_t = p_state.timestamp_ms
            if p_state.open_positions:
                return ()
            if current_t in (t0, t0 + 10800000, t0 + 21600000):
                return [("trade_entry", (self.call_inst,))]
            return ()

        bundle = DataBundle(
            instruments=(self.call_inst,),
            observations=tuple(observations),
            settlements=(),
        )

        engine = EventEngine(config=config, data_bundle=bundle, signal_fn=custom_signals)
        result = engine.run()

        # Hand calculations: exactly 3 completed trades
        self.assertEqual(len(result.trades), 3)
        self.assertEqual(result.metrics.total_trades, 3)
        self.assertEqual(result.metrics.winning_trades, 2)
        self.assertEqual(result.metrics.losing_trades, 1)

        t1, t2, t3 = result.trades
        self.assertEqual(t1.realized_pnl_btc, Decimal("0.0294"))
        self.assertEqual(t2.realized_pnl_btc, Decimal("-0.0306"))
        self.assertEqual(t3.realized_pnl_btc, Decimal("0.0194"))
        self.assertEqual(result.metrics.net_pnl_btc, Decimal("0.0182"))

    # --------------------------------------------------------------------------
    # 3. Concurrent 2 Open Positions
    # --------------------------------------------------------------------------
    def test_concurrent_two_open_positions(self) -> None:
        """Verify engine correctly holds 2 concurrent open positions when max_open_positions=2."""
        t0 = 1711700000000
        config = RunConfig(
            strategy_name="concurrent_two",
            start_ms=t0,
            end_ms=t0 + 14400000,
            initial_capital_btc=Decimal("10.0"),
            strategy_params=(
                ("max_open_positions", "2"),
                ("auto_entry_hourly", "false"),
                ("stop_loss_btc", "1.0"),  # Do not exit
                ("take_profit_btc", "1.0"),
            ),
        )
        obs = [
            PriceObservation(
                instrument_name=self.call_name,
                observed_at_ms=t0 + 3600000,
                available_at_ms=t0 + 3600000,
                price_basis=PriceBasis.TRADE,
                trade_price_btc=Decimal("0.0500"),
            ),
            PriceObservation(
                instrument_name=self.put_name,
                observed_at_ms=t0 + 7200000,
                available_at_ms=t0 + 7200000,
                price_basis=PriceBasis.TRADE,
                trade_price_btc=Decimal("0.0400"),
            ),
            PriceObservation(
                instrument_name=self.call_name,
                observed_at_ms=t0 + 7200000,
                available_at_ms=t0 + 7200000,
                price_basis=PriceBasis.TRADE,
                trade_price_btc=Decimal("0.0500"),
            ),
        ]

        def signal_two(cfg, visible, state):
            t = state.timestamp_ms
            if t == t0:
                return [("pos1", (self.call_inst,))]
            elif t == t0 + 3600000 and len(state.open_positions) < 2:
                return [("pos2", (self.put_inst,))]
            return ()

        bundle = DataBundle(
            instruments=(self.call_inst, self.put_inst),
            observations=tuple(obs),
        )
        engine = EventEngine(config=config, data_bundle=bundle, signal_fn=signal_two)
        result = engine.run()

        # At end of data (T=t0+14.4M), both positions are concurrently open in open_positions_at_end
        self.assertEqual(len(result.open_positions_at_end), 2)
        pos_ids = {p.position_id for p in result.open_positions_at_end}
        self.assertEqual(len(pos_ids), 2)

    # --------------------------------------------------------------------------
    # 4. Gap Stop Loss
    # --------------------------------------------------------------------------
    def test_gap_stop_loss_fills_at_market_gap_price(self) -> None:
        """Stop-loss triggered during a severe price gap fills at the actual gap price, not the threshold."""
        t0 = 1711700000000
        config = RunConfig(
            strategy_name="gap_stop_test",
            start_ms=t0,
            end_ms=t0 + 10800000,
            initial_capital_btc=Decimal("10.0"),
            strategy_params=(
                ("stop_loss_btc", "0.0100"),  # Stop loss threshold is -0.0100 BTC
                ("take_profit_btc", "0.5000"),
                ("auto_entry_hourly", "false"),
            ),
        )
        # Entry buy at 0.0800 BTC
        # At T=3.6M: Price severely crashes to 0.0200 BTC (loss = -0.0600 BTC >> -0.0100)
        # Stop loss triggers, queues exit order.
        # At T=7.2M: Exit order fills at 0.0200 BTC!
        obs = [
            PriceObservation(
                instrument_name=self.call_name,
                observed_at_ms=t0 + 1000,
                available_at_ms=t0 + 1000,
                price_basis=PriceBasis.TRADE,
                trade_price_btc=Decimal("0.0800"),
            ),
            PriceObservation(
                instrument_name=self.call_name,
                observed_at_ms=t0 + 3600000,
                available_at_ms=t0 + 3600000,
                price_basis=PriceBasis.TRADE,
                trade_price_btc=Decimal("0.0200"),  # Severe gap down
            ),
            PriceObservation(
                instrument_name=self.call_name,
                observed_at_ms=t0 + 7200000,
                available_at_ms=t0 + 7200000,
                price_basis=PriceBasis.TRADE,
                trade_price_btc=Decimal("0.0200"),
            ),
        ]

        def signal_entry(cfg, visible, state):
            if state.timestamp_ms == t0 and not state.open_positions:
                return [("entry", (self.call_inst,))]
            return ()

        bundle = DataBundle(
            instruments=(self.call_inst,),
            observations=tuple(obs),
        )
        engine = EventEngine(config=config, data_bundle=bundle, signal_fn=signal_entry)
        result = engine.run()

        self.assertEqual(len(result.trades), 1)
        closed_trade = result.trades[0]
        self.assertEqual(closed_trade.exit_reason, ReasonCode.STOP_LOSS)

        # Proves gap pricing: fill price is 0.0200, NOT 0.0700!
        exit_fill = closed_trade.close_fills[0]
        self.assertEqual(exit_fill.price_btc, Decimal("0.0200"))
        # Loss is -0.0606 BTC, proving no fake threshold fill
        self.assertEqual(closed_trade.realized_pnl_btc, Decimal("-0.0606"))

    # --------------------------------------------------------------------------
    # 5. Settlement After Missing Intermediate Put Bars
    # --------------------------------------------------------------------------
    def test_missing_put_bars_settlement_at_expiry(self) -> None:
        """If a put option had zero trade bars during intermediate hours, it settles cleanly at expiry."""
        t0 = 1711700000000
        expiry_t = t0 + 7200000

        call = Instrument(
            instrument_name="BTC-TEST-EXP-C",
            option_type=OptionType.CALL,
            strike_usd=Decimal("60000.0"),
            creation_ms=t0 - 3600000,
            expiry_ms=expiry_t,
        )
        put = Instrument(
            instrument_name="BTC-TEST-EXP-P",
            option_type=OptionType.PUT,
            strike_usd=Decimal("60000.0"),
            creation_ms=t0 - 3600000,
            expiry_ms=expiry_t,
        )

        config = RunConfig(
            strategy_name="missing_put_settle",
            start_ms=t0,
            end_ms=expiry_t + 3600000,
            initial_capital_btc=Decimal("10.0"),
            strategy_params=(
                ("stop_loss_btc", "1.0"),
                ("take_profit_btc", "1.0"),
                ("auto_entry_hourly", "false"),
            ),
        )

        # Observations: Both trade at T1 (entry).
        # Intermediate hour: Call trades, but Put has NO BARS AT ALL (missing/no-trade).
        obs = [
            PriceObservation(
                instrument_name="BTC-TEST-EXP-C",
                observed_at_ms=t0 + 1000,
                available_at_ms=t0 + 1000,
                price_basis=PriceBasis.TRADE,
                trade_price_btc=Decimal("0.0500"),
            ),
            PriceObservation(
                instrument_name="BTC-TEST-EXP-P",
                observed_at_ms=t0 + 1000,
                available_at_ms=t0 + 1000,
                price_basis=PriceBasis.TRADE,
                trade_price_btc=Decimal("0.0400"),
            ),
            # Intermediate hour: Only call trades! Put is missing!
            PriceObservation(
                instrument_name="BTC-TEST-EXP-C",
                observed_at_ms=t0 + 3600000,
                available_at_ms=t0 + 3600000,
                price_basis=PriceBasis.TRADE,
                trade_price_btc=Decimal("0.0600"),
            ),
        ]

        # Official delivery settlements at expiry_t: Delivery price S = $50,000
        # Call is OTM: (50000 - 60000) -> 0.0 BTC payoff
        # Put is ITM:  (60000 - 50000) / 50000 = 10000 / 50000 = 0.2000 BTC payoff!
        settlements = [
            SettlementObservation(
                instrument_name="BTC-TEST-EXP-C",
                expiry_ms=expiry_t,
                settlement_price_usd=Decimal("50000.0"),
                settlement_price_btc=Decimal("0.0"),
                is_official_delivery=True,
            ),
            SettlementObservation(
                instrument_name="BTC-TEST-EXP-P",
                expiry_ms=expiry_t,
                settlement_price_usd=Decimal("50000.0"),
                settlement_price_btc=Decimal("0.2000"),
                is_official_delivery=True,
            ),
        ]

        def signal_straddle(cfg, visible, state):
            if state.timestamp_ms == t0 and not state.open_positions:
                return [("straddle", (call, put))]
            return ()

        bundle = DataBundle(
            instruments=(call, put),
            observations=tuple(obs),
            settlements=tuple(settlements),
        )
        engine = EventEngine(config=config, data_bundle=bundle, signal_fn=signal_straddle)
        result = engine.run()

        # The position was settled at expiry despite missing intermediate put bars!
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].status, PositionStatus.SETTLED)
        # Net PnL = (0.2000 Put payoff) - (0.0500 Call entry + 0.0400 Put entry) - fees
        # Delivery fee: min(1.0 * 0.00015, 1.0 * 0.2000 * 0.125) = 0.00015
        # Total fees = 0.0003 * 2 + 0.00015 = 0.00075 BTC
        # Net = 0.2000 - 0.0900 - 0.00075 = +0.10925 BTC
        self.assertEqual(result.trades[0].realized_pnl_btc, Decimal("0.10925"))

    # --------------------------------------------------------------------------
    # 6. Contract Roll Only on New Position
    # --------------------------------------------------------------------------
    def test_contract_roll_only_on_new_positions(self) -> None:
        """Active open positions keep original strikes; contract roll occurs only when opening new position."""
        t0 = 1711700000000
        c1 = Instrument("BTC-EXP-70000-C", OptionType.CALL, Decimal("70000.0"), t0, t0 + 100000000)
        c2 = Instrument("BTC-EXP-80000-C", OptionType.CALL, Decimal("80000.0"), t0, t0 + 100000000)

        config = RunConfig(
            strategy_name="roll_test",
            start_ms=t0,
            end_ms=t0 + 14400000,
            initial_capital_btc=Decimal("10.0"),
            strategy_params=(
                ("stop_loss_btc", "0.01"),
                ("take_profit_btc", "0.01"),
                ("auto_entry_hourly", "false"),
            ),
        )
        obs = [
            # Open pos 1 at strike 70000
            PriceObservation("BTC-EXP-70000-C", t0 + 1000, t0 + 1000, PriceBasis.TRADE, trade_price_btc=Decimal("0.05")),
            # Price surges at T=3.6M, Take Profit triggers
            PriceObservation("BTC-EXP-70000-C", t0 + 3600000, t0 + 3600000, PriceBasis.TRADE, trade_price_btc=Decimal("0.07")),
            PriceObservation("BTC-EXP-70000-C", t0 + 7200000, t0 + 7200000, PriceBasis.TRADE, trade_price_btc=Decimal("0.07")),
            # Open pos 2 at new strike 80000 at T=7.2M
            PriceObservation("BTC-EXP-80000-C", t0 + 7201000, t0 + 7201000, PriceBasis.TRADE, trade_price_btc=Decimal("0.04")),
        ]

        def signal_roll(cfg, visible, state):
            t = state.timestamp_ms
            if t == t0 and not state.open_positions:
                return [("pos1", (c1,))]
            elif t == t0 + 7200000 and not state.open_positions:
                return [("pos2", (c2,))]
            return ()

        bundle = DataBundle(instruments=(c1, c2), observations=tuple(obs))
        engine = EventEngine(config=config, data_bundle=bundle, signal_fn=signal_roll)
        result = engine.run()

        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].legs[0].strike_usd, Decimal("70000.0"))
        # Second position opened with new rolled strike 80000.0
        self.assertEqual(len(result.open_positions_at_end), 1)
        self.assertEqual(result.open_positions_at_end[0].legs[0].strike_usd, Decimal("80000.0"))

    # --------------------------------------------------------------------------
    # 7. Fee Charged Strictly Once Per Fill
    # --------------------------------------------------------------------------
    def test_fee_single_charge(self) -> None:
        """Every fill in the event loop charges fee exactly once on the ledger."""
        t0 = 1711700000000
        config = RunConfig(
            strategy_name="fee_test",
            start_ms=t0,
            end_ms=t0 + 7200000,
            initial_capital_btc=Decimal("10.0"),
            strategy_params=(
                ("auto_entry_hourly", "false"),
            ),
        )
        obs = [
            PriceObservation(self.call_name, t0 + 1000, t0 + 1000, PriceBasis.TRADE, trade_price_btc=Decimal("0.05")),
        ]

        def sig(cfg, v, s):
            if s.timestamp_ms == t0:
                return [("sig1", (self.call_inst,))]
            return ()

        bundle = DataBundle(instruments=(self.call_inst,), observations=tuple(obs))
        engine = EventEngine(config=config, data_bundle=bundle, signal_fn=sig)
        result = engine.run()

        self.assertEqual(len(result.fills), 1)
        fee_entries = [e for e in result.ledger if e.entry_type == EntryType.FEE]
        # Strictly 1 fee entry on ledger
        self.assertEqual(len(fee_entries), 1)
        self.assertEqual(fee_entries[0].amount, Decimal("-0.0003"))

    # --------------------------------------------------------------------------
    # 8. Open Positions at End of Data Excluded from Completed Trade Metrics
    # --------------------------------------------------------------------------
    def test_open_at_end_excluded_from_completed_metrics(self) -> None:
        """Positions still open at end_ms remain in open_positions_at_end and are excluded from completed metrics."""
        t0 = 1711700000000
        config = RunConfig(
            strategy_name="open_end_test",
            start_ms=t0,
            end_ms=t0 + 7200000,
            initial_capital_btc=Decimal("10.0"),
            strategy_params=(
                ("auto_entry_hourly", "false"),
                ("stop_loss_btc", "1.0"),
                ("take_profit_btc", "1.0"),
            ),
        )
        obs = [
            PriceObservation(self.call_name, t0 + 1000, t0 + 1000, PriceBasis.TRADE, trade_price_btc=Decimal("0.05")),
        ]

        def sig(cfg, v, s):
            if s.timestamp_ms == t0:
                return [("open_sig", (self.call_inst,))]
            return ()

        bundle = DataBundle(instruments=(self.call_inst,), observations=tuple(obs))
        engine = EventEngine(config=config, data_bundle=bundle, signal_fn=sig, forced_close_at_end=False)
        result = engine.run()

        # Position was opened and not closed before end_ms
        self.assertEqual(len(result.open_positions_at_end), 1)
        # Completed trades must strictly be 0!
        self.assertEqual(len(result.trades), 0)
        self.assertEqual(result.metrics.total_trades, 0)
        self.assertEqual(result.metrics.win_rate, Decimal("0.0"))

    # --------------------------------------------------------------------------
    # 9. No-Trade Run
    # --------------------------------------------------------------------------
    def test_no_trade_run(self) -> None:
        """When no signals trigger, engine returns a clean RunResult with 0 trades and matching hashes."""
        t0 = 1711700000000
        config = RunConfig(
            strategy_name="no_trade",
            start_ms=t0,
            end_ms=t0 + 7200000,
            initial_capital_btc=Decimal("10.0"),
            strategy_params=(("auto_entry_hourly", "false"),),
        )
        bundle = DataBundle()
        engine = EventEngine(config=config, data_bundle=bundle, signal_fn=lambda *args: ())
        result = engine.run()

        self.assertEqual(len(result.orders), 0)
        self.assertEqual(len(result.fills), 0)
        self.assertEqual(len(result.trades), 0)
        self.assertEqual(len(result.open_positions_at_end), 0)
        self.assertEqual(result.metrics.total_trades, 0)

    # --------------------------------------------------------------------------
    # 10. Prefix Invariance of Event Log and Ledger
    # --------------------------------------------------------------------------
    def test_prefix_invariance(self) -> None:
        """Running over [T0, T1] produces an event log and ledger that is an exact prefix of [T0, T2]."""
        t0 = 1711700000000
        t1 = t0 + 7200000
        t2 = t0 + 14400000

        obs_all = [
            PriceObservation(self.call_name, t0 + 1000, t0 + 1000, PriceBasis.TRADE, trade_price_btc=Decimal("0.05")),
            PriceObservation(self.call_name, t0 + 3600000, t0 + 3600000, PriceBasis.TRADE, trade_price_btc=Decimal("0.08")),
            PriceObservation(self.call_name, t0 + 7200000, t0 + 7200000, PriceBasis.TRADE, trade_price_btc=Decimal("0.08")),
            PriceObservation(self.call_name, t0 + 10800000, t0 + 10800000, PriceBasis.TRADE, trade_price_btc=Decimal("0.09")),
        ]

        def sig(cfg, v, s):
            if s.timestamp_ms == t0 and not s.open_positions:
                return [("entry", (self.call_inst,))]
            return ()

        # Run 1: Shorter period [T0, T1]
        cfg1 = RunConfig("inv_test", t0, t1, Decimal("10.0"), strategy_params=(("auto_entry_hourly", "false"),))
        b1 = DataBundle(instruments=(self.call_inst,), observations=tuple(obs_all))
        res1 = EventEngine(cfg1, b1, signal_fn=sig).run()

        # Run 2: Longer period [T0, T2]
        cfg2 = RunConfig("inv_test", t0, t2, Decimal("10.0"), strategy_params=(("auto_entry_hourly", "false"),))
        res2 = EventEngine(cfg2, b1, signal_fn=sig).run()

        # All ledger entries produced in Run 1 must match the start of Run 2
        len1 = len(res1.ledger)
        self.assertGreater(len1, 0)
        self.assertEqual(res1.ledger, res2.ledger[:len1])

    # --------------------------------------------------------------------------
    # 11. Deterministic Reproducibility
    # --------------------------------------------------------------------------
    def test_deterministic_reproducibility(self) -> None:
        """Running the engine twice on identical inputs produces identical outputs and hashes."""
        t0 = 1711700000000
        cfg = RunConfig("rep_test", t0, t0 + 7200000, Decimal("10.0"), strategy_params=(("auto_entry_hourly", "false"),))
        obs = [
            PriceObservation(self.call_name, t0 + 1000, t0 + 1000, PriceBasis.TRADE, trade_price_btc=Decimal("0.05")),
        ]
        bundle = DataBundle(instruments=(self.call_inst,), observations=tuple(obs))

        def sig(c, v, s):
            if s.timestamp_ms == t0:
                return [("sig", (self.call_inst,))]
            return ()

        res1 = EventEngine(cfg, bundle, signal_fn=sig).run()
        res2 = EventEngine(cfg, bundle, signal_fn=sig).run()

        self.assertEqual(res1.config_hash, res2.config_hash)
        self.assertEqual(res1.data_hash, res2.data_hash)
        self.assertEqual(res1.ledger, res2.ledger)
        self.assertEqual(res1.fills, res2.fills)
        self.assertEqual(res1.metrics, res2.metrics)

    # --------------------------------------------------------------------------
    # 12. Roundtrip Trade Counting
    # --------------------------------------------------------------------------
    def test_multi_leg_straddle_is_one_trade(self) -> None:
        """A multi-leg straddle (Call + Put) is counted as exactly 1 trade, not 2."""
        t0 = 1711700000000
        config = RunConfig(
            strategy_name="straddle_single_count",
            start_ms=t0,
            end_ms=t0 + 10800000,
            initial_capital_btc=Decimal("10.0"),
            strategy_params=(
                ("stop_loss_btc", "1.0"),
                ("take_profit_btc", "0.01"),  # Take profit
                ("auto_entry_hourly", "false"),
            ),
        )
        obs = [
            # Entry fills for Call and Put
            PriceObservation(self.call_name, t0 + 1000, t0 + 1000, PriceBasis.TRADE, trade_price_btc=Decimal("0.05")),
            PriceObservation(self.put_name, t0 + 1000, t0 + 1000, PriceBasis.TRADE, trade_price_btc=Decimal("0.05")),
            # Take profit trigger
            PriceObservation(self.call_name, t0 + 3600000, t0 + 3600000, PriceBasis.TRADE, trade_price_btc=Decimal("0.08")),
            PriceObservation(self.put_name, t0 + 3600000, t0 + 3600000, PriceBasis.TRADE, trade_price_btc=Decimal("0.05")),
            # Exit fills
            PriceObservation(self.call_name, t0 + 7200000, t0 + 7200000, PriceBasis.TRADE, trade_price_btc=Decimal("0.08")),
            PriceObservation(self.put_name, t0 + 7200000, t0 + 7200000, PriceBasis.TRADE, trade_price_btc=Decimal("0.05")),
        ]

        def sig_straddle(c, v, s):
            if s.timestamp_ms == t0 and not s.open_positions:
                return [("straddle", (self.call_inst, self.put_inst))]
            return ()

        bundle = DataBundle(instruments=(self.call_inst, self.put_inst), observations=tuple(obs))
        engine = EventEngine(config=config, data_bundle=bundle, signal_fn=sig_straddle)
        result = engine.run()

        # Both legs were closed, but completed trades is STRICTLY 1!
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.metrics.total_trades, 1)
        self.assertEqual(len(result.trades[0].legs), 2)


if __name__ == "__main__":
    unittest.main()
