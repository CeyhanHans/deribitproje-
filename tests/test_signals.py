"""Acceptance and unit tests for pure signal and calendar engine (evaluate_signals)."""

import math
from datetime import datetime, timezone
from decimal import Decimal
import unittest

from core.contracts import (
    Currency,
    Instrument,
    OptionType,
    PortfolioState,
    Position,
    PositionStatus,
    ReasonCode,
    RunConfig,
    Side,
    Signal,
    SignalEvaluatorProtocol,
)
from strategies.selection import make_sample_catalog_fixture
from strategies.signals import (
    HistoryAsOf,
    SignalEvaluationError,
    UnsupportedFilterError,
    compute_realized_volatility,
    compute_underlying_return,
    evaluate_signals,
)
from strategies.strategy_schema import (
    EntryRule,
    ExitRule,
    ExpirySelectorType,
    ResultMetric,
    RunConfigDefinition,
    Selector,
    StrategyDefinition,
    StrategyLeg,
    StrikeSelectorType,
)

DAY_MS = 86_400_000
HOUR_MS = 3_600_000


class MockBar:
    """Mock OHLC bar for testing."""
    def __init__(self, bucket_start: int, bucket_end: int, close: float, available_at: int | None = None):
        self.bucket_start = bucket_start
        self.bucket_end = bucket_end
        self.close = close
        self.available_at = available_at if available_at is not None else bucket_end


def make_sample_bars(start_ms: int, count: int, base_price: float = 65000.0, step_ms: int = HOUR_MS) -> list[MockBar]:
    bars = []
    price = base_price
    for i in range(count):
        b_start = start_ms + i * step_ms
        b_end = b_start + step_ms
        # deterministic slight trend with oscillations
        price += math.sin(i * 0.2) * 150.0 + 20.0
        bars.append(MockBar(b_start, b_end, price, b_end))
    return bars


