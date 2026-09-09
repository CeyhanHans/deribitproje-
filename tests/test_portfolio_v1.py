"""
Acceptance and unit tests for Task 12:
- BTC cash ledger & double-entry net conservation
- Identical BTC prices with different BTC/USD exchange rates
- Explicit separation of Trade USD PnL vs Portfolio USD equity change vs Buy-and-Hold benchmark
- Short margin stress reserves and release upon close/settlement
- Insufficient capital rejection
- Duplicate fill idempotency
- Single execution settlement idempotency
- Rejection of short positions without explicit margin model
- Short stress risk breach reporting without fake liquidations
- Missing exit preserving position and risk
- Protocol conformance for FillApplicatorProtocol and SettlerProtocol
"""

from decimal import Decimal
import unittest

from core.contracts import (
    Currency,
    EntryType,
    ExecutionModelId,
    Fill,
    FillApplicatorProtocol,
    Instrument,
    LedgerEntry,
    OptionType,
    PortfolioState,
    Position,
    PositionStatus,
    ReasonCode,
    SettlementObservation,
    SettlerProtocol,
    Side,
)
from portfolio import (
    AccountingMetricsSummary,
    DuplicateFillError,
    InsufficientCapitalError,
    MarginModelConfig,
    PortfolioError,
    RiskBreach,
    SettlementError,
    ShortPositionRejectedError,
    apply_fill,
    check_risk_breaches,
    compute_accounting_summary,
    evaluate_equity,
    make_instrument,
    settle,
)


