"""Comprehensive unit tests for Deribit BTC inverse option settlement, delivery pricing, and fee schedule modeling.

Covers all Task 6 mandatory acceptance criteria:
- ITM, ATM, OTM call and put payoffs for both long and short positions.
- Zero payoff tests (ATM and OTM).
- Exact expiry boundary timing.
- Leap-day UTC expiry (2024-02-29 08:00 UTC).
- Strict rejection of negative, zero, NaN, and Infinity inputs.
- Missing delivery price behavior: strictly prohibits perpetual close substitution.
- Daily vs other expiry delivery fee distinction (0% fee for daily options).
- Premium fee cap (12.5% of premium) and delivery fee cap.
- Settlement functions with official delivery even when the option instrument had no quotes or trades.
- Fee schedule historical verification: pre-2026 dates labeled configured_unverified.
"""

import math
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from core.contracts import OptionType, SettlementObservation, SettlementProvider, ValidationError
from ingestion.settlement import (
    CONTRACT_SIZE_BTC,
    DERIBIT_CURRENT_CONFIRMED_FEE_SCHEDULE,
    DERIBIT_HISTORICAL_UNVERIFIED_FEE_SCHEDULE,
    DeliveryPriceRecord,
    DeribitSettlementProvider,
    FeeSchedule,
    MissingDeliveryPriceError,
    SettlementError,
    VerificationStatus,
    calculate_inverse_option_payoff,
    calculate_inverse_option_payoff_per_contract,
    get_fee_schedule_for_timestamp,
    is_daily_option,
    parse_delivery_prices_response,
    parse_option_instrument_name,
)


