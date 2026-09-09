import unittest
from datetime import UTC, datetime

from ingestion.historical_windows import HOUR_MS, HourlyTradeBar, utc_ms
from strategies.leg_matcher import (
    GapReason,
    LegMatcherError,
    has_simultaneous_trades,
    match_hourly_trade_bars,
    match_two_plus_legs,
    to_contract_coverage_reports,
)


START = utc_ms(datetime(2026, 9, 1, 0, 0, tzinfo=UTC))


def bar(instrument_name: str, hour: int, price: float = 0.01) -> HourlyTradeBar:
    bucket_start = START + hour * HOUR_MS
    return HourlyTradeBar(
        instrument_name=instrument_name,
        bucket_start=bucket_start,
        bucket_end=bucket_start + HOUR_MS,
        open=price,
        high=price,
        low=price,
        close=price,
        trade_count=1,
        volume_contracts=1.0,
    )


class LegMatcherTests(unittest.TestCase):
    def test_matches_fully_available_two_leg_hours(self):
        result = match_hourly_trade_bars(
            {
                "call_leg": [bar("BTC-TEST-C", 0), bar("BTC-TEST-C", 1), bar("BTC-TEST-C", 2)],
                "put_leg": [bar("BTC-TEST-P", 0), bar("BTC-TEST-P", 1), bar("BTC-TEST-P", 2)],
            },
            window_start=START,
            window_end=START + 3 * HOUR_MS,
        )

        self.assertEqual(len(result.rows), 3)
        self.assertEqual(result.metrics.eligible_hours, 3)
        self.assertEqual(result.metrics.matched_hours, 3)
        self.assertEqual(result.metrics.skipped_hours, 0)
        self.assertEqual(result.metrics.liquidity_coverage_ratio, 1.0)
        self.assertEqual(result.rows[0].data_quality_label, "strict_trade_matched_no_stale_fill")

    def test_counts_asymmetric_missing_leg_without_stale_fill(self):
        result = match_hourly_trade_bars(
            {
                "call_leg": [bar("BTC-TEST-C", 0), bar("BTC-TEST-C", 1), bar("BTC-TEST-C", 2)],
                "put_leg": [bar("BTC-TEST-P", 0), bar("BTC-TEST-P", 2)],
            },
            window_start=START,
            window_end=START + 3 * HOUR_MS,
        )

        self.assertEqual([row.bucket_start for row in result.rows], [START, START + 2 * HOUR_MS])
        self.assertEqual(result.metrics.eligible_hours, 3)
        self.assertEqual(result.metrics.matched_hours, 2)
        self.assertEqual(result.metrics.skipped_hours, 1)
        self.assertEqual(result.metrics.per_leg_coverage["call_leg"].observed_hours, 3)
        self.assertEqual(result.metrics.per_leg_coverage["put_leg"].observed_hours, 2)
        self.assertEqual(result.metrics.per_leg_coverage["put_leg"].missing_hours, 1)
        self.assertAlmostEqual(result.metrics.liquidity_coverage_ratio, 2 / 3)

    def test_empty_inputs_report_all_hours_skipped(self):
        result = match_hourly_trade_bars(
            {"call_leg": [], "put_leg": []},
            window_start=START,
            window_end=START + 2 * HOUR_MS,
            expected_instruments={"call_leg": "BTC-TEST-C", "put_leg": "BTC-TEST-P"},
        )

        self.assertEqual(result.rows, ())
        self.assertEqual(result.metrics.eligible_hours, 2)
        self.assertEqual(result.metrics.matched_hours, 0)
        self.assertEqual(result.metrics.skipped_hours, 2)
        self.assertEqual(result.metrics.per_leg_coverage["call_leg"].missing_hours, 2)
        self.assertEqual(result.metrics.per_leg_coverage["call_leg"].instrument_name, "BTC-TEST-C")

    def test_rejects_duplicate_bars_for_same_leg_hour(self):
        with self.assertRaisesRegex(LegMatcherError, "duplicate"):
            match_hourly_trade_bars(
                {"call_leg": [bar("BTC-TEST-C", 0), bar("BTC-TEST-C", 0)], "put_leg": [bar("BTC-TEST-P", 0)]},
                window_start=START,
                window_end=START + HOUR_MS,
            )

    def test_out_of_window_bars_do_not_count(self):
        result = match_hourly_trade_bars(
            {
                "call_leg": [bar("BTC-TEST-C", -1), bar("BTC-TEST-C", 0), bar("BTC-TEST-C", 2)],
                "put_leg": [bar("BTC-TEST-P", 0), bar("BTC-TEST-P", 2)],
            },
            window_start=START,
            window_end=START + 2 * HOUR_MS,
        )

        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0].bucket_start, START)
        self.assertEqual(result.metrics.per_leg_coverage["call_leg"].observed_hours, 1)
        self.assertEqual(result.metrics.per_leg_coverage["put_leg"].observed_hours, 1)

    def test_rejects_misaligned_buckets(self):
        bad = HourlyTradeBar(
            instrument_name="BTC-TEST-C",
            bucket_start=START + 1,
            bucket_end=START + HOUR_MS + 1,
            open=0.01,
            high=0.01,
            low=0.01,
            close=0.01,
            trade_count=1,
            volume_contracts=1.0,
        )

        with self.assertRaisesRegex(LegMatcherError, "misaligned bucket_start"):
            match_hourly_trade_bars(
                {"call_leg": [bad], "put_leg": [bar("BTC-TEST-P", 0)]},
                window_start=START,
                window_end=START + HOUR_MS,
            )

    def test_supports_three_legs(self):
        result = match_hourly_trade_bars(
            {
                "call_leg": [bar("BTC-TEST-C", 0), bar("BTC-TEST-C", 1)],
                "put_leg": [bar("BTC-TEST-P", 0), bar("BTC-TEST-P", 1)],
                "hedge_leg": [bar("BTC-HEDGE-C", 1)],
            },
            window_start=START,
            window_end=START + 2 * HOUR_MS,
        )

        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0].bucket_start, START + HOUR_MS)
        self.assertEqual(set(result.rows[0].bars_by_leg), {"call_leg", "put_leg", "hedge_leg"})
        self.assertEqual(result.metrics.eligible_hours, 2)
        self.assertEqual(result.metrics.matched_hours, 1)
        self.assertEqual(result.metrics.per_leg_coverage["hedge_leg"].missing_hours, 1)

    def test_rejects_instrument_mismatch_for_leg(self):
        with self.assertRaisesRegex(LegMatcherError, "instrument mismatch"):
            match_hourly_trade_bars(
                {"call_leg": [bar("BTC-OTHER-C", 0)], "put_leg": [bar("BTC-TEST-P", 0)]},
                window_start=START,
                window_end=START + HOUR_MS,
                expected_instruments={"call_leg": "BTC-TEST-C"},
            )

    def test_single_leg_matching(self):
        # Single leg matching must succeed and produce full coverage metrics
        result = match_hourly_trade_bars(
            {"single_call": [bar("BTC-TEST-C", 0), bar("BTC-TEST-C", 1), bar("BTC-TEST-C", 2)]},
            window_start=START,
            window_end=START + 3 * HOUR_MS,
            min_legs=1,
        )
        self.assertEqual(len(result.rows), 3)
        self.assertEqual(result.metrics.eligible_hours, 3)
        self.assertEqual(result.metrics.matched_hours, 3)
        self.assertEqual(result.metrics.skipped_hours, 0)
        self.assertEqual(result.metrics.liquidity_coverage_ratio, 1.0)
        self.assertEqual(result.metrics.per_leg_coverage["single_call"].observed_hours, 3)

    def test_four_leg_matching(self):
        # 4 legs (Iron Condor: c1, c2, p1, p2)
        # Hour 0: all 4 present -> matched
        # Hour 1: p2 missing -> skipped
        # Hour 2: all 4 present -> matched
        result = match_hourly_trade_bars(
            {
                "call_short": [bar("BTC-C1", 0), bar("BTC-C1", 1), bar("BTC-C1", 2)],
                "call_long":  [bar("BTC-C2", 0), bar("BTC-C2", 1), bar("BTC-C2", 2)],
                "put_short":  [bar("BTC-P1", 0), bar("BTC-P1", 1), bar("BTC-P1", 2)],
                "put_long":   [bar("BTC-P2", 0), bar("BTC-P2", 2)],  # Missing at hour 1
            },
            window_start=START,
            window_end=START + 3 * HOUR_MS,
            min_legs=1,
        )
        self.assertEqual(len(result.rows), 2)
        self.assertEqual([r.bucket_start for r in result.rows], [START, START + 2 * HOUR_MS])
        self.assertEqual(result.metrics.eligible_hours, 3)
        self.assertEqual(result.metrics.matched_hours, 2)
        self.assertEqual(result.metrics.skipped_hours, 1)
        self.assertEqual(result.metrics.per_leg_coverage["put_long"].missing_hours, 1)

    def test_empty_hour_calendar_grid_and_missing_events(self):
        # Empty hour where no legs traded must not be silently dropped:
        # it is tracked in calendar_grid and missing_events so open positions are not lost.
        result = match_hourly_trade_bars(
            {
                "call_leg": [bar("BTC-TEST-C", 0), bar("BTC-TEST-C", 2)],
                "put_leg":  [bar("BTC-TEST-P", 0), bar("BTC-TEST-P", 2)],
            },
            window_start=START,
            window_end=START + 3 * HOUR_MS,
        )
        # 3 calendar buckets total
        self.assertEqual(len(result.calendar_grid), 3)
        # Bucket 0: matched
        self.assertTrue(result.calendar_grid[0].is_matched)
        # Bucket 1: empty hour (missing both legs)
        self.assertFalse(result.calendar_grid[1].is_matched)
        self.assertEqual(set(result.calendar_grid[1].missing_legs), {"call_leg", "put_leg"})
        # Bucket 2: matched
        self.assertTrue(result.calendar_grid[2].is_matched)

        # Missing event recorded
        self.assertEqual(len(result.missing_events), 1)
        self.assertEqual(result.missing_events[0].bucket_start, START + HOUR_MS)

    def test_independent_denominator_per_leg(self):
        # Leg A: 3 observed hours. Leg B: 1 observed hour.
        # Eligible denominator is 4 for both, but observed and coverage_ratio are strictly independent.
        result = match_hourly_trade_bars(
            {
                "leg_a": [bar("BTC-A", 0), bar("BTC-A", 1), bar("BTC-A", 2)],
                "leg_b": [bar("BTC-B", 0)],
            },
            window_start=START,
            window_end=START + 4 * HOUR_MS,
        )
        cov_a = result.metrics.per_leg_coverage["leg_a"]
        cov_b = result.metrics.per_leg_coverage["leg_b"]

        self.assertEqual(cov_a.eligible_hours, 4)
        self.assertEqual(cov_a.observed_hours, 3)
        self.assertEqual(cov_a.missing_hours, 1)
        self.assertEqual(cov_a.coverage_ratio, 0.75)

        self.assertEqual(cov_b.eligible_hours, 4)
        self.assertEqual(cov_b.observed_hours, 1)
        self.assertEqual(cov_b.missing_hours, 3)
        self.assertEqual(cov_b.coverage_ratio, 0.25)

    def test_distinct_trade_times_within_hour_not_labeled_simultaneous_quotes(self):
        # Leg 1 traded at minute 10, Leg 2 traded at minute 50
        bar1 = HourlyTradeBar(
            instrument_name="BTC-C",
            bucket_start=START,
            bucket_end=START + HOUR_MS,
            open=0.01, high=0.01, low=0.01, close=0.01,
            trade_count=1, volume_contracts=1.0,
            first_trade_at=START + 600_000,
            last_trade_at=START + 600_000,  # Minute 10
        )
        bar2 = HourlyTradeBar(
            instrument_name="BTC-P",
            bucket_start=START,
            bucket_end=START + HOUR_MS,
            open=0.02, high=0.02, low=0.02, close=0.02,
            trade_count=1, volume_contracts=1.0,
            first_trade_at=START + 3_000_000,
            last_trade_at=START + 3_000_000,  # Minute 50
        )
        result = match_hourly_trade_bars(
            {"call_leg": [bar1], "put_leg": [bar2]},
            window_start=START,
            window_end=START + HOUR_MS,
        )
        self.assertEqual(len(result.rows), 1)
        row = result.rows[0]
        # Invariant: Distinct trade timestamps within the hour must NOT be labeled simultaneous quotes!
        self.assertFalse(has_simultaneous_trades(row))

    def test_legacy_two_plus_matcher_compatibility(self):
        # match_two_plus_legs strictly enforces 2+ legs
        with self.assertRaisesRegex(LegMatcherError, "at least two legs are required"):
            match_two_plus_legs(
                {"single_call": [bar("BTC-TEST-C", 0)]},
                window_start=START,
                window_end=START + HOUR_MS,
            )

    def test_to_contract_coverage_reports_conversion(self):
        result = match_hourly_trade_bars(
            {
                "call_leg": [bar("BTC-TEST-C", 0)],
                "put_leg": [bar("BTC-TEST-P", 0)],
            },
            window_start=START,
            window_end=START + 2 * HOUR_MS,
            expected_instruments={"call_leg": "BTC-TEST-C", "put_leg": "BTC-TEST-P"},
        )
        reports = to_contract_coverage_reports(result, START, START + 2 * HOUR_MS)
        self.assertEqual(len(reports), 2)
        call_rep = next(r for r in reports if r.instrument_name == "BTC-TEST-C")
        self.assertEqual(call_rep.expected_intervals, 2)
        self.assertEqual(call_rep.observed_intervals, 1)
        self.assertEqual(call_rep.missing_intervals, 1)
        self.assertEqual(call_rep.status, "incomplete")


if __name__ == "__main__":
    unittest.main()
