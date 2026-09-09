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


def sample_strategy():
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
            "entry_rules": [{"name": "enter_when_chain_has_required_legs"}],
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
    )


def observations(option_type: str, prices: list[float]):
    return [
        MarketObservation(
            timestamp_utc=dt(index),
            instrument_name=f"BTC-10SEP26-78500-{option_type[0].upper()}",
            option_type=option_type,
            underlying_price_usd=78500,
            trade_price_btc=price,
            btc_usd=78500,
        )
        for index, price in enumerate(prices)
    ]


class BacktestEngineTests(unittest.TestCase):
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
        # The old premium * 0.0003 formula would charge only 0.8478 USD here.
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


if __name__ == "__main__":
    unittest.main()