class SignalEngineAcceptanceTests(unittest.TestCase):
    def setUp(self):
        # 2024-03-01T00:00:00Z (Friday)
        self.t0 = 1709251200000
        self.strategy_name = "test_signal_straddle"

        self.strategy = StrategyDefinition(
            name=self.strategy_name,
            version="1.0",
            legs=(
                StrategyLeg("call", "call", "long", 1.0, Selector("atm", 0), Selector("nearest_days_to_expiry", 30)),
                StrategyLeg("put", "put", "long", 1.0, Selector("atm", 0), Selector("nearest_days_to_expiry", 30)),
            ),
            entry_rules=(EntryRule("enter_when_chain_has_required_legs", {}),),
            exit_rules=(ExitRule("expiry_exit", "at_expiry"),),
            fee_spread_model="deribit_inverse_option_bid_ask",
            result_metrics=(
                ResultMetric("trade_count", "trades"),
                ResultMetric("net_pnl_usd", "pnl"),
                ResultMetric("max_drawdown_usd", "dd"),
                ResultMetric("win_rate", "win"),
            ),
        )

        self.empty_portfolio = PortfolioState(
            timestamp_ms=self.t0,
            cash_balance_btc=Decimal("10.0"),
            reserved_balance_btc=Decimal("0"),
            open_positions=(),
            closed_positions=(),
            ledger=(),
        )

    def test_protocol_conformance(self):
        """evaluate_signals complies with locked SignalEvaluatorProtocol."""
        self.assertTrue(isinstance(evaluate_signals, SignalEvaluatorProtocol))

    def test_prefix_invariance(self):
        """Future price changes do not change signals produced at earlier timestamps."""
        # 1. Baseline sequence of 60 hourly bars
        baseline_bars = make_sample_bars(self.t0, 60, base_price=65000.0)

        # Evaluate signals across time steps T=20h, 25h, 30h
        t_evals = [self.t0 + 20 * HOUR_MS, self.t0 + 25 * HOUR_MS, self.t0 + 30 * HOUR_MS]
        baseline_signals = []

        for t in t_evals:
            completed = [b for b in baseline_bars if b.bucket_end <= t]
            asof = HistoryAsOf(decision_at_ms=t, completed_bars=completed)
            sigs = evaluate_signals(self.strategy, asof, self.empty_portfolio)
            baseline_signals.append(sigs)

        # 2. Mutate future bars (bars 35 to 60) drastically
        future_mutated_bars = list(baseline_bars[:35])
        for i, b in enumerate(baseline_bars[35:]):
            # Radical spike in future prices
            future_mutated_bars.append(MockBar(b.bucket_start, b.bucket_end, b.close * 2.5 + 50000, b.bucket_end))

        # Re-evaluate signals at T=20h, 25h, 30h with future-mutated dataset
        for idx, t in enumerate(t_evals):
            completed = [b for b in future_mutated_bars if b.bucket_end <= t]
            asof_mutated = HistoryAsOf(decision_at_ms=t, completed_bars=completed)
            sigs_mutated = evaluate_signals(self.strategy, asof_mutated, self.empty_portfolio)

            # Prefix invariance: Signals MUST be identical
            self.assertEqual(len(sigs_mutated), len(baseline_signals[idx]))
            if baseline_signals[idx]:
                self.assertEqual(sigs_mutated[0].signal_id, baseline_signals[idx][0].signal_id)
                self.assertEqual(sigs_mutated[0].decision_at_ms, baseline_signals[idx][0].decision_at_ms)

    def test_incomplete_bar_excluded(self):
        """An incomplete bar (bucket_end > T) is strictly excluded from historical calculations."""
        t_eval = self.t0 + 10 * HOUR_MS

        # Completed bars: 0 to 10
        completed_bars = make_sample_bars(self.t0, 10, base_price=60000.0)

        # Incomplete bar (ongoing bar from 10h to 11h, bucket_end > t_eval) with absurd outlier price
        ongoing_bar = MockBar(t_eval, t_eval + HOUR_MS, close=999_999_999.0, available_at=t_eval + HOUR_MS)

        all_bars_mixed = completed_bars + [ongoing_bar]

        # Enable realized volatility filter
        strat_with_vol = StrategyDefinition(
            name="vol_filter_strat",
            version="1.0",
            legs=self.strategy.legs,
            entry_rules=(
                EntryRule(
                    "periodic_calendar_entry",
                    {"volatility_filter_enabled": True, "warmup_bars": 8, "max_realized_vol": 1.50},
                ),
            ),
            exit_rules=self.strategy.exit_rules,
            fee_spread_model="deribit_inverse_option_bid_ask",
            result_metrics=self.strategy.result_metrics,
        )

        asof = HistoryAsOf(decision_at_ms=t_eval, completed_bars=all_bars_mixed)
        signals = evaluate_signals(strat_with_vol, asof, self.empty_portfolio)

        # If incomplete bar was included, realized volatility would be enormous (> 100.0) and exceed max_realized_vol!
        # Because incomplete bar was excluded, realized volatility is moderate (~0.15 - 0.30) and signal is emitted!
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].reason_code, ReasonCode.ENTRY_SIGNAL)

    def test_warmup_insufficient(self):
        """When warmup window is insufficient, signal evaluation blocks entry until warmup is reached."""
        strat_warmup = StrategyDefinition(
            name="warmup_strat",
            version="1.0",
            legs=self.strategy.legs,
            entry_rules=(
                EntryRule(
                    "periodic_calendar_entry",
                    {"volatility_filter_enabled": True, "warmup_bars": 24},
                ),
            ),
            exit_rules=self.strategy.exit_rules,
            fee_spread_model="deribit_inverse_option_bid_ask",
            result_metrics=self.strategy.result_metrics,
        )

        # 1. Only 10 completed bars < 24 required
        bars_10 = make_sample_bars(self.t0, 10)
        t_eval = self.t0 + 10 * HOUR_MS
        asof_short = HistoryAsOf(decision_at_ms=t_eval, completed_bars=bars_10)

        sigs_short = evaluate_signals(strat_warmup, asof_short, self.empty_portfolio)
        self.assertEqual(sigs_short, ())  # Gated by insufficient warmup

        # 2. 25 completed bars >= 24 required
        bars_25 = make_sample_bars(self.t0, 25)
        t_eval_25 = self.t0 + 25 * HOUR_MS
        asof_full = HistoryAsOf(decision_at_ms=t_eval_25, completed_bars=bars_25)

        sigs_full = evaluate_signals(strat_warmup, asof_full, self.empty_portfolio)
        self.assertEqual(len(sigs_full), 1)  # Warmup satisfied, signal emitted

    def test_weekly_utc_schedule(self):
        """Weekly UTC schedule fires strictly on target weekday and target UTC hour."""
        # 2024-03-01T00:00:00Z is Friday
        strat_weekly = StrategyDefinition(
            name="weekly_strat",
            version="1.0",
            legs=self.strategy.legs,
            entry_rules=(
                EntryRule(
                    "weekly_calendar_entry",
                    {"schedule_type": "weekly", "weekday": 4, "hour_utc": 8},  # Friday 08:00 UTC
                ),
            ),
            exit_rules=self.strategy.exit_rules,
            fee_spread_model="deribit_inverse_option_bid_ask",
            result_metrics=self.strategy.result_metrics,
        )

        # 1. Friday 08:00 UTC -> FIRES
        t_fri_08 = self.t0 + 8 * HOUR_MS
        self.assertEqual(datetime.fromtimestamp(t_fri_08 / 1000, tz=timezone.utc).weekday(), 4)
        self.assertEqual(datetime.fromtimestamp(t_fri_08 / 1000, tz=timezone.utc).hour, 8)
        asof_fri_08 = HistoryAsOf(decision_at_ms=t_fri_08, completed_bars=make_sample_bars(self.t0, 8))
        sigs_fri_08 = evaluate_signals(strat_weekly, asof_fri_08, self.empty_portfolio)
        self.assertEqual(len(sigs_fri_08), 1)

        # 2. Friday 09:00 UTC -> BLOCKED (hour mismatch)
        t_fri_09 = self.t0 + 9 * HOUR_MS
        asof_fri_09 = HistoryAsOf(decision_at_ms=t_fri_09, completed_bars=make_sample_bars(self.t0, 9))
        sigs_fri_09 = evaluate_signals(strat_weekly, asof_fri_09, self.empty_portfolio)
        self.assertEqual(sigs_fri_09, ())

        # 3. Saturday 08:00 UTC -> BLOCKED (weekday mismatch)
        t_sat_08 = self.t0 + 32 * HOUR_MS
        asof_sat_08 = HistoryAsOf(decision_at_ms=t_sat_08, completed_bars=make_sample_bars(self.t0, 32))
        sigs_sat_08 = evaluate_signals(strat_weekly, asof_sat_08, self.empty_portfolio)
        self.assertEqual(sigs_sat_08, ())

    def test_cooldown_gate(self):
        """Cooldown period blocks new entry signals until specified elapsed time has passed."""
        strat_cooldown = StrategyDefinition(
            name="cooldown_strat",
            version="1.0",
            legs=self.strategy.legs,
            entry_rules=(
                EntryRule(
                    "enter_when_chain_has_required_legs",
                    {"cooldown_hours": 24.0},
                ),
            ),
            exit_rules=self.strategy.exit_rules,
            fee_spread_model="deribit_inverse_option_bid_ask",
            result_metrics=self.strategy.result_metrics,
        )

        closed_pos = Position(
            position_id="pos_closed_1",
            strategy_name=self.strategy_name,
            status=PositionStatus.CLOSED,
            legs=(
                Instrument("BTC-EXP-90000-C", OptionType.CALL, Decimal("90000"), self.t0 - 1000, self.t0 + 10000),
            ),
            leg_quantities=(Decimal("1.0"),),
            opened_at_ms=self.t0 - 10 * HOUR_MS,
            closed_at_ms=self.t0,  # closed at T0
        )

        portfolio_after_close = PortfolioState(
            timestamp_ms=self.t0,
            cash_balance_btc=Decimal("10.0"),
            reserved_balance_btc=Decimal("0"),
            open_positions=(),
            closed_positions=(closed_pos,),
            ledger=(),
        )

        # 1. At T0 + 12h (< 24h cooldown) -> BLOCKED
        t_12h = self.t0 + 12 * HOUR_MS
        asof_12h = HistoryAsOf(decision_at_ms=t_12h, completed_bars=make_sample_bars(self.t0, 12))
        sigs_12h = evaluate_signals(strat_cooldown, asof_12h, portfolio_after_close)
        self.assertEqual(sigs_12h, ())

        # 2. At T0 + 23h (< 24h cooldown) -> BLOCKED
        t_23h = self.t0 + 23 * HOUR_MS
        asof_23h = HistoryAsOf(decision_at_ms=t_23h, completed_bars=make_sample_bars(self.t0, 23))
        sigs_23h = evaluate_signals(strat_cooldown, asof_23h, portfolio_after_close)
        self.assertEqual(sigs_23h, ())

        # 3. At T0 + 25h (> 24h cooldown) -> COOLDOWN ELAPSED, FIRES!
        t_25h = self.t0 + 25 * HOUR_MS
        asof_25h = HistoryAsOf(decision_at_ms=t_25h, completed_bars=make_sample_bars(self.t0, 25))
        sigs_25h = evaluate_signals(strat_cooldown, asof_25h, portfolio_after_close)
        self.assertEqual(len(sigs_25h), 1)

    def test_concurrent_position_limit(self):
        """Max open positions gate strictly limits concurrent open positions."""
        config_max_1 = RunConfigDefinition(
            strategy_name=self.strategy_name,
            start_time="2024-03-01T00:00:00Z",
            end_time="2025-06-01T00:00:00Z",
            start_ms=self.t0,
            end_ms=self.t0 + 100 * DAY_MS,
            max_open_positions=1,
            strategy=self.strategy,
        )

        open_pos = Position(
            position_id="pos_open_1",
            strategy_name=self.strategy_name,
            status=PositionStatus.OPEN,
            legs=(
                Instrument("BTC-EXP-90000-C", OptionType.CALL, Decimal("90000"), self.t0 - 1000, self.t0 + 10000),
            ),
            leg_quantities=(Decimal("1.0"),),
            opened_at_ms=self.t0 - 5 * HOUR_MS,
        )

        portfolio_with_1_open = PortfolioState(
            timestamp_ms=self.t0,
            cash_balance_btc=Decimal("10.0"),
            reserved_balance_btc=Decimal("0"),
            open_positions=(open_pos,),
            closed_positions=(),
            ledger=(),
        )

        asof = HistoryAsOf(decision_at_ms=self.t0, completed_bars=make_sample_bars(self.t0, 5))

        # With 1 open and max_open=1 -> BLOCKED
        sigs = evaluate_signals(config_max_1, asof, portfolio_with_1_open)
        self.assertEqual(sigs, ())

        # With 0 open -> FIRES
        sigs_empty = evaluate_signals(config_max_1, asof, self.empty_portfolio)
        self.assertEqual(len(sigs_empty), 1)

        # With max_open=2 and 1 open -> FIRES 2nd position
        config_max_2 = RunConfigDefinition(
            strategy_name=self.strategy_name,
            start_time="2024-03-01T00:00:00Z",
            end_time="2025-06-01T00:00:00Z",
            start_ms=self.t0,
            end_ms=self.t0 + 100 * DAY_MS,
            max_open_positions=2,
            strategy=self.strategy,
        )
        sigs_2nd = evaluate_signals(config_max_2, asof, portfolio_with_1_open)
        self.assertEqual(len(sigs_2nd), 1)

    def test_same_timestamp_no_duplicate_signal(self):
        """Repeated evaluations at the exact same timestamp never create duplicate signals."""
        asof = HistoryAsOf(decision_at_ms=self.t0, completed_bars=make_sample_bars(self.t0, 5))

        # 1. Call twice on empty portfolio -> identical deterministic signal
        sigs1 = evaluate_signals(self.strategy, asof, self.empty_portfolio)
        sigs2 = evaluate_signals(self.strategy, asof, self.empty_portfolio)

        self.assertEqual(len(sigs1), 1)
        self.assertEqual(len(sigs2), 1)
        self.assertEqual(sigs1[0].signal_id, sigs2[0].signal_id)
        self.assertEqual(sigs1[0].decision_at_ms, sigs2[0].decision_at_ms)

        # 2. If portfolio already opened a position at this exact decision_at_ms, suppress duplicate!
        pos_same_time = Position(
            position_id="pos_same_t0",
            strategy_name=self.strategy_name,
            status=PositionStatus.OPEN,
            legs=(
                Instrument("BTC-EXP-90000-C", OptionType.CALL, Decimal("90000"), self.t0 - 1000, self.t0 + 10000),
            ),
            leg_quantities=(Decimal("1.0"),),
            opened_at_ms=self.t0,  # Opened at exact t0
        )
        portfolio_duplicate = PortfolioState(
            timestamp_ms=self.t0,
            cash_balance_btc=Decimal("10.0"),
            reserved_balance_btc=Decimal("0"),
            open_positions=(pos_same_time,),
            closed_positions=(),
            ledger=(),
        )
        sigs_dup = evaluate_signals(self.strategy, asof, portfolio_duplicate)
        self.assertEqual(sigs_dup, ())

    def test_rejects_unsupported_filters(self):
        """Filters without historical data in V1 (IV, Open Interest) raise UnsupportedFilterError."""
        strat_iv = StrategyDefinition(
            name="iv_filter_strat",
            version="1.0",
            legs=self.strategy.legs,
            entry_rules=(
                EntryRule(
                    "signal_rule_entry",
                    {"implied_volatility": 0.65},
                ),
            ),
            exit_rules=self.strategy.exit_rules,
            fee_spread_model="deribit_inverse_option_bid_ask",
            result_metrics=self.strategy.result_metrics,
        )

        asof = HistoryAsOf(decision_at_ms=self.t0, completed_bars=make_sample_bars(self.t0, 5))
        with self.assertRaisesRegex(UnsupportedFilterError, "unsupported signal filter.*implied_volatility"):
            evaluate_signals(strat_iv, asof, self.empty_portfolio)

        strat_oi = StrategyDefinition(
            name="oi_filter_strat",
            version="1.0",
            legs=self.strategy.legs,
            entry_rules=(
                EntryRule(
                    "signal_rule_entry",
                    {"open_interest": 5000},
                ),
            ),
            exit_rules=self.strategy.exit_rules,
            fee_spread_model="deribit_inverse_option_bid_ask",
            result_metrics=self.strategy.result_metrics,
        )
        with self.assertRaisesRegex(UnsupportedFilterError, "unsupported signal filter.*open_interest"):
            evaluate_signals(strat_oi, asof, self.empty_portfolio)


if __name__ == "__main__":
    unittest.main()
