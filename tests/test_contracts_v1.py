"""Acceptance tests for core/contracts.py (Task 1).

Tests include:
- JSON round-trip for all core dataclasses
- Rejection of boolean values passed to numeric fields
- Rejection of NaN and Infinity values
- Rejection of invalid enum values
- Time boundaries: UTC integer ms, [start, end) ranges, lookahead prevention
- Schema version validation and mismatch errors
- Immutability (frozen dataclasses)
- Exact decimal-string JSON formatting for financial fields
- Verification of standard-library-only imports
- Protocol conformance
"""

import inspect
import json
import math
import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import Any

import core.contracts as cc


class TestContractsV1(unittest.TestCase):

    def test_json_roundtrip_all_dataclasses(self) -> None:
        """Verify perfect round-trip serialization and deserialization for all dataclasses."""
        fixtures = [
            (cc.RunConfig, cc.make_sample_run_config()),
            (cc.Instrument, cc.make_sample_instrument()),
            (cc.TradeTick, cc.make_sample_trade_tick()),
            (cc.PriceObservation, cc.make_sample_price_observation()),
            (cc.CoverageReport, cc.make_sample_coverage_report()),
            (cc.SelectionDecision, cc.make_sample_selection_decision()),
            (cc.Signal, cc.make_sample_signal()),
            (cc.OrderIntent, cc.make_sample_order_intent()),
            (cc.Fill, cc.make_sample_fill()),
            (cc.FillDecision, cc.make_sample_fill_decision()),
            (cc.LedgerEntry, cc.make_sample_ledger_entry()),
            (cc.Position, cc.make_sample_position()),
            (cc.EquityPoint, cc.make_sample_equity_point()),
            (cc.PortfolioState, cc.make_sample_portfolio_state()),
            (cc.SettlementObservation, cc.make_sample_settlement_observation()),
            (cc.Metrics, cc.make_sample_metrics()),
            (cc.ReportManifest, cc.make_sample_report_manifest()),
            (cc.RunResult, cc.make_sample_run_result()),
        ]

        for cls, instance in fixtures:
            with self.subTest(cls=cls.__name__):
                json_str = cc.to_json(instance)
                self.assertIsInstance(json_str, str)
                # Verify parseable by standard json
                parsed_dict = json.loads(json_str)
                self.assertIsInstance(parsed_dict, dict)
                # Round-trip back to dataclass
                reconstructed = cc.from_json(cls, json_str)
                self.assertEqual(instance, reconstructed)
                # Double check to_dict / from_dict
                dict_repr = cc.to_dict(instance)
                self.assertEqual(reconstructed, cc.from_dict(cls, dict_repr))

    def test_financial_fields_serialize_as_strings(self) -> None:
        """Verify that all Decimal fields serialize to JSON as strings, never raw floats."""
        inst = cc.make_sample_instrument(strike_usd=Decimal("95000.50"))
        json_data = json.loads(cc.to_json(inst))
        self.assertEqual(json_data["strike_usd"], "95000.50")
        self.assertIsInstance(json_data["strike_usd"], str)

        fill = cc.make_sample_fill(
            price_btc=Decimal("0.08600000"),
            fee_btc=Decimal("0.00030000"),
        )
        fill_json = json.loads(cc.to_json(fill))
        self.assertIsInstance(fill_json["price_btc"], str)
        self.assertIsInstance(fill_json["fee_btc"], str)
        self.assertEqual(fill_json["fee_btc"], "0.00030000")

    def test_reject_bool_in_numeric_fields(self) -> None:
        """Ensure boolean values are strictly rejected where numbers or timestamps are expected."""
        # Initial capital cannot be True or False
        with self.assertRaises(cc.ValidationError):
            cc.RunConfig(
                strategy_name="test",
                start_ms=1000,
                end_ms=2000,
                initial_capital_btc=True,  # type: ignore[arg-type]
            )

        # start_ms cannot be True or False
        with self.assertRaises(cc.ValidationError):
            cc.RunConfig(
                strategy_name="test",
                start_ms=False,  # type: ignore[arg-type]
                end_ms=2000,
                initial_capital_btc=Decimal("1.0"),
            )

        # strike_usd cannot be True
        with self.assertRaises(cc.ValidationError):
            cc.Instrument(
                instrument_name="BTC-27DEC24-90000-C",
                option_type=cc.OptionType.CALL,
                strike_usd=True,  # type: ignore[arg-type]
                creation_ms=1000,
                expiry_ms=2000,
            )

        # quantity cannot be True in OrderIntent
        with self.assertRaises(cc.ValidationError):
            cc.OrderIntent(
                order_id="ord_1",
                position_id="pos_1",
                leg_id="leg_1",
                instrument_name="BTC-27DEC24-90000-C",
                side=cc.Side.BUY,
                quantity=True,  # type: ignore[arg-type]
                decision_at_ms=1000,
                eligible_after_ms=1000,
                expires_at_ms=2000,
            )

    def test_reject_nan_and_infinity(self) -> None:
        """Ensure NaN and Infinity (float or Decimal) are rejected."""
        # Decimal("NaN")
        with self.assertRaises(cc.ValidationError):
            cc.make_sample_instrument(strike_usd=Decimal("NaN"))

        # Decimal("Infinity")
        with self.assertRaises(cc.ValidationError):
            cc.make_sample_instrument(strike_usd=Decimal("Infinity"))

        # float("nan")
        with self.assertRaises(cc.ValidationError):
            cc.make_sample_instrument(strike_usd=float("nan"))  # type: ignore[arg-type]

        # float("inf")
        with self.assertRaises(cc.ValidationError):
            cc.make_sample_instrument(strike_usd=float("inf"))  # type: ignore[arg-type]

        # PriceObservation price cannot be NaN
        with self.assertRaises(cc.ValidationError):
            cc.PriceObservation(
                instrument_name="BTC-27DEC24-90000-C",
                observed_at_ms=1000,
                available_at_ms=1000,
                price_basis=cc.PriceBasis.TRADE,
                trade_price_btc=Decimal("NaN"),
            )

    def test_reject_invalid_enum_values(self) -> None:
        """Ensure invalid enum strings are rejected upon construction and deserialization."""
        with self.assertRaises(cc.ValidationError):
            cc.Instrument(
                instrument_name="BTC-27DEC24-90000-C",
                option_type="future",  # type: ignore[arg-type]
                strike_usd=Decimal("90000.0"),
                creation_ms=1000,
                expiry_ms=2000,
            )

        with self.assertRaises(cc.ValidationError):
            cc.OrderIntent(
                order_id="ord_1",
                position_id="pos_1",
                leg_id="leg_1",
                instrument_name="BTC-27DEC24-90000-C",
                side="HOLD",  # type: ignore[arg-type]
                quantity=Decimal("1.0"),
                decision_at_ms=1000,
                eligible_after_ms=1000,
                expires_at_ms=2000,
            )

        # In deserialization from json
        bad_json = json.dumps({
            "instrument_name": "BTC-27DEC24-90000-C",
            "option_type": "INVALID_TYPE",
            "strike_usd": "90000.0",
            "creation_ms": 1000,
            "expiry_ms": 2000,
        })
        with self.assertRaises(cc.ValidationError):
            cc.from_json(cc.Instrument, bad_json)

    def test_time_range_and_lookahead_boundaries(self) -> None:
        """Validate [start, end) time ranges and lookahead prevention rules."""
        # start_ms >= end_ms rejected in RunConfig
        with self.assertRaises(cc.ValidationError):
            cc.RunConfig(
                strategy_name="test",
                start_ms=2000,
                end_ms=1000,
                initial_capital_btc=Decimal("1.0"),
            )
        with self.assertRaises(cc.ValidationError):
            cc.RunConfig(
                strategy_name="test",
                start_ms=1000,
                end_ms=1000,
                initial_capital_btc=Decimal("1.0"),
            )

        # creation_ms >= expiry_ms rejected in Instrument
        with self.assertRaises(cc.ValidationError):
            cc.Instrument(
                instrument_name="BTC-27DEC24-90000-C",
                option_type=cc.OptionType.CALL,
                strike_usd=Decimal("90000.0"),
                creation_ms=2000,
                expiry_ms=1000,
            )

        # available_at_ms < observed_at_ms rejected in PriceObservation (lookahead prevention)
        with self.assertRaises(cc.ValidationError):
            cc.PriceObservation(
                instrument_name="BTC-27DEC24-90000-C",
                observed_at_ms=2000,
                available_at_ms=1000,
                price_basis=cc.PriceBasis.TRADE,
            )

        # available_at_ms < decision_at_ms rejected in Signal
        with self.assertRaises(cc.ValidationError):
            cc.Signal(
                signal_id="sig_1",
                decision_at_ms=2000,
                available_at_ms=1000,
                strategy_name="test",
                signal_type="enter",
                instrument_names=("BTC-27DEC24-90000-C",),
                target_quantities=(Decimal("1.0"),),
                sides=(cc.Side.BUY,),
                reason_code=cc.ReasonCode.ENTRY_SIGNAL,
            )

        # Negative timestamp rejected
        with self.assertRaises(cc.ValidationError):
            cc.RunConfig(
                strategy_name="test",
                start_ms=-100,
                end_ms=1000,
                initial_capital_btc=Decimal("1.0"),
            )

    def test_schema_version_validation_and_mismatch(self) -> None:
        """Validate strict schema_version enforcement."""
        # Default is SCHEMA_VERSION (1.0)
        cfg = cc.make_sample_run_config()
        self.assertEqual(cfg.schema_version, "1.0")

        # Incompatible schema_version raises SchemaVersionMismatchError
        with self.assertRaises(cc.SchemaVersionMismatchError):
            cc.RunConfig(
                strategy_name="test",
                start_ms=1000,
                end_ms=2000,
                initial_capital_btc=Decimal("1.0"),
                schema_version="2.0",
            )

        # Deserializing mismatched version from json raises error
        bad_json = json.dumps({
            "schema_version": "0.9",
            "strategy_name": "test",
            "start_ms": 1000,
            "end_ms": 2000,
            "initial_capital_btc": "1.0",
        })
        with self.assertRaises(cc.SchemaVersionMismatchError):
            cc.from_json(cc.RunConfig, bad_json)

    def test_frozen_immutability(self) -> None:
        """Ensure all contract dataclasses are frozen and immutable."""
        inst = cc.make_sample_instrument()
        with self.assertRaises(FrozenInstanceError):
            inst.strike_usd = Decimal("100000.0")  # type: ignore[misc]

        cfg = cc.make_sample_run_config()
        with self.assertRaises(FrozenInstanceError):
            cfg.start_ms = 999  # type: ignore[misc]

    def test_standard_library_only(self) -> None:
        """Verify that core/contracts.py only imports standard library modules."""
        imported_modules = inspect.getmembers(cc, inspect.ismodule)
        stdlib_names = {
            "annotations",
            "dataclasses",
            "datetime",
            "decimal",
            "enum",
            "hashlib",
            "inspect",
            "json",
            "math",
            "pathlib",
            "typing",
        }
        for name, mod in imported_modules:
            top_level = mod.__name__.split(".")[0]
            if top_level != "core":
                self.assertIn(
                    top_level,
                    stdlib_names,
                    f"Forbidden external or non-standard module imported: {mod.__name__}",
                )

    def test_provider_protocols_runtime_checkable(self) -> None:
        """Verify provider protocols are runtime checkable."""

        class DummyTradeProvider:
            def fetch(self, plan: Any) -> cc.DownloadResult:
                return cc.DownloadResult(success=True, ticks_count=10, start_ms=0, end_ms=1000)

        class DummyPriceProvider:
            def iter_events(self, start_ms: int, end_ms: int, instruments: Any):
                yield cc.make_sample_price_observation()

        self.assertTrue(isinstance(DummyTradeProvider(), cc.HistoricalTradeProvider))
        self.assertTrue(isinstance(DummyPriceProvider(), cc.PriceProvider))

    def test_decimal_normalization_and_arithmetic_on_frozen_dataclasses(self) -> None:
        """Verify that string or float values passed to financial fields are normalized to Decimal."""
        # 1. String passed to strike_usd
        inst_str = cc.make_sample_instrument(strike_usd="90000")  # type: ignore[arg-type]
        self.assertIsInstance(inst_str.strike_usd, Decimal)
        self.assertEqual(inst_str.strike_usd, Decimal("90000"))
        # Arithmetic works immediately without TypeError
        self.assertEqual(inst_str.strike_usd + Decimal("1000"), Decimal("91000"))
        # JSON serialization outputs a string
        json_data = json.loads(cc.to_json(inst_str))
        self.assertEqual(json_data["strike_usd"], "90000")
        self.assertIsInstance(json_data["strike_usd"], str)

        # 2. Float passed to strike_usd
        inst_flt = cc.make_sample_instrument(strike_usd=90000.5)  # type: ignore[arg-type]
        self.assertIsInstance(inst_flt.strike_usd, Decimal)
        self.assertEqual(inst_flt.strike_usd, Decimal("90000.5"))
        self.assertEqual(inst_flt.strike_usd * 2, Decimal("180001.0"))
        json_flt = json.loads(cc.to_json(inst_flt))
        self.assertEqual(json_flt["strike_usd"], "90000.5")
        self.assertIsInstance(json_flt["strike_usd"], str)

    def test_price_observation_size_validation_and_lookahead_rejection(self) -> None:
        """Verify PriceObservation rejects NaN sizes and bucket lookahead."""
        # Rejection of NaN in trade_size
        with self.assertRaises(cc.ValidationError):
            cc.PriceObservation(
                instrument_name="BTC-27DEC24-90000-C",
                observed_at_ms=1000,
                available_at_ms=1000,
                price_basis=cc.PriceBasis.TRADE,
                trade_size=Decimal("NaN"),
            )

        # Rejection of bucket lookahead (available_at earlier than interval_end)
        with self.assertRaises(cc.ValidationError):
            cc.PriceObservation(
                instrument_name="BTC-27DEC24-90000-C",
                observed_at_ms=0,
                available_at_ms=0,
                price_basis=cc.PriceBasis.TRADE,
                interval_start_ms=0,
                interval_end_ms=3600000,
            )

        # Valid interval observation where available_at >= interval_end
        valid_obs = cc.PriceObservation(
            instrument_name="BTC-27DEC24-90000-C",
            observed_at_ms=3600000,
            available_at_ms=3600000,
            price_basis=cc.PriceBasis.TRADE,
            interval_start_ms=0,
            interval_end_ms=3600000,
        )
        self.assertEqual(valid_obs.interval_end_ms, 3600000)

    def test_inverse_option_currency_conventions(self) -> None:
        """Verify Instrument default and configurable currency fields for BTC inverse options."""
        inst = cc.make_sample_instrument()
        self.assertEqual(inst.base_currency, cc.Currency.BTC)
        self.assertEqual(inst.quote_currency, cc.Currency.BTC)
        self.assertEqual(inst.counter_currency, cc.Currency.USD)
        self.assertEqual(inst.settlement_currency, cc.Currency.BTC)

        # Configurable with quote_currency = USD
        inst_usd = cc.make_sample_instrument(quote_currency=cc.Currency.USD)
        self.assertEqual(inst_usd.quote_currency, cc.Currency.USD)

    def test_metrics_win_rate_optional_when_zero_trades(self) -> None:
        """Verify Metrics accepts win_rate=None when total_trades == 0."""
        zero_metrics = cc.Metrics(
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            break_even_trades=0,
            win_rate=None,
        )
        self.assertIsNone(zero_metrics.win_rate)
        # Round-trip through JSON
        json_str = cc.to_json(zero_metrics)
        recon = cc.from_json(cc.Metrics, json_str)
        self.assertIsNone(recon.win_rate)

        # When total_trades > 0, win_rate cannot be None
        with self.assertRaises(cc.ValidationError):
            cc.Metrics(
                total_trades=1,
                winning_trades=1,
                losing_trades=0,
                break_even_trades=0,
                win_rate=None,
            )

    def test_strict_from_dict_rejects_unknown_fields(self) -> None:
        """Verify from_dict raises ValidationError when unknown fields are supplied."""
        cfg_dict = cc.to_dict(cc.make_sample_run_config())
        cfg_dict["misspelled_config"] = 123
        with self.assertRaises(cc.ValidationError) as ctx:
            cc.from_dict(cc.RunConfig, cfg_dict)
        self.assertIn("misspelled_config", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
