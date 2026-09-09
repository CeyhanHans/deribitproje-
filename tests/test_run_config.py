"""Acceptance and unit tests for RunConfig parsing, date ranges, leg relations, and capability checks."""

import math
from decimal import Decimal
import unittest
from pathlib import Path

from core.contracts import ExecutionModelId, RunConfig
from strategies.strategy_schema import (
    ExpirySelectorType,
    OptionType,
    Selector,
    Side,
    SizingType,
    StrategyDefinition,
    StrategyLeg,
    StrategyValidationError,
    StrikeSelectorType,
    load_run_config_file,
    parse_run_config_dict,
    parse_run_config_json,
    parse_utc_timestamp_ms,
    strategy_from_mapping,
    to_core_run_config,
)


def minimal_valid_run_config_dict():
    return {
        "run_name": "test_run",
        "strategy_name": "test_strat",
        "start_time": "2024-03-01T00:00:00Z",
        "end_time": "2025-06-01T00:00:00Z",
        "decision_resolution": "1h",
        "underlying": "BTC",
        "settlement_currency": "BTC",
        "contract_type": "inverse",
        "initial_cash_btc": "10.0",
        "sizing": {
            "type": "fixed_quantity",
            "value": 1.0,
        },
        "max_open_positions": 1,
        "margin_model": "conservative_stress_reserve",
        "stress_reserve_ratio": "0.5",
        "execution_profile": {
            "execution_model_id": "model_a_bid_ask",
            "fee_rate_amount": "0.0003",
            "fee_cap_ratio": "0.125",
            "slippage_btc": "0",
        },
        "data_policy": {
            "allow_missing_bars": False,
            "price_basis": "trade_and_quotes",
        },
        "strategy": {
            "name": "test_strat",
            "version": "1.0",
            "legs": [
                {
                    "name": "call_leg",
                    "option_type": "call",
                    "side": "long",
                    "quantity": 1.0,
                    "strike": {"type": "atm", "value": 0},
                    "expiry": {"type": "nearest_days_to_expiry", "value": 30},
                },
                {
                    "name": "put_leg",
                    "option_type": "put",
                    "side": "long",
                    "quantity": 1.0,
                    "strike": {"type": "atm", "value": 0},
                    "expiry": {"type": "nearest_days_to_expiry", "value": 30},
                    "same_strike_as": "call_leg",
                    "same_expiry_as": "call_leg",
                },
            ],
            "entry_rules": [
                {
                    "name": "enter_when_chain_has_required_legs",
                    "parameters": {},
                }
            ],
            "exit_rules": [
                {"type": "net_usd_stop_loss", "value": 100.0},
                {"type": "net_usd_take_profit", "value": 200.0},
                {"type": "expiry_exit", "value": "at_expiry"},
            ],
            "fee_spread_model": "deribit_inverse_option_bid_ask",
            "result_metrics": [
                {"name": "trade_count", "description": "Trade count."},
                {"name": "net_pnl_usd", "description": "Net PnL."},
                {"name": "max_drawdown_usd", "description": "Max DD."},
                {"name": "win_rate", "description": "Win rate."},
            ],
        },
    }


