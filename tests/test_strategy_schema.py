import unittest

from strategies.strategy_schema import (
    StrategyValidationError,
    strategy_from_mapping,
    validate_strategy,
)


def valid_strategy_mapping():
    return {
        "name": "Configurable multi-leg BTC option setup",
        "version": "1.0",
        "legs": [
            {
                "name": "long_call",
                "option_type": "call",
                "side": "long",
                "quantity": 1,
                "strike": {"type": "nearest_premium_usd", "value": 20},
                "expiry": {"type": "nearest_days_to_expiry", "value": 7},
            },
            {
                "name": "short_call",
                "option_type": "call",
                "side": "short",
                "quantity": 1,
                "strike": {"type": "nearest_premium_usd", "value": 30},
                "expiry": {"type": "nearest_days_to_expiry", "value": 7},
            },
        ],
        "entry_rules": [
            {
                "name": "enter_when_chain_has_required_legs",
                "parameters": {"min_quote_depth": 1},
            }
        ],
        "exit_rules": [
            {"type": "net_usd_stop_loss", "value": 80},
            {"type": "net_usd_take_profit", "value": 120},
            {"type": "time_exit", "value": "2025-06-20T08:00:00Z"},
            {"type": "expiry_exit", "value": "at_expiry"},
        ],
        "fee_spread_model": "deribit_inverse_option_bid_ask",
        "result_metrics": [
            {"name": "trade_count", "description": "Number of completed strategy trades."},
            {"name": "net_pnl_usd", "description": "Total profit and loss after fees and spread."},
            {"name": "max_drawdown_usd", "description": "Largest peak-to-trough net USD loss."},
            {"name": "win_rate", "description": "Share of completed trades with positive net PnL."},
        ],
    }


class StrategySchemaTests(unittest.TestCase):
    def test_accepts_configurable_multi_leg_strategy(self):
        strategy = strategy_from_mapping(valid_strategy_mapping())

        validate_strategy(strategy)
        self.assertEqual(len(strategy.legs), 2)
        self.assertEqual(strategy.legs[0].strike.value, 20)
        self.assertEqual(strategy.legs[1].strike.value, 30)

    def test_rejects_fixed_or_missing_leg_fields(self):
        raw = valid_strategy_mapping()
        raw["legs"][0]["quantity"] = 0

        with self.assertRaisesRegex(StrategyValidationError, "legs\\[0\\].quantity"):
            strategy_from_mapping(raw)

    def test_rejects_prediction_accuracy_metric_name(self):
        raw = valid_strategy_mapping()
        raw["result_metrics"].append(
            {"name": "prediction_accuracy", "description": "Should not be used for backtest results."}
        )

        with self.assertRaisesRegex(StrategyValidationError, "win_rate"):
            strategy_from_mapping(raw)

    def test_requires_net_usd_exit_thresholds_to_be_configurable_positive_values(self):
        raw = valid_strategy_mapping()
        raw["exit_rules"][0]["value"] = -20

        with self.assertRaisesRegex(StrategyValidationError, "net_usd_stop_loss|exit_rules\\[0\\].value"):
            strategy_from_mapping(raw)

    def test_rejects_unknown_fee_spread_model_reference(self):
        raw = valid_strategy_mapping()
        raw["fee_spread_model"] = "live_order_book_execution"

        with self.assertRaisesRegex(StrategyValidationError, "fee_spread_model"):
            strategy_from_mapping(raw)


if __name__ == "__main__":
    unittest.main()
