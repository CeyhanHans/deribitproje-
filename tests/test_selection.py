"""Acceptance and unit tests for multi-leg contract selection (select_legs)."""

import random
import unittest
from decimal import Decimal

from core.contracts import (
    Currency,
    Instrument,
    LegSelectorProtocol,
    OptionType,
    PositionStatus,
    PriceObservation,
    ReasonCode,
    SelectionDecision,
    TieBreakPolicy,
)
from strategies.selection import (
    AsOfView,
    SelectionError,
    build_call_debit_spread_strategy,
    build_iron_condor_strategy,
    build_long_straddle_strategy,
    build_put_debit_spread_strategy,
    build_strangle_strategy,
    make_sample_catalog_fixture,
    select_legs,
)
from strategies.strategy_schema import (
    ExpirySelectorType,
    Selector,
    Side,
    StrategyDefinition,
    StrategyLeg,
    StrikeSelectorType,
)

DAY_MS = 86_400_000


class SelectionAcceptanceTests(unittest.TestCase):
    def setUp(self):
        # T0 = 2024-03-01T00:00:00Z = 1709251200000 ms
        self.t0 = 1709251200000
        self.spot = Decimal("90000.0")

        # Expiries: 7 days, 30 days, 60 days from T0
        self.exp7 = self.t0 + 7 * DAY_MS
        self.exp30 = self.t0 + 30 * DAY_MS
        self.exp60 = self.t0 + 60 * DAY_MS

        self.strikes = [70000, 75000, 80000, 85000, 90000, 95000, 100000, 105000, 110000]
        self.catalog = make_sample_catalog_fixture(
            decision_at_ms=self.t0,
            strikes=self.strikes,
            expiries=[self.exp7, self.exp30, self.exp60],
            creation_offset_days=30.0,
        )

    def test_protocol_conformance(self):
        """select_legs function satisfies the locked LegSelectorProtocol."""
        self.assertTrue(isinstance(select_legs, LegSelectorProtocol))

    def test_long_straddle_fixture_selection(self):
        """Long straddle selects ATM Call and ATM Put sharing the same strike and expiry."""
        strategy = build_long_straddle_strategy(target_dte=30.0)
        asof = AsOfView(
            decision_at_ms=self.t0,
            underlying_price_usd=self.spot,
            instruments=self.catalog,
            tie_break="lower",
        )

        decision = select_legs(strategy, asof)
        self.assertEqual(decision.reason_code, ReasonCode.ENTRY_SIGNAL)
        self.assertEqual(len(decision.selected_instruments), 2)

        call, put = decision.selected_instruments
        self.assertEqual(call.option_type, OptionType.CALL)
        self.assertEqual(put.option_type, OptionType.PUT)
        self.assertEqual(call.strike_usd, Decimal("90000.0"))
        self.assertEqual(put.strike_usd, Decimal("90000.0"))
        self.assertEqual(call.expiry_ms, self.exp30)
        self.assertEqual(put.expiry_ms, self.exp30)
        self.assertEqual(decision.common_strike_usd, Decimal("90000.0"))
        self.assertEqual(decision.target_expiry_ms, self.exp30)

    def test_strangle_fixture_selection(self):
        """Strangle selects OTM Call (105%) and OTM Put (95%) sharing the same expiry."""
        strategy = build_strangle_strategy(call_moneyness=105.0, put_moneyness=95.0, target_dte=30.0)
        asof = AsOfView(
            decision_at_ms=self.t0,
            underlying_price_usd=self.spot,
            instruments=self.catalog,
        )

        decision = select_legs(strategy, asof)
        self.assertEqual(decision.reason_code, ReasonCode.ENTRY_SIGNAL)
        self.assertEqual(len(decision.selected_instruments), 2)

        call, put = decision.selected_instruments
        # 105% of 90,000 = 94,500 -> closest strike in catalog is 95,000
        self.assertEqual(call.option_type, OptionType.CALL)
        self.assertEqual(call.strike_usd, Decimal("95000.0"))
        # 95% of 90,000 = 85,500 -> closest strike in catalog is 85,000
        self.assertEqual(put.option_type, OptionType.PUT)
        self.assertEqual(put.strike_usd, Decimal("85000.0"))
        self.assertEqual(call.expiry_ms, self.exp30)
        self.assertEqual(put.expiry_ms, self.exp30)
        self.assertIsNone(decision.common_strike_usd)  # Strangle has different strikes

    def test_call_debit_spread_fixture_selection(self):
        """Call debit spread selects Long ATM Call and Short OTM Call (+offset), same expiry."""
        strategy = build_call_debit_spread_strategy(strike_offset_usd=5000.0, target_dte=30.0)
        asof = AsOfView(
            decision_at_ms=self.t0,
            underlying_price_usd=self.spot,
            instruments=self.catalog,
        )

        decision = select_legs(strategy, asof)
        self.assertEqual(decision.reason_code, ReasonCode.ENTRY_SIGNAL)
        self.assertEqual(len(decision.selected_instruments), 2)

        long_call, short_call = decision.selected_instruments
        self.assertEqual(long_call.strike_usd, Decimal("90000.0"))
        self.assertEqual(short_call.strike_usd, Decimal("95000.0"))
        self.assertEqual(long_call.expiry_ms, self.exp30)
        self.assertEqual(short_call.expiry_ms, self.exp30)

    def test_put_debit_spread_fixture_selection(self):
        """Put debit spread selects Long ATM Put and Short OTM Put (-offset), same expiry."""
        strategy = build_put_debit_spread_strategy(strike_offset_usd=-5000.0, target_dte=30.0)
        asof = AsOfView(
            decision_at_ms=self.t0,
            underlying_price_usd=self.spot,
            instruments=self.catalog,
        )

        decision = select_legs(strategy, asof)
        self.assertEqual(decision.reason_code, ReasonCode.ENTRY_SIGNAL)
        self.assertEqual(len(decision.selected_instruments), 2)

        long_put, short_put = decision.selected_instruments
        self.assertEqual(long_put.strike_usd, Decimal("90000.0"))
        self.assertEqual(short_put.strike_usd, Decimal("85000.0"))
        self.assertEqual(long_put.expiry_ms, self.exp30)
        self.assertEqual(short_put.expiry_ms, self.exp30)

    def test_iron_condor_fixture_selection(self):
        """4-leg Iron Condor selects 2 puts and 2 calls across wings with shared expiry."""
        strategy = build_iron_condor_strategy(
            put_wing_moneyness=90.0,      # 81,000 -> 80,000
            put_short_moneyness=95.0,     # 85,500 -> 85,000
            call_short_moneyness=105.0,   # 94,500 -> 95,000
            call_wing_moneyness=110.0,    # 99,000 -> 100,000
            target_dte=30.0,
        )
        asof = AsOfView(
            decision_at_ms=self.t0,
            underlying_price_usd=self.spot,
            instruments=self.catalog,
        )

        decision = select_legs(strategy, asof)
        self.assertEqual(decision.reason_code, ReasonCode.ENTRY_SIGNAL)
        self.assertEqual(len(decision.selected_instruments), 4)

        long_put, short_put, short_call, long_call = decision.selected_instruments
        self.assertEqual(long_put.strike_usd, Decimal("80000.0"))
        self.assertEqual(short_put.strike_usd, Decimal("85000.0"))
        self.assertEqual(short_call.strike_usd, Decimal("95000.0"))
        self.assertEqual(long_call.strike_usd, Decimal("100000.0"))

        self.assertTrue(long_put.strike_usd < short_put.strike_usd < short_call.strike_usd < long_call.strike_usd)
        self.assertEqual({inst.expiry_ms for inst in decision.selected_instruments}, {self.exp30})

    def test_future_strike_exclusion(self):
        """Instruments created in the future (creation_ms > T) are never selected."""
        # Create a candidate catalog where ATM strike (90,000) was created in the FUTURE relative to T0
        future_creation_ms = self.t0 + 5 * DAY_MS
        past_creation_ms = self.t0 - 10 * DAY_MS

        catalog_with_future = [
            # Strike 90,000 (Exact ATM) - CREATED IN FUTURE!
            Instrument(
                instrument_name="BTC-FUTURE-90000-C",
                option_type=OptionType.CALL,
                strike_usd=Decimal("90000.0"),
                creation_ms=future_creation_ms,
                expiry_ms=self.exp30,
            ),
            Instrument(
                instrument_name="BTC-FUTURE-90000-P",
                option_type=OptionType.PUT,
                strike_usd=Decimal("90000.0"),
                creation_ms=future_creation_ms,
                expiry_ms=self.exp30,
            ),
            # Strike 85,000 - CREATED IN PAST (Eligible!)
            Instrument(
                instrument_name="BTC-PAST-85000-C",
                option_type=OptionType.CALL,
                strike_usd=Decimal("85000.0"),
                creation_ms=past_creation_ms,
                expiry_ms=self.exp30,
            ),
            Instrument(
                instrument_name="BTC-PAST-85000-P",
                option_type=OptionType.PUT,
                strike_usd=Decimal("85000.0"),
                creation_ms=past_creation_ms,
                expiry_ms=self.exp30,
            ),
        ]

        strategy = build_long_straddle_strategy(target_dte=30.0)
        asof = AsOfView(
            decision_at_ms=self.t0,
            underlying_price_usd=self.spot,
            instruments=catalog_with_future,
        )

        decision = select_legs(strategy, asof)
        self.assertEqual(decision.reason_code, ReasonCode.ENTRY_SIGNAL)
        # Even though 90,000 was closer to spot (exact ATM), it was in the future and excluded!
        # The eligible strike 85,000 was selected instead:
        self.assertEqual(decision.selected_instruments[0].strike_usd, Decimal("85000.0"))
        self.assertEqual(decision.selected_instruments[1].strike_usd, Decimal("85000.0"))

    def test_missing_delta_observation(self):
        """When delta target is specified, missing or unobserved delta at T results in NO_CANDIDATE_FOUND / DATA_MISSING."""
        delta_strategy = StrategyDefinition(
            name="delta_straddle",
            version="1.0",
            legs=(
                StrategyLeg(
                    name="delta_call",
                    option_type="call",
                    side=Side.LONG.value,
                    quantity=1.0,
                    strike=Selector(StrikeSelectorType.DELTA_TARGET.value, 0.50),
                    expiry=Selector(ExpirySelectorType.NEAREST_DAYS_TO_EXPIRY.value, 30.0),
                ),
                StrategyLeg(
                    name="delta_put",
                    option_type="put",
                    side=Side.LONG.value,
                    quantity=1.0,
                    strike=Selector(StrikeSelectorType.DELTA_TARGET.value, -0.50),
                    expiry=Selector(ExpirySelectorType.SAME_EXPIRY_AS.value, "delta_call"),
                    same_expiry_as="delta_call",
                ),
            ),
            entry_rules=(),
            exit_rules=(),
            fee_spread_model="deribit_inverse_option_bid_ask",
            result_metrics=(),
        )

        # 1. No observations provided -> Selection fails with DATA_MISSING / NO_CANDIDATE_FOUND
        asof_empty = AsOfView(
            decision_at_ms=self.t0,
            underlying_price_usd=self.spot,
            instruments=self.catalog,
            observations_by_instrument={},
        )
        decision = select_legs(delta_strategy, asof_empty)
        self.assertEqual(decision.selected_instruments, ())
        self.assertIn(decision.reason_code, (ReasonCode.DATA_MISSING, ReasonCode.NO_CANDIDATE_FOUND))

        # 2. Provide reliable delta observations available at T
        class MockObs:
            def __init__(self, delta, available_at_ms):
                self.delta = delta
                self.available_at_ms = available_at_ms

        obs = {
            f"BTC-{self.exp30}-90000-C": MockObs(0.50, self.t0),
            f"BTC-{self.exp30}-90000-P": MockObs(-0.50, self.t0),
        }
        asof_with_obs = AsOfView(
            decision_at_ms=self.t0,
            underlying_price_usd=self.spot,
            instruments=self.catalog,
            observations_by_instrument=obs,
        )
        decision_with_obs = select_legs(delta_strategy, asof_with_obs)
        self.assertEqual(decision_with_obs.reason_code, ReasonCode.ENTRY_SIGNAL)
        self.assertEqual(len(decision_with_obs.selected_instruments), 2)
        self.assertEqual(decision_with_obs.selected_instruments[0].strike_usd, Decimal("90000.0"))

    def test_no_matching_pair_found(self):
        """When a required leg has no matching instrument on any candidate expiry, return NO_CANDIDATE_FOUND."""
        # Create a catalog with ONLY calls (no puts at all)
        calls_only = [i for i in self.catalog if i.option_type == OptionType.CALL]
        strategy = build_long_straddle_strategy(target_dte=30.0)

        asof = AsOfView(
            decision_at_ms=self.t0,
            underlying_price_usd=self.spot,
            instruments=calls_only,
        )

        decision = select_legs(strategy, asof)
        self.assertEqual(decision.selected_instruments, ())
        self.assertEqual(decision.reason_code, ReasonCode.NO_CANDIDATE_FOUND)

    def test_input_permutation_determinism(self):
        """Selection is 100% deterministic regardless of input list permutation/order."""
        strategy = build_long_straddle_strategy(target_dte=30.0)
        asof_initial = AsOfView(
            decision_at_ms=self.t0,
            underlying_price_usd=self.spot,
            instruments=self.catalog,
            tie_break="lower",
        )
        baseline_decision = select_legs(strategy, asof_initial)
        baseline_names = [inst.instrument_name for inst in baseline_decision.selected_instruments]

        # Test 25 distinct random permutations of the input catalog
        rng = random.Random(42)
        for _ in range(25):
            shuffled = list(self.catalog)
            rng.shuffle(shuffled)
            asof_shuffled = AsOfView(
                decision_at_ms=self.t0,
                underlying_price_usd=self.spot,
                instruments=shuffled,
                tie_break="lower",
            )
            decision = select_legs(strategy, asof_shuffled)
            result_names = [inst.instrument_name for inst in decision.selected_instruments]
            self.assertEqual(result_names, baseline_names)

    def test_open_position_instruments_remain_immutable_despite_spot_change(self):
        """When a position is open, existing instruments remain fixed; spot drift does not re-select ATM."""
        strategy = build_long_straddle_strategy(target_dte=30.0)

        # Step 1: At T0 (spot = 90,000), select initial ATM instruments
        asof_t0 = AsOfView(
            decision_at_ms=self.t0,
            underlying_price_usd=Decimal("90000.0"),
            instruments=self.catalog,
        )
        initial_decision = select_legs(strategy, asof_t0)
        self.assertEqual(initial_decision.selected_instruments[0].strike_usd, Decimal("90000.0"))

        # Create mock open position holding these instruments
        class MockPosition:
            def __init__(self, instruments):
                self.instruments = instruments
                self.status = PositionStatus.OPEN

        open_pos = MockPosition(initial_decision.selected_instruments)

        # Step 2: At T1 (1 hour later), spot surges to 105,000 USD (+16.6%)
        t1 = self.t0 + 3600 * 1000
        asof_t1 = AsOfView(
            decision_at_ms=t1,
            underlying_price_usd=Decimal("105000.0"),
            instruments=self.catalog,
            open_position=open_pos,
        )

        decision_t1 = select_legs(strategy, asof_t1)
        # Verify instruments did NOT re-select to 105,000 ATM strikes; original 90,000 strike held!
        self.assertEqual(decision_t1.selected_instruments[0].strike_usd, Decimal("90000.0"))
        self.assertEqual(decision_t1.selected_instruments[1].strike_usd, Decimal("90000.0"))

        # Step 3: At T2 (2 hours later), spot plummets to 75,000 USD (-16.6%)
        t2 = self.t0 + 7200 * 1000
        asof_t2 = AsOfView(
            decision_at_ms=t2,
            underlying_price_usd=Decimal("75000.0"),
            instruments=self.catalog,
            open_position=open_pos,
        )

        decision_t2 = select_legs(strategy, asof_t2)
        # Still fixed at original 90,000 strike!
        self.assertEqual(decision_t2.selected_instruments[0].strike_usd, Decimal("90000.0"))
        self.assertEqual(decision_t2.selected_instruments[1].strike_usd, Decimal("90000.0"))

    def test_tie_break_lower_vs_higher(self):
        """Equidistant strikes obey tie_break policy ('lower' chooses lower, 'higher' chooses higher)."""
        # Spot = 95,000 USD. Strikes 90,000 and 100,000 are equidistant (distance = 5,000)
        equidistant_catalog = [
            Instrument(
                instrument_name=f"BTC-{self.exp30}-90000-C",
                option_type=OptionType.CALL,
                strike_usd=Decimal("90000.0"),
                creation_ms=self.t0 - 10 * DAY_MS,
                expiry_ms=self.exp30,
            ),
            Instrument(
                instrument_name=f"BTC-{self.exp30}-90000-P",
                option_type=OptionType.PUT,
                strike_usd=Decimal("90000.0"),
                creation_ms=self.t0 - 10 * DAY_MS,
                expiry_ms=self.exp30,
            ),
            Instrument(
                instrument_name=f"BTC-{self.exp30}-100000-C",
                option_type=OptionType.CALL,
                strike_usd=Decimal("100000.0"),
                creation_ms=self.t0 - 10 * DAY_MS,
                expiry_ms=self.exp30,
            ),
            Instrument(
                instrument_name=f"BTC-{self.exp30}-100000-P",
                option_type=OptionType.PUT,
                strike_usd=Decimal("100000.0"),
                creation_ms=self.t0 - 10 * DAY_MS,
                expiry_ms=self.exp30,
            ),
        ]

        strategy = build_long_straddle_strategy(target_dte=30.0)

        # 1. tie_break = 'lower'
        asof_lower = AsOfView(
            decision_at_ms=self.t0,
            underlying_price_usd=Decimal("95000.0"),
            instruments=equidistant_catalog,
            tie_break="lower",
        )
        decision_lower = select_legs(strategy, asof_lower)
        self.assertEqual(decision_lower.selected_instruments[0].strike_usd, Decimal("90000.0"))

        # 2. tie_break = 'higher'
        asof_higher = AsOfView(
            decision_at_ms=self.t0,
            underlying_price_usd=Decimal("95000.0"),
            instruments=equidistant_catalog,
            tie_break="higher",
        )
        decision_higher = select_legs(strategy, asof_higher)
        self.assertEqual(decision_higher.selected_instruments[0].strike_usd, Decimal("100000.0"))


if __name__ == "__main__":
    unittest.main()