class RunConfigAcceptanceTests(unittest.TestCase):
    def test_parses_two_different_date_ranges(self):
        """Dates are never hardcoded and arbitrary valid date ranges parse dynamically."""
        raw1 = minimal_valid_run_config_dict()
        raw1["start_time"] = "2024-03-01T00:00:00Z"
        raw1["end_time"] = "2025-06-01T00:00:00Z"
        cfg1 = parse_run_config_dict(raw1)
        self.assertEqual(cfg1.start_ms, 1709251200000)
        self.assertEqual(cfg1.end_ms, 1748736000000)

        raw2 = minimal_valid_run_config_dict()
        raw2["start_time"] = "2024-09-01T08:00:00Z"
        raw2["end_time"] = "2024-11-15T08:00:00Z"
        cfg2 = parse_run_config_dict(raw2)
        self.assertEqual(cfg2.start_ms, 1725177600000)
        self.assertEqual(cfg2.end_ms, 1731657600000)

        # Confirm durations differ dynamically
        duration_1_hours = (cfg1.end_ms - cfg1.start_ms) / (3600 * 1000)
        duration_2_hours = (cfg2.end_ms - cfg2.start_ms) / (3600 * 1000)
        self.assertNotEqual(duration_1_hours, duration_2_hours)
        self.assertGreater(duration_1_hours, duration_2_hours)

    def test_parses_bracketed_time_range_string(self):
        """Supports '[2024-03-01T00:00Z,2025-06-01T00:00Z)' notation."""
        raw = minimal_valid_run_config_dict()
        del raw["start_time"]
        del raw["end_time"]
        raw["time_range"] = "[2024-03-01T00:00Z,2025-06-01T00:00Z)"
        cfg = parse_run_config_dict(raw)
        self.assertEqual(cfg.start_ms, 1709251200000)
        self.assertEqual(cfg.end_ms, 1748736000000)

    def test_leap_day_parsing_and_invalid_leap_day(self):
        """Parses valid leap day 2024-02-29 and rejects impossible leap day 2023-02-29."""
        raw = minimal_valid_run_config_dict()
        raw["start_time"] = "2024-02-29T08:00:00Z"
        raw["end_time"] = "2024-03-01T08:00:00Z"
        cfg = parse_run_config_dict(raw)
        self.assertEqual(cfg.start_ms, 1709193600000)

        raw_bad = minimal_valid_run_config_dict()
        raw_bad["start_time"] = "2023-02-29T08:00:00Z"
        with self.assertRaisesRegex(StrategyValidationError, "invalid UTC date string"):
            parse_run_config_dict(raw_bad)

    def test_rejects_identical_and_inverted_start_end(self):
        """Rejects identical start/end and inverted start > end ranges."""
        raw_same = minimal_valid_run_config_dict()
        raw_same["start_time"] = "2024-03-01T00:00:00Z"
        raw_same["end_time"] = "2024-03-01T00:00:00Z"
        with self.assertRaisesRegex(StrategyValidationError, "cannot be identical"):
            parse_run_config_dict(raw_same)

        raw_inverted = minimal_valid_run_config_dict()
        raw_inverted["start_time"] = "2024-05-01T00:00:00Z"
        raw_inverted["end_time"] = "2024-03-01T00:00:00Z"
        with self.assertRaisesRegex(StrategyValidationError, "strictly earlier than end"):
            parse_run_config_dict(raw_inverted)

    def test_rejects_unknown_entry_rule(self):
        """Entry rules outside the known registry must be rejected with StrategyValidationError."""
        raw = minimal_valid_run_config_dict()
        raw["strategy"]["entry_rules"] = [{"name": "unregistered_magical_entry", "parameters": {}}]
        with self.assertRaisesRegex(StrategyValidationError, "unknown entry rule.*unregistered_magical_entry"):
            parse_run_config_dict(raw)

    def test_rejects_duplicate_leg_names(self):
        """Strategy legs with duplicate names must be rejected."""
        raw = minimal_valid_run_config_dict()
        raw["strategy"]["legs"] = [
            {
                "name": "call_leg",
                "option_type": "call",
                "side": "long",
                "quantity": 1.0,
                "strike": {"type": "atm", "value": 0},
                "expiry": {"type": "nearest_days_to_expiry", "value": 30},
            },
            {
                "name": "call_leg",
                "option_type": "put",
                "side": "long",
                "quantity": 1.0,
                "strike": {"type": "atm", "value": 0},
                "expiry": {"type": "nearest_days_to_expiry", "value": 30},
            },
        ]
        with self.assertRaisesRegex(StrategyValidationError, "duplicate leg name.*call_leg"):
            parse_run_config_dict(raw)

    def test_rejects_negative_or_zero_quantity(self):
        """Rejects non-positive quantities in legs and sizing."""
        raw_zero_leg = minimal_valid_run_config_dict()
        raw_zero_leg["strategy"]["legs"][0]["quantity"] = 0
        with self.assertRaisesRegex(StrategyValidationError, "quantity.*positive number"):
            parse_run_config_dict(raw_zero_leg)

        raw_neg_leg = minimal_valid_run_config_dict()
        raw_neg_leg["strategy"]["legs"][0]["quantity"] = -2.5
        with self.assertRaisesRegex(StrategyValidationError, "quantity.*positive number"):
            parse_run_config_dict(raw_neg_leg)

        raw_neg_sizing = minimal_valid_run_config_dict()
        raw_neg_sizing["sizing"]["value"] = -1.0
        with self.assertRaisesRegex(StrategyValidationError, "sizing value must be positive"):
            parse_run_config_dict(raw_neg_sizing)

    def test_rejects_nan_inputs(self):
        """Rejects NaN values in quantity, strike, exit thresholds, capital, and sizing."""
        # NaN in leg quantity
        raw = minimal_valid_run_config_dict()
        raw["strategy"]["legs"][0]["quantity"] = float("nan")
        with self.assertRaisesRegex(StrategyValidationError, "NaN"):
            parse_run_config_dict(raw)

        # NaN in strike value
        raw = minimal_valid_run_config_dict()
        raw["strategy"]["legs"][0]["strike"] = {"type": "exact_usd", "value": float("nan")}
        with self.assertRaisesRegex(StrategyValidationError, "NaN"):
            parse_run_config_dict(raw)

        # NaN in exit thresholds
        raw = minimal_valid_run_config_dict()
        raw["strategy"]["exit_rules"][0]["value"] = float("nan")
        with self.assertRaisesRegex(StrategyValidationError, "NaN"):
            parse_run_config_dict(raw)

        # NaN in initial cash BTC
        raw = minimal_valid_run_config_dict()
        raw["initial_cash_btc"] = "NaN"
        with self.assertRaisesRegex(StrategyValidationError, "NaN"):
            parse_run_config_dict(raw)

        # NaN in sizing value
        raw = minimal_valid_run_config_dict()
        raw["sizing"]["value"] = float("nan")
        with self.assertRaisesRegex(StrategyValidationError, "NaN"):
            parse_run_config_dict(raw)

    def test_rejects_invalid_dte(self):
        """Rejects non-positive or NaN DTE values."""
        raw_zero_dte = minimal_valid_run_config_dict()
        raw_zero_dte["strategy"]["legs"][0]["expiry"] = {"type": "days_to_expiry", "value": 0}
        with self.assertRaisesRegex(StrategyValidationError, "invalid DTE.*positive number of days"):
            parse_run_config_dict(raw_zero_dte)

        raw_neg_dte = minimal_valid_run_config_dict()
        raw_neg_dte["strategy"]["legs"][0]["expiry"] = {"type": "nearest_days_to_expiry", "value": -7}
        with self.assertRaisesRegex(StrategyValidationError, "invalid DTE.*positive number of days"):
            parse_run_config_dict(raw_neg_dte)

        raw_nan_dte = minimal_valid_run_config_dict()
        raw_nan_dte["strategy"]["legs"][0]["expiry"] = {"type": "days_to_expiry", "value": float("nan")}
        with self.assertRaisesRegex(StrategyValidationError, "invalid DTE.*NaN"):
            parse_run_config_dict(raw_nan_dte)

    def test_rejects_unsupported_delta_capability(self):
        """Delta strike selection ('delta_target') is rejected because V1 lacks Greek/vol surface quotes."""
        raw = minimal_valid_run_config_dict()
        raw["strategy"]["legs"][0]["strike"] = {"type": "delta_target", "value": 0.25}
        with self.assertRaisesRegex(
            StrategyValidationError,
            "unsupported capability: delta strike selection \\('delta_target'\\) is not supported in V1",
        ):
            parse_run_config_dict(raw)

    def test_rejects_unsupported_margin_model(self):
        """Unsupported margin models (portfolio margin, naked short unreserved) are rejected."""
        raw_pm = minimal_valid_run_config_dict()
        raw_pm["margin_model"] = "deribit_portfolio_margin"
        with self.assertRaisesRegex(StrategyValidationError, "unsupported margin model.*deribit_portfolio_margin"):
            parse_run_config_dict(raw_pm)

        raw_naked = minimal_valid_run_config_dict()
        raw_naked["margin_model"] = "naked_short_unreserved"
        with self.assertRaisesRegex(StrategyValidationError, "unsupported margin model.*naked_short_unreserved"):
            parse_run_config_dict(raw_naked)

        raw_cross = minimal_valid_run_config_dict()
        raw_cross["margin_model"] = "cross_margin"
        with self.assertRaisesRegex(StrategyValidationError, "unsupported margin model.*cross_margin"):
            parse_run_config_dict(raw_cross)

    def test_rejects_unknown_fields_in_config_and_schema(self):
        """Unknown fields in RunConfig and Strategy mappings raise StrategyValidationError."""
        raw_extra_cfg = minimal_valid_run_config_dict()
        raw_extra_cfg["arbitrary_mystery_field"] = "unexpected"
        with self.assertRaisesRegex(StrategyValidationError, "unexpected field in run config.*arbitrary_mystery_field"):
            parse_run_config_dict(raw_extra_cfg)

        raw_extra_strat = minimal_valid_run_config_dict()
        raw_extra_strat["strategy"]["mystery_strat_key"] = 123
        with self.assertRaisesRegex(StrategyValidationError, "unexpected field in strategy.*mystery_strat_key"):
            parse_run_config_dict(raw_extra_strat)

        raw_extra_leg = minimal_valid_run_config_dict()
        raw_extra_leg["strategy"]["legs"][0]["mystery_leg_param"] = True
        with self.assertRaisesRegex(StrategyValidationError, "unexpected field in leg.*mystery_leg_param"):
            parse_run_config_dict(raw_extra_leg)

    def test_leg_relations_and_atm_selector(self):
        """Validates same_expiry_as, same_strike_as, strike_offset_usd, and ATM strike selector."""
        raw = minimal_valid_run_config_dict()
        raw["strategy"]["legs"] = [
            {
                "name": "atm_call",
                "option_type": "call",
                "side": "long",
                "quantity": 1.0,
                "strike": {"type": "atm", "value": 0},
                "expiry": {"type": "nearest_days_to_expiry", "value": 30},
            },
            {
                "name": "offset_put",
                "option_type": "put",
                "side": "long",
                "quantity": 1.0,
                "strike": {"type": "same_strike_as", "value": "atm_call"},
                "expiry": {"type": "same_expiry_as", "value": "atm_call"},
                "strike_offset_usd": -500.0,
            },
        ]
        cfg = parse_run_config_dict(raw)
        self.assertIsNotNone(cfg.strategy)
        self.assertEqual(len(cfg.strategy.legs), 2)
        self.assertEqual(cfg.strategy.legs[1].strike_offset_usd, -500.0)

        # Self-reference rejection
        raw_self = minimal_valid_run_config_dict()
        raw_self["strategy"]["legs"][0]["same_expiry_as"] = "call_leg"
        with self.assertRaisesRegex(StrategyValidationError, "cannot reference itself"):
            parse_run_config_dict(raw_self)

        # Unknown leg reference rejection
        raw_unknown_ref = minimal_valid_run_config_dict()
        raw_unknown_ref["strategy"]["legs"][0]["same_strike_as"] = "nonexistent_leg"
        with self.assertRaisesRegex(StrategyValidationError, "references unknown leg"):
            parse_run_config_dict(raw_unknown_ref)

    def test_sizing_models(self):
        """Supports fixed_quantity, premium_budget_btc, and capital_fraction."""
        for stype in ("fixed_quantity", "premium_budget_btc", "capital_fraction"):
            raw = minimal_valid_run_config_dict()
            raw["sizing"] = {"type": stype, "value": 0.5}
            cfg = parse_run_config_dict(raw)
            self.assertEqual(cfg.sizing.sizing_type, stype)
            self.assertEqual(cfg.sizing.value, Decimal("0.5"))

        # Unsupported sizing type
        raw_bad = minimal_valid_run_config_dict()
        raw_bad["sizing"] = {"type": "kelly_criterion_leveraged", "value": 1.0}
        with self.assertRaisesRegex(StrategyValidationError, "unsupported sizing type"):
            parse_run_config_dict(raw_bad)

    def test_v1_canonical_configs_parse_and_roundtrip(self):
        """Canonical example configuration files in config/ parse cleanly and roundtrip to core RunConfig."""
        repo_root = Path(__file__).resolve().parent.parent

        # 1. March 2024 to May 2025 Straddle config
        straddle_path = repo_root / "config" / "backtest_straddle_2024_2025.json"
        self.assertTrue(straddle_path.is_file(), f"Missing canonical file: {straddle_path}")
        straddle_cfg = load_run_config_file(straddle_path)

        self.assertEqual(straddle_cfg.start_ms, 1709251200000)
        self.assertEqual(straddle_cfg.end_ms, 1748736000000)
        self.assertEqual(straddle_cfg.decision_resolution_hours, 1)
        self.assertEqual(straddle_cfg.initial_cash_btc, Decimal("10.0"))
        self.assertEqual(straddle_cfg.underlying, "BTC")
        self.assertEqual(straddle_cfg.contract_type, "inverse")
        self.assertEqual(straddle_cfg.margin_model, "conservative_stress_reserve")

        # Roundtrip to frozen core.contracts.RunConfig
        core_run_1 = to_core_run_config(straddle_cfg)
        self.assertIsInstance(core_run_1, RunConfig)
        self.assertEqual(core_run_1.start_ms, 1709251200000)
        self.assertEqual(core_run_1.end_ms, 1748736000000)
        self.assertEqual(core_run_1.decision_resolution_hours, 1)
        self.assertEqual(core_run_1.execution_model_id, ExecutionModelId.MODEL_A_BID_ASK)
        self.assertEqual(core_run_1.initial_capital_btc, Decimal("10.0"))

        # 2. Daily Strangle config
        strangle_path = repo_root / "config" / "backtest_strangle_daily.json"
        self.assertTrue(strangle_path.is_file(), f"Missing canonical file: {strangle_path}")
        strangle_cfg = load_run_config_file(strangle_path)

        self.assertEqual(strangle_cfg.decision_resolution_hours, 24)
        self.assertEqual(strangle_cfg.decision_resolution, "1d")
        self.assertEqual(strangle_cfg.initial_cash_btc, Decimal("5.0"))

        core_run_2 = to_core_run_config(strangle_cfg)
        self.assertIsInstance(core_run_2, RunConfig)
        self.assertEqual(core_run_2.decision_resolution_hours, 24)
        self.assertEqual(core_run_2.initial_capital_btc, Decimal("5.0"))


if __name__ == "__main__":
    unittest.main()
