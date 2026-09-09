import unittest
from datetime import UTC, datetime

from ingestion.historical_windows import HOUR_MS, HourlyTradeBar, utc_ms
from strategies.leg_matcher import LegMatcherError, match_hourly_trade_bars


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


if __name__ == "__main__":
    unittest.main()
