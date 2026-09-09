import math
import unittest
from datetime import datetime, timezone

from strategies.backtest_engine import (
    BacktestEngineError,
    ExecutionModel,
    MarketObservation,
    run_backtest,
)
from strategies.strategy_schema import strategy_from_mapping


def dt(hour: int) -> datetime:
    return datetime(2026, 9, 1, hour, tzinfo=timezone.utc)


def sample_strategy(
    stop_loss: float = 20,
    take_profit: float = 30,
    time_exit_val: str = "2026-09-01T03:00:00Z",
    entry_rule_name: str = "enter_when_chain_has_required_legs",
):
    return strategy_from_mapping(
        {
            "name": "Toy straddle",
            "version": "1.0",
            "legs": [
                {
                    "name": "call_leg",
                    "option_type": "call",
                    "side": "long",
                    "quantity": 1,
                    "strike": {"type": "exact_usd", "value": 78500},
                    "expiry": {"type": "exact_date", "value": "2026-09-10"},
                },
                {
                    "name": "put_leg",
                    "option_type": "put",
                    "side": "long",
                    "quantity": 1,
                    "strike": {"type": "exact_usd", "value": 78500},
                    "expiry": {"type": "exact_date", "value": "2026-09-10"},
                },
            ],
            "entry_rules": [{"name": entry_rule_name}],
            "exit_rules": [
                {"type": "net_usd_stop_loss", "value": stop_loss},
                {"type": "net_usd_take_profit", "value": take_profit},
                {"type": "time_exit", "value": time_exit_val},
                {"type": "expiry_exit", "value": "at_expiry"},
            ],
            "fee_spread_model": "custom_bid_ask_slippage",
            "result_metrics": [
                {"name": "trade_count", "description": "Completed trades."},
                {"name": "net_pnl_usd", "description": "Net PnL after fees and slippage."},
                {"name": "max_drawdown_usd", "description": "Largest equity drop."},
                {"name": "win_rate", "description": "Positive trade share."},
            ],
        }
    )


def observations(
    option_type: str,
    prices: list[float],
    btc_usd_rates: list[float] | None = None,
    instrument_name: str | None = None,
    timestamps: list[datetime] | None = None,
    is_settlement_flags: list[bool] | None = None,
):
    inst = instrument_name or f"BTC-10SEP26-78500-{option_type[0].upper()}"
    obs_list = []
    for index, price in enumerate(prices):
        t = timestamps[index] if timestamps else dt(index)
        rate = btc_usd_rates[index] if btc_usd_rates else 78500.0
        is_settle = is_settlement_flags[index] if is_settlement_flags else False
        obs_list.append(
            MarketObservation(
                timestamp_utc=t,
                instrument_name=inst,
                option_type=option_type,
                underlying_price_usd=rate,
                trade_price_btc=price,
                btc_usd=rate,
                is_settlement=is_settle,
                data_quality_label="settlement" if is_settle else "official-historical",
            )
        )
    return obs_list