class SettlementTests(unittest.TestCase):
    def test_itm_call_long_and_short_payoff(self):
        # K = 60000 USD, S = 70000 USD, Q = 2, C = 1
        # Payoff per contract = (70000 - 60000) / 70000 = 10000 / 70000 = 1/7 BTC
        strike = Decimal("60000")
        settlement = Decimal("70000")
        quantity = Decimal("2.0")

        payoff_per_contract = calculate_inverse_option_payoff_per_contract(OptionType.CALL, strike, settlement)
        expected_per_contract = Decimal("10000") / Decimal("70000")
        self.assertEqual(payoff_per_contract, expected_per_contract)

        # Long position: positive payoff
        long_total = calculate_inverse_option_payoff(
            option_type=OptionType.CALL,
            strike_usd=strike,
            settlement_price_usd=settlement,
            quantity=quantity,
            contract_size=CONTRACT_SIZE_BTC,
            is_long=True,
        )
        self.assertEqual(long_total, expected_per_contract * quantity)
        self.assertGreater(long_total, Decimal("0"))

        # Short position: negative payoff
        short_total = calculate_inverse_option_payoff(
            option_type=OptionType.CALL,
            strike_usd=strike,
            settlement_price_usd=settlement,
            quantity=quantity,
            contract_size=CONTRACT_SIZE_BTC,
            is_long=False,
        )
        self.assertEqual(short_total, -expected_per_contract * quantity)
        self.assertLess(short_total, Decimal("0"))

    def test_itm_put_long_and_short_payoff(self):
        # K = 70000 USD, S = 60000 USD, Q = 3, C = 1
        # Payoff per contract = (70000 - 60000) / 60000 = 10000 / 60000 = 1/6 BTC
        strike = Decimal("70000")
        settlement = Decimal("60000")
        quantity = Decimal("3.0")

        payoff_per_contract = calculate_inverse_option_payoff_per_contract(OptionType.PUT, strike, settlement)
        expected_per_contract = Decimal("10000") / Decimal("60000")
        self.assertEqual(payoff_per_contract, expected_per_contract)

        # Long put: positive payoff
        long_total = calculate_inverse_option_payoff(
            option_type=OptionType.PUT,
            strike_usd=strike,
            settlement_price_usd=settlement,
            quantity=quantity,
            is_long=True,
        )
        self.assertEqual(long_total.quantize(Decimal("0.00000001")), Decimal("0.50000000"))

        # Short put: negative payoff
        short_total = calculate_inverse_option_payoff(
            option_type=OptionType.PUT,
            strike_usd=strike,
            settlement_price_usd=settlement,
            quantity=quantity,
            is_long=False,
        )
        self.assertEqual(short_total.quantize(Decimal("0.00000001")), Decimal("-0.50000000"))

    def test_atm_call_and_put_zero_payoff(self):
        # At The Money: S == K -> Payoff is strictly 0.0
        strike = Decimal("65000")
        settlement = Decimal("65000")

        call_payoff = calculate_inverse_option_payoff(OptionType.CALL, strike, settlement, is_long=True)
        put_payoff = calculate_inverse_option_payoff(OptionType.PUT, strike, settlement, is_long=True)

        self.assertEqual(call_payoff, Decimal("0.0"))
        self.assertEqual(put_payoff, Decimal("0.0"))

    def test_otm_call_and_put_zero_payoff(self):
        # Call OTM: S < K (S = 55000, K = 60000) -> 0.0
        call_otm = calculate_inverse_option_payoff(
            OptionType.CALL, Decimal("60000"), Decimal("55000"), is_long=True
        )
        self.assertEqual(call_otm, Decimal("0.0"))

        # Put OTM: S > K (S = 65000, K = 60000) -> 0.0
        put_otm = calculate_inverse_option_payoff(
            OptionType.PUT, Decimal("60000"), Decimal("65000"), is_long=True
        )
        self.assertEqual(put_otm, Decimal("0.0"))

    def test_exact_expiry_boundary(self):
        # 2024-03-29 08:00:00 UTC
        expiry_dt = datetime(2024, 3, 29, 8, 0, 0, tzinfo=timezone.utc)
        expiry_ms = int(expiry_dt.timestamp() * 1000)
        self.assertEqual(expiry_ms, 1711699200000)

        provider = DeribitSettlementProvider()
        provider.add_delivery_price(expiry_ms, Decimal("69850.50"))

        obs = provider.get("BTC-29MAR24-60000-C", expiry_ms)
        self.assertIsNotNone(obs)
        self.assertEqual(obs.expiry_ms, expiry_ms)
        self.assertEqual(obs.settlement_price_usd, Decimal("69850.50"))
        self.assertTrue(obs.is_official_delivery)

        # Off by 1 millisecond must return None (exact boundary check)
        self.assertIsNone(provider.get("BTC-29MAR24-60000-C", expiry_ms - 1))
        self.assertIsNone(provider.get("BTC-29MAR24-60000-C", expiry_ms + 1))

    def test_leap_day_utc_expiry(self):
        # 2024-02-29 08:00:00 UTC (Leap day)
        leap_dt = datetime(2024, 2, 29, 8, 0, 0, tzinfo=timezone.utc)
        leap_ms = int(leap_dt.timestamp() * 1000)
        self.assertEqual(leap_ms, 1709193600000)

        # Test parsing from Deribit date string '2024-02-29'
        response_data = {
            "data": [
                {"date": "2024-02-29", "delivery_price": 62500.0},
            ]
        }
        records = parse_delivery_prices_response(response_data)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].timestamp_ms, leap_ms)
        self.assertEqual(records[0].delivery_price_usd, Decimal("62500.0"))

        provider = DeribitSettlementProvider(records)
        obs = provider.get("BTC-29FEB24-60000-C", leap_ms)
        self.assertIsNotNone(obs)
        self.assertEqual(obs.settlement_price_usd, Decimal("62500.0"))
        # Payoff: (62500 - 60000) / 62500 = 2500 / 62500 = 0.04 BTC
        self.assertEqual(obs.settlement_price_btc, Decimal("2500") / Decimal("62500"))

    def test_rejects_negative_zero_nan_and_inf_inputs(self):
        # Negative strike
        with self.assertRaises((ValidationError, SettlementError)):
            calculate_inverse_option_payoff(OptionType.CALL, Decimal("-60000"), Decimal("65000"))

        # Zero strike
        with self.assertRaises((ValidationError, SettlementError)):
            calculate_inverse_option_payoff(OptionType.CALL, Decimal("0"), Decimal("65000"))

        # Negative settlement price
        with self.assertRaises((ValidationError, SettlementError)):
            calculate_inverse_option_payoff(OptionType.CALL, Decimal("60000"), Decimal("-65000"))

        # Zero settlement price
        with self.assertRaises((ValidationError, SettlementError)):
            calculate_inverse_option_payoff(OptionType.CALL, Decimal("60000"), Decimal("0"))

        # NaN strike
        with self.assertRaises((ValidationError, SettlementError)):
            calculate_inverse_option_payoff(OptionType.CALL, float("nan"), Decimal("65000"))

        # Inf settlement price
        with self.assertRaises((ValidationError, SettlementError)):
            calculate_inverse_option_payoff(OptionType.CALL, Decimal("60000"), float("inf"))

        # Negative quantity
        with self.assertRaises((ValidationError, SettlementError)):
            calculate_inverse_option_payoff(
                OptionType.CALL, Decimal("60000"), Decimal("65000"), quantity=Decimal("-1.0")
            )

    def test_delivery_missing_prohibits_perpetual_close_substitution(self):
        # Strictly verify: when official delivery price is missing, provider returns None or raises MissingDeliveryPriceError
        provider = DeribitSettlementProvider()
        missing_expiry = 1711699200000

        # get() returns None
        obs = provider.get("BTC-29MAR24-60000-C", missing_expiry)
        self.assertIsNone(obs)

        # get_strict() raises MissingDeliveryPriceError with explicit rule citation
        with self.assertRaises(MissingDeliveryPriceError) as ctx:
            provider.get_strict("BTC-29MAR24-60000-C", missing_expiry)
        self.assertIn("Perpetual close substitution is strictly forbidden", str(ctx.exception))

        # Attempting to register an unverified/perpetual delivery record must be rejected
        with self.assertRaises(SettlementError):
            DeliveryPriceRecord(
                timestamp_ms=missing_expiry,
                delivery_price_usd=Decimal("68000"),
                is_official_delivery=False,  # Unofficial proxy rejected!
            )

    def test_daily_vs_other_expiry_fee_distinction(self):
        schedule = DERIBIT_CURRENT_CONFIRMED_FEE_SCHEDULE
        quantity = Decimal("10.0")
        settlement_val_btc = Decimal("0.10")  # 0.10 BTC per contract

        # 1. Daily option: delivery fee must be strictly 0.0 BTC
        daily_fee = schedule.compute_delivery_fee(
            quantity=quantity,
            settlement_price_btc=settlement_val_btc,
            is_daily=True,
        )
        self.assertEqual(daily_fee, Decimal("0.0"))

        # 2. Non-daily option: standard delivery fee 0.015% of underlying (0.00015 BTC / contract)
        # For 10 contracts: 10 * 0.00015 = 0.0015 BTC
        # Cap is 12.5% of delivery value: 12.5% * (10 * 0.10) = 0.125 BTC > 0.0015 BTC -> fee is 0.0015 BTC
        standard_fee = schedule.compute_delivery_fee(
            quantity=quantity,
            settlement_price_btc=settlement_val_btc,
            is_daily=False,
        )
        self.assertEqual(standard_fee, Decimal("0.0015"))

    def test_premium_fee_cap_and_delivery_fee_cap(self):
        schedule = DERIBIT_CURRENT_CONFIRMED_FEE_SCHEDULE
        quantity = Decimal("1.0")

        # Trading fee: 0.0003 BTC uncapped.
        # Cap is 12.5% of option premium.
        # High premium: 0.05 BTC -> Cap = 0.125 * 0.05 = 0.00625 BTC > 0.0003 BTC -> Uncapped fee applies
        fee_high_prem = schedule.compute_trading_fee(quantity=quantity, premium_price_btc=Decimal("0.05"))
        self.assertEqual(fee_high_prem, Decimal("0.0003"))

        # Low premium: 0.0010 BTC -> Cap = 0.125 * 0.0010 = 0.000125 BTC < 0.0003 BTC -> Cap applies!
        fee_low_prem = schedule.compute_trading_fee(quantity=quantity, premium_price_btc=Decimal("0.0010"))
        self.assertEqual(fee_low_prem, Decimal("0.000125"))

        # Delivery fee cap: 12.5% of settlement value
        # Normal settlement value: 0.05 BTC -> Cap = 0.125 * 0.05 = 0.00625 BTC > 0.00015 BTC -> 0.00015 BTC
        del_fee_normal = schedule.compute_delivery_fee(quantity=quantity, settlement_price_btc=Decimal("0.05"), is_daily=False)
        self.assertEqual(del_fee_normal, Decimal("0.00015"))

        # Tiny settlement value: 0.0004 BTC -> Cap = 0.125 * 0.0004 = 0.00005 BTC < 0.00015 BTC -> Cap applies!
        del_fee_tiny = schedule.compute_delivery_fee(quantity=quantity, settlement_price_btc=Decimal("0.0004"), is_daily=False)
        self.assertEqual(del_fee_tiny, Decimal("0.00005"))

    def test_settlement_provider_works_without_option_quote(self):
        # Requirement: "Settlement enstrüman kotasyonu olmasa da resmi delivery ile çalışır."
        # The option instrument never traded and had 0 orderbook quotes.
        # As long as official delivery price is known, settlement provider computes observation successfully.
        provider = DeribitSettlementProvider()
        expiry_ms = 1711699200000
        provider.add_delivery_price(expiry_ms, Decimal("69850.0"))

        obs = provider.get("BTC-29MAR24-60000-C", expiry_ms)
        self.assertIsNotNone(obs)
        self.assertEqual(obs.settlement_price_usd, Decimal("69850.0"))
        # Payoff: (69850 - 60000) / 69850 = 9850 / 69850 BTC
        expected_btc = Decimal("9850") / Decimal("69850")
        self.assertEqual(obs.settlement_price_btc, expected_btc)

    def test_fee_schedule_historical_verification_status(self):
        # Current confirmed date: 2026-08-17 (1786924800000 ms)
        t_current = 1787000000000
        schedule_current = get_fee_schedule_for_timestamp(t_current)
        self.assertEqual(schedule_current.verification_status, VerificationStatus.CONFIRMED)

        # Historical date: 2024-03-29 (pre-2026) -> must be CONFIGURED_UNVERIFIED
        # Rule: "Tarihsel ücret doğrulanamıyorsa configured_unverified kaydet; bugünkü ücreti geçmişe confirmed diye taşıma."
        t_hist = 1711699200000
        schedule_hist = get_fee_schedule_for_timestamp(t_hist)
        self.assertEqual(schedule_hist.verification_status, VerificationStatus.CONFIGURED_UNVERIFIED)

    def test_is_daily_option_lifespan(self):
        # Daily option listed 24h before expiry
        creation = 1711612800000
        expiry = 1711699200000  # 24h later
        self.assertTrue(is_daily_option("BTC-29MAR24-65000-C", creation, expiry))

        # Monthly option listed 30 days before expiry
        creation_monthly = expiry - (30 * 24 * 3600 * 1000)
        self.assertFalse(is_daily_option("BTC-29MAR24-65000-C", creation_monthly, expiry))

    def test_settlement_provider_protocol_conformance(self):
        provider = DeribitSettlementProvider()
        self.assertIsInstance(provider, SettlementProvider)


if __name__ == "__main__":
    unittest.main()