class PortfolioV1AcceptanceTests(unittest.TestCase):
    """Rigorous acceptance tests for Task 12 Portfolio Ledger and Margin Engine."""

    def setUp(self) -> None:
        self.initial_cash = Decimal("10.0")  # 10.0 BTC
        self.call_instrument_name = "BTC-27DEC24-90000-C"
        self.put_instrument_name = "BTC-27DEC24-90000-P"
        self.base_state = PortfolioState(
            timestamp_ms=1711700000000,
            cash_balance_btc=self.initial_cash,
            reserved_balance_btc=Decimal("0.0"),
            open_positions=(),
            closed_positions=(),
            ledger=(),
        )
        self.margin_config = MarginModelConfig(
            model_name="conservative_stress_reserve",
            stress_reserve_ratio=Decimal("0.5"),
            exchange_margin_equivalent=False,
            allow_naked_short=True,
        )

    # --------------------------------------------------------------------------
    # 1. Protocol Conformance
    # --------------------------------------------------------------------------
    def test_protocol_conformance(self) -> None:
        """apply_fill and settle satisfy FillApplicatorProtocol and SettlerProtocol."""
        self.assertTrue(isinstance(apply_fill, FillApplicatorProtocol))
        self.assertTrue(isinstance(settle, SettlerProtocol))

    # --------------------------------------------------------------------------
    # 2. Double-Entry / Net Conservation
    # --------------------------------------------------------------------------
    def test_double_entry_net_conservation(self) -> None:
        """Every transaction updates cash balance strictly by balance_after == balance_before + amount."""
        # Long entry
        fill_entry = Fill(
            fill_id="fill_de_01",
            order_id="ord_de_01",
            position_id="pos_de_1",
            leg_id="leg_call",
            instrument_name=self.call_instrument_name,
            side=Side.BUY,
            actual_ms=1711700010000,
            model_timestamp_ms=1711700000000,
            quantity=Decimal("1.0"),
            price_btc=Decimal("0.0800"),
            fee_btc=Decimal("0.0003"),
            reference_price_btc=Decimal("0.0800"),
            spread_slippage_cost_btc=Decimal("0.0"),
            execution_model_id=ExecutionModelId.MODEL_A_BID_ASK,
        )
        state_1 = apply_fill(self.base_state, fill_entry)

        # Long exit
        fill_exit = Fill(
            fill_id="fill_de_02",
            order_id="ord_de_02",
            position_id="pos_de_1",
            leg_id="leg_call",
            instrument_name=self.call_instrument_name,
            side=Side.SELL,
            actual_ms=1711703600000,
            model_timestamp_ms=1711703600000,
            quantity=Decimal("1.0"),
            price_btc=Decimal("0.1000"),
            fee_btc=Decimal("0.0003"),
            reference_price_btc=Decimal("0.1000"),
            spread_slippage_cost_btc=Decimal("0.0"),
            execution_model_id=ExecutionModelId.MODEL_A_BID_ASK,
        )
        state_2 = apply_fill(state_1, fill_exit)

        # Verify net conservation on ledger
        running_cash = self.initial_cash
        for entry in state_2.ledger:
            if entry.entry_type != EntryType.RESERVE_ADJUSTMENT:
                expected_balance = running_cash + entry.amount
                self.assertEqual(
                    entry.balance_after,
                    expected_balance,
                    f"Double-entry violation in entry {entry.event_id}",
                )
                running_cash = expected_balance

        self.assertEqual(running_cash, state_2.cash_balance_btc)
        # Net cash change = -0.0800 - 0.0003 + 0.1000 - 0.0003 = +0.0194 BTC
        self.assertEqual(state_2.cash_balance_btc, Decimal("10.0194"))

    # --------------------------------------------------------------------------
    # 3. Currency Separation & Accounting Distinction
    # --------------------------------------------------------------------------
    def test_same_btc_price_different_usd_rates_accounting_separation(self) -> None:
        """Identical BTC trade results produce different USD PnL at different closing exchange rates.
        
        Explicitly verifies the 4 separate metrics:
        1. Realized trade PnL BTC
        2. Realized trade PnL USD (at exit rate)
        3. Portfolio USD change from start
        4. Benchmark buy-and-hold BTC/USD
        """
        # Scenario A: Exits at BTC/USD = $60,000
        # Buy at 0.05 BTC (fee 0.0003), Sell at 0.08 BTC (fee 0.0003). Net = +0.0294 BTC.
        fill_buy = Fill(
            fill_id="f_buy_a",
            order_id="o_buy_a",
            position_id="pos_cur_a",
            leg_id="leg_call",
            instrument_name=self.call_instrument_name,
            side=Side.BUY,
            actual_ms=1711700010000,
            model_timestamp_ms=1711700000000,
            quantity=Decimal("1.0"),
            price_btc=Decimal("0.0500"),
            fee_btc=Decimal("0.0003"),
            reference_price_btc=Decimal("0.0500"),
            spread_slippage_cost_btc=Decimal("0.0"),
            execution_model_id=ExecutionModelId.MODEL_A_BID_ASK,
        )
        fill_sell_a = Fill(
            fill_id="f_sell_a",
            order_id="o_sell_a",
            position_id="pos_cur_a",
            leg_id="leg_call",
            instrument_name=self.call_instrument_name,
            side=Side.SELL,
            actual_ms=1711703600000,
            model_timestamp_ms=1711703600000,
            quantity=Decimal("1.0"),
            price_btc=Decimal("0.0800"),
            fee_btc=Decimal("0.0003"),
            reference_price_btc=Decimal("0.0800"),
            spread_slippage_cost_btc=Decimal("0.0"),
            execution_model_id=ExecutionModelId.MODEL_A_BID_ASK,
        )

        st_open_a = apply_fill(self.base_state, fill_buy)
        st_close_a = apply_fill(st_open_a, fill_sell_a, underlying_price_usd=Decimal("60000.0"))

        # Scenario B: Exactly identical fills, but exits at BTC/USD = $100,000
        fill_sell_b = Fill(
            fill_id="f_sell_b",
            order_id="o_sell_b",
            position_id="pos_cur_a",
            leg_id="leg_call",
            instrument_name=self.call_instrument_name,
            side=Side.SELL,
            actual_ms=1711703600000,
            model_timestamp_ms=1711703600000,
            quantity=Decimal("1.0"),
            price_btc=Decimal("0.0800"),
            fee_btc=Decimal("0.0003"),
            reference_price_btc=Decimal("0.0800"),
            spread_slippage_cost_btc=Decimal("0.0"),
            execution_model_id=ExecutionModelId.MODEL_A_BID_ASK,
        )
        st_close_b = apply_fill(st_open_a, fill_sell_b, underlying_price_usd=Decimal("100000.0"))

        pos_a = st_close_a.closed_positions[0]
        pos_b = st_close_b.closed_positions[0]

        # Both have IDENTICAL realized BTC PnL: +0.0294 BTC
        self.assertEqual(pos_a.realized_pnl_btc, Decimal("0.0294"))
        self.assertEqual(pos_b.realized_pnl_btc, Decimal("0.0294"))

        # But USD trade PnL differs by exchange rate:
        # A: 0.0294 * 60,000 = $1,764
        # B: 0.0294 * 100,000 = $2,940
        self.assertEqual(pos_a.realized_pnl_usd, Decimal("1764.0"))
        self.assertEqual(pos_b.realized_pnl_usd, Decimal("2940.0"))

        # Now test full accounting separation summary
        summary_a = compute_accounting_summary(
            state=st_close_a,
            initial_cash_btc=self.initial_cash,
            initial_underlying_usd=Decimal("60000.0"),
            current_underlying_usd=Decimal("60000.0"),
        )
        # Realized Trade USD PnL is $1,764
        self.assertEqual(summary_a.realized_trade_pnl_usd, Decimal("1764.0"))
        # Portfolio USD change: (10.0294 * 60000) - (10.0 * 60000) = $1,764
        self.assertEqual(summary_a.portfolio_usd_change_from_start, Decimal("1764.0"))
        # Benchmark buy and hold: 10.0 BTC = $600,000
        self.assertEqual(summary_a.benchmark_buy_and_hold_btc, Decimal("10.0"))
        self.assertEqual(summary_a.benchmark_buy_and_hold_usd, Decimal("600000.0"))

    # --------------------------------------------------------------------------
    # 4. Short Stress Reserves and Release Upon Close
    # --------------------------------------------------------------------------
    def test_short_stress_reserve_lock_and_release(self) -> None:
        """Short positions lock stress reserve upon entry, released strictly upon close."""
        short_entry_fill = Fill(
            fill_id="f_short_open",
            order_id="o_short_open",
            position_id="pos_short_1",
            leg_id="leg_call",
            instrument_name=self.call_instrument_name,
            side=Side.SELL,
            actual_ms=1711700010000,
            model_timestamp_ms=1711700000000,
            quantity=Decimal("1.0"),
            price_btc=Decimal("0.0800"),
            fee_btc=Decimal("0.0003"),
            reference_price_btc=Decimal("0.0800"),
            spread_slippage_cost_btc=Decimal("0.0"),
            execution_model_id=ExecutionModelId.MODEL_A_BID_ASK,
        )
        st_short_open = apply_fill(self.base_state, short_entry_fill, margin_config=self.margin_config)

        # Reserve ratio 0.5 * 1.0 = 0.5 BTC locked
        self.assertEqual(st_short_open.reserved_balance_btc, Decimal("0.5"))
        # Cash = 10.0 + 0.0800 - 0.0003 = 10.0797 BTC
        self.assertEqual(st_short_open.cash_balance_btc, Decimal("10.0797"))

        # Short buyback exit
        short_exit_fill = Fill(
            fill_id="f_short_close",
            order_id="o_short_close",
            position_id="pos_short_1",
            leg_id="leg_call",
            instrument_name=self.call_instrument_name,
            side=Side.BUY,
            actual_ms=1711703600000,
            model_timestamp_ms=1711703600000,
            quantity=Decimal("1.0"),
            price_btc=Decimal("0.0400"),  # Bought back cheaper
            fee_btc=Decimal("0.0003"),
            reference_price_btc=Decimal("0.0400"),
            spread_slippage_cost_btc=Decimal("0.0"),
            execution_model_id=ExecutionModelId.MODEL_A_BID_ASK,
        )
        st_short_closed = apply_fill(st_short_open, short_exit_fill, margin_config=self.margin_config)

        # Reserve strictly released back to 0.0 BTC
        self.assertEqual(st_short_closed.reserved_balance_btc, Decimal("0.0"))
        # Cash = 10.0797 - 0.0400 - 0.0003 = 10.0394 BTC
        self.assertEqual(st_short_closed.cash_balance_btc, Decimal("10.0394"))
        # Realized BTC PnL = (0.0800 - 0.0400) - 0.0006 = +0.0394 BTC
        self.assertEqual(st_short_closed.closed_positions[0].realized_pnl_btc, Decimal("0.0394"))

    # --------------------------------------------------------------------------
    # 5. Insufficient Capital Rejection
    # --------------------------------------------------------------------------
    def test_insufficient_capital_long_rejected(self) -> None:
        """Attempting to buy options when premium exceeds available cash is rejected."""
        broke_state = PortfolioState(
            timestamp_ms=1711700000000,
            cash_balance_btc=Decimal("0.05"),  # Only 0.05 BTC available
            reserved_balance_btc=Decimal("0.0"),
            open_positions=(),
            closed_positions=(),
            ledger=(),
        )
        expensive_fill = Fill(
            fill_id="f_exp",
            order_id="o_exp",
            position_id="pos_exp",
            leg_id="leg_call",
            instrument_name=self.call_instrument_name,
            side=Side.BUY,
            actual_ms=1711700010000,
            model_timestamp_ms=1711700000000,
            quantity=Decimal("1.0"),
            price_btc=Decimal("0.0800"),  # Costs 0.0803 > 0.05
            fee_btc=Decimal("0.0003"),
            reference_price_btc=Decimal("0.0800"),
            spread_slippage_cost_btc=Decimal("0.0"),
            execution_model_id=ExecutionModelId.MODEL_A_BID_ASK,
        )
        with self.assertRaises(InsufficientCapitalError):
            apply_fill(broke_state, expensive_fill)

    def test_insufficient_capital_short_reserve_rejected(self) -> None:
        """Attempting to write short option without sufficient cash for stress reserve is rejected."""
        broke_state = PortfolioState(
            timestamp_ms=1711700000000,
            cash_balance_btc=Decimal("0.10"),  # 0.10 BTC available
            reserved_balance_btc=Decimal("0.0"),
            open_positions=(),
            closed_positions=(),
            ledger=(),
        )
        # Requires 0.5 BTC reserve
        short_fill = Fill(
            fill_id="f_short_broke",
            order_id="o_sb",
            position_id="pos_sb",
            leg_id="leg_call",
            instrument_name=self.call_instrument_name,
            side=Side.SELL,
            actual_ms=1711700010000,
            model_timestamp_ms=1711700000000,
            quantity=Decimal("1.0"),
            price_btc=Decimal("0.0800"),
            fee_btc=Decimal("0.0003"),
            reference_price_btc=Decimal("0.0800"),
            spread_slippage_cost_btc=Decimal("0.0"),
            execution_model_id=ExecutionModelId.MODEL_A_BID_ASK,
        )
        with self.assertRaises(InsufficientCapitalError):
            apply_fill(broke_state, short_fill, margin_config=self.margin_config)

    # --------------------------------------------------------------------------
    # 6. Idempotency & Duplicate Fills
    # --------------------------------------------------------------------------
    def test_duplicate_fill_idempotency(self) -> None:
        """Applying the identical fill twice does not double-charge cash or duplicate ledger."""
        fill = Fill(
            fill_id="f_idempotent",
            order_id="o_idem",
            position_id="pos_idem",
            leg_id="leg_call",
            instrument_name=self.call_instrument_name,
            side=Side.BUY,
            actual_ms=1711700010000,
            model_timestamp_ms=1711700000000,
            quantity=Decimal("1.0"),
            price_btc=Decimal("0.0800"),
            fee_btc=Decimal("0.0003"),
            reference_price_btc=Decimal("0.0800"),
            spread_slippage_cost_btc=Decimal("0.0"),
            execution_model_id=ExecutionModelId.MODEL_A_BID_ASK,
        )
        state_once = apply_fill(self.base_state, fill)
        state_twice = apply_fill(state_once, fill)

        # Must be completely identical
        self.assertEqual(state_once.cash_balance_btc, state_twice.cash_balance_btc)
        self.assertEqual(len(state_once.ledger), len(state_twice.ledger))
        self.assertEqual(len(state_once.open_positions), len(state_twice.open_positions))

    # --------------------------------------------------------------------------
    # 7. Settlement Single Execution & Idempotency
    # --------------------------------------------------------------------------
    def test_settlement_single_execution_and_idempotency(self) -> None:
        """Settlement operates strictly once per instrument expiry; subsequent calls are no-ops."""
        fill_call = Fill(
            fill_id="f_set_call",
            order_id="o_set_c",
            position_id="pos_settle_test",
            leg_id="leg_call",
            instrument_name=self.call_instrument_name,
            side=Side.BUY,
            actual_ms=1711700010000,
            model_timestamp_ms=1711700000000,
            quantity=Decimal("1.0"),
            price_btc=Decimal("0.0500"),
            fee_btc=Decimal("0.0003"),
            reference_price_btc=Decimal("0.0500"),
            spread_slippage_cost_btc=Decimal("0.0"),
            execution_model_id=ExecutionModelId.MODEL_A_BID_ASK,
        )
        st_open = apply_fill(self.base_state, fill_call)

        # Settlement observation: BTC-27DEC24-90000-C expiry at S = $95,000 (ITM Call)
        # Payoff per contract = (95000 - 90000) / 95000 = 5000 / 95000 = 0.05263158 BTC
        settlement_obs = SettlementObservation(
            instrument_name=self.call_instrument_name,
            expiry_ms=1800000000000,
            settlement_price_usd=Decimal("95000.0"),
            settlement_price_btc=Decimal("0.05263158"),
            is_official_delivery=True,
        )
        st_settled_1 = settle(st_open, settlement_obs)
        self.assertEqual(len(st_settled_1.open_positions), 0)
        self.assertEqual(len(st_settled_1.closed_positions), 1)
        self.assertEqual(st_settled_1.closed_positions[0].status, PositionStatus.SETTLED)

        # Settle second time: MUST be idempotent
        st_settled_2 = settle(st_settled_1, settlement_obs)
        self.assertEqual(st_settled_1.cash_balance_btc, st_settled_2.cash_balance_btc)
        self.assertEqual(len(st_settled_1.ledger), len(st_settled_2.ledger))

    # --------------------------------------------------------------------------
    # 8. Rejection of Short Positions Without Explicit Margin Model
    # --------------------------------------------------------------------------
    def test_short_rejected_without_explicit_margin_model(self) -> None:
        """Attempting to enter a short position without explicit margin model raises ShortPositionRejectedError."""
        short_fill = Fill(
            fill_id="f_nomodel",
            order_id="o_nomodel",
            position_id="pos_nm",
            leg_id="leg_call",
            instrument_name=self.call_instrument_name,
            side=Side.SELL,
            actual_ms=1711700010000,
            model_timestamp_ms=1711700000000,
            quantity=Decimal("1.0"),
            price_btc=Decimal("0.0800"),
            fee_btc=Decimal("0.0003"),
            reference_price_btc=Decimal("0.0800"),
            spread_slippage_cost_btc=Decimal("0.0"),
            execution_model_id=ExecutionModelId.MODEL_A_BID_ASK,
        )
        # margin_config=None
        with self.assertRaises(ShortPositionRejectedError):
            apply_fill(self.base_state, short_fill, margin_config=None)

        # margin_config with allow_naked_short=False
        strict_margin = MarginModelConfig(allow_naked_short=False)
        with self.assertRaises(ShortPositionRejectedError):
            apply_fill(self.base_state, short_fill, margin_config=strict_margin)

    # --------------------------------------------------------------------------
    # 9. Short Stress Risk Breach Reporting Without Fake Liquidation
    # --------------------------------------------------------------------------
    def test_short_stress_risk_breach_reported_no_fake_liquidation(self) -> None:
        """When liability exceeds reserve under extreme prices, report risk breach without fake liquidations."""
        short_fill = Fill(
            fill_id="f_breach_test",
            order_id="o_bt",
            position_id="pos_bt",
            leg_id="leg_call",
            instrument_name=self.call_instrument_name,  # Strike $90,000 Call
            side=Side.SELL,
            actual_ms=1711700010000,
            model_timestamp_ms=1711700000000,
            quantity=Decimal("1.0"),
            price_btc=Decimal("0.0800"),
            fee_btc=Decimal("0.0003"),
            reference_price_btc=Decimal("0.0800"),
            spread_slippage_cost_btc=Decimal("0.0"),
            execution_model_id=ExecutionModelId.MODEL_A_BID_ASK,
        )
        st_open = apply_fill(self.base_state, short_fill, margin_config=self.margin_config)
        # Reserved = 0.5 BTC

        # Price surges to $250,000 (extreme stress)
        # Call liability = (250,000 - 90,000) / 250,000 = 160,000 / 250,000 = 0.6400 BTC
        # Liability (0.64 BTC) > Allocated Reserve (0.50 BTC) -> RISK BREACH!
        breaches = check_risk_breaches(
            state=st_open,
            underlying_price_usd=Decimal("250000.0"),
            margin_config=self.margin_config,
            timestamp_ms=1711705000000,
        )
        self.assertEqual(len(breaches), 1)
        breach = breaches[0]
        self.assertTrue(breach.is_breached)
        self.assertGreater(breach.liability_btc, breach.reserved_btc)
        self.assertIn("no fake liquidation", breach.message.lower())

        # CRITICAL INVARIANT: The position must STILL BE OPEN! (No fake liquidation was manufactured!)
        self.assertEqual(len(st_open.open_positions), 1)
        self.assertEqual(st_open.open_positions[0].status, PositionStatus.OPEN)

    # --------------------------------------------------------------------------
    # 10. Missing Exit Preserves Position and Risk
    # --------------------------------------------------------------------------
    def test_missing_exit_preserves_position(self) -> None:
        """Missing exit data keeps position open and preserves risk; never deleted."""
        fill = Fill(
            fill_id="f_no_exit",
            order_id="o_ne",
            position_id="pos_ne",
            leg_id="leg_call",
            instrument_name=self.call_instrument_name,
            side=Side.BUY,
            actual_ms=1711700010000,
            model_timestamp_ms=1711700000000,
            quantity=Decimal("1.0"),
            price_btc=Decimal("0.0800"),
            fee_btc=Decimal("0.0003"),
            reference_price_btc=Decimal("0.0800"),
            spread_slippage_cost_btc=Decimal("0.0"),
            execution_model_id=ExecutionModelId.MODEL_A_BID_ASK,
        )
        st_open = apply_fill(self.base_state, fill)
        self.assertEqual(len(st_open.open_positions), 1)

        # Settle an unrelated instrument
        unrelated_settle = SettlementObservation(
            instrument_name="BTC-29MAR24-60000-C",
            expiry_ms=1711699200000,
            settlement_price_usd=Decimal("68000.0"),
            settlement_price_btc=Decimal("0.1176"),
            is_official_delivery=True,
        )
        st_after = settle(st_open, unrelated_settle)

        # Original position remains intact in open_positions
        self.assertEqual(len(st_after.open_positions), 1)
        self.assertEqual(st_after.open_positions[0].position_id, "pos_ne")
        self.assertEqual(st_after.open_positions[0].status, PositionStatus.OPEN)


if __name__ == "__main__":
    unittest.main()
