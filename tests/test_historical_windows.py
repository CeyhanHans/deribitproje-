import json
import tempfile
import unittest
from datetime import UTC, datetime
import math
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from core.contracts import PriceBasis, PriceObservation
from ingestion.historical_windows import (
    DAY_MS,
    HOUR_MS,
    HistoricalWindow,
    HistoricalWindowError,
    HourlyTradeBar,
    aggregate_hourly_trade_bars,
    aggregate_trade_bars,
    build_default_windows,
    build_trade_plan_for_window,
    clean_and_dedup_trades,
    fetch_hourly_trade_bars,
    trade_bar_to_price_observation,
    utc_ms,
    write_window_bars_json,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class HistoricalWindowsTests(unittest.TestCase):
    def test_default_windows_are_independent_and_hourly(self):
        end = datetime(2026, 9, 9, 0, 0, tzinfo=UTC)
        windows = build_default_windows(end_time=end)

        self.assertEqual([window.days for window in windows], [30, 60, 90, 120, 360])
        self.assertEqual([window.label for window in windows], ["30d", "60d", "90d", "120d", "360d"])
        self.assertTrue(all(window.end_timestamp == utc_ms(end) for window in windows))
        self.assertTrue(all(window.resolution == "1h" for window in windows))
        self.assertEqual(windows[0].end_timestamp - windows[0].start_timestamp, 30 * 24 * HOUR_MS)

    def test_window_validation_rejects_non_hourly_resolution(self):
        with self.assertRaisesRegex(HistoricalWindowError, "only 1h"):
            HistoricalWindow("bad", 30, 1, 2, resolution="5m").validate()

    def test_build_trade_plan_uses_time_range(self):
        window = HistoricalWindow("30d", 30, 1000, 2000)
        plan = build_trade_plan_for_window("BTC-27JUN25-100000-C", window, count=20, max_pages=3)

        self.assertEqual(plan.mode, "time")
        self.assertEqual(plan.instrument_name, "BTC-27JUN25-100000-C")
        self.assertEqual(plan.start_timestamp, 1000)
        self.assertEqual(plan.end_timestamp, 2000)
        self.assertEqual(plan.count, 20)
        self.assertEqual(plan.max_pages, 3)

    def test_aggregates_trades_into_hourly_ohlcv_bars(self):
        start = utc_ms(datetime(2026, 9, 9, 1, 0, tzinfo=UTC))
        bars = aggregate_hourly_trade_bars(
            [
                {"instrument_name": "BTC-TEST-C", "timestamp": start + 2_000, "price": 0.011, "amount": 2},
                {"instrument_name": "BTC-TEST-C", "timestamp": start + 1_000, "price": 0.010, "amount": 3},
                {"instrument_name": "BTC-TEST-C", "timestamp": start + HOUR_MS + 1_000, "price": 0.012, "amount": 5},
                {"instrument_name": "BTC-OTHER-C", "timestamp": start + 1_000, "price": 0.5, "amount": 100},
            ],
            instrument_name="BTC-TEST-C",
        )

        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].bucket_start, start)
        self.assertEqual(bars[0].open, 0.010)
        self.assertEqual(bars[0].high, 0.011)
        self.assertEqual(bars[0].low, 0.010)
        self.assertEqual(bars[0].close, 0.011)
        self.assertEqual(bars[0].trade_count, 2)
        self.assertEqual(bars[0].volume_contracts, 5)
        self.assertEqual(bars[0].price_basis, "trade_price_not_historical_bid_ask")

    def test_fetch_hourly_trade_bars_marks_download_summary(self):
        window = HistoricalWindow("30d", 30, 1000, 5000)
        payload = {
            "result": {
                "trades": [
                    {
                        "instrument_name": "BTC-TEST-C",
                        "timestamp": 2000,
                        "trade_seq": 1,
                        "price": 0.01,
                        "amount": 2,
                    }
                ],
                "has_more": False,
            }
        }

        with patch("ingestion.deribit_history.urlopen", return_value=FakeResponse(payload)):
            result = fetch_hourly_trade_bars("BTC-TEST-C", window, count=1, max_pages=1)

        self.assertEqual(result["window"]["label"], "30d")
        self.assertEqual(result["download_summary"]["trade_count"], 1)
        self.assertEqual(result["bar_count"], 1)
        self.assertEqual(result["bars"][0]["price_basis"], "trade_price_not_historical_bid_ask")

    def test_write_window_bars_json(self):
        payload = {"bar_count": 0, "bars": []}
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "bars.json"
            write_window_bars_json(output, payload)
            saved = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(saved, payload)

    def test_sequence_ordering_with_equal_timestamps_and_disordered_trades(self):
        # Disordered trades arriving with both different timestamps and same timestamp with different sequence numbers
        start = utc_ms(datetime(2026, 9, 9, 1, 0, tzinfo=UTC))
        raw_trades = [
            {"instrument_name": "BTC-TEST-C", "timestamp": start + 500, "trade_seq": 3, "price": 0.013, "amount": 1},
            {"instrument_name": "BTC-TEST-C", "timestamp": start + 100, "trade_seq": 1, "price": 0.010, "amount": 2},
            {"instrument_name": "BTC-TEST-C", "timestamp": start + 500, "trade_seq": 2, "price": 0.012, "amount": 1},
        ]
        bars = aggregate_hourly_trade_bars(raw_trades, instrument_name="BTC-TEST-C")
        self.assertEqual(len(bars), 1)
        bar = bars[0]
        # Open should be trade with timestamp start+100 (price 0.010)
        self.assertEqual(bar.open, 0.010)
        # Close should be trade with timestamp start+500, trade_seq=3 (price 0.013)
        self.assertEqual(bar.close, 0.013)
        self.assertEqual(bar.first_trade_at, start + 100)
        self.assertEqual(bar.last_trade_at, start + 500)
        self.assertEqual(bar.available_at, start + HOUR_MS)

    def test_deduplication_by_trade_id_preserves_volume_accuracy(self):
        start = utc_ms(datetime(2026, 9, 9, 1, 0, tzinfo=UTC))
        raw_trades = [
            {"instrument_name": "BTC-TEST-C", "timestamp": start + 100, "trade_id": "T1", "price": 0.010, "amount": 5.0},
            {"instrument_name": "BTC-TEST-C", "timestamp": start + 100, "trade_id": "T1", "price": 0.010, "amount": 5.0}, # Exact duplicate!
            {"instrument_name": "BTC-TEST-C", "timestamp": start + 200, "trade_id": "T2", "price": 0.011, "amount": 3.0},
        ]
        bars = aggregate_hourly_trade_bars(raw_trades, instrument_name="BTC-TEST-C")
        self.assertEqual(len(bars), 1)
        bar = bars[0]
        # Volume must be strictly 8.0 (5.0 + 3.0), not 13.0!
        self.assertEqual(bar.volume_contracts, 8.0)
        self.assertEqual(bar.trade_count, 2)

    def test_rejects_zero_or_negative_amount_and_nan_or_inf(self):
        start = utc_ms(datetime(2026, 9, 9, 1, 0, tzinfo=UTC))

        # Zero amount
        with self.assertRaisesRegex(HistoricalWindowError, "strictly positive"):
            clean_and_dedup_trades([{"timestamp": start, "price": 0.01, "amount": 0.0}])

        # Negative amount
        with self.assertRaisesRegex(HistoricalWindowError, "strictly positive"):
            clean_and_dedup_trades([{"timestamp": start, "price": 0.01, "amount": -1.0}])

        # Negative price
        with self.assertRaisesRegex(HistoricalWindowError, "strictly positive"):
            clean_and_dedup_trades([{"timestamp": start, "price": -0.01, "amount": 1.0}])

        # NaN price
        with self.assertRaisesRegex(HistoricalWindowError, "NaN or Infinity"):
            clean_and_dedup_trades([{"timestamp": start, "price": float("nan"), "amount": 1.0}])

        # Inf amount
        with self.assertRaisesRegex(HistoricalWindowError, "NaN or Infinity"):
            clean_and_dedup_trades([{"timestamp": start, "price": 0.01, "amount": float("inf")}])

    def test_daily_resolution_aggregation(self):
        start = utc_ms(datetime(2026, 9, 9, 0, 0, tzinfo=UTC))
        raw_trades = [
            {"instrument_name": "BTC-TEST-C", "timestamp": start + 3600_000, "price": 0.010, "amount": 2.0},
            {"instrument_name": "BTC-TEST-C", "timestamp": start + 7200_000, "price": 0.015, "amount": 3.0},
            {"instrument_name": "BTC-TEST-C", "timestamp": start + 80_000_000, "price": 0.012, "amount": 1.0},
        ]
        bars = aggregate_trade_bars(raw_trades, instrument_name="BTC-TEST-C", resolution="1d")
        self.assertEqual(len(bars), 1)
        bar = bars[0]
        self.assertEqual(bar.bucket_start, start)
        self.assertEqual(bar.bucket_end, start + DAY_MS)
        self.assertEqual(bar.open, 0.010)
        self.assertEqual(bar.high, 0.015)
        self.assertEqual(bar.close, 0.012)
        self.assertEqual(bar.volume_contracts, 6.0)
        self.assertEqual(bar.resolution, "1d")

    def test_trade_bar_to_price_observation_adapter(self):
        start = utc_ms(datetime(2026, 9, 9, 1, 0, tzinfo=UTC))
        bar = HourlyTradeBar(
            instrument_name="BTC-TEST-C",
            bucket_start=start,
            bucket_end=start + HOUR_MS,
            open=0.010,
            high=0.012,
            low=0.009,
            close=0.011,
            trade_count=3,
            volume_contracts=10.0,
            first_trade_at=start + 100,
            last_trade_at=start + 500,
            available_at=start + HOUR_MS,
        )
        obs = trade_bar_to_price_observation(bar)
        self.assertIsInstance(obs, PriceObservation)
        self.assertEqual(obs.instrument_name, "BTC-TEST-C")
        self.assertEqual(obs.interval_start_ms, start)
        self.assertEqual(obs.interval_end_ms, start + HOUR_MS)
        self.assertEqual(obs.available_at_ms, start + HOUR_MS)
        self.assertEqual(obs.trade_price_btc, Decimal("0.011"))
        self.assertEqual(obs.trade_size, Decimal("10"))
        self.assertEqual(obs.price_basis, PriceBasis.TRADE)


if __name__ == "__main__":
    unittest.main()
