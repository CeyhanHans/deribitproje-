import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from ingestion.historical_windows import (
    HOUR_MS,
    HistoricalWindow,
    HistoricalWindowError,
    aggregate_hourly_trade_bars,
    build_default_windows,
    build_trade_plan_for_window,
    fetch_hourly_trade_bars,
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


if __name__ == "__main__":
    unittest.main()