class BacktestEngineTests(unittest.TestCase):
    # =========================================================================
    # Existing Baseline Tests (Must continue passing without regression)
    # =========================================================================

    def test_runs_two_leg_take_profit_backtest(self):
        result = run_backtest(
            sample_strategy(),
            {
                "call_leg": observations("call", [0.0010, 0.0013, 0.0015]),
                "put_leg": observations("put", [0.0010, 0.0013, 0.0015]),
            },
            ExecutionModel(slippage_bps=0, fee_rate=0),
        )

        self.assertEqual(result.trade_count, 1)
        self.assertEqual(result.trades[0].exit_reason, "net_usd_take_profit")
        self.assertGreater(result.net_pnl_usd, 30)
        self.assertEqual(result.win_rate, 1.0)
        self.assertEqual(result.fill_quality_label, "estimated")
        self.assertEqual(result.accounting_model, "legacy_usd_cashflow")

    def test_runs_stop_loss_backtest(self):
        result = run_backtest(
            sample_strategy(),
            {
                "call_leg": observations("call", [0.0010, 0.0008, 0.0007]),
                "put_leg": observations("put", [0.0010, 0.0008, 0.0007]),
            },
            ExecutionModel(slippage_bps=0, fee_rate=0),
        )

        self.assertEqual(result.trades[0].exit_reason, "net_usd_stop_loss")
        self.assertLess(result.net_pnl_usd, -20)
        self.assertEqual(result.win_rate, 0.0)
        self.assertGreater(result.max_drawdown_usd, 0)

    def test_applies_slippage_and_fee_to_estimated_fill(self):
        clean = run_backtest(
            sample_strategy(),
            {
                "call_leg": observations("call", [0.0010, 0.0012, 0.0012, 0.0012]),
                "put_leg": observations("put", [0.0010, 0.0012, 0.0012, 0.0012]),
            },
            ExecutionModel(slippage_bps=0, fee_rate=0),
        )
        conservative = run_backtest(
            sample_strategy(),
            {
                "call_leg": observations("call", [0.0010, 0.0012, 0.0012, 0.0012]),
                "put_leg": observations("put", [0.0010, 0.0012, 0.0012, 0.0012]),
            },
            ExecutionModel(slippage_bps=100, fee_rate=0.0003),
        )

        self.assertLess(conservative.net_pnl_usd, clean.net_pnl_usd)
        self.assertEqual(conservative.trades[0].fill_quality_label, "estimated")

    def test_inverse_option_fee_uses_contract_amount_not_premium_rate(self):
        strategy = sample_strategy()
        market = {
            "call_leg": observations("call", [0.009, 0.009]),
            "put_leg": observations("put", [0.009, 0.009]),
        }

        fee_free = run_backtest(strategy, market, ExecutionModel(fee_rate=0))
        charged = run_backtest(strategy, market, ExecutionModel(fee_rate=0.0003))

        # Two legs, entry and exit: 4 executions * 0.0003 BTC * 78,500 USD/BTC.
        self.assertAlmostEqual(fee_free.net_pnl_usd - charged.net_pnl_usd, 94.2, places=8)
        self.assertGreater(fee_free.net_pnl_usd - charged.net_pnl_usd, 100 * 0.8478)

    def test_inverse_option_fee_respects_twelve_point_five_percent_premium_cap(self):
        strategy = sample_strategy()
        market = {
            "call_leg": observations("call", [0.001, 0.001]),
            "put_leg": observations("put", [0.001, 0.001]),
        }

        fee_free = run_backtest(strategy, market, ExecutionModel(fee_rate=0))
        charged = run_backtest(strategy, market, ExecutionModel(fee_rate=0.0003))

        # The 0.0003 BTC base fee is capped at 12.5% of the 0.001 BTC premium.
        expected_usd_fee = 4 * (0.001 * 0.125) * 78_500
        self.assertAlmostEqual(
            fee_free.net_pnl_usd - charged.net_pnl_usd,
            expected_usd_fee,
            places=8,
        )

    def test_rejects_missing_leg_observations(self):
        with self.assertRaisesRegex(BacktestEngineError, "put_leg"):
            run_backtest(
                sample_strategy(),
                {"call_leg": observations("call", [0.0010, 0.0012])},
                ExecutionModel(slippage_bps=0, fee_rate=0),
            )

    # =========================================================================
    # Task 2 Acceptance Tests: Bug Fixes & Strict Validations
    # =========================================================================

    def test_untriggered_exit_rules_close_at_last_observation_price_not_entry(self):
        """CRITICAL FIX: When no exit rule triggers, position must close at the final

        observation price, NOT the entry price, and exit_reason must be 'end_of_data'.
        Entry price = 1.0 BTC, final price = 2.0 BTC.
        """
        # Set stop-loss and take-profit thresholds so high they are never reached
        strategy = sample_strategy(
            stop_loss=10_000_000,
            take_profit=10_000_000,
            time_exit_val="2099-01-01T00:00:00Z",
        )

        # Prices: entry at 0.0010, step 2 at 0.0011, final step at 0.0020
        # If exit_observations defaulted to entry, net PnL would be 0 (fees 0)
        # With the fix, exit is at 0.0020, netting a substantial gain.
        market = {
            "call_leg": observations("call", [0.0010, 0.0011, 0.0020]),
            "put_leg": observations("put", [0.0010, 0.0011, 0.0020]),
        }

        result = run_backtest(strategy, market, ExecutionModel(slippage_bps=0, fee_rate=0))

        # Must close at final timestamp with 'end_of_data'
        self.assertEqual(result.trades[0].closed_at_utc, dt(2))
        self.assertEqual(result.trades[0].exit_reason, "end_of_data")

        # Each leg entered at 0.0010 and exited at 0.0020 (gain of +0.0010 BTC * 78,500 * 2 = $157)
        expected_pnl = 2 * (0.0020 - 0.0010) * 78500.0
        self.assertAlmostEqual(result.net_pnl_usd, expected_pnl, places=4)
        self.assertGreater(result.net_pnl_usd, 0)
        self.assertEqual(result.win_rate, 1.0)

    def test_changing_btc_usd_rate_fixtures(self):
        """Verify USD cashflow conversion under rising and falling BTC/USD rates."""
        strategy = sample_strategy(
            stop_loss=10_000_000,
            take_profit=10_000_000,
            time_exit_val="2099-01-01T00:00:00Z",
        )

        # 1. Rising BTC/USD rate: 50,000 -> 60,000 -> 80,000
        # Premium constant at 0.0010 BTC
        rising_market = {
            "call_leg": observations("call", [0.0010, 0.0010, 0.0010], btc_usd_rates=[50000.0, 60000.0, 80000.0]),
            "put_leg": observations("put", [0.0010, 0.0010, 0.0010], btc_usd_rates=[50000.0, 60000.0, 80000.0]),
        }
        res_rising = run_backtest(strategy, rising_market, ExecutionModel(fee_rate=0, slippage_bps=0))
        # Entry: - 2 * 0.0010 * 50000 = -100 USD
        # Exit:  + 2 * 0.0010 * 80000 = +160 USD
        # Net USD PnL = +60 USD
        self.assertAlmostEqual(res_rising.net_pnl_usd, 60.0, places=4)

        # 2. Falling BTC/USD rate: 80,000 -> 60,000 -> 50,000
        falling_market = {
            "call_leg": observations("call", [0.0010, 0.0010, 0.0010], btc_usd_rates=[80000.0, 60000.0, 50000.0]),
            "put_leg": observations("put", [0.0010, 0.0010, 0.0010], btc_usd_rates=[80000.0, 60000.0, 50000.0]),
        }
        res_falling = run_backtest(strategy, falling_market, ExecutionModel(fee_rate=0, slippage_bps=0))
        # Entry: - 2 * 0.0010 * 80000 = -160 USD
        # Exit:  + 2 * 0.0010 * 50000 = +100 USD
        # Net USD PnL = -60 USD
        self.assertAlmostEqual(res_falling.net_pnl_usd, -60.0, places=4)

    def test_rejects_instrument_change_within_leg_series(self):
        """A leg's observations must keep the same instrument name throughout the series."""
        call_obs = observations("call", [0.0010, 0.0012])
        # Modify the second observation to a different strike instrument
        call_obs[1] = MarketObservation(
            timestamp_utc=dt(1),
            instrument_name="BTC-10SEP26-80000-C",  # Changed strike!
            option_type="call",
            underlying_price_usd=78500,
            trade_price_btc=0.0012,
            btc_usd=78500,
        )

        with self.assertRaisesRegex(BacktestEngineError, "instrument changed within series"):
            run_backtest(
                sample_strategy(),
                {"call_leg": call_obs, "put_leg": observations("put", [0.0010, 0.0012])},
            )

    def test_rejects_duplicate_timestamps_within_leg_series(self):
        """A leg cannot contain duplicate timestamps."""
        call_obs = observations(
            "call",
            [0.0010, 0.0012],
            timestamps=[dt(1), dt(1)],  # Duplicate!
        )
        with self.assertRaisesRegex(BacktestEngineError, "duplicate timestamp"):
            run_backtest(
                sample_strategy(),
                {"call_leg": call_obs, "put_leg": observations("put", [0.0010, 0.0012])},
            )

    def test_rejects_nan_and_infinity_in_observations(self):
        """NaN and Infinity in prices or rates must be rejected."""
        # NaN trade price
        nan_obs = observations("call", [0.0010, float("nan")])
        with self.assertRaisesRegex(BacktestEngineError, "cannot be NaN or Infinity"):
            run_backtest(
                sample_strategy(),
                {"call_leg": nan_obs, "put_leg": observations("put", [0.0010, 0.0012])},
            )

        # Infinity btc_usd
        inf_obs = observations("call", [0.0010, 0.0012], btc_usd_rates=[78500.0, float("inf")])
        with self.assertRaisesRegex(BacktestEngineError, "cannot be NaN or Infinity"):
            run_backtest(
                sample_strategy(),
                {"call_leg": inf_obs, "put_leg": observations("put", [0.0010, 0.0012])},
            )

    def test_rejects_negative_costs_in_execution_model(self):
        """Negative slippage or fee rates must be rejected."""
        market = {
            "call_leg": observations("call", [0.0010, 0.0012]),
            "put_leg": observations("put", [0.0010, 0.0012]),
        }
        with self.assertRaisesRegex(BacktestEngineError, "slippage_bps cannot be negative"):
            run_backtest(sample_strategy(), market, ExecutionModel(slippage_bps=-5))

        with self.assertRaisesRegex(BacktestEngineError, "fee_rate cannot be negative"):
            run_backtest(sample_strategy(), market, ExecutionModel(fee_rate=-0.0003))

    def test_zero_settlement_price_allowed_while_zero_trade_price_rejected(self):
        """A standard trade price of 0.0 is invalid, but a settlement price of 0.0 (OTM expiry) is valid."""
        # 1. Zero trade price on normal observation -> Rejected
        with self.assertRaisesRegex(BacktestEngineError, "trade_price_btc must be positive"):
            run_backtest(
                sample_strategy(),
                {
                    "call_leg": observations("call", [0.0010, 0.0]),
                    "put_leg": observations("put", [0.0010, 0.0012]),
                },
            )

        # 2. Zero trade price on settlement observation -> Accepted
        settle_market = {
            "call_leg": observations(
                "call",
                [0.0010, 0.0],
                is_settlement_flags=[False, True],
            ),
            "put_leg": observations(
                "put",
                [0.0010, 0.0012],
                is_settlement_flags=[False, True],
            ),
        }
        # Stop loss large so it doesn't trigger early
        strat = sample_strategy(stop_loss=10_000, take_profit=10_000, time_exit_val="2099-01-01T00:00:00Z")
        result = run_backtest(strat, settle_market, ExecutionModel(fee_rate=0, slippage_bps=0))
        self.assertIsNotNone(result)
        self.assertEqual(result.trades[0].closed_at_utc, dt(1))

    def test_rejects_unsupported_entry_rules(self):
        """Legacy engine only supports 'enter_when_chain_has_required_legs'."""
        strat = sample_strategy(entry_rule_name="rsi_oversold_filter")
        market = {
            "call_leg": observations("call", [0.0010, 0.0012]),
            "put_leg": observations("put", [0.0010, 0.0012]),
        }
        with self.assertRaisesRegex(BacktestEngineError, "unsupported entry rule"):
            run_backtest(strat, market)

    def test_rejects_unsupported_entry_rule_parameters(self):
        """Legacy engine does not evaluate parameters on entry rules and must reject non-empty parameters."""
        raw = {
            "name": "Toy straddle with filter params",
            "version": "1.0",
            "legs": [
                {
                    "name": "call_leg",
                    "option_type": "call",
                    "side": "long",
                    "quantity": 1,
                    "strike": {"type": "exact_usd", "value": 78500},
                    "expiry": {"type": "exact_date", "value": "2026-09-10"},
                },
                {
                    "name": "put_leg",
                    "option_type": "put",
                    "side": "long",
                    "quantity": 1,
                    "strike": {"type": "exact_usd", "value": 78500},
                    "expiry": {"type": "exact_date", "value": "2026-09-10"},
                },
            ],
            "entry_rules": [
                {"name": "enter_when_chain_has_required_legs", "parameters": {"unsupported_filter": True}}
            ],
            "exit_rules": [
                {"type": "net_usd_stop_loss", "value": 20},
                {"type": "net_usd_take_profit", "value": 30},
                {"type": "time_exit", "value": "2026-09-01T03:00:00Z"},
                {"type": "expiry_exit", "value": "at_expiry"},
            ],
            "fee_spread_model": "custom_bid_ask_slippage",
            "result_metrics": [
                {"name": "trade_count", "description": "Completed trades."},
                {"name": "net_pnl_usd", "description": "Net PnL after fees and slippage."},
                {"name": "max_drawdown_usd", "description": "Largest equity drop."},
                {"name": "win_rate", "description": "Positive trade share."},
            ],
        }
        strat = strategy_from_mapping(raw)
        market = {
            "call_leg": observations("call", [0.0010, 0.0012]),
            "put_leg": observations("put", [0.0010, 0.0012]),
        }
        with self.assertRaisesRegex(BacktestEngineError, "unsupported entry rule parameters"):
            run_backtest(strat, market)


if __name__ == "__main__":
    unittest.main()
